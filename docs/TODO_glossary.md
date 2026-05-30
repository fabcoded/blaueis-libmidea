# Glossary companion — deferred work & design sketches

> The glossary (`glossary.yaml`) is the protocol data source-of-truth. A
> handful of entries reference this doc from their `sources:` or `note:`
> by stable section number — for codec frames sketched but not yet built
> (§6) and for excluded fields' specific reopen conditions (§13). This is
> the living home for that deferred work, so an exclusion or a hypothesis
> entry points somewhere real instead of carrying a dead promise.
>
> Section numbers are stable anchors: glossary entries cite `§6` / `§13`,
> so those numbers don't get renumbered. Unlisted numbers are simply
> unused — add a new section with the next free number rather than
> reusing one.
>
> **Public-repo doc.** Everything here is written in our own words. No
> source paths, symbol names, line references, or copied material — same
> leak bar as the glossary `note:` fields.

## §6 — Codec design sketches (frames not yet built)

Frames the protocol model documents but our Python codec does not yet
assemble. Each is referenced from the matching `cmd_*` glossary entry's
`sources:`. The sketch records *why it's deferred* and *what building it
would take*, so the glossary entry can stay `confidence: hypothesis`
honestly.

### §6.1 — `cmd_0x41_ext` (extended state query, sub-page 0x02)

- **Status:** not built by our codec; not observed in our captures.
- **Why it matters:** three glossary fields decode from its response
  (`rsp_0xc1_sub02`), so *some* controller issues this query — just not
  the OEM WiFi dongle or extension board we have on the bench.
- **To build:** assemble the fixed selector body (the `bytes_at` map on
  the glossary entry documents the selector positions) and route the
  `rsp_0xc1_sub02` reply through the existing C1 sub-page parser.
- **To verify:** a capture from a controller that does emit it, or a
  bench unit that answers the query we synthesize — confirming the three
  dependent fields populate as decoded.

### §6.2 — `cmd_0xb0` property-set builder

- **Status:** constructed on demand today, no general builder.
- **Why it matters:** property-only fields (ionizer, breezeless, the
  persistent buzzer alternative, and similar) each hand-roll their B0
  frame; a shared builder would remove the duplication and make new
  property writes a data-only change.
- **To build:** a body assembler that walks each field's
  `protocols.cmd_0xb0` entry (property ID + data bytes) the way the
  `cmd_0x40` builder walks `decode` arrays.
- **To verify:** round-trip — write via the builder, read the value back
  via `rsp_0xb1` / `rsp_0xb1_prop`, confirm it matches.

## §13 — Reopen conditions for excluded fields

Per-field, the *specific* observation that would justify moving a field
out of `feature_available: excluded`. Complements the *generic* recovery
column in `exclusion_reasons.md` — that doc says what kind of evidence a
reason needs; this says what it concretely looks like for the field.

### §13.1 — `rate_select` (`unknown_semantic`)

- **Current disposition:** excluded. The byte is passive — it resets to
  the sentinel `100` ("off/disabled") on every power and mode
  transition, so a non-`100` value is not user-sustainable. The B5
  capability values (`gear_2` / `gen_mode` / `gear_5`) describe the
  supported speed-mode *type*, not the runtime byte; the runtime
  sub-rate meanings are undecoded.
- **Reopen condition:** a `gen_mode` or `gear_5` device on which the
  sub-rate byte is **durable** (survives a mode change), **mode-stable**
  (doesn't snap back to `100`), and **settable** (a written sub-rate
  reads back and persists). On such a unit the byte carries real state
  and the sub-rate values become decodable from paced set/read-back
  captures.
- **On reopen:** decode the sub-rate value set, drop `unknown_semantic`,
  and re-tier from `excluded` to `capability` (B5-gated on cap `0x48`).

### §13.2 — Vane status & travel-limit readbacks (`unknown_semantic`)

Fields: `vane_ud_status`, `vane_lr_status`, `vane_top_status` (2-bit
status enums) and `vane_ud_cool_upper/lower`, `vane_ud_heat_upper/lower`,
`vane_lr_upper/lower` (per-mode travel limits), all in `rsp_0xc1_group11`.

- **Current disposition:** excluded. They *populated* in the Session-15
  capture (status bytes read `0`; limits read a constant `100`/`0`), so
  this is not a never-observed case — but the status enums carry no
  decode map (a bare integer renders meaningless) and the limit
  percent-scale is unverified (hypothesis-only). Nothing a person or
  automation can yet read as signal.
- **Reopen condition:** a capture in which these bytes **track real
  louver motion** — status bytes changing as the strip moves, limits
  changing across a deliberate cool↔heat config — **plus** an enum/scale
  decode (a value→meaning map for the status enums, a verified percent
  formula for the limits).
- **On reopen:** add the decode map/encoding, drop `unknown_semantic`,
  and surface as diagnostic-shelf sensors (`entity_category: diagnostic`).

### §13.3 — Vane angle readbacks (`decode_unverified`, dead sensor)

Fields: `vane_ud_angle` (read `48`→`0` across runs), `vane_lr_angle`
(stable `240` = `0xF0`, out of any 0–100 range).

- **Current disposition:** excluded. The angle hardware sensor is **dead**
  on this unit — the byte returns garbage / out-of-range values, not a
  trustworthy angle. The sibling B1 property reads corroborate (no live
  angle). Publishing it would falsify long-term statistics.
- **Reopen condition:** a unit (or a repaired sensor) where the byte
  leaves its garbage value and **tracks the real vane angle** — ideally a
  before/after capture paired with louver video — confirming the decode.
- **On reopen:** surface as a diagnostic angle sensor; the decode is
  already positioned, so this is an evidence reopen, not a new decode.

### §13.4 — Group-7 / Group-12 unknown bytes (`unknown_semantic`)

Fields: `group7_unknown_byte5/6/7/10/11` (vary with load / counter-like),
`group7_unknown_byte8`, `group12_unknown_byte4/6` (constant), all
undecoded with no known vendor decoder.

- **Current disposition:** excluded, kept as **raw debug documentation**,
  not standing entities. Each field's `description` records the observed
  pattern (increments, load-correlation, or constant value). Investigator-
  reachable via the field-inventory service, the glossary-overrides
  textarea, and the flight-recorder full-frame capture — one door away.
- **Reopen condition:** a **cross-reference decoder** for the Group-7 /
  Group-12 body, OR **multi-session captures at known physical states**
  that pin a varying byte to a quantity (e.g. a byte to a compressor-load
  curve). For the *constant* bytes, a reference confirming the byte is
  reserved would instead reclassify them `protocol_inert`.
- **On reopen:** decode and re-tier the byte that earns it; leave the
  rest as documentation.

### §13.5 — `outdoor_return_air_temp` (`unknown_technical_background`)

- **Current disposition:** excluded. A **real** outdoor return-air
  (suction) thermistor — present in every G3 frame, corroborated by an
  independent source attribution (preserved in the field's `alt_names`) —
  but the wire value is a **raw NTC ADC**
  reading the outdoor MCU does not convert (read constant `114` only
  because outdoor air sat ~3–5 °C all session). With no lookup table,
  publishing `114` as a temperature would mislead and corrupt statistics.
  The reason is a *caveat*, so a power user can already hard-override it
  via the Glossary-Overrides textarea today.
- **Reopen condition:** obtain or derive the **NTC lookup table** (a
  shared-thermistor table from the indoor path, a datasheet curve, or a
  2-point bench calibration) — then a multi-condition capture proving the
  converted value tracks suction temperature. This is the single
  strongest near-term reopen candidate of the excluded set.
- **On reopen:** apply the lookup `encoding`, add a `ha:` block
  (`device_class: temperature`, °C), drop `unknown_technical_background`,
  and surface as a clean diagnostic temperature sensor. If the table
  stays elusive, the interim option is an explicitly raw-ADC diagnostic
  (no °C unit, no `device_class`), disabled-by-default.
