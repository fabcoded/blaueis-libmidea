#!/usr/bin/env python3
"""
Breeze / wind-comfort family — SYNTHETIC codec tests.

All of these fields except breeze_away (on our SKU) are CAPABILITY-TIER:
hidden / unadvertised on our hardware, so we cannot live-verify their
on-device behaviour. Instead we lock the *protocol characterization* we are
confident about (decode + encode round-trip per the OEM source) with synthetic
frames, and ship them so users whose hardware exposes them can confirm or
correct the on-device behaviour.

  Source-characterized, NOT hardware-verified — on-device behaviour awaits
  field feedback. See blaueis-research/findings/breeze_family_reference.md.

Run: python tests/test_breeze_family.py
"""

import sys

from blaueis.core.codec import decode_frame_fields, load_glossary
from blaueis.core.command import build_b0_command_body

passed = failed = 0


def check(label, cond, detail=""):
    global passed, failed
    if cond:
        passed += 1
        print(f"  [PASS] {label}")
    else:
        failed += 1
        print(f"  [FAIL] {label}  {detail}")


def b1(recs):
    """Synthetic B1 property-response body. recs = [(prop, type, data_bytes)]."""
    body = bytearray([0xB1, len(recs)])
    for p, t, d in recs:
        body += bytes([p, t, 0x00, len(d)]) + d
    return bytes(body)


def tx_prop(body, prop):
    """Find a property's data in a B0 TX TLV body (3-byte header)."""
    i = 2
    while i + 3 <= len(body):
        p, _t, ln = body[i], body[i + 1], body[i + 2]
        if p == prop:
            return body[i + 3:i + 3 + ln]
        i += 3 + ln
    return None


G = load_glossary()


def dec(prop, raw, field):
    return decode_frame_fields(b1([(prop, 0x00, bytes([raw]))]), "rsp_0xb1", G).get(field)


def enc(field, value, prop):
    r = build_b0_command_body({"fields": {}}, {field: value}, G)
    return tx_prop(bytes(r["body"]), prop)


# 1. breeze_away (0x42) — the LIVE field on our SKU; off=1, on=2, 0=neutral.
print("\n1. breeze_away (0x42) — prevent-straight-wind / Breeze Away (LIVE on our SKU)")
check("decode raw 1 -> off-value 1", dec(0x42, 1, "breeze_away") == {"value": 1}, f"got {dec(0x42,1,'breeze_away')}")
check("decode raw 2 -> on-value 2", dec(0x42, 2, "breeze_away") == {"value": 2}, f"got {dec(0x42,2,'breeze_away')}")
check("decode raw 0 -> neutral 0", dec(0x42, 0, "breeze_away") == {"value": 0}, f"got {dec(0x42,0,'breeze_away')}")
check("encode on(2)  -> 0x42=0x02", enc("breeze_away", 2, 0x42) == bytes([2]), f"got {enc('breeze_away',2,0x42)}")
check("encode off(1) -> 0x42=0x01", enc("breeze_away", 1, 0x42) == bytes([1]), f"got {enc('breeze_away',1,0x42)}")

# 2. breezeless (0x18) — no-wind feel; bool off=0/on=1.   [capability-tier]
print("\n2. breezeless (0x18) — no-wind / Breezeless  [capability-tier, awaiting field feedback]")
check("decode raw 0 -> False", dec(0x18, 0, "breezeless") == {"value": False})
check("decode raw 1 -> True", dec(0x18, 1, "breezeless") == {"value": True})
check("encode True  -> 0x18=0x01", enc("breezeless", True, 0x18) == bytes([1]))
check("encode False -> 0x18=0x00", enc("breezeless", False, 0x18) == bytes([0]))

# 3. wind_avoid (0x33) — Wind OFF me; bool off=0/on=1.   [capability-tier]
print("\n3. wind_avoid (0x33) — Wind OFF me  [capability-tier, awaiting field feedback]")
check("decode raw 0 -> False", dec(0x33, 0, "wind_avoid") == {"value": False})
check("decode raw 1 -> True", dec(0x33, 1, "wind_avoid") == {"value": True})
# KNOWN DIFFERENCE: the OEM Lua decode folds raw 0x02 -> on; our codec reads
# bit-0 (-> off). Documented, not asserted as correct — awaiting a unit that
# actually emits 0x02 on 0x33 to settle whether to fold it.
check("decode raw 2 -> False (codec bit-0; OEM folds 2->on — KNOWN gap, awaiting feedback)",
      dec(0x33, 2, "wind_avoid") == {"value": False})

# 4. breeze_mild (0x43) — non-consecutive enum off=0x01 / on=0x03.   [capability-tier]
print("\n4. breeze_mild (0x43) — Breeze Mild  [capability-tier, awaiting field feedback]")
check("decode raw 0x01 -> off-value 1", dec(0x43, 1, "breeze_mild") == {"value": 1})
check("decode raw 0x03 -> on-value 3", dec(0x43, 3, "breeze_mild") == {"value": 3})
check("encode on(3)  -> 0x43=0x03", enc("breeze_mild", 3, 0x43) == bytes([3]))
check("encode off(1) -> 0x43=0x01", enc("breeze_mild", 1, 0x43) == bytes([1]))

# 4b. Adjacent comfort fields (auto / cascade / directional).   [capability-tier]
print("\n4b. Adjacent comfort fields  [capability-tier, awaiting field feedback]")


def dec_t(prop, typ, data, field):
    return decode_frame_fields(b1([(prop, typ, bytes(data))]), "rsp_0xb1", G).get(field)


# auto_prevent_straight_wind (0x26,0x02) — bool off=0/on=1 (automatic deflect-up).
check("auto_prevent_straight_wind raw 0 -> False", dec_t(0x26, 0x02, [0], "auto_prevent_straight_wind") == {"value": False})
check("auto_prevent_straight_wind raw 1 -> True", dec_t(0x26, 0x02, [1], "auto_prevent_straight_wind") == {"value": True})

# wind_around / Cascade (0x59) — 2-byte composite: byte0 = on/off state,
# byte1 = up/down direction sub-mode.
check("wind_around_value byte0=0 -> 0", dec_t(0x59, 0x00, [0, 0], "wind_around_value") == {"value": 0})
check("wind_around_value byte0=1 -> 1", dec_t(0x59, 0x00, [1, 1], "wind_around_value") == {"value": 1})
check("wind_around_ud_mode byte1=1 -> upper(1)", dec_t(0x59, 0x00, [1, 1], "wind_around_ud_mode") == {"value": 1})
check("wind_around_ud_mode byte1=2 -> lower(2)", dec_t(0x59, 0x00, [1, 2], "wind_around_ud_mode") == {"value": 2})

# prevent_straight_wind_lr (0x58) — currently a bool (bit-0); the directional
# Upper(2)/Lower(3) semantics are NOT represented. KNOWN capability-tier gap,
# awaiting the directional-enum relabel + a unit that exposes 0x58.
check("prevent_straight_wind_lr raw 0 -> False", dec_t(0x58, 0x00, [0], "prevent_straight_wind_lr") == {"value": False})
check("prevent_straight_wind_lr raw 2 -> False (bit-0 model; direction lost — KNOWN gap, awaiting feedback)",
      dec_t(0x58, 0x00, [2], "prevent_straight_wind_lr") == {"value": False})

# 5. Glossary value-enum consistency with the documented characterization.
print("\n5. Glossary value-enum consistency")


def field(name):
    def f(n):
        if isinstance(n, dict):
            for k, v in n.items():
                if k == name and isinstance(v, dict) and "protocols" in v:
                    return v
                r = f(v)
                if r:
                    return r
    return f(G)


ba = field("breeze_away")
bm = field("breeze_mild")
check("breeze_away values off=1/on=2/neutral=0",
      ba["values"]["off"]["raw"] == 1 and ba["values"]["on"]["raw"] == 2
      and ba["values"]["neutral_unset"]["raw"] == 0)
check("breeze_away neutral not user-selectable",
      ba["values"]["neutral_unset"].get("user_selectable") is False)
check("breeze_mild values off=0x01/on=0x03 (non-consecutive)",
      bm["values"]["off"]["raw"] == 1 and bm["values"]["on"]["raw"] == 3)

print(f"\n{'=' * 48}\nResults: {passed} passed, {failed} failed / {passed + failed} total")
sys.exit(1 if failed else 0)
