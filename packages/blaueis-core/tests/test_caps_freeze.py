"""Caps are queried once at boot and then frozen.

After ``finalize_capabilities`` runs (the end of the one boot capability scan),
a later device-pushed / reconnect-replayed B5 that reports a cap as
not-supported must NOT silently demote a field confirmed at boot — the
"cap killer". It may still ESCALATE (heal) a field and refresh constraints.
``Device._process_frame`` enforces this by passing ``allow_demote=False`` into
``process_b5`` once ``meta.caps_finalized`` is set.

Anchored on cap 0x09 (``louver_swing_angle_ud_enum``, the B0 vane-angle enum),
whose unstable B5 advertisement is what made vane positions silently unsettable.
Mirrors test_b5_integrity_guard.py's build_status setup.
"""

from blaueis.core.codec import load_glossary
from blaueis.core.process import _apply_caps_to_fields, finalize_capabilities
from blaueis.core.status import build_status

UD = "louver_swing_angle_ud_enum"  # simple cap 0x09: raw 1 = supported, 0 = not


def _rec(cap_id: str, data: list[int]) -> dict:
    """A simple-cap (cap_type 0) record, matching parse_b5_tlv's shape. Only
    cap_id / cap_type / data are read by _apply_caps_to_fields."""
    return {"cap_id": cap_id, "cap_type": 0, "data": list(data)}


def _boot_promote_angle(st, g):
    """Simulate the boot B5 advertising cap 0x09 = supported (raw 1 → always)."""
    _apply_caps_to_fields(st, [_rec("0x09", [1])], g)  # allow_demote defaults True
    assert st["fields"][UD]["feature_available"] == "always"


def test_boot_demotion_still_works():
    # During the boot scan (allow_demote=True), a not-supported cap legitimately
    # excludes the field — genuine cap-gating is untouched.
    g = load_glossary()
    st = build_status(device="t", glossary=g)
    _apply_caps_to_fields(st, [_rec("0x09", [0])], g)
    assert st["fields"][UD]["feature_available"] == "excluded"
    assert st["meta"]["frame_counts"].get("cap_demotions_blocked", 0) == 0


def test_finalize_sets_caps_finalized():
    g = load_glossary()
    st = build_status(device="t", glossary=g)
    assert st["meta"]["caps_finalized"] is False
    finalize_capabilities(st, g)
    assert st["meta"]["caps_finalized"] is True


def test_post_boot_b5_cannot_demote_confirmed_field():
    g = load_glossary()
    st = build_status(device="t", glossary=g)
    _boot_promote_angle(st, g)  # boot: angle → always
    finalize_capabilities(st, g)  # caps frozen
    # Post-boot B5 says 0x09 not-supported; the ratchet (allow_demote=False)
    # must keep the boot value and record the blocked cap-killer.
    _apply_caps_to_fields(st, [_rec("0x09", [0])], g, allow_demote=False)
    assert st["fields"][UD]["feature_available"] == "always"  # NOT demoted
    assert st["meta"]["frame_counts"]["cap_demotions_blocked"] == 1


def test_post_boot_b5_can_still_escalate():
    g = load_glossary()
    st = build_status(device="t", glossary=g)
    finalize_capabilities(st, g)  # boot missed 0x09 → angle excluded
    assert st["fields"][UD]["feature_available"] == "excluded"
    # A later good B5 advertises 0x09 = supported: escalate-only heals it,
    # and nothing is counted as a blocked demotion.
    _apply_caps_to_fields(st, [_rec("0x09", [1])], g, allow_demote=False)
    assert st["fields"][UD]["feature_available"] == "always"
    assert st["meta"]["frame_counts"].get("cap_demotions_blocked", 0) == 0


if __name__ == "__main__":
    test_boot_demotion_still_works()
    test_finalize_sets_caps_finalized()
    test_post_boot_b5_cannot_demote_confirmed_field()
    test_post_boot_b5_can_still_escalate()
    print("ok")
