"""filter_clean_due decode + filter_clean_reset encode safety + cap-gating.

Covers the relocation of the indoor filter clean reminder to
``rsp_0xc0 body[13] bit5`` and the new ``filter_clean_reset`` trigger
(SET ``body[10] bit7``). The load-bearing property: pressing reset must
preserve the body[10] sibling controls (sleep / turbo / temperature_unit
/ catch_cold / night_light) — the builder seeds them from status, so a
reset frame does not clobber live state.
"""

from __future__ import annotations

from blaueis.core.codec import decode_frame_fields, load_glossary, walk_fields
from blaueis.core.command import build_command_body
from blaueis.core.status import build_status

GLOSSARY = load_glossary()


def _c0_body(b13: int = 0, b10: int = 0) -> bytes:
    body = bytearray(32)
    body[0] = 0xC0
    body[10] = b10
    body[13] = b13
    return bytes(body)


# ── decode: filter_clean_due reads body[13] bit5 ────────────────────


def test_filter_clean_due_decodes_body13_bit5():
    assert decode_frame_fields(_c0_body(b13=0x20), "rsp_0xc0", GLOSSARY)["filter_clean_due"]["value"] is True
    assert decode_frame_fields(_c0_body(b13=0x00), "rsp_0xc0", GLOSSARY)["filter_clean_due"]["value"] is False


def test_filter_clean_due_ignores_old_body10_bit6():
    # the prior (wrong) position must no longer drive the flag
    assert decode_frame_fields(_c0_body(b10=0x40), "rsp_0xc0", GLOSSARY)["filter_clean_due"]["value"] is False


# ── encode: reset sets body[10] bit7, preserves siblings ────────────


def _status_with_body10(**siblings) -> dict:
    status = build_status(device="test", glossary=GLOSSARY)
    for name, val in siblings.items():
        if name in status["fields"]:
            status["fields"][name].setdefault("sources", {})["rsp_0xc0"] = {
                "value": val,
                "raw": int(val),
                "frame_no": 1,
                "ts": "2026-05-31T12:00:00Z",
                "generation": "legacy",
            }
    status["meta"]["phase"] = "steady_state"
    return status


def test_reset_sets_bit7_and_preserves_live_siblings():
    status = _status_with_body10(
        sleep_mode=True,
        turbo_mode=True,
        temperature_unit=False,
        catch_cold=False,
        night_light=True,
    )
    body = build_command_body(status, {"filter_clean_reset": True}, GLOSSARY, skip_preflight=True)["body"]
    assert body[10] & 0x80, "reset bit7 must be set"
    assert body[10] & 0x01, "sleep_mode (bit0) preserved"
    assert body[10] & 0x02, "turbo_mode (bit1) preserved"
    assert body[10] & 0x10, "night_light (bit4) preserved"
    assert not (body[10] & 0x04), "temperature_unit (bit2) stays clear"


def test_normal_set_does_not_fire_reset():
    status = _status_with_body10(sleep_mode=True)
    body = build_command_body(status, {"sleep_mode": False}, GLOSSARY, skip_preflight=True)["body"]
    assert not (body[10] & 0x80), "bit7 must stay clear on a non-reset SET"


# ── capability gating: both fields gate on FILTER_REMIND 0x0217 ─────


def test_filter_clean_fields_capability_gated_on_filter_remind():
    fields = walk_fields(GLOSSARY)
    for name in ("filter_clean_due", "filter_clean_reset"):
        f = fields[name]
        assert f["feature_available"] == "capability", name
        assert f["capability"]["cap_id_16"] == "0x0217", name
