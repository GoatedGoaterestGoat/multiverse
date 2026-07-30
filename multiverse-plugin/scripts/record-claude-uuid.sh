#!/usr/bin/env bash
# Record claude's session_id for this multiverse session.
#
# Runs on SessionStart. Reads hook input JSON from stdin, extracts
# session_id (the claude session UUID), and writes it to `claude_uuid`
# alongside status.json. Used by the monitor to deduplicate dormant claude
# sessions against live multiverse sessions.

[ -z "${MULTIVERSE_STATUS:-}" ] && exit 0
# Never let a tree-build merge subprocess claim the session pointer. The
# constructor spawns `claude --resume <uuid> -p <instruction>` per merge
# step; that subprocess gets its OWN fresh claude session_id (custom-title
# "priv_compaction") and inherits this plugin, so SessionStart fires here
# with the PARENT session's MULTIVERSE_STATUS. Writing that id would
# repoint claude_uuid at the throwaway summarizer session — making the
# dashboard read the wrong live/full JSONL for the real agent. Same env
# flag spawn-tree-builder.py already uses as its recursion guard.
[ -n "${MULTIVERSE_TREE_BUILDER_ACTIVE:-}" ] && exit 0

# NOT guarded here: Claude-Code-native background forks (`--session-id <new>
# --fork-session --resume <parent.jsonl>`, spawned by Claude Code itself when
# e.g. a run_in_background Bash task completes while the session is detached).
# Such a fork inherits this session's MULTIVERSE_STATUS, so SessionStart fires
# here and the pointer swings onto the fork.
#
# A write-time guard was tried and removed. Detecting the fork requires
# inspecting process ancestry for `--fork-session`, which matches on substring
# and so fires on ANY ancestor whose command line merely mentions the flag — a
# diagnostic command, a grep, a script argument. That silently stops ordinary
# sessions from recording their UUID at all, a worse failure than the one it
# prevents. Claude Code exposes nothing at SessionStart that distinguishes a
# fork, and the fork's transcript has no rows to inspect yet.
#
# Handled at READ time instead, where the evidence is exact: Claude Code stamps
# `sessionKind: "bg"` on rows a background fork writes, so
# sessions.is_native_bg_fork() classifies from committed data rather than
# guesswork. Session.effective_claude_uuid ignores a pointer aimed at such a
# fork, and build_dashboard_rows() hides the fork's duplicate row.
command -v jq &>/dev/null || exit 0

INPUT=$(cat)
SESSION_ID=$(printf '%s' "$INPUT" | jq -r '.session_id // empty' 2>/dev/null)
[ -z "$SESSION_ID" ] && exit 0

STATE_DIR=$(dirname "$MULTIVERSE_STATUS")
UUID_PATH="$STATE_DIR/claude_uuid"
TMP="${UUID_PATH}.$$.$RANDOM.tmp"
if printf '%s\n' "$SESSION_ID" > "$TMP"; then
  mv -f "$TMP" "$UUID_PATH"
else
  rm -f "$TMP" 2>/dev/null || true
fi

exit 0
