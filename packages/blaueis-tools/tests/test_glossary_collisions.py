"""Unit tests for glossary_collisions library + a CI gate against
the live glossary.

The CI gate (``test_glossary_has_no_unexempted_collisions``) holds the
list of currently-known same-bit collisions inline. As each is fixed
during the field-by-field signoff walk, drop its entry from
KNOWN_PENDING_COLLISIONS — the test stays green only if either the
collision is gone OR it's still listed as pending.
"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from blaueis.tools.glossary_collisions import (
    SCOPE_CAPABILITY,
    SCOPE_FLAT_BODY,
    SCOPE_PROPERTY_TLV,
    build_position_catalogue,
    classify_frame,
    find_collisions,
    find_shared_bytes,
    load_allowlist,
)

GLOSSARY_PATH = (
    Path(__file__).resolve().parents[2]
    / "blaueis-core" / "src" / "blaueis" / "core" / "data" / "glossary.yaml"
)


# Currently-known same-bit collisions in the live glossary. Each entry
# is a TODO that the field-by-field signoff walk will resolve. Drop the
# entry when the underlying glossary issue is fixed; the test fails if
# a NEW collision appears OR a fix doesn't drop the entry.
KNOWN_PENDING_COLLISIONS: list[dict] = [
    {
        "scope": "rsp_0xc0",
        "byte": 9,
        "bit": 4,
        "fields": ["auxiliary_heat_level", "eco_mode"],
        "reason": (
            "Structural OEM mode-multiplex: body[9] bit 4 means PTC high bit "
            "in heat/auto, eco_mode in cool/auto/dry. ux.visible_in_modes on "
            "both fields captures the gating. Permanent — not a TODO."
        ),
    },
    {
        "scope": "rsp_0xc0",
        "byte": 10,
        "bit": 6,
        "fields": ["dust_full", "peak_elec"],
        "reason": (
            "Two distinct sensor bits both claim body[10] bit 6 — one byte/bit "
            "is wrong."
        ),
    },
]


@pytest.fixture(scope="module")
def glossary() -> dict:
    return yaml.safe_load(GLOSSARY_PATH.read_text(encoding="utf-8"))


@pytest.fixture(scope="module")
def catalogue(glossary: dict) -> list[dict]:
    return build_position_catalogue(glossary)


# ── Library-shape tests ───────────────────────────────────────────────


def test_classify_frame_known_prefixes() -> None:
    assert classify_frame("cmd_0x40") == (SCOPE_FLAT_BODY, "write")
    assert classify_frame("rsp_0xc0") == (SCOPE_FLAT_BODY, "read")
    assert classify_frame("rsp_0xb1") == (SCOPE_PROPERTY_TLV, "read")
    assert classify_frame("cmd_0xb0") == (SCOPE_PROPERTY_TLV, "write")
    assert classify_frame("rsp_0xb5_tlv") == (SCOPE_CAPABILITY, "cap")
    assert classify_frame("rsp_0xb5") == (SCOPE_CAPABILITY, "cap")


def test_classify_frame_unknown_prefix_returns_none() -> None:
    assert classify_frame("totally_unknown") is None


def test_property_tlv_scoping_isolates_records() -> None:
    """Synthetic glossary: two fields decode at offset 0 of two different
    property_id records of rsp_0xb1. They must NOT collide because each
    TLV record is its own buffer."""
    g = {
        "fields": {
            "sensor": {
                "field_a": {
                    "field_class": "sensor",
                    "feature_available": "readable",
                    "protocols": {
                        "rsp_0xb1": {
                            "decode": [{"property_id": "0xAA,0x00",
                                        "offset": 0, "bits": [7, 0]}]
                        }
                    },
                },
                "field_b": {
                    "field_class": "sensor",
                    "feature_available": "readable",
                    "protocols": {
                        "rsp_0xb1": {
                            "decode": [{"property_id": "0xBB,0x00",
                                        "offset": 0, "bits": [7, 0]}]
                        }
                    },
                },
            },
            "control": {},
        }
    }
    cat = build_position_catalogue(g)
    assert find_collisions(cat) == []
    # Two distinct scopes:
    scopes = {r["scope_key"] for r in cat}
    assert scopes == {
        "rsp_0xb1/prop=0xAA,0x00",
        "rsp_0xb1/prop=0xBB,0x00",
    }


def test_capability_scoping_isolates_cap_records() -> None:
    """Two cap fields with different cap_ids both reading offset 0 of
    rsp_0xb5_tlv must not collide."""
    g = {
        "fields": {
            "sensor": {},
            "control": {
                "cap_a": {
                    "field_class": "stateful_bool",
                    "feature_available": "capability",
                    "capability": {
                        "cap_id": "0x1E",
                        "frames": {
                            "rsp_0xb5_tlv": {
                                "decode": [{"offset": 0, "bits": [0, 0]}]
                            }
                        },
                    },
                },
                "cap_b": {
                    "field_class": "stateful_bool",
                    "feature_available": "capability",
                    "capability": {
                        "cap_id": "0x33",
                        "frames": {
                            "rsp_0xb5_tlv": {
                                "decode": [{"offset": 0, "bits": [0, 0]}]
                            }
                        },
                    },
                },
            },
        }
    }
    cat = build_position_catalogue(g)
    assert find_collisions(cat) == []


def test_flat_body_collision_detected() -> None:
    g = {
        "fields": {
            "sensor": {},
            "control": {
                "field_x": {
                    "field_class": "stateful_bool",
                    "feature_available": "always",
                    "protocols": {
                        "cmd_0x40": {
                            "decode": [{"offset": 5, "bits": [0, 0]}]
                        }
                    },
                },
                "field_y": {
                    "field_class": "stateful_bool",
                    "feature_available": "always",
                    "protocols": {
                        "cmd_0x40": {
                            "decode": [{"offset": 5, "bits": [0, 0]}]
                        }
                    },
                },
            },
        }
    }
    cat = build_position_catalogue(g)
    cols = find_collisions(cat)
    assert len(cols) == 1
    assert cols[0]["fields"] == ["field_x", "field_y"]
    assert cols[0]["byte"] == 5
    assert cols[0]["bit"] == 0


def test_logic_combiner_step_is_skipped() -> None:
    """Decode steps with `logic:` are reads, not claims — they must not
    contribute to the collision graph."""
    g = {
        "fields": {
            "sensor": {
                "alias": {
                    "field_class": "sensor",
                    "feature_available": "readable",
                    "protocols": {
                        "rsp_0xc0": {
                            "decode": [{"logic": "or",
                                        "sources": [{"offset": 1, "bits": [0, 0]}]}]
                        }
                    },
                },
                "owner": {
                    "field_class": "sensor",
                    "feature_available": "readable",
                    "protocols": {
                        "rsp_0xc0": {
                            "decode": [{"offset": 1, "bits": [0, 0]}]
                        }
                    },
                },
            },
            "control": {},
        }
    }
    cat = build_position_catalogue(g)
    assert find_collisions(cat) == []
    # Only `owner` claims the bit; `alias` produces no records.
    assert {r["field"] for r in cat} == {"owner"}


def test_shared_byte_no_bit_overlap_is_not_a_collision() -> None:
    """Different fields, same byte, different bits → no collision; reported
    by find_shared_bytes() instead."""
    g = {
        "fields": {
            "sensor": {},
            "control": {
                "fan_speed_demo": {
                    "field_class": "stateful_enum",
                    "feature_available": "always",
                    "protocols": {
                        "cmd_0x40": {
                            "decode": [{"offset": 3, "bits": [6, 0]}]
                        }
                    },
                },
                "timer_bit_demo": {
                    "field_class": "stateful_bool",
                    "feature_available": "excluded",
                    "protocols": {
                        "cmd_0x40": {
                            "decode": [{"offset": 3, "bits": [7, 7]}]
                        }
                    },
                },
            },
        }
    }
    cat = build_position_catalogue(g)
    assert find_collisions(cat) == []
    shared = find_shared_bytes(cat)
    assert len(shared) == 1
    assert shared[0]["byte"] == 3
    assert sorted(shared[0]["owners"]) == ["fan_speed_demo", "timer_bit_demo"]


def test_allowlist_match_silences_collision() -> None:
    g = {
        "fields": {
            "sensor": {},
            "control": {
                "field_x": {
                    "field_class": "stateful_bool",
                    "feature_available": "always",
                    "protocols": {
                        "cmd_0x40": {
                            "decode": [{"offset": 5, "bits": [0, 0]}]
                        }
                    },
                },
                "field_y": {
                    "field_class": "stateful_bool",
                    "feature_available": "always",
                    "protocols": {
                        "cmd_0x40": {
                            "decode": [{"offset": 5, "bits": [0, 0]}]
                        }
                    },
                },
            },
        }
    }
    cat = build_position_catalogue(g)
    allow = [
        {"scope": "cmd_0x40", "byte": 5, "bit": 0,
         "fields": ["field_x", "field_y"], "reason": "test"}
    ]
    assert find_collisions(cat, allow=allow) == []


def test_allowlist_field_set_must_match_exactly() -> None:
    """Adding a third field to a previously-pair allowlist entry must
    re-trigger the collision."""
    g = {
        "fields": {
            "sensor": {},
            "control": {
                f"field_{n}": {
                    "field_class": "stateful_bool",
                    "feature_available": "always",
                    "protocols": {
                        "cmd_0x40": {
                            "decode": [{"offset": 5, "bits": [0, 0]}]
                        }
                    },
                } for n in ("x", "y", "z")
            },
        }
    }
    cat = build_position_catalogue(g)
    allow = [
        {"scope": "cmd_0x40", "byte": 5, "bit": 0,
         "fields": ["field_x", "field_y"], "reason": "test"}
    ]
    cols = find_collisions(cat, allow=allow)
    # Three-way collision is NOT covered by the two-way allowlist entry.
    assert len(cols) == 1
    assert cols[0]["fields"] == ["field_x", "field_y", "field_z"]


def test_load_allowlist_returns_empty_for_missing_path() -> None:
    assert load_allowlist(None) == []
    assert load_allowlist("/nonexistent/path.yaml") == []


# ── CI gate against the live glossary ─────────────────────────────────


def test_glossary_has_no_unexempted_collisions(catalogue: list[dict]) -> None:
    """The live glossary must not introduce same-bit collisions beyond
    the small KNOWN_PENDING_COLLISIONS list. Drop entries from that list
    when each is resolved during the signoff walk; this test fails if a
    new collision appears OR a fix doesn't shrink the pending list."""
    cols = find_collisions(catalogue, allow=KNOWN_PENDING_COLLISIONS)
    if cols:
        lines = [
            f"  {c['class']}: {c['scope_key']} body[{c['byte']}] bit {c['bit']}: {c['fields']}"
            for c in cols
        ]
        pytest.fail(
            "New same-bit collisions detected:\n" + "\n".join(lines)
            + "\n\nEither fix the glossary or add to KNOWN_PENDING_COLLISIONS."
        )


def test_known_pending_collisions_still_present(catalogue: list[dict]) -> None:
    """The KNOWN_PENDING_COLLISIONS list must not contain stale entries.
    If a collision in the list has been fixed in the glossary (no longer
    detectable), drop the entry — leaving stale entries hides regressions
    and rots the TODO list."""
    actual = {
        (c["scope_key"], c["byte"], c["bit"], tuple(c["fields"]))
        for c in find_collisions(catalogue)
    }
    stale = []
    for entry in KNOWN_PENDING_COLLISIONS:
        key = (entry["scope"], entry["byte"], entry["bit"], tuple(sorted(entry["fields"])))
        if key not in actual:
            stale.append(entry)
    if stale:
        pytest.fail(
            "KNOWN_PENDING_COLLISIONS has stale entries (no longer in glossary):\n"
            + "\n".join(f"  {e}" for e in stale)
            + "\n\nRemove these from the list."
        )
