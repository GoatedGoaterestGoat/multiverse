#!/usr/bin/env bash
# End-to-end test of the pi extension against a mock model.
#
# Covers the seams that only exist at runtime: that pi accepts the typebox
# tool schemas, that a tool call reaches mvctl and comes back as a tool
# result, that lifecycle events write status.json, and that an inter-agent
# message queued on disk is delivered into the conversation.
#
# Needs `pi` on PATH and the plugin's venv (`multiverse setup`). No network
# and no model credentials — the mock provider serves everything.
#
#   tests/test_pi_extension.sh

set -uo pipefail

PLUGIN_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
WORK="$(mktemp -d "${TMPDIR:-/tmp}/mv-pi-test-XXXXXX")"
PORT="$(python3 -c 'import socket;s=socket.socket();s.bind(("127.0.0.1",0));print(s.getsockname()[1]);s.close()')"
MOCK_PID=""
FAILURES=0

cleanup() {
  [ -n "$MOCK_PID" ] && kill "$MOCK_PID" 2>/dev/null
  rm -rf "$WORK"
}
trap cleanup EXIT

py() { "$PLUGIN_ROOT/.venv/bin/python" "$@" 2>/dev/null || python3 "$@"; }

ok()   { printf '  \033[32mPASS\033[0m %s\n' "$1"; }
fail() { printf '  \033[31mFAIL\033[0m %s\n' "$1"; FAILURES=$((FAILURES + 1)); }
check() { if [ "$2" = "$3" ]; then ok "$1"; else fail "$1 (want '$3', got '$2')"; fi; }
contains() {
  if grep -qF -- "$3" "$2"; then ok "$1"; else fail "$1 (missing '$3' in $2)"; fi
}

# --- fixture -----------------------------------------------------------------

export MULTIVERSE_ROOT="$WORK/state"
export MULTIVERSE_PLUGIN_ROOT="$PLUGIN_ROOT"
export MULTIVERSE_SESSION_NAME="tester"
export MULTIVERSE_SESSION_CWD="$WORK/ws"
export MULTIVERSE_STATUS="$WORK/state/sessions/mv1/status.json"
export PI_CODING_AGENT_SESSION_DIR="$WORK/sessions"
export MOCK_OPENAI_URL="http://127.0.0.1:$PORT/v1"
mkdir -p "$WORK/state/sessions/mv1" "$WORK/ws" "$WORK/sessions"

run_pi() {  # run_pi <script.json> <prompt>
  python3 "$PLUGIN_ROOT/tests/mock_openai_server.py" --port "$PORT" --script "$1" \
    >"$WORK/mock.log" 2>&1 &
  MOCK_PID=$!
  for _ in $(seq 1 40); do
    curl -sf "http://127.0.0.1:$PORT/v1/models" >/dev/null && break
    sleep 0.25
  done
  timeout 90 pi \
    -e "$PLUGIN_ROOT/tests/mock-provider.ts" \
    -e "$PLUGIN_ROOT/pi-extension/index.ts" \
    --provider mock --model mock-model \
    --no-extensions --no-skills --no-context-files \
    -p "$2" </dev/null >"$WORK/pi.out" 2>&1
  local rc=$?
  kill "$MOCK_PID" 2>/dev/null; wait "$MOCK_PID" 2>/dev/null; MOCK_PID=""
  return $rc
}

echo "multiverse pi-extension integration test"
echo "  workdir: $WORK"
echo

# --- 1. lifecycle + tool registration ---------------------------------------

echo "1. session lifecycle and tool exposure"
cat > "$WORK/s1.json" <<'JSON'
[{"text": "Nothing to do."}]
JSON
run_pi "$WORK/s1.json" "hello"

check "status.json written" \
  "$(py -c "import json,sys;print(json.load(open('$MULTIVERSE_STATUS'))['status'])")" \
  "ended"
check "session id recorded" \
  "$([ -s "$WORK/state/sessions/mv1/claude_uuid" ] && echo yes || echo no)" "yes"

# The tool schemas pi sends the provider are the contract the model sees; a
# malformed typebox schema shows up here and nowhere else.
REQ="$WORK/s1.requests.jsonl"
for tool in list_agents message_agent ask_agent check_inbox monitor_inbox spawn_agent; do
  contains "tool exposed: $tool" "$REQ" "\"$tool\""
done

# --- 2. a tool call round-trips through mvctl -------------------------------

echo
echo "2. list_agents tool call reaches mvctl"
cat > "$WORK/s2.json" <<'JSON'
[{"tool": {"name": "list_agents", "arguments": {"max_agents": 5}}},
 {"text": "Done."}]
JSON
run_pi "$WORK/s2.json" "who else is here?"

# mvctl's answer for an empty workspace comes back verbatim as the tool
# result, which the second request carries.
contains "tool result reached the model" "$WORK/s2.requests.jsonl" \
  "no named agents in this workspace"

# --- 3. inter-agent message delivery ----------------------------------------

echo
echo "3. inbox delivery"
py - <<PY
import sys
sys.path.insert(0, "$PLUGIN_ROOT")
from src.inbox import write_message_to_inbox
write_message_to_inbox("alice", "tester", "$MULTIVERSE_SESSION_CWD",
                       "MARKER-INBOX-DELIVERY-OK")
PY
cat > "$WORK/s3.json" <<'JSON'
[{"text": "Working."}, {"text": "Acknowledged."}, {"text": "Idle."}]
JSON
run_pi "$WORK/s3.json" "start working"

contains "message delivered into the conversation" "$WORK/s3.requests.jsonl" \
  "MARKER-INBOX-DELIVERY-OK"
check "inbox drained after delivery" \
  "$(py -c "
import sys; sys.path.insert(0,'$PLUGIN_ROOT')
from src.inbox import pending_inbox_count
print(pending_inbox_count('$MULTIVERSE_SESSION_CWD','tester'))")" "0"

# --- 4. compaction plan ------------------------------------------------------

echo
echo "4. compaction planning over a pi transcript"
SESSION="$(ls "$WORK/sessions"/*.jsonl 2>/dev/null | head -1)"
if [ -n "$SESSION" ]; then
  PLAN="$(cd "$PLUGIN_ROOT" && py -m src.mvctl compact-plan --session "$SESSION" --budget 200)"
  check "plan has a summary" \
    "$(printf '%s' "$PLAN" | py -c "import json,sys;print(bool(json.load(sys.stdin)['summary']))")" \
    "True"
  check "plan reports turns" \
    "$(printf '%s' "$PLAN" | py -c "import json,sys;print(json.load(sys.stdin)['_multiverse']['turns']>0)")" \
    "True"
else
  fail "no pi session file was written"
fi

echo
if [ "$FAILURES" -eq 0 ]; then
  echo "all checks passed"
else
  echo "$FAILURES check(s) failed"
  echo "  pi output:   $WORK/pi.out"
  echo "  mock log:    $WORK/mock.log"
fi
exit $((FAILURES > 0))
