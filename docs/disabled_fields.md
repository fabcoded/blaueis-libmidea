# Permanently-disabled glossary fields

> Fields with `feature_available: never` in the glossary are observable
> on the wire but **deliberately not exposed** because their decoding,
> calibration, or hardware presence is unverified. This document is the
> central place to list *why* each one is disabled and *what data the
> community would need to provide* to safely re-enable it.

If you have a unit that exhibits relevant behaviour and can capture
frames or operate in defined states, contributions are very welcome —
especially for the **grouped** fields in §2. See "How to contribute" at
the end.

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
`HVAC-shark/protocols/midea/` with the captures attached.

---

## 2. Multi-byte / grouped fields with unverified combination formula

Fields where the protocol exposes what looks like *components* of a
single physical counter (e.g. days + hours + minutes + seconds bytes
side-by-side), but where we lack the captures needed to verify how
they combine into one value.

These are the fields where **community submissions are most valuable**.

### 2.1 Duration counters (15 fields)

| Group | Component fields | What we think |
|---|---|---|
| `power_on_*` | days, hours, minutes, seconds | Power-on cumulative (since first power up). |
| `total_worked_*` | days, hours, minutes, seconds | Lifetime compressor-active time. |
| `current_session_*` | days, hours, minutes, seconds | Time since last power cycle. |
| `current_work_*` | days, hours, minutes (no seconds) | Time in current run cycle. |

Each component is decoded as an independent integer in its own unit,
but the protocol bytes sit adjacently in the same response and almost
certainly form a single combined counter. We do not know:

- Which byte(s) carry the most-significant bits — the days/hours
  fields might be redundant high-order representations of the
  minutes/seconds fields, OR each field might be the modulo-component
  of a single second-counter (`seconds = total % 60`,
  `minutes = (total // 60) % 60`, etc.).
- Whether the AC zeroes lower-order bytes when higher ones rolls
  (`59:59:59 → 1:00:00:00`) or carries a continuous internal counter
  the firmware decomposes on read.
- What rolling behaviour looks like on the days field as the unit
  approaches its maximum (16-bit BE → 65535 days ≈ 179 years, so we
  may never see a roll from real data).

**Captures we'd need to verify**:

1. **Synchronised reads across rollover**: a stream of `cmd_0x41` →
   `rsp_0xc0` polls (one every 1–5 seconds) covering at least one
   minute → hour rollover and one hour → day rollover, with all four
   component fields decoded per frame. The `flight_recorder` bundle
   from the gateway is enough — see `docs/flight_recorder.md`.
2. **Two units side by side** if available, started at known offsets,
   so we can rule out unit-specific decoding.
3. **Power-cycle observations**: capture both right before and right
   after a power cycle, to confirm which counters reset (`current_session_*`
   should; `power_on_*` and `total_worked_*` should not).

**What a verified test would look like**:

```python
def test_duration_combination_consistent_across_rollover():
    """Given a sequence of frames spanning a minute → hour rollover,
    the combined "total seconds" derived from (days, hours, minutes,
    seconds) must increase monotonically with each frame."""
    frames = load_capture("captures/duration_rollover_001.json")
    last = -1
    for f in frames:
        d = decode_field(f, "current_session_days")
        h = decode_field(f, "current_session_hours")
        m = decode_field(f, "current_session_minutes")
        s = decode_field(f, "current_session_seconds")
        total = d * 86400 + h * 3600 + m * 60 + s
        assert total >= last
        last = total
```

Until that test passes against real captures, the fields stay disabled.

### 2.2 Future grouped fields

Same template applies to any future "split into components" field set
discovered via `field_inventory` scans. List them here when they're
added.

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

## How to contribute

1. **Capture frames** with the gateway's flight recorder — see
   `docs/flight_recorder.md` for how to download a bundle.
2. **Document the operating state** alongside each capture: AC mode,
   target temperature, ambient temperature, what the wall remote shows
   on its display, and whether a compressor is audibly running.
3. **Open a PR or issue** in either `blaueis-libmidea` or
   `HVAC-shark` with the bundle attached and a one-line description
   of what hypothesis the data lets us test.

Even a single rollover-spanning capture for a duration field would let
us promote the related fields out of `feature_available: never`. The
bottleneck is data, not code.
