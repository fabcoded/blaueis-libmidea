#!/usr/bin/env bash
#
# Optional local checks on agent tool calls. Runs whatever a workspace supplies;
# a silent no-op otherwise.
#
# The counterpart of tools/local_gate.sh for a surface git never sees: commands
# an AI coding agent is about to run — an issue body or PR description handed to
# `gh`, say. Registered as a Claude Code PreToolUse hook in .claude/settings.json,
# it receives the pending tool call as JSON on stdin and hands that to every
# executable in `.localchecks/agent-pretool.d/` one level above the repository
# root. A check that exits 2 blocks the call and its stderr is reported back to
# the agent. If there is no such directory, nothing happens and nothing is
# reported.
#
# The repository root is taken from this file's location, not the working
# directory: the hook runs from wherever the agent session was started.
#
# It never downloads or fetches anything. It only executes files already present
# on this machine, outside this repository.
set -euo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
DIR="${LOCAL_CHECKS_DIR:-$(dirname "$ROOT")/.localchecks}/agent-pretool.d"
[ -d "$DIR" ] || exit 0
payload="$(cat)"
for check in "$DIR"/*.sh; do
    [ -x "$check" ] || continue
    printf '%s' "$payload" | "$check" || exit $?
done
exit 0
