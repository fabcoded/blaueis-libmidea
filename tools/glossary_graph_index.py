#!/usr/bin/env python3
"""glossary_graph_index.py — put the glossary's fields into the code graph.

Optional developer tool. Used by tools/graph_refresh.sh; not needed to build,
test or use this library.

Why this exists
---------------
graphify (the code-knowledge-graph tool) ships no YAML extractor. Its
documentation lists YAML as indexed, but its extractors provide json_config
and nothing for YAML, and the effect is measurable: YAML contributes exactly
zero nodes to a graph while JSON contributes hundreds.

For this project that is not a minor gap. `glossary.yaml` defines 198 protocol
fields (83 under fields.control, 115 under fields.sensor) plus 26 composite
members across 8 of them, and it is the primary artefact the whole library is
built around. So the single most useful question — "which glossary field backs
this entity?" — could never be answered from the graph, because the fields were
never in it. An early attempt to ask exactly that returned nothing, and the
absence was easy to misread as the graph being a weak analyser rather than the
data simply being absent.

This does not patch graphify and does not touch the glossary. It reads the YAML
with line numbers, emits one node per field, and unions them into an existing
graph in the same namespace.

Deliberately NOT `graphify merge-graphs`: that namespaces ids as
"<repo>::<local_id>" because it exists to join different repos, which here would
rewrite every id and orphan the supplement onto placeholder parents instead of
the real file node.

Usage:
  ./glossary_graph_index.py <glossary.yaml> --repo-root <repo> --merge-into <graph.json>
  ./glossary_graph_index.py <glossary.yaml> --repo-root <repo> --out <supplement.json>
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

try:
    from ruamel.yaml import YAML
except ModuleNotFoundError:
    raise SystemExit("error: ruamel.yaml required (pip install ruamel.yaml)") from None

# Emission is restricted to the two real field planes plus composite members.
#
# An earlier version free-walked the document and emitted anything carrying a
# `description:` key. That was wrong by a wide margin: it swept in the
# `encodings:`, `frames:` and `protocol_generations:` sections plus every field's
# own `values:` enum entries and `capability:` sub-block, producing 493 nodes of
# which only 224 were fields. The document's real shape is fields.{control,sensor}
# — 83 + 115 = 198 — plus 26 composite members under 8 fields.
FIELD_SECTIONS = ("control", "sensor")

# Retained only as a sanity assertion; satisfied by every real field.
FIELD_MARKERS = ("description", "field_class", "data_type", "signoff")

ORIGIN = "glossary-supplement"


def slug(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "_", text.lower()).strip("_")


def file_node_id(rel_path: str) -> str:
    """graphify's file-node id: path minus extension, slugified.

    Mirrors graphify so links attach to the file node it already created. Note
    the scheme is lossy — `blaueis-gateway.target` and `blaueis-gateway@.service`
    in one directory collide, and that collision genuinely exists in
    blaueis-libmidea today. It does not affect glossary.yaml, whose id is unique,
    but callers creating a file node must check before claiming an id.
    """
    stem = rel_path.rsplit(".", 1)[0] if "." in rel_path.rsplit("/", 1)[-1] else rel_path
    return slug(stem)


def key_line(mapping, key) -> int:
    """Line of `key` itself.

    Deliberately NOT value.lc.line: ruamel sets that to the mapping's first
    *child*, so it lands one line below the field name in every single case.
    An earlier version used it and every emitted node was off by one — with the
    wrong line baked into the node id, so ids churned as well.
    """
    try:
        return mapping.lc.data[key][0] + 1  # ruamel is 0-based
    except Exception:
        try:
            return mapping[key].lc.line  # last resort, still better than +1
        except Exception:
            return 1


def collect_fields(doc) -> list[dict]:
    """Fields under fields.{control,sensor}, plus their composite members."""
    fields = doc.get("fields") if hasattr(doc, "get") else None
    if not hasattr(fields, "items"):
        raise SystemExit("error: no `fields:` mapping in the glossary")

    out: list[dict] = []
    for section in FIELD_SECTIONS:
        block = fields.get(section)
        if not hasattr(block, "items"):
            continue
        for name, body in block.items():
            if not hasattr(body, "items"):
                continue
            if not any(m in body for m in FIELD_MARKERS):
                print(f"  note: {section}.{name} has no field marker — emitting anyway", file=sys.stderr)
            out.append(
                {
                    "name": str(name),
                    "line": key_line(block, name),
                    "category": section,
                    "kind": "field",
                    "field_class": str(body.get("field_class") or ""),
                    "signoff": str(body.get("signoff") or ""),
                }
            )
            members = body.get("composite", {})
            members = members.get("members") if hasattr(members, "get") else None
            if hasattr(members, "items"):
                for mname, mbody in members.items():
                    out.append(
                        {
                            "name": str(mname),
                            "line": key_line(members, mname),
                            "category": f"{section}/{name}",
                            "kind": "composite_member",
                            "field_class": str(mbody.get("field_class") or "") if hasattr(mbody, "get") else "",
                            "signoff": str(mbody.get("signoff") or "") if hasattr(mbody, "get") else "",
                        }
                    )
    return out


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("glossary", help="path to glossary.yaml")
    ap.add_argument("--repo-root", required=True, help="repo root, so source_file matches graphify's relative paths")
    ap.add_argument("--out", help="write the supplement to its own graph.json")
    ap.add_argument("--merge-into", help="union directly into an existing graph.json (idempotent)")
    args = ap.parse_args()
    if not args.out and not args.merge_into:
        ap.error("one of --out / --merge-into is required")

    root = Path(args.repo_root).resolve()
    gpath = Path(args.glossary).resolve()
    rel = str(gpath.relative_to(root))

    yaml = YAML(typ="rt")
    yaml.allow_duplicate_keys = True  # we only need positions here
    data = yaml.load(gpath.read_text(encoding="utf8"))

    fields = collect_fields(data)

    parent_id = file_node_id(rel)
    nodes, links = [], []
    seen: set[str] = set()
    for f in fields:
        nid = f"{parent_id}_{slug(f['name'])}_{f['line']}"
        if nid in seen:
            continue
        seen.add(nid)
        nodes.append(
            {
                "label": f["name"],
                "file_type": "code",
                "source_file": rel,
                "source_location": f"L{f['line']}",
                "_origin": ORIGIN,
                "id": nid,
                "norm_label": f["name"].lower(),
                "glossary_category": f["category"],
                "glossary_field_class": f["field_class"],
                "glossary_signoff": f["signoff"],
            }
        )
        links.append(
            {
                "relation": "contains",
                "confidence": "EXTRACTED",
                "source_file": rel,
                "source_location": f"L{f['line']}",
                "weight": "1.0",
                "_origin": ORIGIN,
                "source": parent_id,
                "target": nid,
                "confidence_score": "1.0",
            }
        )

    print(f"glossary: {rel}")
    print(f"recovered {len(nodes)} field node(s)")

    if args.out:
        Path(args.out).parent.mkdir(parents=True, exist_ok=True)
        Path(args.out).write_text(
            json.dumps(
                {
                    "directed": True,
                    "multigraph": False,
                    "graph": {},
                    "nodes": nodes,
                    "links": links,
                    "hyperedges": [],
                }
            ),
            encoding="utf8",
        )
        print(f"wrote {args.out}")

    if args.merge_into:
        target = Path(args.merge_into)
        graph = json.loads(target.read_text(encoding="utf8"))
        lk = "links" if "links" in graph else "edges"

        before = len(graph["nodes"])
        graph["nodes"] = [n for n in graph["nodes"] if n.get("_origin") != ORIGIN]
        graph[lk] = [link for link in graph[lk] if link.get("_origin") != ORIGIN]
        dropped = before - len(graph["nodes"])

        existing = {n["id"] for n in graph["nodes"]}
        if parent_id not in existing:
            # graphify indexes no YAML at all, so the glossary has no file node.
            # Create one so the field nodes hang off something real.
            graph["nodes"].append(
                {
                    "label": rel.rsplit("/", 1)[-1],
                    "file_type": "code",
                    "source_file": rel,
                    "source_location": "L1",
                    "_origin": ORIGIN,
                    "id": parent_id,
                    "norm_label": rel.rsplit("/", 1)[-1].lower(),
                }
            )
            existing.add(parent_id)
            print(f"  created file node for {rel} (graphify indexes no YAML)")

        added = [n for n in nodes if n["id"] not in existing]
        added_ids = {n["id"] for n in added}
        skipped = [n["id"] for n in nodes if n["id"] not in added_ids]
        if skipped:
            print(
                f"warning: {len(skipped)} node id(s) already present, skipped: {', '.join(skipped[:5])}",
                file=sys.stderr,
            )
        # Only links whose target was actually added. Appending all of them would
        # wire the glossary file node to whatever unrelated node already held a
        # colliding id.
        graph["nodes"].extend(added)
        graph[lk].extend([link for link in links if link["target"] in added_ids])
        target.write_text(json.dumps(graph), encoding="utf8")
        print(f"merged into {target}")
        print(f"  replaced {dropped} prior supplement node(s); added {len(added)}")
        print(f"  graph now {len(graph['nodes'])} nodes / {len(graph[lk])} links")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
