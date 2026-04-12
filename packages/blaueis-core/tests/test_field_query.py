#!/usr/bin/env python3
"""Tests for field_query.read_field — per-frame source priority resolver.

Validates the priority-list semantics that drive the new per-frame
storage model:

  T1 — Scope vocabulary: every protocol_* token + concrete frame keys
       resolve to the right subset of `sources`.
  T2 — Newest-wins: within a scope, the slot with the latest `ts` wins.
  T3 — Cascade fallthrough: priority list walked left-to-right, first
       non-empty scope returns its winner, later scopes ignored.
  T4 — Default priority resolution: explicit > field default > global
       ["protocol_all"].
  T5 — Disagreement listing: every slot whose value differs from the
       winner is reported, regardless of which scope matched.
  T6 — None returns: no field, no sources, or no scope matches → None.
  T7 — End-to-end: a real C0 frame populated by process_data_frame
       can be read back via read_field with the default priority.

Run: python tools-serial/tests/test_field_query.py
"""

import sys
from pathlib import Path

TOOLS = Path(__file__).resolve().parents[1]

from blaueis.core.status import build_status  # noqa: E402
from blaueis.core.query import read_field  # noqa: E402
from blaueis.core.codec import load_glossary  # noqa: E402
from blaueis.core.process import process_raw_frame  # noqa: E402

passed = failed = 0


def check(label, condition, detail=""):
    global passed, failed
    if condition:
        passed += 1
        print(f"  [PASS] {label}")
    else:
        failed += 1
        print(f"  [FAIL] {label}: {detail}")


# ── Synthetic-status helpers ──────────────────────────────────────


def _make_status(field_name="vane_position", sources=None, default_priority=None):
    """Build a minimal status dict with one field carrying the given slots."""
    return {
        "fields": {
            field_name: {
                "feature_available": "always",
                "data_type": "uint8",
                "writable": False,
                "sources": sources or {},
                "default_priority": default_priority or ["protocol_all"],
            }
        }
    }


def _slot(value, ts, generation):
    return {"value": value, "raw": value, "frame_no": 1, "ts": ts, "generation": generation}


# ── T1: Scope vocabulary ──────────────────────────────────────────


def test_scope_vocabulary():
    print("\n1. Scope vocabulary — every token resolves correctly")
    sources = {
        "rsp_0xc0": _slot(5, "2026-04-11T10:00:00Z", "legacy"),
        "rsp_0xc1_sub02": _slot(5, "2026-04-11T10:00:01Z", "legacy"),
        "rsp_0xb1": _slot(7, "2026-04-11T10:00:02Z", "legacy"),
        "rsp_0xb5": _slot(8, "2026-04-11T10:00:03Z", "new"),
        "rsp_0xa1": _slot(4, "2026-04-11T10:00:04Z", None),
    }
    status = _make_status(sources=sources)

    # protocol_all → newest of everything = rsp_0xa1 (ts ...:04Z, value 4)
    r = read_field(status, "vane_position", priority=["protocol_all"])
    check("protocol_all picks newest across all generations", r["source"] == "rsp_0xa1" and r["value"] == 4, f"got {r}")

    # protocol_legacy → newest legacy slot = rsp_0xb1 (ts ...:02Z, value 7)
    r = read_field(status, "vane_position", priority=["protocol_legacy"])
    check("protocol_legacy excludes new + unknown", r["source"] == "rsp_0xb1" and r["value"] == 7, f"got {r}")

    # protocol_new → only rsp_0xb5
    r = read_field(status, "vane_position", priority=["protocol_new"])
    check("protocol_new returns the single new-gen slot", r["source"] == "rsp_0xb5" and r["value"] == 8, f"got {r}")

    # protocol_unknown → only rsp_0xa1 (generation None)
    r = read_field(status, "vane_position", priority=["protocol_unknown"])
    check(
        "protocol_unknown returns generation=None slot only", r["source"] == "rsp_0xa1" and r["value"] == 4, f"got {r}"
    )

    # Concrete frame key → just that slot
    r = read_field(status, "vane_position", priority=["rsp_0xc0"])
    check("concrete frame key picks one slot", r["source"] == "rsp_0xc0" and r["value"] == 5, f"got {r}")

    # scope_matched annotation matches the priority entry that fired
    check("scope_matched == 'rsp_0xc0'", r["scope_matched"] == "rsp_0xc0", f"got {r['scope_matched']}")


# ── T2: Newest-wins within a scope ────────────────────────────────


def test_newest_wins():
    print("\n2. Newest-wins within a scope")
    # Three legacy slots with different ts; newest must win.
    sources = {
        "rsp_0xc0": _slot(1, "2026-04-11T10:00:00Z", "legacy"),
        "rsp_0xc1_group4": _slot(2, "2026-04-11T10:00:05Z", "legacy"),
        "rsp_0xb1": _slot(3, "2026-04-11T10:00:02Z", "legacy"),
    }
    status = _make_status(sources=sources)
    r = read_field(status, "vane_position", priority=["protocol_legacy"])
    check("newest of three legacy slots", r["source"] == "rsp_0xc1_group4" and r["value"] == 2, f"got {r}")
    check("ts of winner is the newest", r["ts"] == "2026-04-11T10:00:05Z", f"got {r['ts']}")


# ── T3: Cascade fallthrough ───────────────────────────────────────


def test_cascade_fallthrough():
    print("\n3. Cascade fallthrough — first non-empty scope wins")

    # Only legacy slots → [protocol_new, protocol_legacy] cascades through
    legacy_only = {"rsp_0xc0": _slot(5, "2026-04-11T10:00:00Z", "legacy")}
    r = read_field(_make_status(sources=legacy_only), "vane_position", priority=["protocol_new", "protocol_legacy"])
    check("cascade falls through to protocol_legacy", r["source"] == "rsp_0xc0" and r["value"] == 5, f"got {r}")
    check(
        "scope_matched records the second cascade entry",
        r["scope_matched"] == "protocol_legacy",
        f"got {r['scope_matched']}",
    )

    # Both gens populated → first scope wins (no fallthrough needed)
    both = {
        "rsp_0xc0": _slot(5, "2026-04-11T10:00:00Z", "legacy"),
        "rsp_0xb5": _slot(8, "2026-04-11T10:00:01Z", "new"),
    }
    r = read_field(_make_status(sources=both), "vane_position", priority=["protocol_new", "protocol_legacy"])
    check("first scope wins when populated", r["source"] == "rsp_0xb5" and r["value"] == 8, f"got {r}")
    check(
        "scope_matched records the first cascade entry",
        r["scope_matched"] == "protocol_new",
        f"got {r['scope_matched']}",
    )

    # User example: latest, protocol_new, rsp_0xa1 → fall back to a1 if no new
    sources = {
        "rsp_0xc0": _slot(5, "2026-04-11T10:00:00Z", "legacy"),
        "rsp_0xa1": _slot(4, "2026-04-11T10:00:01Z", None),
    }
    r = read_field(_make_status(sources=sources), "vane_position", priority=["protocol_new", "rsp_0xa1"])
    check("user example: fall through new → a1", r["source"] == "rsp_0xa1" and r["value"] == 4, f"got {r}")

    # Cascade exhausts entirely → None
    r = read_field(
        _make_status(sources={"rsp_0xa1": _slot(4, "2026-04-11T10:00:00Z", None)}),
        "vane_position",
        priority=["protocol_new", "protocol_legacy"],
    )
    check("cascade exhausts → None", r is None, f"got {r}")


# ── T4: Default priority resolution ───────────────────────────────


def test_default_priority_resolution():
    print("\n4. Default priority resolution — explicit > field > global")
    sources = {
        "rsp_0xc0": _slot(5, "2026-04-11T10:00:00Z", "legacy"),
        "rsp_0xb5": _slot(8, "2026-04-11T10:00:01Z", "new"),
    }

    # No field default, no explicit → falls back to global ["protocol_all"]
    s = _make_status(sources=sources, default_priority=["protocol_all"])
    r = read_field(s, "vane_position")
    check("global default ['protocol_all'] picks newest", r["source"] == "rsp_0xb5", f"got {r}")

    # Field default = ['protocol_legacy'] → returns legacy slot
    s = _make_status(sources=sources, default_priority=["protocol_legacy"])
    r = read_field(s, "vane_position")
    check("field default_priority = legacy → legacy wins", r["source"] == "rsp_0xc0", f"got {r}")
    check("scope_matched honours field default", r["scope_matched"] == "protocol_legacy", f"got {r['scope_matched']}")

    # Explicit priority overrides field default
    r = read_field(s, "vane_position", priority=["protocol_new"])
    check("explicit priority overrides field default", r["source"] == "rsp_0xb5", f"got {r}")


# ── T5: Disagreement listing ──────────────────────────────────────


def test_disagreement_listing():
    print("\n5. Disagreement listing — full sources dict, not just matched scope")
    sources = {
        "rsp_0xc0": _slot(5, "2026-04-11T10:00:00Z", "legacy"),
        "rsp_0xc1_sub02": _slot(5, "2026-04-11T10:00:01Z", "legacy"),  # agrees
        "rsp_0xb5": _slot(7, "2026-04-11T10:00:02Z", "new"),  # disagrees
        "rsp_0xa1": _slot(4, "2026-04-11T10:00:03Z", None),  # disagrees
    }
    status = _make_status(sources=sources)

    # Read with protocol_legacy → winner is rsp_0xc1_sub02 (newest legacy, value 5)
    r = read_field(status, "vane_position", priority=["protocol_legacy"])
    check("legacy winner picked", r["source"] == "rsp_0xc1_sub02" and r["value"] == 5, f"got {r}")

    # Disagreements span ALL slots, not just legacy ones
    disagreement_slots = {d["slot"] for d in r["disagreements"]}
    check(
        "disagreements include rsp_0xb5 (new gen, different value)",
        "rsp_0xb5" in disagreement_slots,
        f"got {disagreement_slots}",
    )
    check(
        "disagreements include rsp_0xa1 (unknown gen, different value)",
        "rsp_0xa1" in disagreement_slots,
        f"got {disagreement_slots}",
    )
    check(
        "rsp_0xc0 NOT in disagreements (value matches winner)",
        "rsp_0xc0" not in disagreement_slots,
        f"got {disagreement_slots}",
    )

    # All-agree case → empty disagreements
    sources_agree = {
        "rsp_0xc0": _slot(5, "2026-04-11T10:00:00Z", "legacy"),
        "rsp_0xb5": _slot(5, "2026-04-11T10:00:01Z", "new"),
    }
    r = read_field(_make_status(sources=sources_agree), "vane_position", priority=["protocol_all"])
    check("all-agree → empty disagreements list", r["disagreements"] == [], f"got {r['disagreements']}")


# ── T6: None returns ──────────────────────────────────────────────


def test_none_returns():
    print("\n6. None-return paths")

    # Field doesn't exist → None
    s = _make_status(sources={})
    check("missing field → None", read_field(s, "ghost") is None, "")

    # Field exists but sources empty → None
    check("empty sources → None", read_field(s, "vane_position") is None, "")

    # No scope matches → None
    sources = {"rsp_0xc0": _slot(5, "2026-04-11T10:00:00Z", "legacy")}
    s = _make_status(sources=sources)
    r = read_field(s, "vane_position", priority=["protocol_new"])
    check("strict protocol_new with no new slots → None", r is None, f"got {r}")

    # Slots without ts are skipped
    sources = {"rsp_0xc0": {"value": 5, "ts": None, "generation": "legacy"}}
    s = _make_status(sources=sources)
    r = read_field(s, "vane_position", priority=["protocol_legacy"])
    check("slot without ts is skipped → None", r is None, f"got {r}")


# ── T7: End-to-end with the live decoder ──────────────────────────


def test_end_to_end_with_real_frame():
    print("\n7. End-to-end — real C0 frame populated by process_data_frame")
    glossary = load_glossary()
    status = build_status("test", glossary)

    import yaml

    fixture_path = Path(__file__).parent / "test-cases" / "xtremesaveblue_s11_frames" / "c0_frames.yaml"
    with open(fixture_path) as f:
        fixture = yaml.safe_load(f)
    body = bytes.fromhex(fixture["frames"][0]["body_hex"].replace(" ", ""))
    process_raw_frame(status, body, glossary, timestamp="2026-04-11T12:00:00Z")

    # target_temperature should resolve via the default ['protocol_all'] priority
    r = read_field(status, "target_temperature")
    check("target_temperature read after C0 frame", r is not None, "got None")
    check("source is rsp_0xc0", r["source"] == "rsp_0xc0", f"got {r['source']}")
    check("generation annotation = legacy", r["generation"] == "legacy", f"got {r['generation']}")
    check(
        "scope_matched = protocol_all (global default)",
        r["scope_matched"] == "protocol_all",
        f"got {r['scope_matched']}",
    )
    check("ts roundtrips", r["ts"] == "2026-04-11T12:00:00Z", f"got {r['ts']}")
    check("disagreements empty (only one slot)", r["disagreements"] == [], f"got {r['disagreements']}")


def main():
    test_scope_vocabulary()
    test_newest_wins()
    test_cascade_fallthrough()
    test_default_priority_resolution()
    test_disagreement_listing()
    test_none_returns()
    test_end_to_end_with_real_frame()

    total = passed + failed
    print(f"\n{'=' * 60}")
    print(f"Results: {passed} passed, {failed} failed / {total} total")
    return 0 if failed == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
