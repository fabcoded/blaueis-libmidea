"""Tests for glossary_lint — focuses on the mode-subset check for truthy forces.

Rule: for every truthy force edge A → B=value,
    A.ux.visible_in_modes ⊆ B.ux.visible_in_modes

Rationale: if A is visible in mode M and forces B to a non-default truthy value,
then B must also be visible in M — otherwise enabling A in M would try to force
B truthy in a mode where B cannot exist. Falsy forces (value=0/False) are always
safe because turning a sibling off is universally valid.
"""
from __future__ import annotations

from blaueis.tools.glossary_lint import lint


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
