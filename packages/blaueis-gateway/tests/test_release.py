"""Release tracking: latest-release lookup, fallback, state file, tag checkout."""

import io
import json
import subprocess
import urllib.error

import pytest
from blaueis.gateway import release


class _Resp(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def _urlopen_returning(payload):
    def fake(req, timeout):
        return _Resp(json.dumps(payload).encode())

    return fake


def _urlopen_raising(exc):
    def fake(req, timeout):
        raise exc

    return fake


def _http_error(code):
    return urllib.error.HTTPError(release.LATEST_RELEASE_URL, code, "x", {}, None)


def test_latest_release_tag(monkeypatch):
    monkeypatch.setattr(release.urllib.request, "urlopen", _urlopen_returning({"tag_name": "v0.1.0"}))
    assert release.latest_release_tag() == "v0.1.0"
    assert release.resolve_target() == ("v0.1.0", None)


def test_no_release_falls_back_to_main_with_warning(monkeypatch):
    monkeypatch.setattr(release.urllib.request, "urlopen", _urlopen_raising(_http_error(404)))
    assert release.latest_release_tag() is None
    ref, warning = release.resolve_target()
    assert ref == "main"
    assert "no GitHub release" in warning


@pytest.mark.parametrize(
    "exc",
    [_http_error(403), urllib.error.URLError("offline"), TimeoutError("slow")],
)
def test_lookup_failure_is_not_a_fallback(monkeypatch, exc):
    monkeypatch.setattr(release.urllib.request, "urlopen", _urlopen_raising(exc))
    with pytest.raises(release.ReleaseLookupError):
        release.resolve_target()


def test_bad_tag_name_rejected(monkeypatch):
    monkeypatch.setattr(release.urllib.request, "urlopen", _urlopen_returning({"tag_name": "--upload-pack=x"}))
    with pytest.raises(release.ReleaseLookupError):
        release.latest_release_tag()


def test_explicit_ref_skips_lookup(monkeypatch):
    monkeypatch.setattr(release.urllib.request, "urlopen", _urlopen_raising(AssertionError("no lookup")))
    assert release.resolve_target("v0.1.0rc1") == ("v0.1.0rc1", None)
    with pytest.raises(ValueError):
        release.resolve_target("-x")


def test_state_round_trip(tmp_path):
    assert release.read_state(str(tmp_path)) == {}
    release.write_state(str(tmp_path), "v0.2.0", "b" * 40, "v0.1.0", "a" * 40)
    assert release.read_state(str(tmp_path)) == {
        "current_ref": "v0.2.0",
        "current_sha": "b" * 40,
        "previous_ref": "v0.1.0",
        "previous_sha": "a" * 40,
    }


def _git(cwd, *args):
    return subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True, text=True).stdout.strip()


@pytest.fixture
def origin_and_clone(tmp_path):
    origin = tmp_path / "origin"
    origin.mkdir()
    _git(origin, "init", "-q", "-b", "main")
    _git(origin, "config", "user.email", "t@example.invalid")
    _git(origin, "config", "user.name", "t")
    for n, tag in ((1, "v0.1.0"), (2, "v0.2.0"), (3, None)):
        (origin / "f").write_text(str(n))
        _git(origin, "add", "f")
        _git(origin, "commit", "-q", "-m", f"c{n}")
        if tag:
            _git(origin, "tag", "-a", tag, "-m", tag)
    clone = tmp_path / "clone"
    _git(tmp_path, "clone", "-q", "--depth", "1", "--branch", "v0.1.0", f"file://{origin}", str(clone))
    return origin, clone


def test_fetch_and_checkout_tag_then_branch(origin_and_clone):
    origin, clone = origin_and_clone
    d = str(clone)
    assert release.current_ref_name(d) == "v0.1.0"
    start = release.head_sha(d)

    sha = release.fetch_ref(d, "v0.2.0")
    assert sha == _git(origin, "rev-parse", "v0.2.0^{commit}")
    release.checkout(d, sha)
    release.write_state(d, "v0.2.0", sha, "v0.1.0", start)
    assert (clone / "f").read_text() == "2"
    assert release.current_ref_name(d) == "v0.2.0"
    assert _git(clone, "describe", "--tags", "--always") == "v0.2.0"

    # Rollback target is still local after the shallow tag fetch.
    release.checkout(d, release.read_state(d)["previous_sha"])
    assert (clone / "f").read_text() == "1"

    sha = release.fetch_ref(d, "main")
    assert sha == _git(origin, "rev-parse", "main")
    release.checkout(d, sha)
    assert (clone / "f").read_text() == "3"


def test_fetch_unknown_ref_raises(origin_and_clone):
    _, clone = origin_and_clone
    with pytest.raises(RuntimeError):
        release.fetch_ref(str(clone), "v9.9.9")


# ── Remote {"type": "update"} handler ─────────────────


@pytest.fixture
def server_mod(monkeypatch, tmp_path):
    from blaueis.gateway import server

    monkeypatch.setattr(server, "INSTALL_DIR", str(tmp_path))
    monkeypatch.setattr(server, "_get_version", lambda: server.GW_VERSION)  # no restart
    return server


async def test_remote_update_refuses_when_lookup_fails(server_mod, monkeypatch):
    def boom(*a, **k):
        raise release.ReleaseLookupError("GitHub API answered HTTP 403")

    monkeypatch.setattr(release, "resolve_target", boom)
    monkeypatch.setattr(release, "fetch_ref", lambda *a: pytest.fail("must not fetch"))
    result = await server_mod.GatewayServer._run_update(None)
    assert result["ok"] is False
    assert result["steps"] == [("resolve", False, "GitHub API answered HTTP 403")]


async def test_remote_update_checks_out_target_and_records_previous(server_mod, monkeypatch, tmp_path):
    checked_out = []
    monkeypatch.setattr(release, "resolve_target", lambda: ("main", "no GitHub release published yet"))
    monkeypatch.setattr(release, "head_sha", lambda d: "a" * 40)
    monkeypatch.setattr(release, "current_ref_name", lambda d: "v0.1.0")
    monkeypatch.setattr(release, "fetch_ref", lambda d, ref: "b" * 40)
    monkeypatch.setattr(release, "checkout", lambda d, sha: checked_out.append(sha))
    # pip install -e — the handler imports subprocess locally, so patch the module.
    monkeypatch.setattr(subprocess, "run", lambda *a, **k: subprocess.CompletedProcess(a, 0, "", ""))
    result = await server_mod.GatewayServer._run_update(None)
    assert result["ok"] is True
    assert checked_out == ["b" * 40]
    assert result["steps"][0] == ("resolve", True, "no GitHub release published yet")
    assert release.read_state(str(tmp_path)) == {
        "current_ref": "main",
        "current_sha": "b" * 40,
        "previous_ref": "v0.1.0",
        "previous_sha": "a" * 40,
    }
