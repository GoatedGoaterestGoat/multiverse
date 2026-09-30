# Pi

Native interactive spawn/resume uses the exact agent session file. Enable
`settings.inbox_bridge` to load the source-owned `harnesses/pi/extension.ts`.
It accepts native steer/follow-up requests over a private local socket, preserves
message IDs, reports status/session identity, and supports receipt checks.
There is no inbox polling or automatic acknowledgement. Core owns message
storage, delivery tracking and retries; the agent acknowledges individual IDs.

Native acceptance is not proof of reading or answering. Isolated ask and usage
remain explicitly unavailable until implemented and tested.
