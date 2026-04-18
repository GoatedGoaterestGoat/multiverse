#!/usr/bin/env bash
# On a Notification hook fire, set status to "waiting" — but only when it's
# actually blocking the agent. Filters out informational notifications like
# `idle_prompt` ("Claude is waiting for your input"), which would otherwise
# overwrite the post-Stop "idle" status with "waiting" purely because the
# user hasn't typed anything in a while.

[ -z "${MULTIVERSE_STATUS:-}" ] && exit 0

# Read the hook input JSON from stdin (claude-code passes it on stdin).
INPUT=$(cat)

# Extract notification_type. If jq isn't available or parsing fails,
# fall back to setting "waiting" (preserves the old behavior).
if command -v jq &>/dev/null; then
  NTYPE=$(printf '%s' "$INPUT" | jq -r '.notification_type // empty' 2>/dev/null)
else
  NTYPE=""
fi

# Informational notifications — agent is not blocked on anything.
case "$NTYPE" in
  idle_prompt|elicitation_complete|elicitation_response|auth_success|computer_use_enter|computer_use_exit)
    exit 0
    ;;
esac

# Anything else (permission prompts, unknown types) → mark as waiting.
"$(dirname "$0")/set-status.sh" waiting
