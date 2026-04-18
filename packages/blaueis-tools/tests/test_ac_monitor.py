"""Tests for ac_monitor.py pure functions — identify_body, save_status, build_query_table.

Usage:  python -m pytest packages/blaueis-tools/tests/test_ac_monitor.py -v
"""

import json
import tempfile
from pathlib import Path

from blaueis.core.codec import load_glossary
from blaueis.core.status import build_status
from blaueis.tools.ac_monitor import build_query_table, identify_body, save_status

# ── identify_body ───────────────────────────────────────────────────────


def test_identify_c0():
    assert identify_body(b"\xc0\x00\x00") == "rsp_0xc0"


def test_identify_a0_as_c0():
    assert identify_body(b"\xa0\x00\x00") == "rsp_0xc0"


def test_identify_c1_group4():
    # body[3] = 0x44 → group 4
    assert identify_body(b"\xc1\x00\x00\x44") == "rsp_0xc1_group4"


def test_identify_c1_group5():
    assert identify_body(b"\xc1\x00\x00\x45") == "rsp_0xc1_group5"


def test_identify_b5():
    assert identify_body(b"\xb5\x00") == "rsp_0xb5"


def test_identify_b1():
    assert identify_body(b"\xb1\x00") == "rsp_0xb1"


def test_identify_a1():
    assert identify_body(b"\xa1\x00") == "rsp_0xa1"


def test_identify_empty():
    assert identify_body(b"") is None


def test_identify_unknown():
    assert identify_body(b"\xff\x00") is None


# ── save_status ─────────────────────────────────────────────────────────


def test_save_status_round_trip():
    status = {"fields": {"power": {"value": True}}, "caps": {}}
    with tempfile.NamedTemporaryFile(mode="w", suffix=".json", delete=False) as f:
        path = Path(f.name)
    save_status(status, path)
    loaded = json.loads(path.read_text())
    assert loaded == status


def test_save_status_valid_json():
    status = {"test": 123, "nested": {"a": [1, 2, 3]}}
    with tempfile.NamedTemporaryFile(mode="w", suffix=".json", delete=False) as f:
        path = Path(f.name)
    save_status(status, path)
    # Should not raise
    json.loads(path.read_text())


# ── build_query_table ───────────────────────────────────────────────────


def test_build_query_table_has_rsp_keys():
    glossary = load_glossary()
    status = build_status(glossary)
    table = build_query_table(status, glossary)
    assert isinstance(table, dict)
    for key in table:
        assert key.startswith("rsp_"), f"key {key} doesn't start with rsp_"


def test_build_query_table_filters_never():
    glossary = load_glossary()
    status = build_status(glossary)
    # Mark a field as never-available
    for name in list(status["fields"].keys())[:3]:
        status["fields"][name]["feature_available"] = "never"
    table = build_query_table(status, glossary)
    # Those fields should not appear in any table values
    never_fields = [n for n in list(status["fields"].keys())[:3]]
    for fields_list in table.values():
        for nf in never_fields:
            assert nf not in fields_list, f"never-field {nf} should be filtered"
