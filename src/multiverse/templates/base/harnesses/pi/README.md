# Pi

Native interactive spawn/resume uses the exact agent session file and passes
`--name` with the mv2 agent name, with or without the bridge. The `session_name`
capability reports this existing naming support. Enable
`settings.inbox_bridge` to load the source-owned `harnesses/pi/extension.ts`.
It accepts native steer/follow-up requests over a private local socket, reports
status/session identity, and supports receipt checks. The model receives the
sender line and message text; receipt metadata stays outside the model input.
Core owns storage, delivery tracking and retries. `mv2 inbox` shows unread
inbox-only messages and marks them read automatically. `mv2 inbox --all` browses
full raw history. Use `mv2 deliver AGENT --check` to reconcile uncertain native
delivery and `--retry` to retry definite failures.

Native acceptance is not proof of reading or answering. Isolated ask and usage
remain explicitly unavailable until implemented and tested.
