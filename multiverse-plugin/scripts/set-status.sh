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

# Proof of life for dashboards on OTHER hosts, which cannot see this host's
# tmux server and so have no other way to tell "alive on a remote" from
# "that remote is gone". Touched before every guard below, because a session
# still firing hooks is still alive whatever those guards decide to write.
# The contents name the machine, so a dashboard elsewhere can tell a claim it
# made itself from one made by a host it cannot see. Same identity rule as
# hosts.current_host(); see its docstring.
printf '%s' "${MULTIVERSE_HOST_ID:-$(hostname)}" \
  > "$(dirname "$MULTIVERSE_STATUS")/heartbeat" 2>/dev/null || true

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
