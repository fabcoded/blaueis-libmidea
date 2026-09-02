# Library hardening outline — from 0.1.0 to rock-solid

> Companion to the 0.1.0 wayfinder map (fabcoded/blaueis-ha-midea#16). Phase 0 lists what
> gates 0.1.0; the later phases are the route after it. Written 2026-09-02 against the
> measurements below; re-measure before quoting them later.

## 1. Where the library stands (measured)

| Area | State |
|---|---|
| Public surface | `__all__` in 2 of 26 modules; package `__init__`s export nothing deliberate; the integration imports ~a dozen symbols by module path |
| Typing | 185 of 265 functions carry a return annotation; no `py.typed`; no type checker in CI |
| Errors | six exception classes (`FrameError`, `HandshakeError` → `AuthenticationError`, `ReplayError`, `FormulaError`, `SlotPoolExhausted`) with no common base; 34 `except Exception` in the library, 25 in the integration |
| State | three module-level caches (`_glossary_cache`, `_SCHEMA_CACHE`, `_VALIDATOR`); the cross-test pollution the tracker records is the visible symptom |
| Tests | core 285 · client 207 · gateway 45; the UART handshake/reassembly state machine and the client reconnect/supervisor path have no tests |
| CI | lint (pre-commit) + pytest on Python 3.12 only; the gateway runs on 3.11 (Bookworm); no build/`twine check`, no coverage, no matrix |
| Packaging | static `0.1.0`, no license/readme/urls/classifiers, `setuptools-scm` listed but unused (decided on the map: fix for 0.1.0) |
| Docs | README, architecture, protocol and operations docs; no API reference, no CHANGELOG, `versioning.md` describes an unimplemented version exchange |
| Runtime | asyncio throughout; known limits: a 5 MB debug ring can exceed the client's 1 MB frame cap; `frame_spacing_ms` and queue depth are tuned, not asserted |

## 2. The picture — what "state of the art" means for a library like this

1. **A declared, typed public surface.** Every package `__init__` re-exports exactly what consumers may use, with `__all__`; everything else is private by name. `py.typed` ships once the public surface type-checks clean (mypy `--strict` on that surface, not on everything). Deprecations go through `warnings.deprecated` with a removal version.
2. **One exception family.** `BlaueisError` at the root; `ProtocolError` (frame, codec, replay), `HandshakeError`/`AuthenticationError`, `GatewayError` (`SlotPoolExhausted`, queue full), `GlossaryError` (schema, override). Consumers catch the family, never `Exception`; the library never swallows with bare `except`.
3. **Packaging and release as decided on the map.** PEP 621 metadata, SPDX license, per-package README/LICENSE, wheels + sdist, trusted publishing with attestations, tag → draft release → human publish, internal pins `~=` between the packages, exact pins in the integration.
4. **Quality gates that cannot be skipped.** Lint + format, type check on the public surface, tests on every supported Python (3.11 floor — the Pi's version — through current), `python -m build` + `twine check` on every PR so packaging cannot rot, a coverage floor on the critical paths (codec round-trips, handshake, reconnect, UART state machine). Property-based tests (hypothesis) for the codec: decode(encode(x)) == x.
5. **Data is API.** The glossary YAML and its JSON schema are versioned artefacts with accessors (`load_glossary()`, `load_glossary_schema()`, later `validate_glossary()`); consumers never touch the file layout. Schema changes follow the same compatibility rules as code.
6. **Runtime robustness.** No blocking I/O inside the event loop (hosts like Home Assistant detect it); timeouts on every await that leaves the process; explicit backpressure (queue depth, slot pool) with typed errors; bounded messages (chunked debug dump or a negotiated frame cap); reconnect with jitter and an asserted invariant that status never wipes on reconnect.
7. **Security hygiene.** The crypto v2 design stays; PSK rotation is a documented flow; dependencies are pinned in dev and floored in metadata, with automated update PRs; nothing secret reaches logs or diagnostics bundles.
8. **Documentation that matches the artefact.** A quickstart that works for an installed package, an API reference generated from docstrings (mkdocs + mkdocstrings), a CHANGELOG (Keep a Changelog), and policy docs that describe only what exists.
9. **Compatibility you can state.** Supported Python versions and gateway/integration pairs in a table; `protocol_version` compatibility tested against recorded handshakes; a 1.0 promise once the surface has been stable for a release cycle.

## 3. The route

### Phase 0 — gates 0.1.0 (inside the map)

What a stranger touches, plus what becomes expensive once installs exist:

- Packaging metadata, per-package LICENSE/README, `setuptools>=77`, drop `setuptools-scm`, `blaueis-core~=0.1.0` in client and gateway *(decided: PyPI trusted publishing, license tickets)*.
- `load_glossary_schema()` public in `blaueis.core.codec`, `inventory` switched to it; `__all__` in the `blaueis.core` and `blaueis.client` package `__init__`s naming the symbols the integration imports — the surface 0.1.0 promises, nothing more.
- Integration: drop the `lib/` `sys.path` insertion and `_SCHEMA_PATH`; `requirements: ["blaueis-core==0.1.0", "blaueis-client==0.1.0"]`; a startup warning if a stale `lib/` directory is present.
- CI (libmidea): test matrix `3.11` + `3.12`; a `build` job running `python -m build` and `twine check` for the three published packages on every PR.
- Everything else on the map's release checklist (installer release-tracking, HACS files, workflows, doc fixes).

Not gates: type checking, coverage, the exception hierarchy, docs tooling — none of them changes what a stranger experiences at install, and all are additive later.

### Phase 1 — 0.1.x (the weeks after)

- Exception family introduced *additively* (existing classes gain the new bases; nothing renamed); bare `except Exception` in the library replaced by the family or removed; the integration's 25 catches narrowed.
- `py.typed` + mypy on the public surface in CI; annotate the remaining 80 functions.
- Tests for the two untested critical paths (UART handshake/reassembly with fake streams; client reconnect/supervisor with the no-status-wipe invariant); the cross-test pollution fixed by turning the three module-level caches into objects with explicit reset.
- `validate_glossary(doc)` in core, used by inventory and the integration; the integration drops its direct jsonschema use.
- Bounded debug dump (chunked transfer or negotiated `max_size`); dead `build_follow_me_frame` removed or unified.
- libmidea CHANGELOG started at 0.1.0; README quickstart rewritten for an installed package; `versioning.md` rewritten to current truth *(decided on the map's truth-pass ticket)*.

### Phase 2 — 0.2

- Gateway from PyPI (wheel carries units + a Python `blaueis-gw`; pip update/rollback) *(out of scope on the 0.1.0 map, direction agreed)*.
- Software-version exchange in the handshake and the integration's "update available" surface.
- API reference (mkdocs + mkdocstrings) published from CI; coverage floor enforced; Python 3.13 in the matrix; hypothesis round-trip tests for the codec; dependency-update automation.

### Phase 3 — 1.0

- Stability promise per `versioning.md`: `protocol_version` frozen, WebSocket API additive-only, SemVer guarantees on the declared surface, documented deprecation policy and supported-versions table.

## 4. Principles the work is judged by

- Additive before breaking; a breaking change needs a deprecation cycle.
- Every public symbol has a docstring, a type, and a test.
- The library never catches `Exception` bare; consumers never have to.
- No blocking I/O in the event loop; every outbound await has a timeout.
- Caches are objects with explicit reset, never module globals.
- The glossary schema is versioned like code.
- Measure before claiming: the numbers in §1 are the baseline, re-measured at each phase.
