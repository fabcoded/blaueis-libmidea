"""Glossary lint: validate ux.visible_in_modes + mutual_exclusion.when_on.forces.

Checks per field:
- every `forces` key references an existing top-level field
- every forced value is either the target's default or a legal enum/numeric value
- every field with a mutex block also declares ux.visible_in_modes (so overlay
  can resolve mode-gating)
- visible_in_modes entries come from the accepted HVAC mode vocabulary
- no conflicting non-zero forces between two fields (A→B=x AND B→A=y where
  both x,y are non-default is a mutual-hard-on cycle)

Exit 0 = clean, 1 = violations printed.
"""
from __future__ import annotations

import sys
from pathlib import Path

try:
    import yaml
except ImportError:
    print("glossary_lint: PyYAML required", file=sys.stderr)
    sys.exit(2)


VALID_MODES = {"cool", "heat", "fan_only", "dry", "auto"}


def load_glossary(path: Path) -> dict:
    """Flatten fields.{group}.{name} → {name: def} for linting."""
    with path.open() as f:
        doc = yaml.safe_load(f)
    fields = doc.get("fields") or {}
    flat: dict = {}
    for group, group_fields in fields.items():
        if not isinstance(group_fields, dict):
            continue
        for name, fdef in group_fields.items():
            if name in flat:
                flat[f"__dup__{group}_{name}"] = fdef
            else:
                flat[name] = fdef
    return flat


def field_default(fdef: dict) -> int | bool | None:
    enc = (fdef.get("codec") or {}).get("encoding") or {}
    if "default" in fdef:
        return fdef["default"]
    if "default" in enc:
        return enc["default"]
    dt = fdef.get("data_type")
    if dt == "bool":
        return 0
    if dt in ("uint8", "uint16", "int8", "int16"):
        return 0
    return None


def field_value_domain(fdef: dict) -> set | None:
    """Set of legal raw values, or None if open-ended numeric."""
    cap = fdef.get("capability") or {}
    vals = cap.get("values") or {}
    domain = set()
    for vdef in vals.values():
        if isinstance(vdef, dict) and "raw" in vdef:
            domain.add(vdef["raw"])
    if domain:
        return domain
    enc = (fdef.get("codec") or {}).get("encoding") or {}
    enc_vals = enc.get("values") or {}
    for vdef in enc_vals.values():
        if isinstance(vdef, dict) and "raw" in vdef:
            domain.add(vdef["raw"])
    return domain or None


def lint(glossary: dict) -> list[str]:
    errors: list[str] = []
    names = set(glossary.keys())

    # Index: (field, forced_value) pairs for cycle detection
    mex_edges: dict[str, dict[str, object]] = {}

    for name, fdef in glossary.items():
        if not isinstance(fdef, dict):
            continue

        ux = fdef.get("ux") or {}
        visible_modes = ux.get("visible_in_modes")
        if visible_modes is not None:
            if not isinstance(visible_modes, list):
                errors.append(f"{name}: ux.visible_in_modes must be a list")
            else:
                for m in visible_modes:
                    if m not in VALID_MODES:
                        errors.append(
                            f"{name}: ux.visible_in_modes contains unknown "
                            f"mode '{m}' (valid: {sorted(VALID_MODES)})"
                        )

        mex = fdef.get("mutual_exclusion") or {}
        when_on = mex.get("when_on") or {}
        forces = when_on.get("forces") or {}
        if not forces:
            continue

        mex_edges[name] = dict(forces)

        if visible_modes is None:
            errors.append(
                f"{name}: has mutual_exclusion but no ux.visible_in_modes "
                f"(overlay cannot resolve mode gating)"
            )

        for target, forced_value in forces.items():
            if target not in names:
                errors.append(
                    f"{name}: forces '{target}' which is not a defined field"
                )
                continue
            target_def = glossary[target]
            domain = field_value_domain(target_def)
            default = field_default(target_def)
            if target_def.get("data_type") == "bool":
                if forced_value not in (0, 1, True, False):
                    errors.append(
                        f"{name}: forces {target}={forced_value!r} but target "
                        f"is bool (expected 0/1)"
                    )
            elif domain and forced_value not in domain and forced_value != default:
                errors.append(
                    f"{name}: forces {target}={forced_value!r} not in domain "
                    f"{sorted(domain)} and not the default {default!r}"
                )

    # Cycle: A→B=x (x≠default_B) AND B→A=y (y≠default_A)
    for a, a_forces in mex_edges.items():
        for b, forced_b in a_forces.items():
            if b not in mex_edges:
                continue
            b_forces = mex_edges[b]
            if a not in b_forces:
                continue
            forced_a = b_forces[a]
            b_default = field_default(glossary.get(b, {}))
            a_default = field_default(glossary.get(a, {}))
            if forced_b != b_default and forced_a != a_default:
                errors.append(
                    f"mutex cycle: {a} forces {b}={forced_b} AND "
                    f"{b} forces {a}={forced_a} (both non-default)"
                )

    return errors


def main() -> int:
    if len(sys.argv) > 1:
        path = Path(sys.argv[1])
    else:
        here = Path(__file__).resolve()
        candidates = [
            here.parents[4] / "blaueis-core" / "src" / "blaueis" / "core" / "data" / "glossary.yaml",
            Path("/workspaces/hvac-shark-dev/blaueis-ha-midea/custom_components/blaueis_midea/lib/blaueis/core/data/glossary.yaml"),
        ]
        path = next((p for p in candidates if p.exists()), candidates[0])

    if not path.exists():
        print(f"glossary_lint: file not found: {path}", file=sys.stderr)
        return 2

    glossary = load_glossary(path)
    errors = lint(glossary)

    if errors:
        for e in errors:
            print(e)
        print(f"\n{len(errors)} violation(s) in {path}")
        return 1

    mex_count = sum(
        1 for f in glossary.values()
        if isinstance(f, dict) and (f.get("mutual_exclusion") or {}).get("when_on", {}).get("forces")
    )
    ux_count = sum(
        1 for f in glossary.values()
        if isinstance(f, dict) and (f.get("ux") or {}).get("visible_in_modes")
    )
    print(f"clean: {len(glossary)} fields, {ux_count} with ux, {mex_count} with mex — {path.name}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
