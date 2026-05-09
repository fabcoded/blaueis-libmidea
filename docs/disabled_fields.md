# Permanently-disabled glossary fields

> Fields with `feature_available: excluded` in the glossary are observable
> on the wire but **deliberately not exposed** because their decoding,
> calibration, or hardware presence is unverified. This document is the
> central place to list *why* each one is disabled and *what data the
> community would need to provide* to safely re-enable it.

If you have a unit that exhibits relevant behaviour and can capture
frames or operate in defined states, contributions are very welcome —
especially for the **grouped** fields in §2. See "How to contribute" at
the end.

For the formal schema contract — the closed list of `excluded_reasons`
values, override-eligibility per reason, and the status-feedback flow —
see `exclusion_reasons.md`. This document is the human reading layer
listing the current members; that one is the contract.

---

## 1. Dead / unreliable hardware sensors

Fields that read but have been observed to return garbage, drift, or
stale values across captures. Hardware-level evidence rules them out;
no protocol-level fix would help.

| Field | Reason |
|---|---|
| `vane_ud_status`, `vane_lr_status`, `vane_top_status` | Reported state lags actual louver position by seconds; not safe to drive automations from. |
| `vane_ud_angle`, `vane_lr_angle` | Angle sensors return 0 or constants on all observed units. Treated as dead. |
| `vane_ud_cool_upper/lower`, `vane_ud_heat_upper/lower`, `vane_lr_upper/lower` | Per-mode vane limits — bytes report stable values but their effect on louver behaviour cannot be reproduced. |

**To re-enable any of these**: capture-pair before/after the position
change, plus a video showing the actual louver angle, across at least
two distinct mode/setpoint combinations. Open an issue in
`blaueis-hvacshark/protocols/midea/` with the captures attached.

---

## 2. Multi-byte / grouped fields

Fields where the protocol exposes *components* of a single physical
counter (e.g. days + hours + minutes + seconds bytes side-by-side).
The combination formula is wired through the codec's
``composite/derived_from`` synthesis pass; user-visible glossary fields
expose only the synthesised aggregate.

### 2.1 Duration counters — synthesised

The four duration-counter groups are now aggregate fields backed by
oracle-validated synthesis (golden vectors in
``packages/blaueis-core/tests/fixtures/duration_counter_vectors.json``).
The component bytes are wire-internal — never surfaced as standalone
glossary fields:

| Aggregate field | Frame | Wire bytes | Formula |
|---|---|---|---|
| `power_on_time` | rsp_0xc1_group0 | body[4..8] | `days*86400 + hours*3600 + minutes*60 + seconds` |
| `total_worked_time` | rsp_0xc1_group0 | body[9..13] | same |
| `current_session_time` | rsp_0xc1_group0 | body[14..18] | same |
| `current_work_time` | rsp_0xa1 | body[9..12] | `days*86400 + hours*3600 + minutes*60` (no seconds on wire) |

All four are `feature_available: readable`, unit `s`. Probe captures
showed always-zero on test units; the formula is locked down via
oracle parity tests and is a separate concern from whether a given
unit populates non-zero values.

The fifth synthesised time field, `dr_time`, lives in the B1 property
0x8F,0x00 (hours + minutes only, no days/seconds). It stays
`feature_available: capability` since it's only meaningful on
DR-equipped SKUs.

### 2.2 Future grouped fields

Same template applies to any future "split into components" field set
discovered via `field_inventory` scans. The glossary's
``composite/derived_from`` blocks plus ``encoding`` on
``composite_member_wire`` (for multi-byte members) are the supported
shape.

---

## 3. Decoding unknown / disputed

Fields where the byte is observed but a reliable formula is missing.

| Field | What's missing |
|---|---|
| `ipm_module_temp` | IPM (insulated power module) temperature — raw value 0–255 with no shared encoding across captures. Needs paired thermistor lookup or a reference reading from the unit's service menu. |
| `outdoor_return_air_temp` | Reported as raw ADC. Needs an NTC-thermistor lookup table specific to this AC family. |
| `local_body_sense` | Built-in occupancy sensor active flag. Bit interpretation (sticky vs latched vs instantaneous) unverified. |

**To re-enable**: provide either (a) a service-menu screenshot showing
the value next to a known temperature, or (b) two captures from the
same unit at two known IPM/return-air temperatures (e.g. cold-start
vs steady-state).

---

## 4. Unknown probe-discovery bytes

Bytes the `field_inventory` scan classified as "populated" (carrying
non-zero, non-constant data) but whose meaning hasn't been
identified by any source documentation (glossary, Lua decoders, weex,
node-mideahvac).

| Field | Observed pattern |
|---|---|
| `group7_unknown_byte5` | Increments across probe runs (FD → FE → FF). Possibly a frame-counter. |
| `group7_unknown_byte6` | Decrements with compressor frequency (07 → 02). Possibly a load index. |
| `group7_unknown_byte7` | Mostly stable (01); jumped to 02 at low compressor frequency. |
| `group7_unknown_byte8` | Constant 0x06 across all runs. |
| `group7_unknown_byte10` | Non-monotonic (8F → 58 → B3). Possibly a reading flag. |
| `group7_unknown_byte11` | Decrements with load (02 → 01 → 00). |
| `group12_unknown_byte4` | Constant 0x02 across all runs. |
| `group12_unknown_byte6` | Constant 0x0F across all runs. |

**To re-enable**: any cross-reference from another vendor's open
implementation, OR captures from a unit operating at *known* states
(e.g. specific compressor frequency, ambient temperature) that let us
correlate the byte to a physical quantity.

---

## 5. Excluded — decoder works, exposing is wrong

Sections 1–4 cover *evidence* gaps: bytes flow but the decode can't
be trusted. This section is the opposite — the decode is verified,
but surfacing the field as an HA entity would create a worse outcome
than hiding it. Membership criteria:

1. The wire decode is verified (capture-cross-checked).
2. Hiding is a deliberate integration design decision, not protocol
   caution.
3. Re-enabling is a UX or scope question, not an evidence question.

Two recurring patterns appear here. Future additions go under the
matching sub-section, or open a new one with the same shape.

### 5.1 Conflicting sources of truth with Home Assistant

The field exposes an AC-internal feature whose function is already
provided — usually better — by HA itself. Surfacing it gives the
user two competing controls for the same outcome and forces them
to mentally reconcile state across systems.

| Field(s) | Reason |
|---|---|
| `power_off_timer`, `power_on_timer` | AC-internal countdown switches. HA's automation engine, schedule helper, and the broader ecosystem (scripts, scenes, calendars) supersede the AC's single fire-and-forget countdown. Users who want a delayed power transition should drive `climate.turn_off` / `climate.turn_on` from an HA automation. |
| `power_off_time_value`, `power_on_time_value` | Companion duration values for the timer switches above. Only meaningful while the parent timer is engaged, which it isn't (per above). Decoded but not exposed. |

**To re-enable** any entry here: an integration design decision that
documents how the HA-side and AC-side worlds are reconciled — for
example, "HA wins; the AC-side surface is auto-cleared on every set",
or "user picks one mode and the other is hidden". Until that
reconciliation rule exists, exposing the field is net-negative.

### 5.2 Protocol-reserved bits with no user semantic

The byte position is required by the wire protocol (the encoder
writes a fixed value to keep the frame format compatible) but the
bit doesn't represent any user-controllable function. There is
nothing to expose because there is no semantic at the field level —
just a frame-formatting requirement.

| Field | Reason |
|---|---|
| `fan_speed_timer_bit` | `cmd_0x40` body[3] bit 7 is fixed at 1 (`0x80`) on every frame, regardless of timer state. The encoder writes the constant to match the manufacturer's frame format; there is nothing for the user to set or read. |

**To re-enable** any entry here: protocol observation showing the
bit actually carries variable, user-meaningful state on some firmware
or hardware variant — i.e. the premise of the section no longer
holds. Until then, the encoder's fixed value is the only correct
behaviour and there's nothing to surface.

---

## How to contribute

1. **Capture frames** with the gateway's flight recorder — see
   `docs/flight_recorder.md` for how to download a bundle.
2. **Document the operating state** alongside each capture: AC mode,
   target temperature, ambient temperature, what the wall remote shows
   on its display, and whether a compressor is audibly running.
3. **Open a PR or issue** in either `blaueis-libmidea` or
   `blaueis-hvacshark` with the bundle attached and a one-line description
   of what hypothesis the data lets us test.

Even a single rollover-spanning capture for a duration field would let
us promote the related fields out of `feature_available: excluded`. The
bottleneck is data, not code.
