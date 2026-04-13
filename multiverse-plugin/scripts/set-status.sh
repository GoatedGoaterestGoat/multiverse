#!/usr/bin/env bash
# Write a status value to $MULTIVERSE_STATUS if set.
# Called by Claude Code hooks inside a multiverse session.
#
# Usage: set-status.sh <status>
#   <status> is a short token: idle | working | waiting | ended

# Not running under the monitor → do nothing
[ -z "${MULTIVERSE_STATUS:-}" ] && exit 0

STATUS="${1:-unknown}"
TS=$(date +%s)

mkdir -p "$(dirname "$MULTIVERSE_STATUS")"
printf '{"status":"%s","ts":%d}\n' "$STATUS" "$TS" > "$MULTIVERSE_STATUS"

exit 0
