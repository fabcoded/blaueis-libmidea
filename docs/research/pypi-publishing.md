# PyPI trusted publishing for the library monorepo

> Research note, 2026-09-02. Question: what is the right shape for a
> tag-triggered GitHub Actions workflow that builds and publishes
> `blaueis-core`, `blaueis-client` and `blaueis-gateway` from this one
> repository with PyPI trusted publishing (OIDC), and what must each
> `pyproject.toml` carry for the upload to be accepted and the project
> page to be right. `blaueis-tools` stays unpublished.
>
> Every claim below is either cited to a primary source (URL in brackets)
> or marked *observed* — the result of building `blaueis-core` locally
> with `build` 1.6.0 / setuptools 84.0.0 / twine 7.0.0 on 2026-09-02.

## Summary

1. **One workflow file, three matrix legs, three jobs.** `.github/workflows/publish.yml`, triggered by `push` of a `v*` tag: a `build` matrix job (one `python -m build` + one artifact per package), a `publish-testpypi` matrix job, then a `publish-pypi` matrix job that `needs` the TestPyPI job. Each publish is its own job because invoking `pypa/gh-action-pypi-publish` more than once in one job is unsupported, and reusable workflows cannot be trusted publishers. Full YAML in §1.
2. **One trusted publisher per PyPI project — six registrations, one identity.** Register a *pending* publisher for each of the three names on pypi.org (environment `pypi`) and again on test.pypi.org (environment `testpypi`), all with owner `fabcoded`, repository `blaueis-libmidea`, workflow `publish.yml`. PyPI explicitly allows one publisher to be registered against multiple projects. The three names are unregistered on PyPI today (*observed*: JSON API returns 404 for all three).
3. **Metadata to add in every published `pyproject.toml`:** `readme`, `license = "CC0-1.0"` (SPDX string, PEP 639), `license-files = ["LICENSE"]`, `authors`, `[project.urls]`, `classifiers` (never a `License ::` classifier — setuptools refuses to build), and bump `build-system.requires` to `setuptools>=77.0` (first version with PEP 639 support). Only `name` and `version` are required for the upload itself; the rest is what makes the project page right.
4. **Sharing the root `LICENSE`:** `..` is forbidden in `license-files` and `readme` paths, so each package directory needs its own `LICENSE` — a committed copy (recommended) or a git symlink to `../../LICENSE` (*observed* to produce a real 7169-byte file in wheel and sdist). Each package also needs its own `README.md`; `readme = "../../README.md"` is a hard build error, and a missing README leaves the PyPI page empty.
5. **`setuptools-scm` in `blaueis-core` is inert for versioning** (static `version`, no `dynamic`) **but active as a file finder:** it pulled every git-tracked test fixture into the sdist (*observed*: 132 vs 87 entries). Remove it. The `data/*.yaml`, `data/*.json`, `data/device_quirks/*.yaml` files are carried in the wheel **and** the sdist with or without it (*observed*: 6 files in the wheel).
6. **Pin the internal dependency** to `blaueis-core~=0.1.0` (`>=0.1.0, ==0.1.*`) in `blaueis-client` and `blaueis-gateway`; an unpinned `blaueis-core` lets a future `0.2.0` core break a fresh install of gateway `0.1.0`, which the 0.x policy in `docs/versioning.md` ("updated together") does not intend.

## 1. Workflow shape — one file, three packages, trusted publishing

### Facts that fix the shape

- Trusted publishing needs the OIDC permission on the job: "you **must** provide this permission at either the job level (**strongly recommended**) or workflow level (**discouraged**)" — `permissions: id-token: write`. Without it "the publishing action won't have sufficient permissions to identify itself to PyPI" [[docs.pypi.org/using-a-publisher](https://docs.pypi.org/trusted-publishers/using-a-publisher/)].
- "Trusted publishing cannot be used from within a reusable workflow at this time" [[gh-action-pypi-publish README](https://github.com/pypa/gh-action-pypi-publish)]; "Reusable workflows cannot currently be used as the workflow in a Trusted Publisher. This is a practical limitation, and is being tracked in warehouse#11096" [[docs.pypi.org/troubleshooting](https://docs.pypi.org/trusted-publishers/troubleshooting/)]. So the three-package logic lives in *one* self-contained workflow file, not in a reusable workflow called three times.
- "invoking `pypi-publish` more than once in the same job is not considered supported" [[README](https://github.com/pypa/gh-action-pypi-publish)]. Three sequential publish *steps* in one job are out; a **matrix** (one job per package) is the supported form.
- "This action has nothing to do with building package distributions. Users are responsible for preparing dists for upload by putting them into the `dist/` folder prior to running this Action" [[README](https://github.com/pypa/gh-action-pypi-publish)]. "Building distributions in a publishing job is unsupported; publishing jobs should only download the already-built artifacts and upload them" [[packaging.python.org guide](https://packaging.python.org/en/latest/guides/publishing-package-distribution-releases-using-github-actions-ci-cd-workflows/)]. PyPI's security model: "Your publishing job should (ideally) have only two steps: Retrieve the publishable distribution files from a separate build job; Publish the distributions using pypa/gh-action-pypi-publish@release/v1" [[docs.pypi.org/security-model](https://docs.pypi.org/trusted-publishers/security-model/)].
- The workflow filename is the identity: "The claims defined in an OIDC token are bound to the workflow, meaning that a workflow defined at foo.yml in org/repo cannot impersonate a workflow defined at bar.yml in org/repo" — and "If foo.yml is renamed to bar.yml, then the new bar.yml will be indistinguishable from the old bar.yml" [[security-model](https://docs.pypi.org/trusted-publishers/security-model/)]. Pick the filename once (`publish.yml` below) and never rename it without re-registering.
- The minted token covers every project the identity is registered on: "This API token is scoped to every project with a matching Trusted Publisher, meaning that it can be used to upload to multiple projects (if so configured)" and it is "short-lived (15 minute)" [[docs.pypi.org/internals](https://docs.pypi.org/trusted-publishers/internals/)]. A single publish job could therefore upload all six files at once; the matrix is chosen for per-package isolation and per-package environment URLs, not because the token requires it.
- Environments: "Configuring an environment is optional, but **strongly** recommended: with a GitHub environment, you can apply additional restrictions to your trusted workflow, such as requiring manual approval on each run by a trusted subset of repository maintainers" [[docs.pypi.org/adding-a-publisher](https://docs.pypi.org/trusted-publishers/adding-a-publisher/)]. The packaging guide goes further: "For security reasons, you must require manual approval on each run for the `pypi`" environment [[guide](https://packaging.python.org/en/latest/guides/publishing-package-distribution-releases-using-github-actions-ci-cd-workflows/)]. If a publisher was registered with an environment, the workflow must use it: "check if the workflow is using the same environment as configured when the publisher was configured on PyPI" [[troubleshooting](https://docs.pypi.org/trusted-publishers/troubleshooting/)].
- Matrix values may be used in the environment: `jobs.<job_id>.environment` accepts the contexts "github, needs, strategy, matrix, vars, inputs", and `jobs.<job_id>.environment.url` "github, needs, strategy, matrix, job, runner, env, vars, steps, inputs" [[GitHub contexts](https://docs.github.com/en/actions/reference/workflows-and-actions/contexts)]. `jobs.<job_id>.if` does **not** get `matrix`, so tag-gating goes on the job, not per leg.
- Artifacts: "Artifact names must be unique since each created artifact is idempotent so multiple jobs cannot modify the same artifact" and "uploading to the same artifact via multiple jobs is _not_ supported with `v4`" [[actions/upload-artifact](https://github.com/actions/upload-artifact)] — hence `dist-${{ matrix.package }}`.
- Tag trigger: `on.push.tags` "accept glob patterns that use characters like `*`, `**`, `+`, `?`, `!`" [[GitHub workflow syntax](https://docs.github.com/en/actions/reference/workflows-and-actions/workflow-syntax)]. The packaging guide uses `on: push` plus `if: startsWith(github.ref, 'refs/tags/')` to "only publish to PyPI on tag pushes" [[guide](https://packaging.python.org/en/latest/guides/publishing-package-distribution-releases-using-github-actions-ci-cd-workflows/)]; the YAML below combines a tag filter on the trigger with the same guard on the PyPI job so a manual `workflow_dispatch` run stops at TestPyPI.
- TestPyPI: the action takes `repository-url: https://test.pypi.org/legacy/` [[README](https://github.com/pypa/gh-action-pypi-publish)]. TestPyPI is a separate instance with separate accounts and publishers: "these are two separate servers and the login details from the test server are not shared with the main server", and "The Test system occasionally deletes packages and accounts" [[packaging tutorial](https://packaging.python.org/en/latest/tutorials/packaging-projects/)]. The guide registers the publisher on both `https://pypi.org/manage/account/publishing/` and `https://test.pypi.org/manage/account/publishing/`, with environment names `pypi` and `testpypi` respectively [[guide](https://packaging.python.org/en/latest/guides/publishing-package-distribution-releases-using-github-actions-ci-cd-workflows/)].
- Re-uploads: a version can only be uploaded once; "you may use `skip-existing` (disabled by default)" to tolerate that [[README](https://github.com/pypa/gh-action-pypi-publish)]. On the TestPyPI leg this makes repeated dry runs of the same version idempotent.
- Attestations: "Generating signed digital attestations for all the distribution files and uploading them all together is now on by default for all projects using Trusted Publishing" and support "is currently limited to Trusted Publishing flows using PyPI or TestPyPI" [[README](https://github.com/pypa/gh-action-pypi-publish)]; "attestations are generated and uploaded automatically by default, with no additional configuration necessary" [[docs.pypi.org/attestations](https://docs.pypi.org/attestations/producing-attestations/)]. Nothing to add.
- Metadata check: unless `verify-metadata: false`, the action runs `twine check ${INPUT_PACKAGES_DIR%%/}/*` — without `--strict` — before `twine upload` [[twine-upload.sh](https://github.com/pypa/gh-action-pypi-publish/blob/release/v1/twine-upload.sh)]. *Observed*: with today's metadata `twine check` reports "PASSED with warnings" (`long_description` missing), i.e. the upload would go through, with an empty project page.
- Version pin of the action: both PyPI's docs and the packaging guide use `pypa/gh-action-pypi-publish@release/v1` [[using-a-publisher](https://docs.pypi.org/trusted-publishers/using-a-publisher/)], [[guide](https://packaging.python.org/en/latest/guides/publishing-package-distribution-releases-using-github-actions-ci-cd-workflows/)]; the README's pro tip is to "pin versions of Actions that you use to tagged versions or sha1 commit identifiers" [[README](https://github.com/pypa/gh-action-pypi-publish)]. Latest tag at time of writing: v1.14.2 [[releases](https://github.com/pypa/gh-action-pypi-publish/releases)].

### Registrations to make (before the first tag)

On **pypi.org → account sidebar → Publishing** ("the page is under your account sidebar instead of any project's sidebar (since the project doesn't exist yet)") create three *pending* publishers. "A 'pending' publisher does **not** create a project or reserve a project's name **until** it is actually used to publish" and "'Pending' publishers are converted into 'normal' publishers on first use, meaning that no further configuration is required". Caveat: "If you create a 'pending' publisher but another user registers the project name before you actually publish to it, your 'pending' publisher will be **invalidated**" [[creating-a-project-through-oidc](https://docs.pypi.org/trusted-publishers/creating-a-project-through-oidc/)].

| Field | Value (each of the three registrations) |
|---|---|
| PyPI project name | `blaueis-core` / `blaueis-client` / `blaueis-gateway` — must equal `[project].name`; a mismatch fails with "the project name supplied in the upload's metadata does not match it" [[troubleshooting](https://docs.pypi.org/trusted-publishers/troubleshooting/)] |
| Owner | `fabcoded` |
| Repository name | `blaueis-libmidea` |
| Workflow name | `publish.yml` (the filename under `.github/workflows/`) [[adding-a-publisher](https://docs.pypi.org/trusted-publishers/adding-a-publisher/)] |
| Environment name | `pypi` |

Repeat the three on **test.pypi.org** with environment `testpypi`. "A publisher can be registered against multiple PyPI projects (e.g. for a multi-project repository)" [[adding-a-publisher](https://docs.pypi.org/trusted-publishers/adding-a-publisher/)], so the same identity tuple is entered three times per index. Rate limit is irrelevant at this scale ("no more than 100 publishers can be registered by a single user or IP address within a 24 hour window" [[troubleshooting](https://docs.pypi.org/trusted-publishers/troubleshooting/)]).

On **GitHub → Settings → Environments** create `pypi` with *Required reviewers* ("Only one of the required reviewers needs to approve the job for it to proceed") and a *Deployment branches and tags* rule limited to `v*`; "A job that references an environment must follow any protection rules for the environment before running" [[GitHub environments](https://docs.github.com/en/actions/how-tos/deploy/configure-and-manage-deployments/manage-environments)]. `testpypi` needs no reviewers. Environments are auto-created on first reference ("Running a workflow that references an environment that does not exist will create an environment with the referenced name" — same page), but the protection rules are not, so create `pypi` by hand first.

### The workflow — `.github/workflows/publish.yml`

Action versions follow the packaging guide as published on 2026-09-02 (`checkout@v6`, `setup-python@v6`, `upload-artifact@v5`, `download-artifact@v6`) [[guide](https://packaging.python.org/en/latest/guides/publishing-package-distribution-releases-using-github-actions-ci-cd-workflows/)]; `persist-credentials: false` is from the same guide. Python 3.12 matches `ci.yml`.

```yaml
name: publish

on:
  push:
    tags: ["v*"]          # lockstep release tag, e.g. v0.1.0 — all three packages
  workflow_dispatch:      # manual dry run: builds + TestPyPI only (PyPI job is tag-gated)

jobs:
  build:
    name: build ${{ matrix.package }}
    runs-on: ubuntu-latest
    strategy:
      matrix:
        package: [blaueis-core, blaueis-client, blaueis-gateway]
    steps:
      - uses: actions/checkout@v6
        with:
          persist-credentials: false
      - uses: actions/setup-python@v6
        with:
          python-version: "3.12"
      - name: Install pypa/build
        run: python -m pip install --upgrade build
      - name: Build sdist and wheel
        run: python -m build --outdir dist/ "packages/${{ matrix.package }}"
      - name: Guard — tag matches version, license file present in the wheel
        if: github.ref_type == 'tag'
        env:
          TAG: ${{ github.ref_name }}
        run: |
          set -euo pipefail
          ver="${TAG#v}"
          ls dist/*-"${ver}".tar.gz dist/*-"${ver}"-py3-none-any.whl
          unzip -l dist/*.whl | grep -q '\.dist-info/licenses/LICENSE$'
      - uses: actions/upload-artifact@v5
        with:
          name: dist-${{ matrix.package }}
          path: dist/
          if-no-files-found: error

  publish-testpypi:
    name: TestPyPI ${{ matrix.package }}
    needs: build
    runs-on: ubuntu-latest
    strategy:
      matrix:
        package: [blaueis-core, blaueis-client, blaueis-gateway]
    environment:
      name: testpypi
      url: https://test.pypi.org/p/${{ matrix.package }}
    permissions:
      id-token: write       # mandatory for trusted publishing; job level, not workflow level
    steps:
      - uses: actions/download-artifact@v6
        with:
          name: dist-${{ matrix.package }}
          path: dist/
      - uses: pypa/gh-action-pypi-publish@release/v1
        with:
          repository-url: https://test.pypi.org/legacy/
          skip-existing: true   # re-running the same version against TestPyPI is a no-op

  publish-pypi:
    name: PyPI ${{ matrix.package }}
    if: github.event_name == 'push' && startsWith(github.ref, 'refs/tags/v')
    needs: [build, publish-testpypi]
    runs-on: ubuntu-latest
    strategy:
      matrix:
        package: [blaueis-core, blaueis-client, blaueis-gateway]
    environment:
      name: pypi            # required reviewers + tag rule v* configured in repo settings
      url: https://pypi.org/p/${{ matrix.package }}
    permissions:
      id-token: write
    steps:
      - uses: actions/download-artifact@v6
        with:
          name: dist-${{ matrix.package }}
          path: dist/
      - uses: pypa/gh-action-pypi-publish@release/v1
```

Notes on the choices:

- The build job is a matrix too, so each artifact holds exactly one package's sdist + wheel and the publish legs need no `packages-dir` filtering (`packages-dir` defaults to `dist/` [[README](https://github.com/pypa/gh-action-pypi-publish)]).
- `needs: [build, publish-testpypi]` makes the TestPyPI upload a real gate: with the default `fail-fast`, one failing TestPyPI leg cancels the others and the PyPI job never starts [[GitHub matrix](https://docs.github.com/en/actions/how-tos/write-workflows/choose-what-workflows-do/run-job-variations)].
- The guard step exists because PEP 639 says "Build tools MUST raise an error if any individual user-specified pattern does not match at least one file" [[PEP 639](https://peps.python.org/pep-0639/)], but setuptools 84 only emits a `SetuptoolsDeprecationWarning` ("Pattern 'LICENSE' did not match any files") and builds a wheel without any `License-File` (*observed*). PyPI would accept that wheel.
- `python -m build` on one package directory builds "a binary wheel and a source tarball" [[guide](https://packaging.python.org/en/latest/guides/publishing-package-distribution-releases-using-github-actions-ci-cd-workflows/)]; both are uploaded.
- Three PyPI legs upload in parallel. Nothing in the upload path orders them by dependency; a fresh `pip install blaueis-gateway` resolves `blaueis-core` only once that leg has landed too — seconds apart in practice. If a leg fails after others succeeded, re-run the workflow with `skip-existing: true` added to the PyPI leg as well (same README fact as above) rather than re-tagging.
- After the dry run, the tutorial's install check is `pip install --index-url https://test.pypi.org/simple/ --no-deps <name>` — `--no-deps` because "Since TestPyPI doesn't have the same packages as the live PyPI, it's possible that attempting to install dependencies may fail or install something unexpected" [[tutorial](https://packaging.python.org/en/latest/tutorials/packaging-projects/)]. The three sibling packages *are* on TestPyPI after the job; `pyyaml`, `websockets`, `cryptography` etc. may not be.

Variant, not recommended by default: per-package environments (`pypi-blaueis-core`, …) via `environment.name: pypi-${{ matrix.package }}` — legal per the context table above, gives per-package approval and deployment history at the cost of six environments and six registrations with distinct environment names.

## 2. Metadata each `pyproject.toml` must carry

### What the upload actually requires vs. what the page needs

- Required: `name` ("Required field; cannot be marked as dynamic") and `version` ("Required field") [[writing-pyproject-toml](https://packaging.python.org/en/latest/guides/writing-pyproject-toml/)]. Everything else is optional for acceptance; *observed*: the current `blaueis-core` metadata passes `twine check` with warnings only.
- `description`: "This should be a one-line description of your project, to show as the 'headline' of your project page on PyPI" [[writing-pyproject-toml](https://packaging.python.org/en/latest/guides/writing-pyproject-toml/)]. All four already have one.
- `readme`: "Typically, your project will have a README.md or README.rst file and you just put its file name here"; `.md` implies GitHub-flavoured Markdown [[writing-pyproject-toml](https://packaging.python.org/en/latest/guides/writing-pyproject-toml/)]. Spec: "If the file path ends in a case-insensitive `.md` suffix, then tools MUST assume the content-type is `text/markdown`" [[pyproject-toml spec](https://packaging.python.org/en/latest/specifications/pyproject-toml/)]. Missing today in all four; setuptools also warns at sdist time: "standard file not found: should have one of README, README.rst, README.txt, README.md" (*observed*).
- `license`: "The new format for license is a valid SPDX license expression consisting of one or more license identifiers"; "A previous PEP had specified license to be a table with a file or a text key, this format is now deprecated" [[writing-pyproject-toml](https://packaging.python.org/en/latest/guides/writing-pyproject-toml/)]. The root `LICENSE` is "CC0 1.0 Universal"; `CC0-1.0` appears as a valid identifier in PEP 639's own examples [[PEP 639](https://peps.python.org/pep-0639/)]. This becomes `License-Expression: CC0-1.0` in Metadata-Version 2.4 (*observed*), which PyPI stores and serves (*observed*: `https://pypi.org/pypi/setuptools/json` returns `license_expression: "MIT"`, `license_files: ["LICENSE"]`; both keys are listed in the JSON API reference [[docs.pypi.org/api/json](https://docs.pypi.org/api/json/)]).
- `license-files`: "This is a list of license files and files containing other legal information you want to distribute with your package"; patterns "are relative to the pyproject.toml directory" [[writing-pyproject-toml](https://packaging.python.org/en/latest/guides/writing-pyproject-toml/)]. Spec: "Parent directory indicators (`..`) MUST NOT be used"; "Build tools: MUST include all files matched by a listed pattern in all distribution archives"; "If the `license-files` key is not defined, tools can decide how to handle license files" [[pyproject-toml spec](https://packaging.python.org/en/latest/specifications/pyproject-toml/)], [[PEP 639](https://peps.python.org/pep-0639/)]. The matched file lands in `.dist-info/licenses/` in the wheel [[PEP 639](https://peps.python.org/pep-0639/)] (*observed*: `blaueis_core-0.1.0.dist-info/licenses/LICENSE`). PyPI-side validation: "PyPI SHOULD validate that all specified files are present in that distribution archive, and MUST reject uploads that do not validate" [[PEP 639](https://peps.python.org/pep-0639/)].
- `classifiers`: "A list of PyPI classifiers that apply to your project" [[writing-pyproject-toml](https://packaging.python.org/en/latest/guides/writing-pyproject-toml/)] — pick from https://pypi.org/classifiers/. **Do not add a `License ::` classifier**: "The use of `License ::` classifiers is deprecated and tools MAY issue a warning" [[spec](https://packaging.python.org/en/latest/specifications/pyproject-toml/)]; with an expression present "build tools MAY raise an error" [[PEP 639](https://peps.python.org/pep-0639/)], and setuptools does: `InvalidConfigError: License classifiers have been superseded by license expressions` — the build aborts (*observed*).
- `authors`: "lists of people identified by a name and/or an email address" [[writing-pyproject-toml](https://packaging.python.org/en/latest/guides/writing-pyproject-toml/)]; "Both keys are optional, but at least one of the keys must be specified" [[spec](https://packaging.python.org/en/latest/specifications/pyproject-toml/)]. A name-only entry is enough.
- `[project.urls]`: shown in PyPI's sidebar; well-known labels (Homepage, Documentation, Repository, Issues, Changelog) get semantic rendering [[writing-pyproject-toml](https://packaging.python.org/en/latest/guides/writing-pyproject-toml/)]. Via trusted publishing "Packages uploaded using GHA from a repository will have the GitHub URLs for that repository verified" (green check); "URL verification occurs when release files are uploaded and is not repeated afterwards" [[docs.pypi.org/project_metadata](https://docs.pypi.org/project_metadata/)] — so the URLs should be present in the **first** upload.
- `keywords`: optional, "will help PyPI's search box" [[writing-pyproject-toml](https://packaging.python.org/en/latest/guides/writing-pyproject-toml/)].

### Minimum setuptools

"Support for project.license-files and SPDX license expressions in project.license (PEP 639) were introduced in version 77.0.0" [[setuptools pyproject_config](https://setuptools.pypa.io/en/latest/userguide/pyproject_config.html)]; v77.0.0 (19 Mar 2025) also "Deprecated `project.license` as a TOML table" and "Added exception (or warning) when deprecated license classifiers are used" [[setuptools history](https://setuptools.pypa.io/en/latest/history.html)]. Today's `requires = ["setuptools>=68.0"]` therefore has to become `["setuptools>=77.0"]`. In practice isolated builds already fetch the newest release (*observed*: 84.0.0), but the floor is what protects `--no-isolation` builds and documents intent. `tool.setuptools.license-files` is "**Deprecated** – use project.license-files instead" (its default globs were `['LICEN[CS]E*', 'COPYING*', 'NOTICE*', 'AUTHORS*']`) [[pyproject_config](https://setuptools.pypa.io/en/latest/userguide/pyproject_config.html)] — the defaults only ever look inside the package directory, which is why nothing is picked up today.

### Sharing the root `LICENSE` (CC0) and the README question

The project root for each distribution is `packages/<name>/`, and both `license-files` and `readme` are resolved from there:

| Approach | Result (*observed*, setuptools 84) | Verdict |
|---|---|---|
| `license-files = ["../../LICENSE"]` | `SetuptoolsDeprecationWarning: Pattern '../../LICENSE' cannot contain '..'`; still copied for now | Forbidden by the spec; will become an error. Do not use. |
| `readme = "../../README.md"` | `DistutilsOptionError: Cannot access '…/../../README.md' (or anything outside '…/packages/core')` | Hard error. |
| Committed copy `packages/<name>/LICENSE`, `license-files = ["LICENSE"]` | `License-File: LICENSE`, `.dist-info/licenses/LICENSE` (7169 bytes), `twine check` PASSED | **Recommended.** Four identical 7 KB copies of CC0 text; nothing to keep in sync beyond the file itself. |
| Git symlink `packages/<name>/LICENSE -> ../../LICENSE` | Identical wheel and sdist content to the copy (real file, 7169 bytes, first line "Creative Commons Legal Code") | Works on Linux runners; a Windows checkout without symlink support would ship a one-line text file instead. Acceptable if the maintainers accept that caveat. |
| Symlink `packages/<name>/README.md -> ../../README.md` | Builds; `Description-Content-Type: text/markdown`, body is the monorepo README | Works, but every project page would carry the same monorepo text. |

Each package should have **its own** `README.md`: the PyPI page for `blaueis-gateway` should describe the gateway (install on the Pi, `blaueis-configure`), the one for `blaueis-client` the client library, and so on. The root README stays the repository landing page. A README is not required for the upload to be accepted (only warnings), but the project page is blank without it.

### Recommended `[project]` block (shown for `blaueis-core`; the others differ in name, description, dependencies)

```toml
[build-system]
requires = ["setuptools>=77.0"]
build-backend = "setuptools.build_meta"

[project]
name = "blaueis-core"
version = "0.1.0"
description = "Midea HVAC serial protocol library — codec, glossary, state, query, command"
readme = "README.md"
requires-python = ">=3.11"
license = "CC0-1.0"
license-files = ["LICENSE"]
authors = [{name = "fabcoded"}]
keywords = ["midea", "hvac", "air-conditioner", "uart", "home-automation"]
classifiers = [
    "Development Status :: 4 - Beta",
    "Intended Audience :: Developers",
    "Operating System :: OS Independent",
    "Programming Language :: Python :: 3",
    "Programming Language :: Python :: 3.11",
    "Programming Language :: Python :: 3.12",
    "Topic :: Home Automation",
]
dependencies = [
    "pyyaml>=6.0",
    "jsonschema>=4.0",
    "cryptography>=41.0",
]

[project.optional-dependencies]
dev = ["pytest>=7.0", "ruff>=0.4"]

[project.urls]
Homepage = "https://github.com/fabcoded/blaueis-libmidea"
Repository = "https://github.com/fabcoded/blaueis-libmidea"
Documentation = "https://github.com/fabcoded/blaueis-libmidea/tree/main/docs"
Issues = "https://github.com/fabcoded/blaueis-libmidea/issues"

[tool.setuptools.packages.find]
where = ["src"]

[tool.setuptools.package-data]
"blaueis.core" = ["data/*.yaml", "data/*.json", "data/device_quirks/*.yaml"]
```

*Observed* result of exactly this block (with a stub README): `Metadata-Version: 2.4`, `License-Expression: CC0-1.0`, `License-File: LICENSE`, `Project-URL:` lines, `Description-Content-Type: text/markdown`, `twine check` PASSED for both sdist and wheel. The author name is the commit identity used in this repository; add `email = …` if the maintainer wants it on the page.

## 3. Per-package state, `setuptools-scm`, and package data

### What is missing today

Read from the four `packages/*/pyproject.toml` files on 2026-09-02. All four carry `name`, `version = "0.1.0"`, `description`, `requires-python = ">=3.11"`, `dependencies`, a `dev` extra and `[tool.setuptools.packages.find] where = ["src"]`. None carries any of the page-facing fields.

| Field | blaueis-core | blaueis-client | blaueis-gateway | blaueis-tools (unpublished) |
|---|---|---|---|---|
| `readme` | missing | missing | missing | missing |
| `license` (SPDX) | missing | missing | missing | missing |
| `license-files` | missing | missing | missing | missing |
| `authors` | missing | missing | missing | missing |
| `[project.urls]` | missing | missing | missing | missing |
| `classifiers` | missing | missing | missing | missing |
| `keywords` (optional) | missing | missing | missing | missing |
| `description` | present | present | present | present |
| `build-system.requires` | `setuptools>=68.0`, **`setuptools-scm`** — bump to `>=77.0`, drop scm | `setuptools>=68.0` — bump | `setuptools>=68.0` — bump | `setuptools>=68.0` |
| `[project.scripts]` | — | — | `blaueis-configure` | `blaueis-collisions` |
| Internal dependency | — | `blaueis-core` unpinned | `blaueis-core` unpinned | `blaueis-core`, `blaueis-client` unpinned |
| `README.md` in package dir | none | none | none | none |
| `LICENSE` in package dir | none | none | none | none |

Also relevant: `blaueis` is a PEP 420 namespace shared by all four distributions — there is no `src/blaueis/__init__.py` in any package (*observed*). "When using `tool.setuptools.packages.find` in `pyproject.toml`, setuptools will consider implicit namespaces is active by default" [[setuptools package_discovery](https://setuptools.pypa.io/en/latest/userguide/package_discovery.html)], and the built wheel contains `blaueis/core/…` with no `blaueis/__init__.py` (*observed*). Nobody must add one; it would shadow the other distributions.

### `setuptools-scm` in `blaueis-core`

Harmful? Not to correctness, but it is dead weight with a side effect:

- For versioning it does nothing: version inference "automatically enables version inference when **both** conditions are met: `setuptools-scm[simple]` is listed in `build-system.requires` [and] `version` is included in `project.dynamic`"; the older implicit activation "was removed because it caused ambiguous activation — projects using setuptools-scm **only** for its file finder would unexpectedly trigger version inference" [[setuptools-scm usage](https://setuptools-scm.readthedocs.io/en/latest/usage/)]. `blaueis-core` has a static `version = "0.1.0"` and no `dynamic`, so the built version is `0.1.0` either way (*observed*).
- Its file finder is still active: "setuptools-scm implements a file_finders entry point which returns all files tracked by your SCM"; "All tracked files are automatically included in the sdist" [[usage](https://setuptools-scm.readthedocs.io/en/latest/usage/)]. *Observed* in the git checkout: sdist with `setuptools-scm` = 132 entries (91 under `tests/`, including every YAML/JSON fixture under `tests/test-cases/`); without it = 87 entries (46 `tests/*.py`, no fixtures). The wheel is identical (29 entries) in both cases.
- It also adds a network fetch to every isolated build (*observed*: `setuptools-scm==10.2.2` installed into the build env) and a second build dependency to audit.

Do: replace `requires = ["setuptools>=68.0", "setuptools-scm"]` with `requires = ["setuptools>=77.0"]`. If tag-derived versions are wanted later, that is a separate decision (`dynamic = ["version"]` plus a `[tool.setuptools_scm]` table with `root = "../.."` per package) — not needed for a static lockstep 0.1.0.

### Package data — confirmed

`[tool.setuptools.package-data] "blaueis.core" = ["data/*.yaml", "data/*.json", "data/device_quirks/*.yaml"]` is the documented mechanism: "The `package_data` argument is a dictionary that maps from package names to lists of glob patterns", patterns are relative to the package directory and "you _must_ use a forward slash (`/`) as the path separator" [[setuptools datafiles](https://setuptools.pypa.io/en/latest/userguide/datafiles.html)]. Wheel inclusion condition is "(not exclude-package-data) and ((include-package-data and MANIFEST.in) or package-data)"; sdist inclusion is "MANIFEST.in or (package-data and not exclude-package-data)" — both satisfied by `package-data` alone, no `MANIFEST.in` needed [[datafiles](https://setuptools.pypa.io/en/latest/userguide/datafiles.html)].

*Observed* in the wheel built from the repository as-is, and again without `setuptools-scm`: all six files —
`blaueis/core/data/glossary.yaml`, `glossary_schema.json`, `glossary_collisions.allow.yaml`, `device_quirks_schema.json`, `device_quirks/xtremesaveblue_full_profile_v1.yaml`, `device_quirks/xtremesaveblue_power_quirk.yaml` — and the same six in the sdist under `src/blaueis/core/data/`. The `data/` and `data/device_quirks/` directories are not packages (no `__init__.py`) and do not need to be; the globs are what carry them.

### `blaueis-tools` and the root `pyproject.toml`

`blaueis-tools` depends on `blaueis-core` and `blaueis-client` and is simply left out of the matrix; nothing about it affects the other three. Two belt-and-braces options if an accidental upload is a concern: the "special `Private :: Do Not Upload` classifier" makes PyPI refuse a distribution [[writing-pyproject-toml](https://packaging.python.org/en/latest/guides/writing-pyproject-toml/)] — it can go into `blaueis-tools` and into the root `pyproject.toml` (`name = "blaueis-libmidea"`, no `[build-system]`, but a stray `blaueis_libmidea.egg-info/` at the root shows it has been `pip install -e .`-ed at least once).

## 4. Internal dependency `blaueis-gateway → blaueis-core` (and client)

Today both `blaueis-client` and `blaueis-gateway` declare `"blaueis-core"` with no specifier. The repository's own policy for 0.x is "Breaking changes allowed. Gateway and client should be updated together" (`docs/versioning.md` §4). An unpinned dependency contradicts that: once `blaueis-core 0.2.0` exists, `pip install blaueis-gateway==0.1.0` on a fresh machine pulls core 0.2.0.

Recommended for the lockstep 0.1.0 release: the compatible-release operator — "`~= 1.4.5`" is equivalent to "`>= 1.4.5, == 1.4.*`", and it "MUST NOT be used with a single segment version number such as `~=1`" [[version specifiers](https://packaging.python.org/en/latest/specifications/version-specifiers/)]:

```toml
dependencies = [
    "blaueis-core~=0.1.0",     # >=0.1.0, ==0.1.*
    ...
]
```

That admits core patch releases (`0.1.1`) without re-releasing gateway/client and refuses `0.2.0`. If the maintainers want literal lockstep (every release bumps all three together, no independent core patches), `blaueis-core==0.1.0` is the stricter form ("A version matching clause includes the version matching operator `==` and a version identifier" [[version specifiers](https://packaging.python.org/en/latest/specifications/version-specifiers/)]) at the cost of a mandatory gateway/client re-release for every core fix. Either is better than no specifier. The same edit applies to `blaueis-tools` if it is ever published.

Nothing in the upload path checks that `blaueis-core 0.1.0` exists when `blaueis-gateway 0.1.0` is uploaded, so the pin does not constrain job ordering — only installs.

## Gaps / open points

- **Classifier validity**: `Topic :: Home Automation` and the other classifiers above were not checked against the live list at https://pypi.org/classifiers/ during this research; PyPI validates classifiers on upload, so verify before the first real tag (the TestPyPI dry run will catch a typo).
- **setuptools vs. PEP 639 on unmatched globs**: setuptools 84 warns instead of erroring when `license-files` matches nothing; the guard step in the workflow covers that until setuptools turns the deprecation into an error.
- **Symlink vs. copy for `LICENSE`**: both verified on Linux; the copy is recommended purely because of the Windows-checkout caveat. Decide once, apply to all four packages.
- **Per-package environments**: the recommended YAML uses shared `pypi` / `testpypi` environments (three registrations each). Per-package environments are a legal variant; nothing in PyPI's docs prefers one over the other.
- **`fail-fast` on the PyPI matrix**: an already-finished leg cannot be undone by cancelling the others; a half-published lockstep release is recovered by re-running with `skip-existing: true`, not by re-tagging. Whether to set `skip-existing: true` on the PyPI leg permanently is a maintainer choice.
- **TestPyPI hygiene**: TestPyPI "occasionally deletes packages and accounts"; the three TestPyPI pending publishers may need re-creating if that happens.
- **`docs/versioning.md`** already describes a single `vX.Y.Z` tag for the whole library — consistent with the lockstep trigger here; it should gain one line pointing at the publish workflow when the workflow lands (documentation-parity rule).
- **Pending-publisher invalidation**: the three PyPI names are free today, but a pending publisher does not reserve them; publish soon after registering.

## Sources

Primary sources consulted (all fetched 2026-09-02):

- PyPI trusted publishers — adding a publisher: https://docs.pypi.org/trusted-publishers/adding-a-publisher/
- PyPI trusted publishers — using a publisher (workflow, `id-token: write`): https://docs.pypi.org/trusted-publishers/using-a-publisher/
- PyPI trusted publishers — creating a project through OIDC (pending publishers): https://docs.pypi.org/trusted-publishers/creating-a-project-through-oidc/
- PyPI trusted publishers — security model: https://docs.pypi.org/trusted-publishers/security-model/
- PyPI trusted publishers — troubleshooting: https://docs.pypi.org/trusted-publishers/troubleshooting/
- PyPI trusted publishers — internals (token scope, lifetime): https://docs.pypi.org/trusted-publishers/internals/
- PyPI attestations — producing attestations: https://docs.pypi.org/attestations/producing-attestations/
- PyPI project metadata — URLs and verification: https://docs.pypi.org/project_metadata/
- PyPI JSON API reference (`license_expression`, `license_files` keys): https://docs.pypi.org/api/json/
- pypa/gh-action-pypi-publish README: https://github.com/pypa/gh-action-pypi-publish — and its upload script https://github.com/pypa/gh-action-pypi-publish/blob/release/v1/twine-upload.sh, releases https://github.com/pypa/gh-action-pypi-publish/releases
- Python Packaging User Guide — publishing with GitHub Actions: https://packaging.python.org/en/latest/guides/publishing-package-distribution-releases-using-github-actions-ci-cd-workflows/
- Python Packaging User Guide — writing your pyproject.toml: https://packaging.python.org/en/latest/guides/writing-pyproject-toml/
- Python Packaging User Guide — pyproject.toml specification: https://packaging.python.org/en/latest/specifications/pyproject-toml/
- Python Packaging User Guide — version specifiers: https://packaging.python.org/en/latest/specifications/version-specifiers/
- Python Packaging User Guide — packaging tutorial (TestPyPI): https://packaging.python.org/en/latest/tutorials/packaging-projects/
- PEP 639 — license metadata: https://peps.python.org/pep-0639/
- setuptools — pyproject.toml configuration: https://setuptools.pypa.io/en/latest/userguide/pyproject_config.html
- setuptools — data files: https://setuptools.pypa.io/en/latest/userguide/datafiles.html
- setuptools — package discovery (namespaces): https://setuptools.pypa.io/en/latest/userguide/package_discovery.html
- setuptools — changelog (v77.0.0): https://setuptools.pypa.io/en/latest/history.html
- setuptools-scm — usage (activation, file finder): https://setuptools-scm.readthedocs.io/en/latest/usage/
- GitHub Actions reference (outside the ticket's named list, used only for workflow-syntax facts): workflow syntax https://docs.github.com/en/actions/reference/workflows-and-actions/workflow-syntax; contexts https://docs.github.com/en/actions/reference/workflows-and-actions/contexts; matrix jobs https://docs.github.com/en/actions/how-tos/write-workflows/choose-what-workflows-do/run-job-variations; environments https://docs.github.com/en/actions/how-tos/deploy/configure-and-manage-deployments/manage-environments; actions/upload-artifact https://github.com/actions/upload-artifact
- Live checks: https://pypi.org/pypi/blaueis-core/json, https://pypi.org/pypi/blaueis-client/json, https://pypi.org/pypi/blaueis-gateway/json (all 404 — unregistered); https://pypi.org/pypi/setuptools/json (`license_expression` served).

Local evidence (*observed*): builds of `packages/blaueis-core` with `build` 1.6.0, setuptools 84.0.0 (isolated env), twine 7.0.0, on the repository state at commit `def13c1`.
