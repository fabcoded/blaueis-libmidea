# Releasing

How a lockstep release of `blaueis-core`, `blaueis-client` and
`blaueis-gateway` is cut. `blaueis-tools` is not published.

The pipeline is `.github/workflows/publish.yml`, triggered by pushing a `v*`
tag:

1. **build** — one sdist + wheel per package. On a tag, a guard checks that the
   file version equals the tag (without `v`) and that the wheel carries its
   `LICENSE`.
2. **TestPyPI** — uploads all three (`skip-existing`, so a re-run is a no-op).
3. **PyPI** — waits for approval on the `pypi` environment, then uploads all
   three. Trusted publishing (OIDC): no API tokens, no secrets.
4. **draft release** — creates a *draft* GitHub release for the tag with
   `scripts/install.sh` attached. Tags with a pre-release suffix (`v0.1.0rc1`)
   are marked as pre-releases.

A manual run (*Actions → publish → Run workflow*) builds and uploads to
TestPyPI only — a dry run.

**Publishing the draft by hand is the gate.** The installer, `blaueis-gw
update` and the gateway's remote update all resolve "the latest release"
through the GitHub Releases API (`operations.md` §2, §5), and that API only
returns published, non-pre-release releases. Until a human clicks *Publish*,
no gateway moves — even though the packages are already on PyPI.

## One-time setup (before the first tag)

### Six trusted-publisher registrations

Pending publishers, created under the account sidebar → *Publishing* (the
projects do not exist yet). Three on each index, all with the same identity:

| Field | Value |
|---|---|
| PyPI project name | `blaueis-core`, `blaueis-client`, `blaueis-gateway` (one registration each) |
| Owner | `fabcoded` |
| Repository name | `blaueis-libmidea` |
| Workflow name | `publish.yml` |
| Environment name | `pypi` on https://pypi.org/manage/account/publishing/ · `testpypi` on https://test.pypi.org/manage/account/publishing/ |

A pending publisher does not reserve the name — publish soon after
registering. TestPyPI occasionally deletes projects and accounts; re-create
its three registrations if that happens.

### GitHub environments

*Settings → Environments*:

- **`pypi`** — *Required reviewers*: the maintainer. *Deployment branches and
  tags*: tags matching `v*`. Create it by hand before the first tag; an
  auto-created environment has no protection rules.
- **`testpypi`** — no reviewers.

## Cutting a release

1. Bump `version` by hand in `packages/blaueis-{core,client,gateway}/pyproject.toml`
   to the same value (e.g. `0.1.0rc1`). The client and gateway pin
   `blaueis-core~=0.1.0rc1`. That form is deliberate: `~=0.1.0` would not
   admit `0.1.0rc1` even with pre-releases allowed, so
   `pip install blaueis-gateway==0.1.0rc1` could not resolve its core
   dependency. `~=0.1.0rc1` admits the rc, `0.1.0` and every later `0.1.x`
   but not `0.2`. Because the specifier names a pre-release, it also lets pip
   pick later pre-releases (`0.1.1rc1`), so the final-release bump returns the
   pin to `~=0.1.0` in client and gateway; adjust it when the minor version
   changes.
2. Merge to `main`, then tag and push:
   `git tag -a v0.1.0rc1 -m "v0.1.0rc1" && git push origin v0.1.0rc1`.
3. Watch the workflow. When TestPyPI is green, optionally check an install:
   `pip install --index-url https://test.pypi.org/simple/ --no-deps blaueis-core==0.1.0rc1`
   (`--no-deps`: TestPyPI lacks most third-party dependencies).
4. Approve the `pypi` deployment. The three uploads run in parallel.
5. Review the draft release (notes are prefilled by GitHub's generated notes;
   `install.sh` is attached) and publish it.

If one PyPI leg fails after the others have uploaded, use *Re-run failed jobs*
— never re-tag. **Never reuse a version**: a broken release is yanked on PyPI,
its GitHub release set back to draft, and fixed with the next patch or rc
number.

## Release order for 0.1.0

Across the three repositories (map: fabcoded/blaueis-ha-midea#16):

1. **rc cycle** — `v0.1.0rc1` of this repo published (PyPI, then the draft
   published by hand) → the maintainer's gateway moved to it over SSH with
   `sudo blaueis-gw update --ref v0.1.0rc1` (pre-releases are never "latest")
   → blaueis-ha-midea `v0.1.0rc1` → HACS custom-repository rehearsal → the
   rehearsal proofs of fabcoded/blaueis-ha-midea#28. A throwaway `v0.1.0rc2`
   exercises `blaueis-gw update --rollback`.
2. **final** — `v0.1.0` in the same order: this repo (its version bump also
   returns the `blaueis-core` pin in client and gateway to `~=0.1.0`), then
   blaueis-ha-midea, then blaueis-hvacshark's annotated tag last. The README
   install one-liner switches to the release-asset URL only then.
