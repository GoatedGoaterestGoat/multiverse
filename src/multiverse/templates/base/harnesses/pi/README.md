# Pi adapter

Targets Pi 0.84.3. [Upstream docs](https://github.com/earendil-works/pi/tree/main/packages/coding-agent).

The manifest launches an interactive Pi process in tmux, appends the universe's short bootstrap to Pi's usual system prompt, and uses a persistent session file. Pi's own tools, permission settings and project context still apply. Universe skills are discovered separately through `mv2 catalog`.

The default manifest does **not** load `extension.ts`. Like the Codex and Claude adapters, plain Pi follows the inbox instructions in the prompt. Adding a harness requires only its JSON manifest; it does not require TypeScript.

The bundled extension is optional. To enable it, add `"--extension", "{harness_dir}/extension.ts"` to both the `spawn` and `resume` arrays in `harness.json`. It adds no model tools. It observes lifecycle events and polls the durable inbox every 1.5 seconds while idle, using Pi's native `sendMessage` API. Messages wait during an active turn; urgent steering and out-of-band ask are not implemented. `/multiverse list` shows the catalog; `/multiverse review ...` loads the exact prompt. Unknown or ambiguous names never run a workflow. These automatic features apply only when the extension is enabled.

Queued means written to disk. The cursor means injected into Pi, not read, answered or completed. A crash around injection can produce a duplicate. Use message IDs and `reply_to` when correlating work. The TUI need not be running for delivery.

If delivery fails, the extension displays an error and leaves unread messages queued. Check `mv2 inspect`, `mv2 inbox`, `runtime.json`, and Pi's extension diagnostics. An idle pane alone does not prove Pi is ready. After updating an installed universe's adapter, resume the agent to load it; existing universes are deliberately not silently upgraded.
