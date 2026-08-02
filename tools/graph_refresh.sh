#!/usr/bin/env bash
#
# Inspect or rebuild this repo's code-knowledge graph (graphify).
#
# The graph is an optional developer aid. Nothing in this repo needs it: not the
# build, not the tests, not CI. It is never rebuilt automatically — no git hook
# triggers it — and a rebuild additionally requires this working copy to opt in
# by creating .graphify-enabled (gitignored). Two independent conditions, because
# a rebuild is minutes of disk work that must never fire on a deploy target, a
# CI runner or a throwaway clone.
#
#   ./tools/graph_refresh.sh --status   is the graph current?  (instant)
#   ./tools/graph_refresh.sh            rebuild it
#   ./tools/graph_refresh.sh --force    rebuild, overriding graphify's
#                                       node-count guard after a big deletion
#
# Requires graphify on PATH (`uv tool install graphifyy` / `pipx install
# graphifyy`). If it is absent, --status still reports usefully and a rebuild
# exits with a clear message.

set -euo pipefail

ROOT="$(git rev-parse --show-toplevel)"
OUT="$ROOT/graphify-out"
GRAPH="$OUT/graph.json"
LOCK="$OUT/.graph_refresh.lock"
SENTINEL=".graphify-enabled"
GLOSSARY="$ROOT/packages/blaueis-core/src/blaueis/core/data/glossary.yaml"
INDEXER="$ROOT/tools/glossary_graph_index.py"

MODE="refresh"
FORCE=()
case "${1:-}" in
    --status) MODE="status" ;;
    --force)  FORCE=(--force) ;;
    "")       ;;
    *) echo "usage: $(basename "$0") [--status|--force]" >&2; exit 2 ;;
esac

# ── freshness ────────────────────────────────────────────────────────────────
# Computed, never cached: the graph records the commit it was built from, so
# comparing that against HEAD is exact and costs nothing. There is deliberately
# no stale-marker file to fall out of sync with reality.
report_status() {
    local head built
    head="$(git rev-parse HEAD)"
    if [ ! -f "$GRAPH" ]; then
        echo "graph: ABSENT — run ./tools/graph_refresh.sh to build it"
        return 1
    fi
    built="$(python3 -c '
import json,sys
try:
    print(json.load(open(sys.argv[1])).get("built_at_commit") or "")
except Exception:
    print("")' "$GRAPH" 2>/dev/null || true)"
    if [ -z "$built" ]; then
        echo "graph: present, but records no commit — treat as POTENTIALLY OUT OF DATE"
        return 1
    fi
    if [ "$built" = "$head" ]; then
        echo "graph: current (built at ${built:0:7}, HEAD ${head:0:7})"
        return 0
    fi
    echo "graph: POTENTIALLY OUT OF DATE — built at ${built:0:7}, HEAD is ${head:0:7}"
    echo "       Anything it reports may predate your working tree."
    echo "       Rebuild with ./tools/graph_refresh.sh"
    return 1
}

if [ "$MODE" = "status" ]; then
    report_status || true
    exit 0
fi

# ── opt-in gate ──────────────────────────────────────────────────────────────
# A rebuild runs only where this working copy has explicitly asked for one.
# --status is exempt: it is read-only and instant.
#
# This exists because AGENTS.md invites tooling — including AI agents — to
# refresh the graph, and a checkout is not always somewhere minutes of disk
# churn is welcome. A deploy target, a CI runner, a throwaway clone and a bisect
# worktree all read the same instructions. Opting in per working copy makes the
# intent explicit instead of assumed.
if [ ! -f "$ROOT/$SENTINEL" ]; then
    echo "graph refresh is not enabled for this checkout — doing nothing."
    echo "  A rebuild is minutes of disk work, so it is opt-in per working copy."
    echo "  To enable deliberately:  touch $SENTINEL"
    echo "  Do NOT enable it on a deploy target, CI runner or throwaway clone."
    exit 0
fi

# ── rebuild ──────────────────────────────────────────────────────────────────
command -v graphify >/dev/null 2>&1 || {
    echo "error: graphify not on PATH (uv tool install graphifyy)" >&2; exit 1; }

mkdir -p "$OUT"
# mkdir is atomic, so this is a real mutex rather than a check-then-write race.
# A crashed run leaves the directory behind; the message says how to clear it.
if ! mkdir "$LOCK" 2>/dev/null; then
    echo "error: a refresh is already running (lock: ${LOCK#"$ROOT"/})." >&2
    echo "       If you are sure none is, remove that directory and retry." >&2
    exit 1
fi
trap 'rmdir "$LOCK" 2>/dev/null || true' EXIT

# Supplement nodes must come out BEFORE graphify runs. They inflate graph.json
# above what graphify's own extraction yields, which trips its data-loss guard
# ("new graph has N nodes but existing graph.json has M. Refusing to overwrite")
# — and then the rebuild silently does not happen at all.
if [ -f "$GRAPH" ]; then
    python3 - "$GRAPH" <<'PY'
import json, sys
p = sys.argv[1]
g = json.load(open(p))
lk = "links" if "links" in g else "edges"
n0, l0 = len(g["nodes"]), len(g[lk])
g["nodes"] = [n for n in g["nodes"] if not str(n.get("_origin", "")).endswith("-supplement")]
g[lk] = [l for l in g[lk] if not str(l.get("_origin", "")).endswith("-supplement")]
if len(g["nodes"]) != n0:
    json.dump(g, open(p, "w"))
    print(f"  stripped {n0 - len(g['nodes'])} supplement node(s), {l0 - len(g[lk])} link(s)")
PY
fi

graphify update "$ROOT" "${FORCE[@]}"

# graphify indexes no YAML, so the glossary — the 198 protocol fields this
# library is built around — is invisible to it. Put the fields back.
if [ -f "$INDEXER" ] && [ -f "$GLOSSARY" ]; then
    python3 "$INDEXER" "$GLOSSARY" --repo-root "$ROOT" --merge-into "$GRAPH"
fi

echo
report_status || true
