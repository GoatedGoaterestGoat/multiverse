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

Steer/follow-up to a stopped or dormant agent resumes its exact conversation with the framed sender and text as an interactive command-line prompt, using Pi's literal positional prompt after `--`, alongside the saved session file. The `wake` capability is supported in plain and bridge modes. No mod/extension delivery is needed for this initial prompt. Process startup is recorded as accepted, with consumption unconfirmed. Additional messages during bridge startup remain pending until registration, then drain in order. A failed startup leaves saved input for explicit retry; accepted or uncertain prompts are never replayed. Live plain sessions still cannot receive native delivery. Inbox-only never wakes. Dashboard `m` uses this same send path.
