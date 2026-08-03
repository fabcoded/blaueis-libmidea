#!/usr/bin/env python3
"""CLI for the glossary bit-collision checker.

Wraps the library in :mod:`blaueis.tools.glossary_collisions`, running it
against the bundled glossary (``blaueis.core``'s ``glossary.yaml``) plus the
``glossary_collisions.allow.yaml`` allowlist that ships alongside it.

Installed as the ``blaueis-collisions`` console script; also runnable as
``python -m blaueis.tools.check_collisions``.

Usage:
    blaueis-collisions
    blaueis-collisions --field power_off_timer
    blaueis-collisions --frame cmd_0x40 rsp_0xc0
    blaueis-collisions --class read write
    blaueis-collisions --format json > catalogue.json
    blaueis-collisions --ci          # quiet; exit 1 on unwhitelisted
    blaueis-collisions --include-shared --field fan_speed_timer_bit
    blaueis-collisions --list-scopes

Exit code: 0 if no unwhitelisted collisions, 1 otherwise.

The library has pytest coverage in ``tests/test_glossary_collisions.py``;
this CLI's smoke test lives in ``tests/test_check_collisions.py``.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

import yaml
from blaueis.core.codec import GLOSSARY_PATH

from .glossary_collisions import (
    build_position_catalogue,
    find_collisions,
    find_shared_bytes,
    load_allowlist,
)

GLOSSARY = GLOSSARY_PATH
ALLOWLIST = GLOSSARY_PATH.parent / "glossary_collisions.allow.yaml"


def _print_text_report(
    catalogue: list[dict],
    collisions: list[dict],
    shared: list[dict] | None,
    *,
    field_filter: set[str] | None,
    quiet: bool,
) -> None:
    scopes: dict[str, list[dict]] = {}
    for r in catalogue:
        scopes.setdefault(r["scope_key"], []).append(r)

    if not quiet:
        for scope_key in sorted(scopes):
            recs = scopes[scope_key]
            cls = recs[0]["class"].upper()
            scope_type = recs[0]["scope_type"]
            print(f"=== {cls}: {scope_key}  ({scope_type}, {len(recs)} bit slots claimed) ===")
            scope_collisions = [c for c in collisions if c["scope_key"] == scope_key]
            if scope_collisions:
                print(f"  COLLISIONS ({len(scope_collisions)}):")
                for c in scope_collisions:
                    star = " ⚠" if field_filter and any(f in field_filter for f in c["fields"]) else ""
                    print(f"    body[{c['byte']}] bit {c['bit']}: {c['fields']}{star}")
            else:
                print("  no collisions")
            if shared:
                scope_shared = [s for s in shared if s["scope_key"] == scope_key]
                for s in scope_shared:
                    layout = ", ".join(f"bit {b}={(v[0] if len(v) == 1 else v)}" for b, v in s["bit_layout"].items())
                    print(f"  shared body[{s['byte']}]: {s['owners']}  ({layout})")
    else:
        for c in collisions:
            print(f"  {c['class'].upper()}: {c['scope_key']} body[{c['byte']}] bit {c['bit']}: {c['fields']}")

    if collisions:
        print(f"\n{len(collisions)} unwhitelisted collision(s).", file=sys.stderr)
    elif not quiet:
        print("\nClean — no unwhitelisted bit collisions.")


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    ap.add_argument("--field", nargs="+", help="Restrict report to specific field name(s)")
    ap.add_argument("--frame", nargs="+", help="Restrict to specific frame_id(s)")
    ap.add_argument(
        "--class", dest="classes", nargs="+", choices=["read", "write", "cap"], help="Restrict to one or more classes"
    )
    ap.add_argument(
        "--format",
        choices=["text", "json"],
        default="text",
        help="Output format. json dumps the full catalogue (use for the 'database' query case).",
    )
    ap.add_argument(
        "--allow", default=None, help=f"Allowlist YAML file (default: {ALLOWLIST.name} next to the glossary)"
    )
    ap.add_argument("--no-allow", action="store_true", help="Ignore any allowlist; report every collision")
    ap.add_argument("--ci", action="store_true", help="Quiet mode; only print unwhitelisted collisions; exit 1 if any")
    ap.add_argument(
        "--include-shared", action="store_true", help="Include shared-byte (non-collision) info in text report"
    )
    ap.add_argument(
        "--list-scopes", action="store_true", help="Print scope taxonomy and per-scope claim counts, then exit"
    )
    args = ap.parse_args(argv)

    glossary = yaml.safe_load(GLOSSARY.read_text())
    full_catalogue = build_position_catalogue(glossary)

    field_filter = set(args.field) if args.field else None
    frame_filter = set(args.frame) if args.frame else None
    class_filter = set(args.classes) if args.classes else None

    display_catalogue = full_catalogue
    if field_filter:
        relevant_scopes = {r["scope_key"] for r in full_catalogue if r["field"] in field_filter}
        display_catalogue = [r for r in full_catalogue if r["scope_key"] in relevant_scopes]
    if frame_filter:
        display_catalogue = [r for r in display_catalogue if r["frame"] in frame_filter]
    if class_filter:
        display_catalogue = [r for r in display_catalogue if r["class"] in class_filter]

    if args.no_allow:
        allow = None
    else:
        allow_path = Path(args.allow) if args.allow else ALLOWLIST
        allow = load_allowlist(allow_path)

    all_collisions = find_collisions(full_catalogue, allow=allow)
    display_scopes = {r["scope_key"] for r in display_catalogue}
    display_collisions = [c for c in all_collisions if c["scope_key"] in display_scopes]

    if args.list_scopes:
        print("Scope taxonomy + claim counts (across full glossary):\n")
        scopes: dict[str, dict] = {}
        for r in full_catalogue:
            s = scopes.setdefault(
                r["scope_key"], {"scope_type": r["scope_type"], "class": r["class"], "claims": 0, "fields": set()}
            )
            s["claims"] += 1
            s["fields"].add(r["field"])
        for k in sorted(scopes):
            v = scopes[k]
            print(
                f"  [{v['class']:5s}] {k:50s}  type={v['scope_type']:14s}  "
                f"{v['claims']:4d} bit-claims, {len(v['fields']):3d} fields"
            )
        return 0

    if args.format == "json":
        out = {
            "catalogue": display_catalogue,
            "collisions": display_collisions,
            "shared_bytes": find_shared_bytes(display_catalogue) if args.include_shared else [],
        }
        print(json.dumps(out, indent=2, sort_keys=True))
        return 1 if display_collisions else 0

    shared = find_shared_bytes(display_catalogue) if args.include_shared else None
    _print_text_report(display_catalogue, display_collisions, shared, field_filter=field_filter, quiet=args.ci)
    return 1 if display_collisions else 0


if __name__ == "__main__":
    sys.exit(main())
