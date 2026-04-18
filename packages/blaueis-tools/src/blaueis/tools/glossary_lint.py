"""Glossary lint: validate ux.visible_in_modes + mutual_exclusion.when_on.forces.

Checks per field:
- every `forces` key references an existing top-level field
- every forced value is either the target's default or a legal enum/numeric value
- every field with a mutex block also declares ux.visible_in_modes (so overlay
  can resolve mode-gating)
- visible_in_modes entries come from the accepted HVAC mode vocabulary
- no conflicting non-zero forces between two fields (A→B=x AND B→A=y where
  both x,y are non-default is a mutual-hard-on cycle)
- truthy forces satisfy the mode subset rule: if A forces B to a non-default
  truthy value, every mode A is visible in must also be a mode B is visible in
  (otherwise turning A on in a mode where B is mode-hidden would try to force
  B truthy in a mode it cannot occupy)

Exit 0 = clean, 1 = violations printed.
"""
from __future__ import annotations

import sys
from collections import defaultdict
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


def _is_truthy_force(forced_value: object, target_default: object) -> bool:
    """Force is truthy if it's bool-True / numeric-nonzero AND not the target's default."""
    if forced_value == target_default:
        return False
    if isinstance(forced_value, bool):
        return forced_value
    if isinstance(forced_value, int):
        return forced_value != 0
    return bool(forced_value)


def _mode_subset_error(
    source: str,
    source_visible: list | None,
    target: str,
    target_visible: list | None,
    forced_value: object,
) -> str | None:
    """Return an error string if source.visible_in_modes ⊄ target.visible_in_modes.

    None means "visible everywhere"; an empty list would mean "nowhere" (treated
    as a trivially-satisfied subset).
    """
    if target_visible is None:
        return None
    if source_visible is None:
        return (
            f"{source}: truthy force {target}={forced_value!r} fails mode subset — "
            f"{source} is visible in all modes but {target} only in "
            f"{sorted(set(target_visible))}"
        )
    if not isinstance(source_visible, list) or not isinstance(target_visible, list):
        return None
    a_set = set(source_visible)
    b_set = set(target_visible)
    if a_set.issubset(b_set):
        return None
    missing = a_set - b_set
    return (
        f"{source}: truthy force {target}={forced_value!r} fails mode subset — "
        f"{target} is not visible in {sorted(missing)} (where {source} is)"
    )


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

            # Mode-subset check for truthy forces.
            # A falsy force (=0/False) is always safe — turning a sibling off
            # is universally valid. A truthy force injects a value that must
            # itself be valid in every mode where A can fire.
            if _is_truthy_force(forced_value, default):
                target_visible = (target_def.get("ux") or {}).get("visible_in_modes")
                subset_error = _mode_subset_error(
                    name, visible_modes, target, target_visible, forced_value,
                )
                if subset_error:
                    errors.append(subset_error)

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


def _forces_of(fdef: dict) -> dict:
    return ((fdef.get("mutual_exclusion") or {}).get("when_on") or {}).get("forces") or {}


def _is_falsy_force(v: object) -> bool:
    return v == 0 or v is False


def build_mutex_report(glossary: dict) -> dict:
    """Heuristic completeness report for the mutex graph.

    Three findings, all informational — not build-breaking:

    - ``asymmetric``: A→B=0 exists but B→A=0 is missing, and B has a mutex
      block of its own (so it's not a pure "victim" field like swing_vertical).

    - ``missing_siblings``: pairs (A, B) with no edge in either direction whose
      bidirectional-neighbor sets overlap heavily — they look like siblings in
      the same exclusion clique that forgot to point at each other.

    - ``activators``: truthy force edges (A→B=v with v truthy & non-default).
      Classified by what the reverse edge does: supervisor (B→A=0 disables the
      supervisor), pure_activator (no reverse edge), mutual_activator (B→A also
      truthy — redundant with cycle lint but surfaced here too).
    """
    mex_fields = {}
    for name, fdef in glossary.items():
        if isinstance(fdef, dict) and _forces_of(fdef):
            mex_fields[name] = _forces_of(fdef)

    bidir: dict[str, set[str]] = defaultdict(set)
    for a, fa in mex_fields.items():
        for b, v in fa.items():
            if not _is_falsy_force(v):
                continue
            if b not in mex_fields:
                continue
            if _is_falsy_force(mex_fields[b].get(a, "MISS")):
                bidir[a].add(b)
                bidir[b].add(a)

    asymmetric = []
    for a, fa in mex_fields.items():
        for b, v in fa.items():
            if not _is_falsy_force(v):
                continue
            if b not in mex_fields:
                continue  # victim field — no mutex block, cannot reciprocate
            if a not in mex_fields[b]:
                asymmetric.append({"from": a, "to": b, "value": v})

    missing_siblings = []
    names = sorted(mex_fields)
    seen = set()
    for a in names:
        for b in names:
            if a >= b or (a, b) in seen:
                continue
            seen.add((a, b))
            if b in bidir[a]:
                continue  # already bidirectionally connected
            if a in mex_fields[b] or b in mex_fields[a]:
                continue  # asymmetric case — reported separately
            na, nb = bidir[a], bidir[b]
            if len(na) < 2 or len(nb) < 2:
                continue
            common = na & nb
            if len(common) < 2:
                continue
            ratio = len(common) / min(len(na), len(nb))
            if ratio >= 0.67:
                missing_siblings.append({
                    "a": a, "b": b,
                    "common": sorted(common),
                    "overlap": round(ratio, 2),
                })

    activators = []
    for a, fdef in glossary.items():
        if not isinstance(fdef, dict):
            continue
        fa = _forces_of(fdef)
        for b, v in fa.items():
            if _is_falsy_force(v):
                continue
            b_def = glossary.get(b)
            if not isinstance(b_def, dict):
                continue
            b_default = field_default(b_def)
            if v == b_default:
                continue
            fb = _forces_of(b_def)
            reverse = fb.get(a, "__missing__")
            if reverse == "__missing__":
                kind = "pure_activator"
                desc = (
                    f"{a} auto-engages {b}={v!r}; {b} has no reverse edge, "
                    f"so it stays on when {a} is disabled"
                )
            elif _is_falsy_force(reverse):
                kind = "supervisor"
                desc = (
                    f"{a} auto-engages {b}={v!r} as its mechanism; directly "
                    f"enabling {b} disables {a} (supervisor released)"
                )
            else:
                kind = "mutual_activator"
                desc = (
                    f"{a} forces {b}={v!r} and {b} forces {a}={reverse!r} — "
                    f"mutual activation (also flagged by cycle lint if both "
                    f"non-default)"
                )
            activators.append({
                "from": a, "to": b, "value": v,
                "kind": kind, "reverse": reverse, "description": desc,
            })

    return {
        "asymmetric": asymmetric,
        "missing_siblings": missing_siblings,
        "activators": activators,
    }


def format_mutex_report(report: dict) -> str:
    asym = report.get("asymmetric") or []
    sibs = report.get("missing_siblings") or []
    acts = report.get("activators") or []

    if not asym and not sibs and not acts:
        return "No uncovered mutex edges detected."

    lines: list[str] = []

    if asym:
        lines.append(f"Asymmetric falsy forces ({len(asym)}):")
        lines.append(
            "  A→B=0 exists but B→A=0 is missing. Add the reverse edge unless "
            "the asymmetry is intentional (e.g. a supervisor/worker pair where "
            "only the supervisor turns off the worker)."
        )
        for f in asym:
            lines.append(
                f"  • {f['from']} → {f['to']}={f['value']}  "
                f"(suggest: {f['to']}.mutual_exclusion.when_on.forces."
                f"{f['from']}: 0)"
            )
        lines.append("")

    if sibs:
        lines.append(f"Candidate missing-pair edges ({len(sibs)}):")
        lines.append(
            "  Neither field excludes the other, but their exclusion neighbors "
            "overlap heavily — they look like clique-mates. Consider adding a "
            "symmetric force=0 pair. Safe to ignore if the AC firmware already "
            "enforces the mutex, but documenting it helps the overlay and UI."
        )
        for f in sibs:
            lines.append(
                f"  • {f['a']} ↔ {f['b']}: overlap={f['overlap']}, "
                f"common=[{', '.join(f['common'])}]"
            )
        lines.append("")

    if acts:
        lines.append(f"Functional activator relationships ({len(acts)}):")
        lines.append(
            "  Truthy forces describe composition (A auto-engages B), not "
            "exclusion. These are not gaps — they document how features "
            "layer on top of each other."
        )
        for f in acts:
            lines.append(
                f"  • [{f['kind']}] {f['from']} → {f['to']}={f['value']!r}"
            )
            lines.append(f"      {f['description']}")

    return "\n".join(lines)


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

    report = build_mutex_report(glossary)
    if report["asymmetric"] or report["missing_siblings"]:
        print()
        print(format_mutex_report(report))

    return 0


if __name__ == "__main__":
    sys.exit(main())
