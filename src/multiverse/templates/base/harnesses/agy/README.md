# Antigravity CLI

Native interactive spawn and resume load the mv2 bootstrap via `--add-dir`
(a launch-private `AGENTS.md`) and track the session by its reported
Antigravity conversation UUID (`--conversation`).

By default (`inbox_bridge` unset or `true`), mv2 also passes a session-only
`--settings` file (`statusLine`) and `.agents/hooks.json` (`SessionStart`,
`PreInvocation`, `Stop`). Live status, usage, and conversation binding start
immediately; live message delivery activates on the agent's first `mv2` shell
command (such as `mv2 report`):

- `steer` (and idle `follow-up`) submit through `agy agentapi send-message`
- busy `follow-up` queues for end-of-turn delivery through the `Stop` hook
- `mv2 deliver AGENT --check` confirms delivery against `transcript.jsonl`

Set `settings.inbox_bridge` to `false` to launch plain without hooks,
statusLine, or live delivery. Steer/follow-up to a stopped or dormant agent
wakes it by resuming its exact conversation with `--prompt-interactive=...`.
