"""Tests for glossary_lint — focuses on the mode-subset check for truthy forces.

Rule: for every truthy force edge A → B=value,
    A.ux.visible_in_modes ⊆ B.ux.visible_in_modes

Rationale: if A is visible in mode M and forces B to a non-default truthy value,
then B must also be visible in M — otherwise enabling A in M would try to force
B truthy in a mode where B cannot exist. Falsy forces (value=0/False) are always
safe because turning a sibling off is universally valid.
"""
from __future__ import annotations

from blaueis.tools.glossary_lint import (
    build_mutex_report,
    format_mutex_report,
    lint,
)


def _field(
    *, data_type: str = "bool",
    default: int | None = 0,
    visible_in_modes: list | None = None,
    forces: dict | None = None,
    values: dict | None = None,
) -> dict:
    d: dict = {"data_type": data_type}
    if default is not None:
        d["default"] = default
    if visible_in_modes is not None:
        d["ux"] = {"visible_in_modes": visible_in_modes}
    if forces is not None:
        d["mutual_exclusion"] = {"when_on": {"forces": forces}}
        d.setdefault("ux", {}).setdefault(
            "visible_in_modes", visible_in_modes or ["cool", "heat"],
        )
    if values is not None:
        d["capability"] = {"values": values}
    return d


class TestModeSubsetTruthy:
    def test_subset_passes(self):
        """no_wind_sense (cool/heat) → breezeless=1 (all five) — real-glossary case."""
        g = {
            "no_wind_sense": _field(
                visible_in_modes=["cool", "heat"],
                forces={"breezeless": 1},
            ),
            "breezeless": _field(
                visible_in_modes=["cool", "heat", "fan_only", "dry", "auto"],
            ),
        }
        assert lint(g) == []

    def test_equal_sets_pass(self):
        g = {
            "a": _field(visible_in_modes=["cool"], forces={"b": 1}),
            "b": _field(visible_in_modes=["cool"]),
        }
        assert lint(g) == []

    def test_disjoint_fails(self):
        """jet_cool (cool) → frost_protection=1 (heat) — the motivating bad case."""
        g = {
            "jet_cool": _field(
                visible_in_modes=["cool"], forces={"frost_protection": 1},
            ),
            "frost_protection": _field(visible_in_modes=["heat"]),
        }
        errs = lint(g)
        assert any("mode subset" in e for e in errs)
        assert any("jet_cool" in e and "frost_protection" in e for e in errs)

    def test_source_superset_fails(self):
        """A visible in more modes than B — strict subset required."""
        g = {
            "a": _field(visible_in_modes=["cool", "heat"], forces={"b": 1}),
            "b": _field(visible_in_modes=["cool"]),
        }
        errs = lint(g)
        assert any("mode subset" in e and "['heat']" in e for e in errs)

    def test_source_all_modes_target_restricted_fails(self):
        """A=None (all modes) forcing B with restrictions — fails."""
        g = {
            "a": _field(
                visible_in_modes=["cool", "heat", "fan_only", "dry", "auto"],
                forces={"b": 1},
            ),
            "b": _field(visible_in_modes=["cool"]),
        }
        errs = lint(g)
        assert any("mode subset" in e for e in errs)

    def test_target_all_modes_passes(self):
        """Target has no visible_in_modes → visible everywhere → subset trivially holds."""
        g = {
            "a": _field(visible_in_modes=["cool"], forces={"b": 1}),
            "b": _field(),  # no ux.visible_in_modes
        }
        # No mode-subset error; but still must have ux on A (it does via forces helper)
        errs = [e for e in lint(g) if "mode subset" in e]
        assert errs == []


class TestFalsyForcesAreSafe:
    def test_falsy_disjoint_passes(self):
        """A (cool) forces B=0 (heat-only) — OK, off is always valid."""
        g = {
            "a": _field(visible_in_modes=["cool"], forces={"b": 0}),
            "b": _field(visible_in_modes=["heat"]),
        }
        errs = [e for e in lint(g) if "mode subset" in e]
        assert errs == []

    def test_falsy_with_false_literal(self):
        g = {
            "a": _field(visible_in_modes=["cool"], forces={"b": False}),
            "b": _field(visible_in_modes=["heat"]),
        }
        errs = [e for e in lint(g) if "mode subset" in e]
        assert errs == []


class TestEnumForces:
    def test_truthy_enum_checks_modes(self):
        """Numeric enum value != default is treated as truthy."""
        g = {
            "a": _field(
                visible_in_modes=["cool"],
                forces={"b": 2},
            ),
            "b": _field(
                data_type="uint8",
                default=0,
                visible_in_modes=["heat"],
                values={"off": {"raw": 0}, "low": {"raw": 1}, "high": {"raw": 2}},
            ),
        }
        errs = [e for e in lint(g) if "mode subset" in e]
        assert errs, f"expected mode-subset error, got: {lint(g)}"

    def test_enum_default_force_is_safe(self):
        """Forcing an enum target to its own default is a no-op, not truthy."""
        g = {
            "a": _field(
                visible_in_modes=["cool"],
                forces={"b": 0},
            ),
            "b": _field(
                data_type="uint8", default=0, visible_in_modes=["heat"],
                values={"off": {"raw": 0}, "on": {"raw": 1}},
            ),
        }
        errs = [e for e in lint(g) if "mode subset" in e]
        assert errs == []


class TestMutexReportAsymmetric:
    def test_asymmetric_detected(self):
        """A→B=0 without B→A=0 (both mex fields) — flagged."""
        g = {
            "a": _field(visible_in_modes=["cool"], forces={"b": 0, "c": 0}),
            "b": _field(visible_in_modes=["cool"], forces={"a": 0}),
            "c": _field(visible_in_modes=["cool"], forces={"a": 0}),  # placeholder so c has mex
        }
        # Break reverse: c forces a, but not b
        g["c"]["mutual_exclusion"]["when_on"]["forces"] = {"a": 0}
        # Now a→c=0 exists, c→a=0 exists (symmetric). Add asymmetry:
        g["a"]["mutual_exclusion"]["when_on"]["forces"]["c"] = 0  # already there
        # Create a clean asymmetric: remove c→a
        del g["c"]["mutual_exclusion"]["when_on"]["forces"]["a"]
        # Give c some other mex so it qualifies as a mex field
        g["c"]["mutual_exclusion"]["when_on"]["forces"]["b"] = 0

        r = build_mutex_report(g)
        asym = [(x["from"], x["to"]) for x in r["asymmetric"]]
        assert ("a", "c") in asym, asym

    def test_victim_not_flagged(self):
        """B has no mutex block (victim) — asymmetry is normal, don't flag."""
        g = {
            "a": _field(visible_in_modes=["cool"], forces={"swing": 0}),
            "swing": _field(visible_in_modes=["cool"]),  # no mutex
        }
        r = build_mutex_report(g)
        assert r["asymmetric"] == []

    def test_reciprocated_not_flagged(self):
        g = {
            "a": _field(visible_in_modes=["cool"], forces={"b": 0}),
            "b": _field(visible_in_modes=["cool"], forces={"a": 0}),
        }
        r = build_mutex_report(g)
        assert r["asymmetric"] == []

    def test_truthy_forces_ignored(self):
        """no_wind_sense→breezeless=1 is not a candidate for symmetric-off report."""
        g = {
            "nws": _field(visible_in_modes=["cool"], forces={"bl": 1}),
            "bl": _field(visible_in_modes=["cool", "heat", "fan_only", "dry", "auto"]),
        }
        r = build_mutex_report(g)
        assert r["asymmetric"] == []


class TestMutexReportSiblings:
    def test_clique_missing_edge(self):
        """Three fields pairwise excluding each other, fourth partially missing."""
        g = {
            "a": _field(forces={"x": 0, "y": 0}, visible_in_modes=["cool"]),
            "b": _field(forces={"x": 0, "y": 0}, visible_in_modes=["cool"]),
            "x": _field(forces={"a": 0, "b": 0, "y": 0}, visible_in_modes=["cool"]),
            "y": _field(forces={"a": 0, "b": 0, "x": 0}, visible_in_modes=["cool"]),
        }
        # a and b share {x, y} as common bidir neighbors but don't exclude each other.
        r = build_mutex_report(g)
        pairs = [tuple(sorted([f["a"], f["b"]])) for f in r["missing_siblings"]]
        assert ("a", "b") in pairs, pairs

    def test_low_overlap_not_flagged(self):
        """Pair shares only 1 common neighbor — below threshold."""
        g = {
            "a": _field(forces={"x": 0}, visible_in_modes=["cool"]),
            "b": _field(forces={"x": 0, "y": 0, "z": 0}, visible_in_modes=["cool"]),
            "x": _field(forces={"a": 0, "b": 0}, visible_in_modes=["cool"]),
            "y": _field(forces={"b": 0}, visible_in_modes=["cool"]),
            "z": _field(forces={"b": 0}, visible_in_modes=["cool"]),
        }
        r = build_mutex_report(g)
        assert r["missing_siblings"] == []

    def test_asymmetric_case_not_in_siblings(self):
        """If one-way edge exists, it's 'asymmetric' not 'missing_siblings'."""
        g = {
            "a": _field(forces={"b": 0, "x": 0, "y": 0}, visible_in_modes=["cool"]),
            "b": _field(forces={"x": 0, "y": 0}, visible_in_modes=["cool"]),  # no a→
            "x": _field(forces={"a": 0, "b": 0}, visible_in_modes=["cool"]),
            "y": _field(forces={"a": 0, "b": 0}, visible_in_modes=["cool"]),
        }
        r = build_mutex_report(g)
        pairs = [tuple(sorted([f["a"], f["b"]])) for f in r["missing_siblings"]]
        assert ("a", "b") not in pairs  # already reported as asymmetric


class TestMutexReportActivators:
    def test_supervisor_pattern(self):
        """A→B=1, B→A=0 — classic supervisor/worker."""
        g = {
            "super": _field(visible_in_modes=["cool"], forces={"work": 1}),
            "work": _field(
                visible_in_modes=["cool", "heat", "fan_only", "dry", "auto"],
                forces={"super": 0},
            ),
        }
        r = build_mutex_report(g)
        kinds = [(x["from"], x["to"], x["kind"]) for x in r["activators"]]
        assert ("super", "work", "supervisor") in kinds

    def test_pure_activator(self):
        """A→B=1, B has no reverse edge — pure activator."""
        g = {
            "trigger": _field(visible_in_modes=["cool"], forces={"helper": 1}),
            "helper": _field(
                visible_in_modes=["cool", "heat", "fan_only", "dry", "auto"],
            ),
        }
        r = build_mutex_report(g)
        kinds = [(x["from"], x["to"], x["kind"]) for x in r["activators"]]
        assert ("trigger", "helper", "pure_activator") in kinds

    def test_falsy_forces_not_listed(self):
        g = {
            "a": _field(forces={"b": 0}, visible_in_modes=["cool"]),
            "b": _field(forces={"a": 0}, visible_in_modes=["cool"]),
        }
        r = build_mutex_report(g)
        assert r["activators"] == []

    def test_real_glossary_supervisor(self):
        """no_wind_sense → breezeless=1 is the known supervisor pair."""
        from pathlib import Path

        from blaueis.tools.glossary_lint import load_glossary

        here = Path(__file__).resolve()
        root = next(
            p for p in here.parents if (p / "packages" / "blaueis-core").exists()
        )
        path = (
            root / "packages" / "blaueis-core" / "src" / "blaueis" / "core"
            / "data" / "glossary.yaml"
        )
        r = build_mutex_report(load_glossary(path))
        assert any(
            x["from"] == "no_wind_sense"
            and x["to"] == "breezeless"
            and x["kind"] == "supervisor"
            for x in r["activators"]
        )


class TestMutexReportFormat:
    def test_empty_report(self):
        out = format_mutex_report(
            {"asymmetric": [], "missing_siblings": [], "activators": []},
        )
        assert "No uncovered" in out

    def test_rendered_includes_activator(self):
        out = format_mutex_report({
            "asymmetric": [], "missing_siblings": [],
            "activators": [{
                "from": "nws", "to": "bl", "value": 1,
                "kind": "supervisor", "reverse": 0,
                "description": "nws auto-engages bl",
            }],
        })
        assert "[supervisor]" in out
        assert "nws" in out and "bl" in out

    def test_rendered_includes_suggestion(self):
        out = format_mutex_report({
            "asymmetric": [{"from": "a", "to": "b", "value": 0}],
            "missing_siblings": [],
        })
        assert "b.mutual_exclusion.when_on.forces.a: 0" in out

    def test_rendered_includes_overlap(self):
        out = format_mutex_report({
            "asymmetric": [],
            "missing_siblings": [
                {"a": "eco", "b": "sleep", "common": ["turbo"], "overlap": 1.0},
            ],
        })
        assert "eco" in out and "sleep" in out
        assert "turbo" in out


class TestRealGlossaryReport:
    def test_real_glossary_matches_expectations(self):
        """Baseline: the real glossary has the expected known gaps.

        Update these if the glossary changes; they document what the
        report currently tells us.
        """
        from pathlib import Path

        from blaueis.tools.glossary_lint import load_glossary

        here = Path(__file__).resolve()
        root = next(
            p for p in here.parents if (p / "packages" / "blaueis-core").exists()
        )
        path = root / "packages" / "blaueis-core" / "src" / "blaueis" / "core" / "data" / "glossary.yaml"
        r = build_mutex_report(load_glossary(path))

        asym = {(x["from"], x["to"]) for x in r["asymmetric"]}
        # Preset gap: frost→sleep exists, reverse missing
        assert ("frost_protection", "sleep_mode") in asym
        # Wind/breeze: strong_wind is "victim" side of three pairs
        assert ("breeze_mild", "strong_wind") in asym
        assert ("breezeless", "strong_wind") in asym

        sibs = {tuple(sorted([x["a"], x["b"]])) for x in r["missing_siblings"]}
        # eco and sleep are both presets but don't exclude each other
        assert ("eco_mode", "sleep_mode") in sibs


class TestRealGlossaryClean:
    """Regression: the real glossary must remain clean."""

    def test_ships_clean(self):
        from pathlib import Path

        from blaueis.tools.glossary_lint import load_glossary

        # Walk up from this test file to repo root, then to glossary.yaml.
        here = Path(__file__).resolve()
        root = next(
            p for p in here.parents if (p / "packages" / "blaueis-core").exists()
        )
        path = root / "packages" / "blaueis-core" / "src" / "blaueis" / "core" / "data" / "glossary.yaml"
        assert path.exists(), f"glossary not found at {path}"
        errs = lint(load_glossary(path))
        assert errs == [], "real glossary has lint violations:\n" + "\n".join(errs)
