#!/usr/bin/env bash
#
# Optional local developer checks. Runs whatever a workspace supplies; a silent
# no-op otherwise.
#
# Some checkouts sit inside a larger workspace that carries extra checks — ones
# specific to that workspace and not meaningful in a clone on its own. Rather
# than commit those here, this looks one level above the repository root for a
# `.localchecks/` directory and runs what it finds. If there is none, nothing
# happens and nothing is reported.
#
# It never downloads or fetches anything. It only executes files already present
# on this machine, outside this repository.
set -euo pipefail
ROOT="$(git rev-parse --show-toplevel)"
DIR="${LOCAL_CHECKS_DIR:-$(dirname "$ROOT")/.localchecks}/pre-commit.d"
[ -d "$DIR" ] || exit 0
rc=0
for check in "$DIR"/*.sh; do
    [ -x "$check" ] || continue
    "$check" "$@" || rc=1
done
exit "$rc"
