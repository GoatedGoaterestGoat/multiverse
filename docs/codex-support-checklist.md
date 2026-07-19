# Codex Support Checklist

First-draft implementation checklist for adding Codex sessions to the Multiverse
wrapper and providing a Codex-side Multiverse plugin.

Checked items are implemented on the `codex-support` branch. Verification items
remain unchecked until the stated coverage has been completed end to end.

The intended architecture is one shared Multiverse orchestration layer with
runtime-specific adapters for Claude Code and Codex. Codex support should not copy
the monitor, inbox, spawn-request, or SecondBrain state machines.

## Scope and Runtime Model

- [ ] Define the initial supported Codex versions and platforms.
- [x] Add a runtime identifier (`claude` or `codex`) to each Multiverse session's
      `meta.json`.
- [x] Treat legacy session metadata without a runtime identifier as Claude Code.
- [x] Replace Claude-specific session identity assumptions with a runtime-neutral
      session ID interface.
- [ ] Introduce a runtime adapter boundary for session discovery, creation, resume,
      status, metadata, prompts, and side questions.
- [x] Support Claude and Codex sessions in the same Multiverse state root.
- [x] Support Claude and Codex sessions in the same workspace dashboard.
- [x] Preserve name uniqueness across both runtimes within a workspace.
- [x] Preserve existing Claude-only behavior when no Codex options are used.
- [x] Decide how a user chooses the runtime for a new session.
- [x] Decide whether a spawned subagent inherits the caller's runtime by default.

## Codex Plugin

- [x] Create a tracked Codex plugin under `plugins/multiverse-codex/`.
- [x] Add the required Codex plugin manifest and directory structure.
- [x] Add a Codex-side MCP server entry point.
- [x] Expose the Multiverse MCP tools in Codex-managed sessions.
- [x] Give the tools the same names, argument shapes, and response semantics as the
      Claude plugin where Codex permits it.
- [x] Ensure plugin paths resolve independently of the user's current directory.
- [x] Ensure the plugin receives the session-scoped `MULTIVERSE_*` environment.
- [x] Ensure the plugin is a no-op or returns a clear error when used outside the
      Multiverse wrapper.
- [x] Reuse the shared MCP tool descriptions in Codex so guidance for durable
      messages, side questions, inbox monitoring, and fresh-agent spawning cannot
      drift between runtimes.
- [x] Determine which Claude hooks have direct Codex equivalents.
- [x] Implement non-hook fallbacks for required lifecycle or inbox behavior where
      Codex has no equivalent extension point.

## Wrapper Configuration and Setup

- [x] Add a configurable Codex executable, such as `MULTIVERSE_CODEX_BIN`.
- [x] Preserve `MULTIVERSE_CLAUDE_BIN` and existing Claude configuration behavior.
- [x] Extend `multiverse setup` to detect and report Codex prerequisites.
- [x] Persist or restore the Codex configuration needed to resume sessions from a
      different terminal.
- [x] Keep Claude arguments from being forwarded accidentally to Codex sessions.
- [ ] Define how runtime-specific default arguments are configured.
- [x] Propagate runtime-specific environment variables through `tmux new-session -e`.
- [x] Load the Claude plugin only for Claude sessions.
- [x] Load the Codex plugin only for Codex sessions.
- [x] Continue to support extra Claude plugin directories without applying them to
      Codex sessions.
- [ ] Decide whether Codex needs an equivalent extra-plugin configuration setting.

## Session Lifecycle

- [x] Create a new Codex session in a detached tmux session.
- [x] Record the Codex session ID in Multiverse state.
- [x] Apply the Multiverse agent name to a Codex session or maintain an equivalent
      durable name mapping when Codex cannot store it natively.
- [x] Deliver an initial prompt to a newly created Codex session.
- [x] Resume an existing Codex session by ID.
- [x] Resume a dormant Codex session under its existing Multiverse name.
- [x] Auto-resume a dormant Codex session for an urgent inbox message.
- [x] End a Codex wrapper process cleanly and record `ended` status.
- [x] Kill a live Codex tmux session and remove only its Multiverse wrapper state.
- [x] Delete a dormant Codex session only after the existing confirmation flow.
- [x] Preserve the current cross-host liveness safeguards.
- [x] Prevent two wrappers from claiming the same Codex session and agent name.
- [x] Preserve sessions when the monitor exits.
- [x] Verify attach and detach behavior through tmux.
- [x] Verify tmux mouse/scroll behavior in Codex sessions.

## Session Discovery and Dashboard

- [x] Discover resumable Codex sessions for the monitor's workspace.
- [x] Distinguish active, ended, and dormant Codex sessions.
- [x] Extract or maintain a stable session name.
- [x] Extract a first-user-message preview.
- [ ] Determine whether a reliable live context/token count is available.
- [x] Display a neutral placeholder when Codex cannot provide a token count.
- [x] Add a Runtime column or another unambiguous Claude/Codex indicator.
- [x] Include Codex sessions in workspace filtering and sorting.
- [x] Deduplicate wrapped Codex sessions from the dormant-session picker.
- [x] Keep temporary Codex side-question sessions out of the dashboard and agent
      registry by using non-materialized ephemeral threads.
- [x] Ensure `Enter` attaches to a live Codex session and resumes an ended/dormant one.
- [x] Ensure `R` can filter or select resumable sessions by runtime.
- [x] Ensure `k` follows the same live/ended/dormant safety rules for Codex.

## Status Tracking

- [x] Map Codex lifecycle signals to Multiverse's common status vocabulary.
- [x] Record `starting` while a Codex process initializes.
- [x] Record `working` while Codex is processing a turn or using a tool.
- [x] Record `waiting` only for genuinely blocking user decisions.
- [x] Record `idle` after a normal completed turn.
- [x] Record `listening` while `monitor_inbox` is active.
- [x] Record `ended` when the Codex process exits.
- [x] Avoid stale live-looking statuses after crashes.
- [x] Avoid status writes from ephemeral side-question processes into the caller's
      session directory.

## MCP Tool: `list_agents`

- [x] List named Claude and Codex agents from one shared workspace registry.
- [x] Mark the calling Codex agent as `this is you`.
- [x] Preserve status-priority sorting and `max_agents` truncation.
- [x] Support cross-workspace listing with `working_directory`.
- [x] Exclude temporary side-question sessions and legacy folded ghost rows.
- [x] Return the same user-facing text shape from Claude and Codex without
      exposing each agent's runtime.

## MCP Tool: `message_agent`

- [x] Send durable Codex-to-Codex messages.
- [x] Send durable Codex-to-Claude messages.
- [x] Receive durable Claude-to-Codex messages.
- [x] Preserve the existing atomic inbox file format.
- [x] Preserve cross-workspace routing and reply instructions.
- [x] Deliver queued messages into the Codex agent's context exactly once.
- [x] Ensure delivered messages persist in the Codex conversation history.
- [x] Define Codex inbox delivery as explicit pull via `check_inbox`; passive
      lifecycle-hook delivery is not required.
- [x] Support `push_notification=True` for dormant Codex agents.
- [x] Support push delivery to live, unattached Codex agents.
- [x] Avoid clobbering input when a user is attached to the Codex terminal.
- [x] Treat an agent in `monitor_inbox` as already listening.
- [x] Report the actual delivery route in the tool result.
- [x] Preserve inter-agent send, route, and delivery logs.

## MCP Tools: `check_inbox` and `monitor_inbox`

- [x] Let a Codex agent claim and read pending messages mid-turn with
      `check_inbox`.
- [x] Preserve atomic claim semantics so racing processes cannot double-deliver.
- [x] Return the same empty-inbox response as the Claude tool.
- [x] Let a Codex agent block asynchronously in `monitor_inbox`.
- [x] Return immediately when messages are already pending.
- [x] Preserve timeout behavior and interruptibility.
- [x] Display `listening` while the Codex agent is parked.
- [x] Restore the appropriate status after the wait returns or is cancelled.

## MCP Tool: `ask_agent`

- [x] Ask a Codex agent an ephemeral question from a Codex caller.
- [x] Ask a Codex agent from a Claude caller.
- [x] Ask a Claude agent from a Codex caller.
- [x] Run the question against the target's existing conversation context.
- [x] Prevent tools and additional agentic turns in the side-question session.
- [x] Avoid interrupting or mutating the target's main session.
- [x] Enforce the configured timeout.
- [x] Clean up temporary session artifacts on success, failure, and timeout.
- [x] Register materialized Claude fork artifacts before launch; Codex forks use
      `ephemeral: true` and do not create a rollout artifact to register.
- [x] Strip caller-scoped Multiverse environment variables from the temporary
      process.
- [x] Support cross-workspace questions.
- [x] Fail the ask explicitly rather than resume or mutate the target if Codex cannot
      create a non-mutating ephemeral fork.

## MCP Tool: `spawn_agent`

- [x] Allow a Codex agent to request a fresh subagent.
- [x] Reuse the existing filesystem request and monitor approval protocol.
- [x] Show the caller runtime and proposed child runtime in the approval modal.
- [x] Let the user accept, rename, reject, or cancel the request.
- [x] Keep concurrent spawn requests FIFO and single-modal.
- [x] Preserve forced detach and automatic reattach while approval is required.
- [x] Preserve progress notifications during an indefinite approval wait.
- [x] Cancel and clean up the request when the tool call is interrupted.
- [x] Reap orphaned requests when the caller process is gone.
- [x] Start the approved Codex child with the instruction as its first prompt.
- [x] Keep long or multiline initial instructions off the shell command line.
- [x] Return the same success and rejection semantics as the Claude tool.

## Monitor Features

- [x] `n`: create a named Codex session.
- [x] `R`: resume a dormant Codex session.
- [x] `a`: ask all named agents across both runtimes.
- [x] `i`: inspect a Codex agent's pending inbox without consuming it.
- [x] `Enter`: attach to or resume the selected Codex session.
- [x] `k`: kill or delete a Codex session with the correct safety flow.
- [x] `r`: refresh mixed-runtime state.
- [x] `q`: leave all Claude and Codex sessions running.
- [x] `w`: expose the mixed-runtime monitor through the existing web share.
- [x] `S`: start and manage the existing device-level SecondBrain connector for a
      mixed-runtime workspace.
- [ ] `S`: complete the Codex query, action, and event parity detailed below.
- [x] Hide or clearly disable Claude-only compaction actions for Codex rows.
- [x] Keep `c`, `x`, `v`, and `b` unchanged for Claude rows.

## SecondBrain Integration

The completed SecondBrain implementation from `main` was integrated into the
`codex-support` branch on 2026-07-18. Codex parity targets that integrated feature
set. Protocol entries that are not implemented for Claude are tracked separately
and are not Codex parity blockers.

### Recommended Implementation Order

1. Complete and test the mixed-runtime read queries.
2. Add the read-only Codex transcript adapter and durable cursor.
3. Implement live and resumed Codex delivery for `prompt_agent`.
4. Define runtime selection and complete both SecondBrain spawn paths.
5. Emit and verify `user.prompt`, `agent.response`, and Codex spawn events.
6. Investigate structured Codex questions and permissions, then implement only the
   event/action pairs for which reliable correlation is available.
7. Run replay, deduplication, ACL, reconnect, and real-device verification across the
   completed feature set.

### Shared Connector Foundation

- [x] Use one connector daemon, device enrollment, and pinned server connection for
      Claude and Codex sessions rather than starting a connector per runtime.
- [x] Preserve the existing workspace ACL for every query, action, and emitted event.
- [x] Preserve durable event sequencing, buffering, acknowledgement, replay, and
      reconnect gap recovery for mixed-runtime events.
- [x] Preserve `LOCK`/`UNLOCK` behavior while events continue to enter the durable
      outbox.
- [x] Preserve action-id deduplication, first-writer-wins decisions, and the existing
      `ok`, `error`, and `rejected` result semantics.
- [x] Keep connector state and signal writes atomic when Claude and Codex processes
      publish concurrently.
- [x] Keep runtime out of SecondBrain payloads and preserve the established query
      result shapes.

### Read Queries

- [x] Keep `list_workspaces` runtime-neutral and scoped to the connector's granted
      workspace IDs.
- [x] Feed `get_dashboard`, `list_agents`, and `get_status` from the shared
      mixed-runtime session roster.
- [x] Make `get_dashboard` return a stable ID, common status, inbox count, updated
      timestamp, and preview for live, ended, and dormant Codex rows.
- [x] Return `"-"` for unavailable Codex token usage and keep the Claude-only
      auto-build field `off`.
- [x] Verify `list_agents` returns Codex agents with the existing name, status, inbox,
      token, and last-updated fields and no unapproved schema changes.
- [x] Verify `get_status` has identical lookup, not-found, and cross-workspace behavior
      for Claude and Codex targets.
- [x] Support `ask_agent` queries targeting Codex agents through a non-mutating
      ephemeral fork.
- [ ] Verify timeout and side-fork failure results for `ask_agent`; ACL,
      malformed-request, and not-found query behavior is covered for Codex rows.
- [ ] Treat `get_transcript` as a connector-wide future feature: it is currently not
      implemented for Claude and does not block initial Codex parity.

### Action: `prompt_agent`

- [ ] Deliver a prompt to a live, idle Codex agent without changing the action schema.
- [ ] Deliver a prompt to a live Codex agent that is already working or listening.
- [ ] Wait for a starting Codex TUI to become ready using a bounded,
      runtime-specific readiness signal.
- [ ] Resume a dormant or ended Codex agent by its Codex session ID and supply the
      action text as its resumption prompt.
- [ ] Preserve typed-but-unsent Codex terminal input during live prompt injection, or
      use a runtime API that does not modify the user's input buffer.
- [ ] Refuse unsafe delivery when the Codex agent is waiting for an answer or
      permission decision, is live on another host, or has no usable tmux/session ID.
- [ ] Preserve the existing busy, locked, waiting, not-found, delivery-failed, and
      timeout result semantics.
- [ ] Add action-id deduplication tests proving a retried prompt is delivered at most
      once to a Codex session.

### Action: `answer_question`

- [ ] Determine which Codex lifecycle event exposes a structured user question,
      options, multi-select rules, and a stable correlation ID.
- [ ] Normalize Codex questions to the existing question and answer payload schema
      without relying on Claude `AskUserQuestion` hook fields.
- [ ] Deliver single-select, multi-select, and custom-text answers through a
      Codex-specific API or validated TUI interaction.
- [ ] Correlate an answer with the pending question and reject stale, missing,
      malformed, or already-answered requests.
- [ ] Preserve foreign-host and non-waiting-state safeguards.
- [ ] If Codex cannot expose or answer structured questions reliably, document the
      capability as unsupported instead of emitting misleading events.

### Action: `answer_spawn_request`

- [x] Reuse the filesystem-backed spawn request and first-writer-wins resolution
      protocol for requests originating from either runtime.
- [ ] Define the approved child's runtime for a SecondBrain decision: inherit the
      caller runtime unless the protocol gains an explicit runtime field.
- [ ] Persist that runtime in the accepted resolution so a Codex caller does not
      accidentally spawn a Claude child.
- [ ] Verify accept, rename, reject, duplicate-decision, caller-liveness, and workspace
      mismatch behavior for Codex-originated requests.
- [ ] Verify the resulting `user.spawn_decision` and `agent.created` events preserve
      the established payload shapes.

### Action: `spawn_agent`

- [ ] Define runtime selection for a SecondBrain-initiated direct spawn, which has no
      local approval widget; require an explicit protocol value or a documented
      default.
- [ ] Start a Codex child with the requested name and optional instruction as its
      first prompt.
- [ ] Preserve workspace ACL, name uniqueness, cross-host liveness, and action-id
      deduplication.
- [ ] Return the same duplicate-name, bad-request, forbidden, and start-failure result
      semantics as the Claude path.
- [ ] Emit `agent.created` only after the Codex session is successfully materialized.

### Other Protocol Actions

- [ ] Implement connector-wide `answer_permission_prompt` before treating Codex
      permission decisions as remotely actionable; the action is not yet implemented
      for Claude either.
- [ ] Implement connector-wide `message_agent` only if durable agent-to-agent inbox
      delivery is intentionally exposed through SecondBrain; it is not implemented
      by the current connector.
- [x] Keep reserved `kill_agent`, `compact_session`, and `resume_agent` actions out of
      initial Codex parity. In particular, do not add Multiverse compaction for Codex.

### Outbound Events

- [x] Emit first-connect `workspace.connected` summaries from mixed Claude/Codex
      workspaces using runtime-aware side questions.
- [x] Queue `agent.created` from the shared successful-creation choke point for both
      runtimes.
- [ ] Add Codex-specific coverage for the `agent.created` payload and ensure failed
      or duplicate creations emit nothing.
- [ ] Emit `user.prompt` for real Codex user turns while filtering Multiverse-injected
      wrappers, local commands, and other synthetic input.
- [ ] Include the same overview-since-last and partial `full` transcript fields in a
      Codex `user.prompt` event.
- [ ] Emit `agent.response` once after a meaningful Codex turn completes normally and
      becomes idle; suppress empty, interrupted, and waiting responses.
- [x] Queue `agent.spawn_request` and `user.spawn_decision` at their shared filesystem
      publication points regardless of caller runtime.
- [ ] Verify both spawn event payloads and their ordering for a Codex caller.
- [ ] Emit paired `agent.question` and `user.answered` events only when a stable Codex
      question ID and structured answer can be correlated.
- [ ] Emit paired `agent.permission_request` and `user.permission_decision` events
      only when Codex exposes a stable tool/request ID and reliable allow/deny result.
- [ ] Keep waiting question and permission events separate from normal
      `agent.response` completion events.
- [x] Leave protocol-only `workspace.disconnected` and `user.status_changed` outside
      Codex parity until the shared connector has baseline producers for them.
- [x] Do not emit agent-to-agent inbox traffic as SecondBrain user conversation
      events.

### Codex Transcript Source

- [ ] Add a runtime-specific transcript adapter for Codex rollout history rather than
      reading Claude `full_jsonls/` or invoking the Claude compaction parser.
- [ ] Define a durable Codex cursor using the session/thread ID plus a stable rollout
      item or event position.
- [ ] Preserve the existing peek/commit rule: advance the cursor only after the
      corresponding SecondBrain event is durably queued.
- [ ] Render Codex user text, assistant text, and relevant tool calls into the same
      role-labelled `full` format expected by SecondBrain.
- [ ] Filter system scaffolding, synthetic resume prompts, inbox wrappers, and
      ephemeral side-question turns from user-facing transcript text.
- [ ] Handle resume, branch/revert behavior, partial final turns, and Codex built-in
      compaction without transcript gaps or duplicate event context.
- [ ] Keep transcript collection read-only with respect to the Codex session and do
      not create a second history mirror solely for SecondBrain.

### SecondBrain Verification

- [ ] Test all implemented queries against live, ended, and dormant Codex targets and
      a mixed-runtime workspace.
- [ ] Test live injection, starting readiness, dormant resume, user attachment,
      typed-input preservation, and duplicate action delivery for `prompt_agent`.
- [ ] Test direct and approval-based Codex spawns, runtime inheritance/selection,
      rename, rejection, interruption, and duplicate decisions.
- [ ] Test event ordering and exactly-once publication for prompt/response,
      question/answer, permission request/decision, and spawn request/decision pairs.
- [ ] Test connector restart, server reconnect, acknowledgement replay, sequence-gap
      recovery, `LOCK`/`UNLOCK`, and ACL rejection with mixed-runtime events pending.
- [ ] Test transcript cursor recovery across resume, partial turns, event queue
      failure, and Codex built-in compaction.
- [ ] Perform an end-to-end device test from a SecondBrain client without modifying
      persistent test agents unintentionally.

## Explicitly Out of Scope for Initial Codex Support

- [x] Do not implement Multiverse's custom compaction algorithm for Codex sessions.
- [x] Do not mirror Codex transcripts into `full_jsonls/` solely for compaction.
- [x] Do not build compaction trees for Codex sessions.
- [x] Do not expose Compact, Restore, Visualize, or Auto-build as functioning actions
      on Codex rows.
- [x] Do not interfere with Codex's built-in compaction behavior.
- [x] Keep all existing Claude compaction features working for Claude sessions.
- [x] Do not make conversation branching, git worktrees, or persistent agent-agent
      threads prerequisites for the first Codex release.

## Logging, Recovery, and Concurrency

- [x] Reuse `MULTIVERSE_ROOT` as the single absolute state root for both runtimes.
- [x] Keep runtime and connector cross-process coordination writes atomic, including
      session status/metadata, runtime IDs, inbox and spawn files, shared logs,
      full-history mirror synchronization, web-share records, and SecondBrain state.
- [x] Preserve inbox exactly-once claim behavior under mixed-runtime races.
- [x] Preserve host-aware liveness checks on shared storage.
- [x] Recover cleanly when a Codex process exits without a lifecycle callback.
- [x] Recover cleanly when tmux exists but the Codex process has died.
- [x] Avoid reaping a Codex session that is live on another host.
- [x] Keep runtime-specific temporary files from appearing as dormant agents.
- [x] Add enough runtime information to inter-agent logs to diagnose mixed-runtime
      delivery failures.

## Verification

Real-terminal checks for attach/detach, mouse/scroll, monitor restart,
cross-terminal resume, and MCP-result history persistence were confirmed by the
user on macOS on 2026-07-18. Automated tests use isolated state roots and fake
runtime/tmux processes so they do not create or modify persistent agents.

- [x] Unit-test legacy session metadata defaulting to Claude.
- [x] Unit-test mixed-runtime session discovery and name collision handling.
- [x] Unit-test Codex status mapping and crash correction.
- [x] Unit-test Codex inbox delivery and atomic claims.
- [x] Unit-test Codex ephemeral-session isolation and process cleanup.
- [x] End-to-end test Codex plugin loading inside a wrapped session.
- [x] End-to-end test all six MCP tools from a Codex session.
- [x] Test the communication matrix: Claude-to-Claude, Claude-to-Codex,
      Codex-to-Claude, and Codex-to-Codex.
- [x] Test passive and push delivery for live, dormant, attached, and listening agents.
- [x] Test mixed-runtime ask-all.
- [x] Test Codex spawn approval, rejection, cancellation, and orphan cleanup.
- [x] Race-test atomic replacement, first-writer-wins claims, spawn request and
      resolution publication, append-only logs, full-history sync, compaction claims,
      device-key creation, and the SecondBrain singleton lock.
- [x] Test monitor restart while Codex sessions remain live.
- [x] Test Codex resume from a different terminal.
- [ ] Test web-share attach to a Codex session.
- [ ] Test supported SecondBrain queries, events, and actions with a Codex target.
- [x] Run regression tests for the existing Claude wrapper and plugin.
- [x] Verify on macOS.
- [ ] Verify on Linux where practical.

## Documentation and Distribution

- [ ] Document installing the Codex plugin.
- [ ] Document launching a Codex session from the Multiverse wrapper.
- [ ] Document running mixed Claude/Codex workspaces.
- [ ] Document runtime-specific configuration variables.
- [ ] Document which monitor actions are Claude-only.
- [ ] Document Codex status and token-count limitations, if any.
- [ ] Update the repository layout documentation.
- [ ] Update the plugin/wrapper architecture diagram with the runtime adapter layer.
- [ ] Add Codex troubleshooting for plugin loading, session discovery, resume, and
      inbox delivery.
- [ ] Define versioning and release packaging for the Codex plugin.

## Open Decisions for Review

- [x] Default the New Session modal to Claude while presenting a runtime selector.
- [x] Put runtime selection in the New Session modal; no launch flag is required yet.
- [x] Make `spawn_agent` inherit the caller's runtime.
- [x] Use an explicit Runtime column in the dashboard.
- [x] Share one agent-name namespace across runtimes within a workspace.
- [ ] What Codex-native session metadata can be treated as stable public API?
- [x] If Codex cannot provide a non-mutating ephemeral fork, return an explicit ask
      error; never fall back to resuming or mutating the target thread.
- [ ] Which Codex lifecycle events are reliable enough to drive status and
      SecondBrain observation without terminal scraping?
