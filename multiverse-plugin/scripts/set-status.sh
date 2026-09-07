#!/usr/bin/env bash
# Write a status value to $MULTIVERSE_STATUS if set.
# Called by Claude Code hooks inside a multiverse session.
#
# Usage: set-status.sh <status>
#   <status> is a short token: idle | working | waiting | ended | retiring | retired

# Not running under the monitor → do nothing
[ -z "${MULTIVERSE_STATUS:-}" ] && exit 0

STATUS="${1:-unknown}"
TS=$(date +%s)

# A dashboard kill owns the lifecycle state until runtime and tmux teardown
# finish. Hooks may still fire while the runtime handles SIGTERM; do not let
# those late writes replace the visible `ending` state.
if [ "$STATUS" != "retired" ] \
   && [ -e "$(dirname "$MULTIVERSE_STATUS")/ending" ]; then
  exit 0
fi

# Retirement is permanent. A late hook from the runtime being stopped must
# never make the handed-off agent look live or ordinarily resumable again.
if [ "$STATUS" != "retired" ] \
   && [ -f "$MULTIVERSE_STATUS" ] \
   && grep -Eq '"status"[[:space:]]*:[[:space:]]*"(retiring|retired)"' "$MULTIVERSE_STATUS"; then
  exit 0
fi

mkdir -p "$(dirname "$MULTIVERSE_STATUS")"
TMP="${MULTIVERSE_STATUS}.$$.$RANDOM.tmp"
if printf '{"status":"%s","ts":%d}\n' "$STATUS" "$TS" > "$TMP"; then
  mv -f "$TMP" "$MULTIVERSE_STATUS"
else
  rm -f "$TMP" 2>/dev/null || true
fi

exit 0
