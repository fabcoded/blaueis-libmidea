# Contributing to blaueis-libmidea

Contributions are welcome. This project is CC0 — by submitting a change you agree that your contribution is dedicated to the public domain under the same terms.

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

Each package has its own tests and ruff config.

## Running tests and linting

```sh
# From each package directory:
ruff check && ruff format --check
python3 -m pytest
```

Tests must stay green on every PR. Approximate counts today: core 83, gateway 43, client 147, tools 48.

## What good PRs look like

- **Minimal.** One logical change per PR. Bundle of unrelated edits = multiple PRs.
- **Tested.** New behaviour has a test. Fixed bug has a regression test.
- **Documented.** If you change the glossary, the field's `description:` / `note:` carry the meaning — not a separate markdown file.
- **Honest about confidence.** Use the `confidence:` scale (`confirmed > consistent > hypothesis > disputed > unknown`). Don't mark something `confirmed` that hasn't been round-tripped on real hardware.

## License and attribution

By contributing, you dedicate your contribution to the public domain under [CC0 1.0 Universal](LICENSE). If you have attribution or licensing concerns about any content in this repository, please open an issue — we will respond promptly.
