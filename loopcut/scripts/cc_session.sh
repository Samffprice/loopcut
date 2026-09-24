#!/bin/sh
# Blender with Loopcut's model played by a Claude Code session (harness/cc_bridge.py).
# Start the bridge first (`python3 loopcut/harness/cc_bridge.py serve`); this uses its current run.
# Conversations and checkpoints go into the run folder unless LOOPCUT_DATA_DIR says otherwise.
set -eu
repo="$(cd "$(dirname "$0")/../.." && pwd)"
work="$(dirname "$repo")"
current="$work/out/cc_bridge/current.txt"
[ -f "$current" ] || { echo "No bridge run: start loopcut/harness/cc_bridge.py serve first." >&2; exit 1; }
run="$(cat "$current")"
export LOOPCUT_BASE_URL="http://127.0.0.1:${LOOPCUT_BRIDGE_PORT:-8765}/v1"
export LOOPCUT_API_KEY=claude-code-bridge
export LOOPCUT_MODEL=claude-code
export LOOPCUT_DATA_DIR="${LOOPCUT_DATA_DIR:-$run/loopcut_data}"
mkdir -p "$LOOPCUT_DATA_DIR"
exec "$repo/loopcut/scripts/dev.sh" "$@"
