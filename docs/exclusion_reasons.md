# Exclusion reasons — schema contract

> Why a glossary field is `feature_available: excluded`, what evidence
> would unlock it, and how the override mechanism behaves per reason.
> This doc is the formal contract; `disabled_fields.md` is the
> human-facing reading layer that lists the current members.

## 1. Reason vocabulary

A field with `feature_available: excluded` carries a non-empty
`excluded_reasons:` list. Each entry is one of:

| Reason | Meaning | Generic recovery |
|---|---|---|
| `unknown_semantic` | Bits/bytes populated; meaning unidentified. | Cross-reference from another implementation; or captures at known physical states that correlate the byte to a quantity. |
| `decode_unverified` | Decoder coded; formula contested across captures or firmware. | Behavioral observations to disambiguate between candidate formulas (paired captures at known reference values, service-menu screenshots). |
| `unknown_technical_background` | Decoder coded; formula needs reference material we don't have. | Datasheets, NTC lookup tables, protocol references — engineering documentation, not behavioral observation. |
| `never_observed` | Decoder coded; value never seen populate or change in any capture. | A single capture where the field genuinely changes (e.g. real PM2.5 event, vane angle change). |
| `unnecessary_automation` | Decode trustworthy; hidden because Home Assistant's mechanism supersedes the AC-side feature. | UX / integration design decision — define how HA-side and AC-side worlds reconcile (e.g. "HA wins; AC-side surface auto-cleared on every set"). |
| `protocol_inert` | Wire-encoding bookkeeping; no field-level user semantic (reserved bit, fixed constant). | Protocol observation showing the bit carries variable, user-meaningful state on some firmware or hardware variant — i.e. the premise of the reason no longer holds. |
| `never_tested_write` | Encoder exists in code; no end-to-end round-trip evidence (write → wire → read-back). | Round-trip capture: send command, AC accepts, field reads back as commanded. |
| `unsafe_write` | Encoder works; writing has known unintended side effects. | Field-specific safety analysis. Reserved — not currently in use. |

Field-specific recovery prose lives in the existing `note:` field on
each glossary entry; the table above is the *generic* recovery
shape per reason.

## 2. List composition — worst-wins

`excluded_reasons:` is always an array, minimum length 1. A field
can carry multiple reasons when several apply concurrently — for
example, a feature whose surfacing is a UX call *and* whose write
path was never round-tripped.

```yaml
excluded_reasons: [unnecessary_automation, never_tested_write]
```

When multiple reasons apply, the override-eligibility outcome is the
**most restrictive** of all reasons in the list.

## 3. Override-eligibility

The integration's Glossary-Overrides textarea lets a user hard-override
an excluded field. The merge result depends on the field's reasons:

| Reason in the list | Override outcome |
|---|---|
| `protocol_inert` | **rejected** — nothing to surface; merge is skipped, error returned. |
| `unknown_semantic` | **rejected** — surfacing produces noise, not signal. |
| `unsafe_write` | **rejected** — active harm risk. |
| `decode_unverified` | **caveat** — merge proceeds; user is told decoder is unverified. |
| `unknown_technical_background` | **caveat** — merge proceeds; user is told formula needs reference material. |
| `never_observed` | **caveat** — merge proceeds; user is told value has never been seen populate. |
| `never_tested_write` | **caveat** — merge proceeds; user is told write path is unverified. |
| `unnecessary_automation` | **accepted** — merge proceeds cleanly; UX choice, user knows what they're doing. |

Worst-wins on the list:

- Any reason from the **rejected** rows present → field is rejected.
- Else any reason from the **caveat** rows present → field is caveat.
- Else (only `unnecessary_automation`) → accepted clean.

Example: `[unnecessary_automation, never_tested_write]` → caveat
(because `never_tested_write` is in the list).

## 4. Status feedback

`apply_override()` (in `blaueis-core/glossary_override.py`) returns a
structured array of `OverrideMessage` records. Each message carries:

- `severity`: `info` | `warning` | `error`
- `code`: machine-readable identifier — `excluded_accepted` |
  `excluded_caveat` | `excluded_rejected` | `protected_key` (the
  pre-existing meta-strip warning).
- `field`: the glossary field path the message is about (or `None`
  for top-level messages).
- `reasons`: the field's `excluded_reasons` list, when applicable.
- `message`: a human-readable one-line summary.

The integration surfaces this in two places — one user-facing, one
forensic:

**User-facing — read-only field below the override textarea.** A
single line that summarises the parse outcome of the *stored* YAML.
Recomputed on every form render (so it reflects what the integration
is actually using, not your pending edits). One of:

- *(empty)* — no override configured.
- `parse ok` — validates clean, no exclusion-gating messages.
- `parse with warning (check log)` — validates and is applied, but
  emitted at least one `excluded_caveat` or `excluded_accepted`
  message. Per-field detail lives in HA's logs (Settings → System →
  Logs, filter on `blaueis_midea`).
- `parse failed (check log)` — stored YAML did not validate. The
  integration ignores the override at runtime; the previous-good
  override stays in effect until the user fixes the YAML.

**Forensic — diagnostics bundle.** `Download diagnostics` from the
integration's device page emits a structured `glossary_override`
section containing the raw YAML, the affected leaf paths, and the
full structured `messages` array (severity, code, field, reasons,
message). Useful for issue reports; not a runtime UX surface.

No name or icon mutation on entities. No Repairs entries. No setup-log
WARNING per override. No persistent notifications. The HA log
(`_LOGGER.info` / `_LOGGER.warning` per message) is where per-field
detail surfaces; the read-only field above is the at-a-glance summary.

## 5. Schema shape

```yaml
fields:
  control:
    power_off_timer:
      feature_available: excluded
      excluded_reasons:
        - unnecessary_automation
        - never_tested_write
      # rest of the field unchanged
```

Schema rules:

- `excluded_reasons` is an array of unique strings; each entry must be
  a value from the closed enum in §1.
- `minItems: 1` — empty list is invalid.
- **Required when excluded (enforced).** The schema requires
  `excluded_reasons` whenever a field-level `feature_available ==
  "excluded"`, via an `allOf if/then` block in the field-definition
  schema. The migration is complete — every field-level excluded field
  carries reasons, and `test_schema_validation.py` asserts a reason-less
  excluded field is rejected. The requirement is **field-level only**:
  the capability per-cap-value enum (`#/$defs/cap_value`) also uses
  `feature_available: "excluded"` to mean "unsupported on this raw" and
  intentionally needs no reasons.

## 6. Cross-references

- `docs/disabled_fields.md` — current members per section, prose
  rationale, contribution prompts.
- `packages/blaueis-core/src/blaueis/core/data/glossary_schema.json` —
  the canonical schema definition.
- `packages/blaueis-core/src/blaueis/core/glossary_override.py` —
  override merge implementation, `OverrideMessage` dataclass, code
  vocabulary.
