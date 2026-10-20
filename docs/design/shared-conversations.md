# Shared conversations and attachment cleanup

A Codex conversation driven through several clients is one mv2 agent. Its own
shell reads the same inbox and reports the same work whether the driving client
is mv2's terminal or the user's other Codex client on the same server. Sharing an
idle or active conversation is normal; manual resume has no subscriber-count or
idle precondition. Automatic recovery still requires the existing idle and
message-safety checks.

## Upgrade running Codex bridge agents

Run callbacks through the current mv2 installation. A connected Codex conversation can retain an earlier launch's tool environment, without `MV2_IDENTITY_MODE` and with its old `MV2_LAUNCH_ID`. The Codex bridge explicitly opts into compatibility for this case; other adapters default to refusing it.

Core requires a canonical historical launch ID and a readable `launches/LID/request.json` under this same agent. Its recorded universe, full agent ID, agent directory and launch ID must match the caller. The request must have `shared_service=true`, bridge mode and a relay with `binds_session=true`. Core then applies the current saved-policy, exact bound native thread, canonical storage, workspace, universe and agent-directory checks.

This route permits only the agent's own `report`, `inbox`, sending as the agent and verification of its existing session binding. Missing or damaged proof, a foreign thread or scope, and an incorrect explicit identity mode remain refused. Runner and control updates and `calling_socket` retain strict launch checks. No caller environment is rewritten or given the current launch ID.

By default, Codex can keep an idle conversation loaded for up to 30 minutes after its last client leaves. Restarting the frontend does not guarantee that its cached tool settings change. The compatibility route lets verified old tools work without changing those settings. A separate older mv2 checkout still runs its old callback code and is not covered; use the current installation.

## Agent identity and terminal identity

The adapter declares an optional agent identity policy. Core builds its tool
environment from the canonical universe, full agent ID, agent directory, CLI and
Python paths, PATH and an explicit `MV2_IDENTITY_MODE`. Each launch requests its current CLI, Python and PATH
values; saved values describe the request rather than the loaded native environment. The provider declares
the native conversation marker and storage scope used to validate the caller.
Core checks them against the agent's persisted binding, workspace and service
identity. Missing or inconsistent proof is rejected; it never becomes an
operator call. A genuinely unscoped human CLI remains an operator call.
Read-only commands such as `catalog`, `list`, `command` and `doctor` do not
resolve the caller's agent identity and remain available with stale shell values.

The native tool environment contains no `MV2_LAUNCH_ID`, generation-private
control socket or tmux socket. The frontend, runner and relay retain those private
values. No old relay address is rebound to a new terminal, and no callback is
given the latest launch ID as a substitute for its own identity.

| Operation | Identity and ownership rules |
| --- | --- |
| Own `report` and `inbox`, including `inbox --wait` | Explicit agent proof or opted-in retained bridge proof; valid across terminal replacement and while its wrapper is stopped or failed. It cannot clear a launch error or make a terminal ready. |
| `send`, `deliver`, `ask`, usage and management commands | A validated agent may act normally. Recipient native session, message receipts, target launch and pane checks remain unchanged. |
| `session-bind` with agent proof, including a retained bridge launch | Only verifies the exact already-bound ID, without writing. Establishing or changing a binding still requires the strict runner callback. |
| `runtime-update`, runner binding, control RPC, attachment relay and child-spawn socket selection | Exact launch identity remains required. Old terminals cannot control replacement attachments. |

Plugin MCP environment references alone do not acquire native-shell authority:
the shell's native conversation proof is required. Explicit plugin/API
delegation remains a separate path. Plugins cannot override the reserved
`MV2_IDENTITY_MODE` key. These checks enforce local identity consistency; they do
not create a security boundary against another process owned by the same user.

## Closing an attachment

Before manual resume or completed shutdown, core waits for the previous launch's
runner, relay, status observer, recovery helper and startup delivery to drain.
The wait is bounded and happens outside the launch lock; identity and cleanup
are checked again under that lock before replacement. Incomplete cleanup gives
an error and leaves the old attachment alone. Move and remove also refuse while
cleanup remains incomplete.

Each launch keeps `cleanup.json` and metadata-only `roles/*.json` files in its
launch directory. They record role, process identity, endpoint and open/close
timing, never protocol frames or message text. Closed connection history keeps
only its latest 100 records; active, pending and unknown records are retained.
Short read-only recovery checks use their enclosing monitor/worker role rather
than writing a record per check. Observation and pruning share a journal lock.
If the launch directory is missing, optional connection records are skipped with
a warning; native status still updates, and removed directories are not recreated.
Completed cleanup is persisted;
after a crash, a role is considered gone only when its recorded process is
provably absent or its PID belongs to a different process. Unknown state blocks
handover.

Ordinary `kill` and `stop` retain their conversation-wide native cancellation
semantics. `mv2 kill NAME --attachment-only` and `stop --attachment-only` close
only mv2's terminal and helpers; they do not cancel native work. This is useful
for a refused attachment or while the conversation is being driven elsewhere.
Neither route detaches outside clients or restarts the shared daemon. Normal
terminal exit still detaches viewers of that mv2 pane and removes its wrapper;
automatic recovery retains the pane and attached viewers.

## Native refusal and retry

The adapter supplies structured native launch errors or recognizes a narrowly
specified, version-tested native refusal widget from the current owned pane.
Core does not infer refusal from scrollback or guess whether a conversation has
other subscribers. Unknown screens and versions are not classified.

The first refusal is saved in `native-refusal.json` and the exact launch's
runtime record. Its native message stays primary in list, inspect, wait and
delivery errors, even if a later relay failure or child exit adds diagnostics.
Evidence is limited to 16 KiB; visible-pane observation is bounded to 32 KiB and
does not include scrollback. A refusal never triggers plain fallback, automatic
recovery, a new conversation or message replay.

Definitely unsent core startup messages become failed with the native cause and
remain saved for explicit retry. A prompt already handed to the native CLI
remains uncertain rather than becoming replayable. Pressing R may restore Codex's
composer, but it never clears mv2's refusal or releases saved input.
Close that attachment with `--attachment-only` and explicitly resume again.
Only a successful new mv2 launch clears the previous launch's error.

Codex retains the current grants of a shared conversation. Every bridge binding
still validates the exact conversation, workspace and effective grants. A
requested/effective mismatch is reported plainly; mv2 cannot force new grants
into a conversation retained by another client and does not unload it to do so.
