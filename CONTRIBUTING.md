# Contributing to blaueis-libmidea

Contributions are welcome. This project is MIT-licensed — by submitting a change you agree that your contribution is licensed under the same terms.

## Before you start

- For anything non-trivial, **open an issue first** describing the change and your plan. Small fixes and typo corrections can go straight to PR.
- Read [`AGENTS.md`](AGENTS.md) — it describes conventions, testing expectations, and the citation rules that are load-bearing for this project.

## Citation rule — the one that matters

This library builds on community research (see [README.md#acknowledgments](README.md#acknowledgments)). The protocol knowledge here is documented as **our own observation of the wire format**, expressed in our own words and variable names.

When editing code or docs, **never**:

- Reference file paths, function names, or line numbers from external implementations.
- Copy content from external source code — comments, variable names, logic blocks, or whitespace idiosyncrasies.

Structured-provenance fields (`alt_names:` and `sources:` inside `glossary.yaml`) are the one exception — they exist precisely to map concepts across projects. See the glossary's file-header comments and the workspace-level `AGENTS.md` for the exact rule.

## Development setup

```sh
# From the repo root:
pip install -e packages/blaueis-core packages/blaueis-gateway packages/blaueis-client packages/blaueis-tools
```

Each package has its own tests; the ruff config is shared at the repo root
(`pyproject.toml`). The ruff version is pinned in `.pre-commit-config.yaml`,
which CI runs directly — so there is a single pin, not two to keep in sync.

Install the git hooks once after cloning, so the same gates run locally
instead of first failing in CI:

```sh
pip install pre-commit   # if you don't already have it
pre-commit install
```

## Running tests and linting

```sh
# Lint from the repo root:
ruff check && ruff format --check
# Tests from each package directory:
python3 -m pytest
```

Lint and tests must stay green on every PR (both are CI gates). Tests run on
Python 3.11 (the floor, and the Pi's Bookworm system Python) and 3.12.
Approximate test counts today: core 285, gateway 84, client 213, tools 60.

CI also builds the three published packages and runs `twine check --strict`
on them, so packaging breakage shows up on the PR, not at release time.
Locally, per package: `python -m build packages/<pkg> && twine check --strict dist/*`.
Release steps: [docs/releasing.md](docs/releasing.md).

## What good PRs look like

- **Minimal.** One logical change per PR. Bundle of unrelated edits = multiple PRs.
- **Tested.** New behaviour has a test. Fixed bug has a regression test.
- **Documented.** If you change the glossary, the field's `description:` / `note:` carry the meaning — not a separate markdown file.
- **Honest about confidence.** Use the `confidence:` scale (`confirmed > consistent > hypothesis > disputed > unknown`). Don't mark something `confirmed` that hasn't been round-tripped on real hardware.

## License and attribution

By contributing, you agree that your contribution is licensed under the [MIT License](LICENSE). If you have attribution or licensing concerns about any content in this repository, please open an issue — we will respond promptly.
