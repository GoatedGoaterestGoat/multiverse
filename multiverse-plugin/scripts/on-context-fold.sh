#!/usr/bin/env bash
# PostToolUse hook for context_fold
#
# When the context_fold MCP tool runs, it writes a control action to
# $MULTIVERSE_CONTROL. This hook detects that and tells Claude to stop
# so the multiverse wrapper can resume with the folded context.
#
# If not running under the multiverse wrapper (no $MULTIVERSE_CONTROL),
# this hook does nothing — the user gets the manual "Run: claude --resume"
# message from the MCP tool instead.

# Not running under wrapper → let Claude handle it normally
if [ -z "${MULTIVERSE_CONTROL:-}" ]; then
  exit 0
fi

# MCP tool wrote a control action → signal Claude to stop
if [ -s "$MULTIVERSE_CONTROL" ]; then
  cat <<'EOF'
{"continue":false,"stopReason":"Context folded. Resuming with optimized context..."}
EOF
fi
