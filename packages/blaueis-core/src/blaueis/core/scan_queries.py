"""Canonical scan-query builders for field-inventory / probe runs.

Single source of truth for the queries an inventory scan injects: the
frame builders for queries not modeled in the glossary, the known B1
property-ID table, and :func:`build_scan_queries`, which assembles the
full (label, frame_bytes) list against a loaded glossary. Both the CLI
probe tooling and the HA integration's field-inventory service consume
this module, so query coverage cannot drift between them.
"""

from __future__ import annotations

import logging

from .frame import build_frame

log = logging.getLogger(__name__)

# ── Frame builders for queries not in the glossary ──────────────────────


def build_direct_subpage_query(subpage: int, appliance: int = 0xAC, proto: int = 0) -> bytes:
    """14-byte direct C1 sub-page query (§3.1.4.4).

    body[0]=0x41, body[1]=subpage (0x01 or 0x02). Hypothesis.
    """
    body = bytes([0x41, subpage & 0xFF])
    return build_frame(body=body, msg_type=0x03, appliance=appliance, proto=proto)


def build_optcommand_query(opt_cmd: int, query_stat: int = 0x00, appliance: int = 0xAC, proto: int = 0) -> bytes:
    """24-byte optCommand query (§3.1.4.3).

    body[0]=0x41, body[1]=0x21, body[4]=optCommand, body[5]=0xFF,
    body[7]=queryStat.
    """
    body = bytearray(24)
    body[0] = 0x41
    body[1] = 0x21
    body[4] = opt_cmd & 0xFF
    body[5] = 0xFF
    body[7] = query_stat & 0xFF
    return build_frame(body=bytes(body), msg_type=0x03, appliance=appliance, proto=proto)


def build_group_query_raw(page: int, variant: int = 0x81, appliance: int = 0xAC, proto: int = 0) -> bytes:
    """Generic group page query — allows arbitrary page + variant byte."""
    body = bytearray(24)
    body[0] = 0x41
    body[1] = variant
    body[2] = 0x01
    body[3] = page & 0xFF
    return build_frame(body=bytes(body), msg_type=0x03, appliance=appliance, proto=proto)


def build_device_id_query(appliance: int = 0xAC, proto: int = 0) -> bytes:
    """msg_type=0x07 device ID / SN query (§5.6)."""
    return build_frame(body=bytes([0x00]), msg_type=0x07, appliance=appliance, proto=proto)


def build_b1_property_query(prop_ids: list[tuple[int, int]], appliance: int = 0xAC, proto: int = 0) -> bytes:
    """B1 property query — body[0]=0xB1, body[1]=count, then (lo, hi) pairs."""
    body = bytearray()
    body.append(0xB1)
    body.append(len(prop_ids))
    for lo, hi in prop_ids:
        body.append(lo & 0xFF)
        body.append(hi & 0xFF)
    return build_frame(body=bytes(body), msg_type=0x03, appliance=appliance, proto=proto)


# ── Known B1 property IDs to probe ─────────────────────────────────────

# All known B0/B1 property IDs from community protocol research (§3.5),
# organised by tranche so the response analysis is easy to read.
# Format: (lo, hi, label). The probe iterates this list in order and
# bundles BATCH (8) IDs per request frame.
#
# A device that does not implement a property typically replies with
# data_len=0 for that prop_id rather than dropping it. The interesting
# signal in the response is therefore not "any reply" but "data_len > 0
# AND data bytes look plausible".
B1_PROPERTY_IDS = [
    # ── Pre-existing properties (probed in previous sessions) ─────────
    (0x15, 0x00, "indoor_humidity"),
    (0x3F, 0x00, "error_code_query"),
    (0x41, 0x00, "mode_query"),
    (0x1A, 0x00, "tone_buzzer"),
    (0x18, 0x00, "no_wind_sense"),
    (0x32, 0x00, "wind_straight_avoid"),
    (0x39, 0x00, "self_clean"),
    (0x42, 0x00, "prevent_straight_wind"),
    (0x48, 0x00, "rate_select"),
    (0x09, 0x00, "wind_swing_ud_angle"),
    (0x0A, 0x00, "wind_swing_lr_angle"),
    (0x0B, 0x02, "pm25_value"),
    (0x28, 0x02, "operating_time"),
    (0x91, 0x00, "has_icheck"),
    (0x4B, 0x00, "fresh_air"),
    (0xAD, 0x00, "comfort"),
    (0xE3, 0x00, "ieco_switch"),
    (0x47, 0x00, "high_temperature_monitor"),
    # ── Tier 1 bool / uint8 properties (bulk add 2026-04-11) ──────────
    (0x21, 0x00, "cool_hot_sense"),
    (0x26, 0x02, "auto_prevent_straight_wind"),
    (0x34, 0x00, "intelligent_wind"),
    (0x3A, 0x00, "child_prevent_cold_wind"),
    (0x1B, 0x02, "little_angel"),
    (0x29, 0x00, "security"),
    (0x31, 0x00, "intelligent_control"),
    (0x44, 0x00, "face_register"),
    (0x4E, 0x00, "even_wind"),
    (0x4F, 0x00, "single_tuyere"),
    (0x58, 0x00, "prevent_straight_wind_lr"),
    (0x98, 0x00, "cvp"),
    (0xAA, 0x00, "new_wind_sense"),
    (0x01, 0x02, "pre_cool_hot"),
    (0x34, 0x02, "body_check"),
    # ── Tier 2 mito_*_temp (temp_offset50_half) ───────────────────────
    (0x8D, 0x00, "mito_cool_temp"),
    (0x8E, 0x00, "mito_heat_temp"),
    # ── Tier 3 2-byte composites ──────────────────────────────────────
    (0x4C, 0x00, "extreme_wind"),
    (0x59, 0x00, "wind_around"),
    (0x8F, 0x00, "dr_time"),
    (0x27, 0x02, "remote_control_lock"),
    # ── Tier 4 multi-byte numerics ────────────────────────────────────
    (0x49, 0x00, "prevent_super_cool"),
    # ── Tier 5 deferred properties (composite/string-shaped) ──────────
    (0x09, 0x04, "filter_level"),
    (0x20, 0x00, "voice_control"),
    (0x24, 0x00, "volume_control"),
    (0x90, 0x00, "cool_heat_amount"),
    (0xE0, 0x00, "ieco_frame"),
    (0xAB, 0x00, "indoor_unit_code"),
    (0xAC, 0x00, "outdoor_unit_code"),
    (0x51, 0x00, "parent_control"),
    (0x25, 0x02, "temperature_ranges"),
    (0x30, 0x02, "main_horizontal_guide_strip"),
    (0x31, 0x02, "sup_horizontal_guide_strip"),
]


def build_scan_queries(glossary: dict, proto: int = 0) -> list[tuple[str, bytes]]:
    """Assemble the full scan query list: (label, frame_bytes) pairs.

    Coverage: the glossary-defined UART query frames, the two C1 direct
    sub-pages, all known B1 property IDs in batches of 8, the 0x07
    device-ID query, and the raw group-page sweep.
    """
    from .codec import build_frame_from_spec

    queries: list[tuple[str, bytes]] = []

    # Glossary-defined frames
    for fid in [
        "cmd_0xb5_extended",
        "cmd_0xb5_simple",
        "cmd_0x41",
        "cmd_0x41_group4_power",
        "cmd_0x41_group5",
        "cmd_0x41_ext",
    ]:
        spec = glossary.get("frames", {}).get(fid)
        if not spec:
            log.debug("glossary frame %s not present; skipped", fid)
            continue
        bus = spec.get("bus", ["uart", "rt"])
        if "uart" not in bus:
            continue
        try:
            frame = build_frame_from_spec(fid, glossary, proto=proto)
            queries.append((fid, frame))
        except Exception as e:
            log.debug("skip glossary frame %s: %s", fid, e)

    for sp in [0x01, 0x02]:
        queries.append((f"direct_subpage_0x{sp:02X}", build_direct_subpage_query(sp, proto=proto)))

    BATCH = 8
    for i in range(0, len(B1_PROPERTY_IDS), BATCH):
        batch = B1_PROPERTY_IDS[i : i + BATCH]
        ids = [(lo, hi) for lo, hi, _ in batch]
        labels = [lbl for _, _, lbl in batch]
        queries.append((f"B1_props_{'+'.join(labels)}", build_b1_property_query(ids, proto=proto)))

    queries.append(("device_id_0x07", build_device_id_query(proto=proto)))

    for page in [0x40, 0x42, 0x43, 0x46, 0x47, 0x48, 0x49, 0x4A, 0x4B, 0x4C, 0x4D, 0x4E, 0x4F]:
        queries.append((f"group_0x{page:02X}_v21", build_group_query_raw(page, variant=0x21, proto=proto)))

    for page in [0x41, 0x43]:
        queries.append(
            (
                f"group_0x{page:02X}_v21_rt_test",
                build_group_query_raw(page, variant=0x21, proto=proto),
            )
        )

    return queries
