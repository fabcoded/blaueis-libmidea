# Glossary companion — deferred work & design sketches

> The glossary (`glossary.yaml`) is the protocol data source-of-truth. A
> handful of entries reference this doc from their `sources:` or `note:`
> by stable section number — for codec frame-builder status (§6)
> and for excluded fields' specific reopen conditions (§13). This is
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

## §6 — Codec frame builders (per-entry status)

Frame-builder work referenced from the matching `cmd_*` glossary entries'
`sources:`. Section numbers are stable anchors; entries state their current
status rather than being removed when built.

### §6.1 — `cmd_0x41_ext` (extended state query, sub-page 0x02)

- **Status:** built — the generic `bytes_at` body assembler
  (`build_frame_from_spec`) constructs it, and the field-inventory tooling
  issues it as a probe. The `rsp_0xc1_sub02` reply routes through the
  existing C1 sub-page parser.
- **Remaining:** evidence, not code. Never observed from a controller in
  our captures, and our bench unit has not been confirmed to answer the
  synthesized query — the three glossary fields that decode from
  `rsp_0xc1_sub02` stay `confidence: hypothesis` until a capture confirms
  they populate as decoded.

### §6.2 — `cmd_0xb0` property-set builder

- **Status:** built and in live use — the shared body assembler
  (`build_b0_command_body`) walks each field's `protocols.cmd_0xb0` entry
  (property ID + data bytes) the way the `cmd_0x40` builder walks `decode`
  arrays; property writes are a data-only change. Verified by encode
  round-trip tests against `rsp_0xb1` / `rsp_0xb1_prop` decodes.
- **Remaining:** nothing — the anchor stays because glossary entries cite it.

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

### §13.6 — `peak_elec` (`unknown_semantic`)

- **Current disposition:** excluded; its `rsp_0xc0` decode block was
  **deleted** (not just hidden) to clear the `body[10] bit6` collision
  with the relocated `filter_clean_due`. The prior `body[10] bit6` claim
  was wrong: six decoders read that bit as peak-valley / natural-fan, and
  one source has the peak/valley read commented out at bit5. Source-only,
  never wire-observed; the real demand-response mechanism in this protocol
  is a property-TLV window (`dr_time`), not a C0 status bit.
- **Reopen condition:** a live capture where a C0 status bit toggles in
  correlation with an induced peak-electricity / demand-response event
  **and** a source decoder that reads a peak/DR flag from that specific
  bit. Do **not** relocate onto the `dr_time` property — that is the DR
  *window*, a distinct concept from a peak/valley status flag.
- **On reopen:** position the decode at the proven bit, or re-tier to a
  property-TLV sensor; drop `unknown_semantic`.

### §13.7 — Filter family: clean-flag watch + deferred fresh-air pair

Covers the `filter_clean_due` (relocated to `rsp_0xc0 body[13] bit5`) and
`filter_clean_reset` (`cmd_0x40 body[10] bit7`) fields, both
capability-gated on FILTER_REMIND (`0x0217`) and therefore **dormant on
our XtremeSaveBlue** (cap not advertised — bench scan + live HA snapshot).

- **Active-behaviour reopen (`filter_clean_due` / `filter_clean_reset`):**
  both are signed off at `consistent` on six-decoder + unit-test grounds,
  but the *live* behaviour (flag firing on a dirty filter; the reset
  clearing it) is **unverified** — our hardware can't exercise it. Confirm
  on a unit that advertises `0x0217`: a capture where `body[13] bit5` goes
  to 1, then a `filter_clean_reset` press drives it back to 0. On
  confirmation, the fields can be promoted toward `stable`.
- **`body[13]` dry-humidity watch (`filter_clean_due`):** in dry/smart_dry
  the OEM read-route packs the humidity target onto `body[13] & 0x7F`,
  which covers bit5 — a false "clean due" risk. Mitigated by
  `ux.visible_in_modes` (excludes dry/smart_dry) and inert on our wire
  (we map C0 humidity to `body[19]`). Reopen if a real dry-mode humidity
  capture shows `body[13]` carrying the `0x7F` humidity on our firmware.
  Note `target_temperature` reads the same byte (`body[13] & 0x1F`,
  ungated) and shares this latent multiplex — track jointly.
- **Deferred fresh-air pair (not added):** `filter_replace_due`
  (`body[13] bit6`, "replace the filter") and `filter_replace_reset`
  (`cmd_0x40 body[22] bit3`), plus the fresh-air lifetime counters
  (`body[24..27]` LE16) and the `0x09,0x04` level/value TLV. Un-defer when
  a unit advertises `fresh_air` (`0x004B`). The `body[22]` reset shares the
  byte with `comfort_power_save` / `independent_ptc`; model those before
  building it. Full mechanism map: `internal-tests/findings/15_filter_dust_mechanisms.md`.

### §13.8 — `target_temperature` extended-setpoint ladder (`decode_unverified`)

- **Current disposition:** the `rsp_0xc0 body[13]` extended-setpoint override
  is now mode-gated (suppressed in dry/smart_dry via the
  `temp_extended_setpoint_active` decode predicate — that bug is **fixed**).
  But the override's *formula* is still incomplete: we apply a flat `add: 12`
  (`raw + 12`), correct only for the normal arm `raw 1..25` (→ 13–37 °C). The
  OEM body[13] setpoint is a value-gated **three-way ladder**: `raw == 0` → no
  override (sentinel, handled), `1..25` → `raw + 12`, **`26..31` → `raw - 19`**
  (→ 7–12 °C, the deep/8 °C-heat low arm). Our flat `+12` mis-decodes `raw
  26..31` to 38–43 °C.
- **Why it's latent (range-masked):** the field's `range: [16.0, 30.5]`
  value-gate suppresses 38–43 °C to `None`, so today the user sees *no value*
  rather than a wrong one. It would only surface if the range were widened.
  (A maintained open-source reference decoder is also `+12`-only — its own
  code notes additional range is possible — so even upstream is incomplete here.)
- **Reopen condition:** a non-dry capture that lights up `body[13]` as a
  genuine low extended setpoint (`raw ≥ 26`) to validate the `raw - 19` arm.
  A single scalar `add:` can't express the piecewise ladder — implement it as
  a named decode transform on the same `decode_predicates` substrate this
  session introduced. Then `target_temperature` can sign off.

### §13.9 — `night_light` (`never_observed`, hardware-absent)

- **Current disposition:** excluded. A real on/off courtesy night-light LED
  (decode correct: `body[10] bit4`), but **non-functional on our SKU** —
  live-tested 2026-05-31, an HA write does not engage in cool/idle or in sleep
  mode (readback stays off), while a preset write in the same run did stick, so
  the write path works. The bit reads 0 in every capture. It is **capless** (no
  B5 gate), so it cannot be surfaced only-where-supported; excluded so a dead
  control is not shown here (NOT a display dimmer — display brightness is the
  separate `screen_display`).
- **Reopen condition:** a unit that physically has the night-light LED, where
  the HA write engages (readback flips on, then clears on reset). On such a unit
  it re-tiers to `always` (it has no cap to gate it).

### §13.10 — `turbo_mode` (`never_observed`, wrong wire path on this SKU)

- **Current disposition:** excluded. The decode is correct and cross-bus
  validated on our wired-controller rig, where toggling turbo drives the XYE
  mode-flags byte and matches this bit across 332 time-matched frame pairs. On
  our gateway-attached unit, though, the bit is never set: in every one of our
  own C0 captures it reads 0, and when the boost is enabled physically at the
  unit the device sets `body[8] bit 5` (`strong_wind`) and leaves this bit
  clear. A live write over a direct device connection (2026-06-03) reads back
  as 0 across a poll. This SKU's boost therefore runs through `strong_wind`,
  which is what the integration's "Turbo" preset writes; excluded so a dead
  control is not offered here.
- **Note the difference from §13.9:** this field is **not** capless — cap
  `0x1A` exists and reads 1 (cool+heat) on our unit, i.e. the capability
  advertises a feature the write does not deliver. Cap-gating therefore cannot
  hide it, which is why exclusion is the mechanism.
- **Reopen condition:** a unit whose firmware acts on `body[10] bit 1` — the
  write engages and the readback holds across a poll, or the bit is observed
  set in a capture. `never_observed` is a caveat-class reason, so such a unit
  can already re-enable the field through a glossary override without a
  glossary change; the reopen is to re-tier it back to `readable` by default.
