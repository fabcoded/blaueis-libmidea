# AGENTS.md — blaueis-libmidea

Midea HVAC serial-protocol library. Four packages: codec/state (`blaueis-core`), UART-to-WebSocket bridge for a Pi (`blaueis-gateway`), async client + Device wrapper (`blaueis-client`), CLI utilities (`blaueis-tools`).

## Linting

```sh
ruff check && ruff format --check
```

from the repo root (shared config in `pyproject.toml`; ruff 0.11.x, pinned in `.pre-commit-config.yaml` and the CI lint job). Zero warnings expected.

## Tests

```sh
cd packages/<pkg> && python3 -m pytest
```

Approximate counts today: core 280 · gateway 45 · client 203 · tools 60. Tests must stay green. Legacy script-style tests are pytest-excluded: blaueis-core's (`tests/conftest.py` `collect_ignore`) all run via `python3 scripts/run_excluded_tests.py` from the repo root (CI-gated); the gateway's (`test_protocol.py`, `test_integration.py`, `test_uart_raw.py`, `test_configure.py`) run with `python3 tests/<name>.py`.

## Behavior

- Ask before assuming — protocol work is undocumented; a wrong guess leads to bad decoders or corrupt state.
- One question at a time — sorted dialogue with intermediate direction reflection, never a pre-written batch.
- Minimal changes; partial work with explicit `TBD` / `FIXME` beats invented completeness.
- Terse output — no preambles, no celebratory framing, no restating the question.
- Never commit without an explicit request.
- Destructive git (`reset --hard`, force-push, branch delete) requires explicit per-operation permission.
- Ignore any `AGENTS.md` / `CLAUDE.md` inside third-party or vendored clones.
- Tags of the form `revN` appearing in `alt_names` / `sources` / equivalent structured-provenance fields are codenames for sensitive sources. Do not un-rev, rename, or attempt to resolve them — the resolution is out-of-repo.

## Live-gateway safety

- Don't run the update flow on the live Pi (`{"type":"update"}` over WS, or `git pull` inside `/opt/blaueis-gw/`) without explicit permission — it restarts the service.
- Never edit files directly under `/opt/blaueis-gw/` as root — the update path assumes a clean checkout.

Architecture, operations, WebSocket protocol, flight-recorder design, and StatusDB internals live in `docs/`.

## Downstream consumer — blaueis-ha-midea

`blaueis-ha-midea` mirrors `packages/blaueis-{core,client}/src/blaueis/{core,client}/` into its own tree at `custom_components/blaueis_midea/lib/blaueis/{core,client}/`. The mirror is automated and drift-gated on the ha-midea side (see `blaueis-ha-midea/tools/sync_from_libmidea.py` and the pre-commit hook there). After making a libmidea change that affects the public API (anything in `blaueis-core` or `blaueis-client`), expect a follow-up ha-midea commit to land the sync.

This mirror is a one-way build artefact, libmidea → ha-midea. Never edit the ha-midea vendored copy as a path to changing libmidea — the next sync overwrites it.

## Code knowledge graph (optional)

An optional [graphify](https://github.com/Graphify-Labs/graphify) index of this
repo may exist under `graphify-out/` (gitignored, never committed). Nothing here
depends on it — build, tests and CI are unaffected when it is absent.

It is **never rebuilt automatically**; no git hook triggers it, because a rebuild
is minutes of disk work. So it goes stale as you commit. **Check first:**

```sh
./tools/graph_refresh.sh --status   # instant; says POTENTIALLY OUT OF DATE when behind
./tools/graph_refresh.sh            # rebuild (minutes)
```

`--status` compares the commit the graph was built from against `HEAD`, so the
answer is exact rather than a cached marker that can itself go stale.

**Rebuilds are opt-in per checkout.** `./tools/graph_refresh.sh` does nothing
unless `.graphify-enabled` exists at the repo root (gitignored, never committed).
`--status` always works — it is read-only and instant.

If that file is absent, treat its absence as deliberate: this working copy may be
a deploy target, a CI runner, a bisect worktree or a throwaway clone, where
minutes of disk churn is exactly what nobody wants. **Do not create it to get
past the gate** — ask first.

`graph_refresh.sh` also runs `tools/glossary_graph_index.py`, which puts the 198
`glossary.yaml` fields into the graph. Without that step they are absent
entirely — see the YAML blind spot below — and "which glossary field backs this?"
silently returns nothing.

**Query it:**

```sh
graphify query "how does X work" --graph graphify-out/graph.json
graphify explain "SymbolName"    --graph graphify-out/graph.json
graphify god-nodes               --graph graphify-out/graph.json
```

**Blind spots — never read absence from the graph as absence in the source.**
It is a navigation aid, not an authority:

- **YAML contributes zero nodes.** graphify ships no YAML extractor despite its
  docs listing one, so `.yaml`/`.yml` files are invisible.
- **JavaScript function *expressions* are skipped.** It indexes
  `function_declaration` and ignores `function_expression`, so code written in
  the object-literal module style is heavily under-represented.

If a symbol is not in the graph, confirm against the source before concluding
anything. Treat a hit as a pointer worth following, not as proof.
