# Codex

Use `"$MV2_CLI"` for mv2 commands. Check `"$MV2_CLI" inbox` at startup and between work steps. Report your progress with `"$MV2_CLI" report --phase working --summary 'Your progress'`. Use phase `done` when the task is complete.

A connected Codex conversation is one mv2 agent, whichever Codex client drives it. Its tools use that agent's identity. Do not copy or replace its `MV2_*` variables. When you launch child agents, mv2 finds the current parent terminal's server from the agent record.

Use `"$MV2_CLI" capabilities --agent NAME` to check message support. Connected launches receive messages directly; launch-only agents read their inbox. Add `--wait` to a spawn or resume when another agent must be ready before you send work.

The saved native conversation belongs to this agent. Start another mv2 agent for a separate conversation. Native `/new`, `/clear` and navigation to another conversation are refused on a connected launch.

Native `/btw` opens an unmanaged side chat; mv2 messages still go to the main conversation. The side inherits your tools and permissions and can run mv2 as you. It is not isolated or tracked as another agent.

A shared conversation keeps its current Codex permissions. If a requested permission differs, inspect the launch error and use supported Codex controls to change it deliberately. mv2 does not detach other clients or restart the shared Codex server to change permissions.

Use `"$MV2_CLI" inspect NAME` for connection or startup errors. A refused launch stays refused even if a retry inside Codex succeeds. Resume the agent through mv2 to validate a fresh attachment.
