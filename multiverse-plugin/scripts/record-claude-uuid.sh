#!/usr/bin/env bash
# Record claude's session_id for this multiverse session.
#
# Runs on SessionStart. Reads hook input JSON from stdin, extracts
# session_id (the claude session UUID), and writes it to `claude_uuid`
# alongside status.json. Used by the monitor to deduplicate dormant claude
# sessions against live multiverse sessions.

[ -z "${MULTIVERSE_STATUS:-}" ] && exit 0
command -v jq &>/dev/null || exit 0

INPUT=$(cat)
SESSION_ID=$(printf '%s' "$INPUT" | jq -r '.session_id // empty' 2>/dev/null)
[ -z "$SESSION_ID" ] && exit 0

STATE_DIR=$(dirname "$MULTIVERSE_STATUS")
printf '%s\n' "$SESSION_ID" > "$STATE_DIR/claude_uuid"

exit 0
