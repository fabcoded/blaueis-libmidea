"""Release tracking for the gateway checkout in ``/opt/blaueis-gw``.

The gateway install is a git checkout pinned to the latest GitHub release —
the same source of truth HACS uses for the integration. This module is the
Python side of that rule, used by the remote ``{"type": "update"}`` command;
``scripts/install.sh`` and ``scripts/blaueis-update`` implement the same rule
in bash and share the state file format below.

State file ``<install_dir>/.update-state`` (``key=value`` lines)::

    current_ref=v0.1.0
    current_sha=<commit>
    previous_ref=v0.1.0rc1
    previous_sha=<commit>

``blaueis-gw update --rollback`` checks out ``previous_sha``.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import urllib.error
import urllib.request

GITHUB_REPO = "fabcoded/blaueis-libmidea"
LATEST_RELEASE_URL = f"https://api.github.com/repos/{GITHUB_REPO}/releases/latest"
FALLBACK_REF = "main"
STATE_FILE = ".update-state"

# Tag / branch names only — no leading dash (git option injection), no spaces.
_REF_RE = re.compile(r"^[A-Za-z0-9._][A-Za-z0-9._/-]*$")


class ReleaseLookupError(RuntimeError):
    """The latest release could not be determined (network, rate limit, bad reply)."""


def valid_ref(ref: str) -> bool:
    return bool(_REF_RE.fullmatch(ref)) and ".." not in ref


def latest_release_tag(timeout: float = 10.0) -> str | None:
    """Tag of the latest published, non-prerelease GitHub release.

    Returns ``None`` when the repository has no release yet (HTTP 404).
    Raises :class:`ReleaseLookupError` for every other failure — the caller
    must not guess a target when it cannot tell.
    """
    req = urllib.request.Request(
        LATEST_RELEASE_URL,
        headers={"Accept": "application/vnd.github+json", "User-Agent": "blaueis-gateway"},
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            data = json.load(resp)
    except urllib.error.HTTPError as e:
        if e.code == 404:
            return None
        raise ReleaseLookupError(f"GitHub API answered HTTP {e.code}") from e
    except (urllib.error.URLError, TimeoutError, OSError, ValueError) as e:
        raise ReleaseLookupError(f"GitHub API unreachable: {e}") from e
    tag = data.get("tag_name") if isinstance(data, dict) else None
    if not isinstance(tag, str) or not valid_ref(tag):
        raise ReleaseLookupError(f"unexpected tag_name in release reply: {tag!r}")
    return tag


def resolve_target(ref: str | None = None, timeout: float = 10.0) -> tuple[str, str | None]:
    """Return ``(ref, warning)``: the explicit ref, else the latest release tag,
    else ``main`` with a warning when no release exists yet."""
    if ref:
        if not valid_ref(ref):
            raise ValueError(f"invalid ref: {ref!r}")
        return ref, None
    tag = latest_release_tag(timeout=timeout)
    if tag is None:
        return FALLBACK_REF, f"no GitHub release published yet — falling back to '{FALLBACK_REF}'"
    return tag, None


def read_state(install_dir: str) -> dict[str, str]:
    state: dict[str, str] = {}
    try:
        with open(os.path.join(install_dir, STATE_FILE)) as f:
            for line in f:
                key, sep, value = line.strip().partition("=")
                if sep and key:
                    state[key] = value
    except FileNotFoundError:
        pass
    return state


def write_state(install_dir: str, current_ref: str, current_sha: str, previous_ref: str, previous_sha: str) -> None:
    path = os.path.join(install_dir, STATE_FILE)
    tmp = path + ".tmp"
    with open(tmp, "w") as f:
        f.write(f"current_ref={current_ref}\n")
        f.write(f"current_sha={current_sha}\n")
        f.write(f"previous_ref={previous_ref}\n")
        f.write(f"previous_sha={previous_sha}\n")
    os.replace(tmp, path)


def _git(install_dir: str, *args: str, timeout: float = 60) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["git", *args],
        cwd=install_dir,
        capture_output=True,
        text=True,
        timeout=timeout,
    )


def head_sha(install_dir: str) -> str:
    r = _git(install_dir, "rev-parse", "HEAD", timeout=10)
    return r.stdout.strip() if r.returncode == 0 else ""


def current_ref_name(install_dir: str) -> str:
    """Human name of what HEAD is on: the state file's ref if it still matches,
    else the exact tag, else the branch, else the short SHA."""
    sha = head_sha(install_dir)
    state = read_state(install_dir)
    if sha and state.get("current_sha") == sha and state.get("current_ref"):
        return state["current_ref"]
    for args in (("describe", "--tags", "--exact-match", "HEAD"), ("symbolic-ref", "--short", "-q", "HEAD")):
        r = _git(install_dir, *args, timeout=10)
        if r.returncode == 0 and r.stdout.strip():
            return r.stdout.strip()
    return sha[:12] or "unknown"


def fetch_ref(install_dir: str, ref: str) -> str:
    """Fetch ``ref`` (tag or branch) from origin and return the commit it names.

    Raises ``RuntimeError`` with git's message when the ref cannot be fetched.
    """
    if not valid_ref(ref):
        raise ValueError(f"invalid ref: {ref!r}")
    r = _git(install_dir, "ls-remote", "--exit-code", "--tags", "origin", f"refs/tags/{ref}", timeout=30)
    if r.returncode == 0:
        r = _git(install_dir, "fetch", "-q", "--depth", "1", "--force", "origin", f"refs/tags/{ref}:refs/tags/{ref}")
        target = f"refs/tags/{ref}^{{commit}}"
    else:
        r = _git(install_dir, "fetch", "-q", "--depth", "50", "origin", ref)
        target = "FETCH_HEAD^{commit}"
    if r.returncode != 0:
        raise RuntimeError(f"git fetch {ref} failed: {r.stderr.strip() or r.stdout.strip()}")
    r = _git(install_dir, "rev-parse", "--verify", "-q", target, timeout=10)
    if r.returncode != 0 or not r.stdout.strip():
        raise RuntimeError(f"cannot resolve {ref} after fetch")
    return r.stdout.strip()


def checkout(install_dir: str, sha: str) -> None:
    """Detached, forced checkout of ``sha`` — the install is a clean checkout by contract."""
    r = _git(install_dir, "-c", "advice.detachedHead=false", "checkout", "-q", "--force", "--detach", sha)
    if r.returncode != 0:
        raise RuntimeError(f"git checkout failed: {r.stderr.strip() or r.stdout.strip()}")
