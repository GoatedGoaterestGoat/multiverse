# Antigravity CLI

Native interactive spawn and resume load the mv2 bootstrap via `--add-dir`
(a launch-private `AGENTS.md`) and track the session by its reported
Antigravity conversation UUID (`--conversation`).

By default (`inbox_bridge` unset or `true`), mv2 also passes a session-only
`--settings` file (`statusLine`) and `.agents/hooks.json` (`SessionStart`,
`PreInvocation`, `PostInvocation`, `Stop`). Live status, usage, and
conversation binding start immediately. The hooks inject mv2 messages as real
user messages:

- a busy `steer` goes in before the agent's next model call
- a busy `follow-up` goes in when the turn ends
- an idle agent is woken with a fixed ping through `agy agentapi send-message`,
  which works after its first `mv2` shell command (such as `mv2 report`)
- `mv2 deliver AGENT --check` confirms delivery against `transcript.jsonl`

Set `settings.inbox_bridge` to `false` to launch plain without hooks,
statusLine, or live delivery. Steer/follow-up to a stopped or dormant agent
wakes it by resuming its exact conversation with `--prompt-interactive=...`.
