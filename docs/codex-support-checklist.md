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
- [ ] Give the tools the same names, argument shapes, and response semantics as the
      Claude plugin where Codex permits it.
- [x] Ensure plugin paths resolve independently of the user's current directory.
- [x] Ensure the plugin receives the session-scoped `MULTIVERSE_*` environment.
- [x] Ensure the plugin is a no-op or returns a clear error when used outside the
      Multiverse wrapper.
- [ ] Add Codex-side instructions that explain when to use durable messages, side
      questions, inbox monitoring, and fresh-agent spawning.
- [x] Determine which Claude hooks have direct Codex equivalents.
- [x] Implement non-hook fallbacks for required lifecycle or inbox behavior where
      Codex has no equivalent extension point.

## Wrapper Configuration and Setup

- [x] Add a configurable Codex executable, such as `MULTIVERSE_CODEX_BIN`.
- [x] Preserve `MULTIVERSE_CLAUDE_BIN` and existing Claude configuration behavior.
- [ ] Extend `multiverse setup` to detect and report Codex prerequisites.
- [ ] Persist or restore the Codex configuration needed to resume sessions from a
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
- [ ] Delete a dormant Codex session only after the existing confirmation flow.
- [x] Preserve the current cross-host liveness safeguards.
- [ ] Prevent two wrappers from claiming the same Codex session and agent name.
- [x] Preserve sessions when the monitor exits.
- [ ] Verify attach and detach behavior through tmux.
- [ ] Verify tmux mouse/scroll behavior in Codex sessions.

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
- [ ] Keep temporary Codex side-question sessions out of the dashboard and agent
      registry.
- [x] Ensure `Enter` attaches to a live Codex session and resumes an ended/dormant one.
- [x] Ensure `R` can filter or select resumable sessions by runtime.
- [ ] Ensure `k` follows the same live/ended/dormant safety rules for Codex.

## Status Tracking

- [x] Map Codex lifecycle signals to Multiverse's common status vocabulary.
- [x] Record `starting` while a Codex process initializes.
- [x] Record `working` while Codex is processing a turn or using a tool.
- [x] Record `waiting` only for genuinely blocking user decisions.
- [x] Record `idle` after a normal completed turn.
- [x] Record `listening` while `monitor_inbox` is active.
- [x] Record `ended` when the Codex process exits.
- [ ] Avoid stale live-looking statuses after crashes.
- [ ] Avoid status writes from ephemeral side-question processes into the caller's
      session directory.

## MCP Tool: `list_agents`

- [x] List named Claude and Codex agents from one shared workspace registry.
- [x] Mark the calling Codex agent as `this is you`.
- [x] Preserve status-priority sorting and `max_agents` truncation.
- [x] Support cross-workspace listing with `working_directory`.
- [ ] Exclude temporary side-question sessions and legacy folded ghost rows.
- [x] Return the same user-facing text shape from Claude and Codex without
      exposing each agent's runtime.

## MCP Tool: `message_agent`

- [x] Send durable Codex-to-Codex messages.
- [x] Send durable Codex-to-Claude messages.
- [x] Receive durable Claude-to-Codex messages.
- [x] Preserve the existing atomic inbox file format.
- [x] Preserve cross-workspace routing and reply instructions.
- [x] Deliver queued messages into the Codex agent's context exactly once.
- [ ] Ensure delivered messages persist in the Codex conversation history.
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

- [ ] Ask a Codex agent an ephemeral question from a Codex caller.
- [ ] Ask a Codex agent from a Claude caller.
- [ ] Ask a Claude agent from a Codex caller.
- [ ] Run the question against the target's existing conversation context.
- [ ] Prevent tools and additional agentic turns in the side-question session.
- [ ] Avoid interrupting or mutating the target's main session.
- [ ] Enforce the configured timeout.
- [ ] Clean up temporary session artifacts on success, failure, and timeout.
- [ ] Register temporary artifacts before launch so the dashboard can hide them.
- [ ] Strip caller-scoped Multiverse environment variables from the temporary
      process.
- [ ] Support cross-workspace questions.
- [ ] Define an explicit fallback if Codex cannot fork an existing session without
      mutating it.

## MCP Tool: `spawn_agent`

- [x] Allow a Codex agent to request a fresh subagent.
- [x] Reuse the existing filesystem request and monitor approval protocol.
- [ ] Show the caller runtime and proposed child runtime in the approval modal.
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
- [ ] `a`: ask all named agents across both runtimes.
- [x] `i`: inspect a Codex agent's pending inbox without consuming it.
- [x] `Enter`: attach to or resume the selected Codex session.
- [ ] `k`: kill or delete a Codex session with the correct safety flow.
- [x] `r`: refresh mixed-runtime state.
- [x] `q`: leave all Claude and Codex sessions running.
- [x] `w`: expose the mixed-runtime monitor through the existing web share.
- [ ] `S`: expose Codex sessions through the SecondBrain connector.
- [x] Hide or clearly disable Claude-only compaction actions for Codex rows.
- [x] Keep `c`, `x`, `v`, and `b` unchanged for Claude rows.

## SecondBrain Integration

SecondBrain is still being developed on `main`; Codex parity should target the
feature set present when the branches are integrated.

- [ ] Include Codex sessions in `get_dashboard`, `list_agents`, and `get_status`.
- [ ] Include runtime information in SecondBrain session data if the protocol permits.
- [ ] Support SecondBrain `ask_agent` queries targeting Codex agents.
- [ ] Support SecondBrain `prompt_agent` actions targeting live Codex agents.
- [ ] Support SecondBrain `prompt_agent` actions that resume dormant Codex agents.
- [ ] Preserve typed-but-unsent Codex terminal input when injecting a live prompt, or
      use a safer runtime API if available.
- [ ] Emit `workspace.connected` summaries from mixed Claude/Codex workspaces.
- [ ] Emit `agent.created` for new Codex agents.
- [ ] Emit `user.prompt` for Codex user turns.
- [ ] Emit `agent.response` when Codex finishes a normal turn.
- [ ] Emit question/answer and permission request/decision events where Codex exposes
      the necessary lifecycle data.
- [ ] Render partial Codex transcripts for SecondBrain without depending on the Claude
      JSONL parser.
- [ ] Preserve durable event buffering, replay, and workspace ACL behavior.

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
- [ ] Keep all cross-process writes atomic.
- [x] Preserve inbox exactly-once claim behavior under mixed-runtime races.
- [x] Preserve host-aware liveness checks on shared storage.
- [ ] Recover cleanly when a Codex process exits without a lifecycle callback.
- [ ] Recover cleanly when tmux exists but the Codex process has died.
- [x] Avoid reaping a Codex session that is live on another host.
- [ ] Keep runtime-specific temporary files from appearing as dormant agents.
- [x] Add enough runtime information to inter-agent logs to diagnose mixed-runtime
      delivery failures.

## Verification

- [x] Unit-test legacy session metadata defaulting to Claude.
- [x] Unit-test mixed-runtime session discovery and name collision handling.
- [ ] Unit-test Codex status mapping and crash correction.
- [x] Unit-test Codex inbox delivery and atomic claims.
- [ ] Unit-test Codex temporary-session registration and cleanup.
- [ ] End-to-end test Codex plugin loading inside a wrapped session.
- [ ] End-to-end test all six MCP tools from a Codex session.
- [x] Test the communication matrix: Claude-to-Claude, Claude-to-Codex,
      Codex-to-Claude, and Codex-to-Codex.
- [x] Test passive and push delivery for live, dormant, attached, and listening agents.
- [ ] Test mixed-runtime ask-all.
- [ ] Test Codex spawn approval, rejection, cancellation, and orphan cleanup.
- [ ] Test monitor restart while Codex sessions remain live.
- [ ] Test Codex resume from a different terminal.
- [ ] Test web-share attach to a Codex session.
- [ ] Test supported SecondBrain queries, events, and actions with a Codex target.
- [ ] Run regression tests for the existing Claude wrapper and plugin.
- [ ] Verify on macOS and Linux where practical.

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
- [ ] What is the required semantic fallback if Codex cannot provide a non-mutating
      fork for `ask_agent`?
- [ ] Which Codex lifecycle events are reliable enough to drive status and
      SecondBrain observation without terminal scraping?
