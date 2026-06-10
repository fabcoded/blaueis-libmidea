"""Drift guard for the canonical scan-query list.

The scan-query builder is consumed by two twins (the CLI probe/inventory
tools and the HA integration's field-inventory service). This test pins
the contract both rely on: every query builds against the bundled
glossary, every built frame is wire-valid, and the expected coverage
groups are all present — so a glossary frame-id rename or a builder
signature change fails loudly here instead of being swallowed by the
consumers' per-query error handling.
"""

from __future__ import annotations

import math

from blaueis.core.codec import load_glossary
from blaueis.core.frame import parse_frame
from blaueis.core.scan_queries import B1_PROPERTY_IDS, build_scan_queries

# Glossary-defined frames the scan must include. If one of these ids is
# renamed in the glossary, this list is the loud failure point — update
# it together with scan_queries.build_scan_queries.
EXPECTED_GLOSSARY_FRAME_IDS = [
    "cmd_0xb5_extended",
    "cmd_0xb5_simple",
    "cmd_0x41",
    "cmd_0x41_group4_power",
    "cmd_0x41_group5",
    "cmd_0x41_ext",
]


def _queries() -> list[tuple[str, bytes]]:
    return build_scan_queries(load_glossary())


def test_every_expected_glossary_frame_is_built():
    labels = {label for label, _ in _queries()}
    missing = [fid for fid in EXPECTED_GLOSSARY_FRAME_IDS if fid not in labels]
    assert not missing, (
        f"scan list lost glossary frames {missing} — frame id renamed in "
        "glossary.yaml or build_frame_from_spec rejected the spec"
    )


def test_every_frame_is_wire_valid():
    bad = []
    for label, frame in _queries():
        try:
            parsed = parse_frame(frame)
        except Exception as e:  # noqa: BLE001 — collecting all failures
            bad.append(f"{label}: {e}")
            continue
        if not parsed:
            bad.append(f"{label}: parse_frame returned falsy")
    assert not bad, "invalid scan frames:\n  " + "\n  ".join(bad)


def test_coverage_groups_present():
    labels = [label for label, _ in _queries()]
    b1_batches = [l for l in labels if l.startswith("B1_props_")]
    assert len(b1_batches) == math.ceil(len(B1_PROPERTY_IDS) / 8)
    assert "device_id_0x07" in labels
    assert sum(1 for l in labels if l.startswith("direct_subpage_")) == 2
    assert sum(1 for l in labels if l.startswith("group_0x")) >= 13
    # Total floor: 6 glossary frames + 2 subpages + batches + 0x07 + 15 groups
    assert len(labels) >= 24 + len(b1_batches)


def test_no_duplicate_labels():
    labels = [label for label, _ in _queries()]
    assert len(labels) == len(set(labels))
