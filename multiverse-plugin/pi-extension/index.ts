/**
 * Multiverse for pi.
 *
 * pi's counterpart to the Claude Code plugin and the Codex plugin: it exposes
 * the inter-agent tools, writes the session status the monitor dashboard
 * reads, records the native session id, delivers inbox messages, and connects
 * pi's prompt/response lifecycle to SecondBrain.
 *
 * ## How it talks to Multiverse
 *
 * All shared state — the inbox, spawn brokering, session metadata, ask-forks
 * — lives on disk under `~/.multiverse` and is implemented once, in Python,
 * in `src/`. Rather than write a second implementation of that on-disk
 * contract in TypeScript (two copies, free to drift), this extension shells
 * out to `mvctl` (`src/mvctl.py`), a thin CLI over the exact functions the
 * MCP server calls.
 *
 * The cost is one short-lived process per tool call, which is invisible next
 * to a model round-trip. The benefit is that a fix to delivery routing or
 * inbox claiming lands on every runtime at once.
 *
 * ## Compaction
 *
 * pi compacts natively. This extension does not intercept
 * `session_before_compact` and holds no compaction code — same as the Codex
 * runtime. `reserveTokens` / `keepRecentTokens` in pi's own settings, and
 * `/compact`, are the whole story.
 *
 * ## What pi gives us that Claude Code does not
 *
 * `pi.sendMessage(..., { triggerTurn: true })` delivers into a running
 * session, so an inbox message reaches an agent sitting idle at its prompt
 * without typing into its terminal. There is no tmux `send-keys` path here
 * and no carve-out for when a user is attached.
 *
 * Loaded by `bin/multiverse-pi-session`, the dedicated runner the monitor
 * execs for a pi-runtime session; standalone use is
 * `pi --extension <plugin_root>/pi-extension/index.ts`.
 */

import { existsSync } from "node:fs";
import { join } from "node:path";
import type { ExtensionAPI, ExtensionContext } from "@earendil-works/pi-coding-agent";
import { Type } from "typebox";

// ---------------------------------------------------------------------------
// mvctl bridge
// ---------------------------------------------------------------------------

const PLUGIN_ROOT = process.env.MULTIVERSE_PLUGIN_ROOT ?? "";
const SESSION_NAME = process.env.MULTIVERSE_SESSION_NAME ?? "";
const SESSION_CWD = process.env.MULTIVERSE_SESSION_CWD ?? "";

/** True when the monitor spawned us and the shared state dir is addressable. */
const WRAPPED = Boolean(PLUGIN_ROOT && SESSION_NAME && SESSION_CWD);

/** ``~/.multiverse`` as src/paths.py resolves it, or "" if it can't be derived. */
function multiverseDir(): string {
	const explicit = process.env.MULTIVERSE_ROOT;
	if (explicit) return explicit;
	const status = process.env.MULTIVERSE_STATUS;
	// .../.multiverse/sessions/<id>/status.json → .../.multiverse/
	if (status) return join(status, "..", "..", "..");
	const home = process.env.HOME;
	return home ? join(home, ".multiverse") : "";
}

/**
 * Whether a SecondBrain signal could be recorded at all.
 *
 * Every `record_*_signal` in sb_notify is gated on this device being enrolled
 * and the workspace having connected before; with no `state.json` the recorder
 * is a guaranteed no-op. Checking that here turns two Python interpreter
 * startups per turn — one for the prompt, one for the response, each importing
 * the sb_notify chain — into one `stat` for every session in a workspace that
 * has never used SecondBrain, which is the common case.
 *
 * Deliberately re-checked per call rather than cached: a user can enrol partway
 * through a session, and a stale `false` would silently drop their signals. The
 * check is intentionally coarse — it only skips work that provably cannot have
 * an effect, and leaves the authoritative seeded/exposed gate in Python.
 */
function secondBrainMayRecord(): boolean {
	const root = multiverseDir();
	if (!root) return false;
	try {
		return existsSync(join(root, "secondbrain", "state.json"));
	} catch {
		return false;
	}
}

/** Default ceiling for a bridge call. Long-running commands opt out. */
const DEFAULT_TIMEOUT_MS = 120_000;

/**
 * Inbox delivery only makes sense in a session that outlives one turn.
 *
 * `print` and `json` are one-shot: pi runs the prompt and exits. Delivering
 * there would consume a message into a process that is about to disappear,
 * and waking it with `triggerTurn` starts a turn the run was never going to
 * wait for — which in practice hangs the invocation. A multiverse pi session
 * is always the interactive one, so this costs nothing operationally.
 */
function isLongLived(ctx: ExtensionContext): boolean {
	return ctx.mode === "tui" || ctx.mode === "rpc";
}

type Exec = ExtensionAPI["exec"];

/**
 * Build the argv for an mvctl invocation.
 *
 * Prefers the plugin's own virtualenv interpreter over `uv run`: `uv run`
 * re-resolves the lockfile on every call, which turns a ~70 ms exec into a
 * ~400 ms one, and these run on the interactive path.
 */
function mvctlArgv(args: string[]): { cmd: string; argv: string[] } {
	const venvPython = join(PLUGIN_ROOT, ".venv", "bin", "python");
	if (existsSync(venvPython)) {
		return { cmd: venvPython, argv: ["-m", "src.mvctl", ...args] };
	}
	return { cmd: "uv", argv: ["run", "python", "-m", "src.mvctl", ...args] };
}

interface MvctlResult {
	ok: boolean;
	text: string;
}

/**
 * Run one mvctl command.
 *
 * `timeout: null` means no client-side deadline at all — used for the
 * commands whose whole point is to block until a human acts.
 */
async function mvctl(
	exec: Exec,
	args: string[],
	opts: { timeout?: number | null; signal?: AbortSignal } = {},
): Promise<MvctlResult> {
	if (!PLUGIN_ROOT) {
		return { ok: false, text: "multiverse: MULTIVERSE_PLUGIN_ROOT is not set" };
	}
	const { cmd, argv } = mvctlArgv(args);
	const timeout = opts.timeout === undefined ? DEFAULT_TIMEOUT_MS : opts.timeout;
	try {
		const r = await exec(cmd, argv, {
			cwd: PLUGIN_ROOT,
			...(timeout === null ? {} : { timeout }),
			signal: opts.signal,
		});
		if (r.code !== 0) {
			return { ok: false, text: (r.stderr || r.stdout || `mvctl exited ${r.code}`).trim() };
		}
		return { ok: true, text: (r.stdout ?? "").trim() };
	} catch (err) {
		return { ok: false, text: `multiverse: ${String(err)}` };
	}
}

/** Fire-and-forget bridge call; bookkeeping must never break a turn. */
function mvctlDetached(exec: Exec, args: string[]): void {
	void mvctl(exec, args, { timeout: 15_000 }).catch(() => undefined);
}

function toolText(text: string) {
	return { content: [{ type: "text" as const, text }], details: {} };
}

// ---------------------------------------------------------------------------

export default function multiverse(pi: ExtensionAPI) {
	const exec = pi.exec.bind(pi);

	// ----- status ----------------------------------------------------------
	//
	// The dashboard's status column is driven entirely by status.json. On
	// Claude Code a shell script writes it from six hooks; here the same
	// values come from the agent lifecycle directly.
	//
	// `waiting` has no pi analogue: pi has no permission gate, so a pi agent
	// never blocks on user approval. It moves between idle / working /
	// listening / ended only.

	// Status writes are SERIALIZED, not fire-and-forget. Each one is its own
	// Python process, so racing them means the OS decides which lands last: the
	// `idle` from agent_settled could outlive pi and overwrite
	// session_shutdown's `ended`, leaving a dead agent rendered idle — and
	// message_agent then routes to it as a live target. Chaining makes the
	// order here match the order the events fired. mvctl's writer holds a
	// cross-process lock and refuses a live status over `ended` as well, which
	// is what covers a racer outside this process.
	//
	// `ended` is additionally terminal in-process once pi itself is quitting:
	// a late lifecycle event must not queue another live status behind it.
	//
	// Consecutive identical values are dropped. Every write costs an
	// interpreter startup plus the src.sessions import chain, and
	// `tool_execution_start` fires once per tool call — a 30-tool turn spawned
	// 30 processes to rewrite the same `working`. Only transitions matter.
	let sessionEnded = false;
	let lastStatus: string | undefined;
	let statusChain: Promise<void> = Promise.resolve();
	const setStatus = (status: string): Promise<void> => {
		if (sessionEnded || status === lastStatus) return statusChain;
		if (status === "ended") sessionEnded = true;
		lastStatus = status;
		statusChain = statusChain
			.then(async () => {
				const r = await mvctl(exec, ["status", status], { timeout: 15_000 });
				// A failed write must not be remembered as the current value,
				// or the dedupe above would suppress every retry of it and the
				// session would keep displaying whatever it last managed to
				// write.
				if (!r.ok) {
					if (lastStatus === status) lastStatus = undefined;
					if (status === "ended") sessionEnded = false;
				}
			})
			.catch(() => undefined);
		return statusChain;
	};

	pi.on("session_start", async (_event, ctx) => {
		const id = ctx.sessionManager.getSessionId();
		if (id) mvctlDetached(exec, ["record-session", id]);
		if (WRAPPED) {
			// Versioned proof for the connector's live prompt path. Await before
			// advertising idle: an older already-running extension never calls this
			// command, so the connector can reject instead of queueing forever.
			const capability = await mvctl(exec, ["record-pi-prompt-consumer"], {
				timeout: 15_000,
			});
			if (!capability.ok && ctx.hasUI) {
				ctx.ui.notify(
					`multiverse: could not advertise SecondBrain prompt delivery (${capability.text})`,
					"warning",
				);
			}
		}
		setStatus("idle");
		// Deliberately NOT delivering here. A session started with an initial
		// prompt (`pi "task"`, which is how spawn_agent hands a subagent its
		// work) submits that prompt right after session_start returns; a
		// delivery that triggers its own turn first makes pi reject the
		// initial prompt outright ("Agent is already processing"). The watcher
		// below picks up anything pending once the agent is genuinely idle,
		// which is a couple of seconds later and collides with nothing.
		if (WRAPPED && isLongLived(ctx)) startInboxWatcher(pi, ctx);
	});

	// SecondBrain observes real pi user turns through the same durable signal
	// queue as the Claude/Codex hooks. This includes prompts injected by the
	// SecondBrain queue below: sendUserMessage emits input with source=extension.
	pi.on("input", async (event) => {
		if (!WRAPPED || !event.text || !secondBrainMayRecord()) return;
		await mvctl(
			exec,
			[
				"secondbrain-user-prompt",
				"--text-base64",
				Buffer.from(event.text, "utf8").toString("base64"),
			],
			{ timeout: 15_000 },
		);
	});

	pi.on("before_agent_start", async () => setStatus("working"));
	pi.on("agent_start", async () => setStatus("working"));
	pi.on("tool_execution_start", async () => setStatus("working"));

	pi.on("agent_settled", async (_event, ctx) => {
		setStatus("idle");
		if (WRAPPED && secondBrainMayRecord()) {
			// Record the completed response before starting any queued continuation,
			// so SecondBrain's transcript cursor cannot fold two turns together.
			await mvctl(exec, ["secondbrain-response"], { timeout: 15_000 });
		}
		// End-of-turn is the natural delivery point: anything that arrived
		// while we were working gets picked up without interrupting the run.
		// A SecondBrain prompt is a real user turn and takes priority over inbox
		// context; the latter will be delivered when that turn settles.
		if (WRAPPED && isLongLived(ctx)) {
			const prompted = await deliverSecondBrainPrompts(pi, ctx).catch(() => false);
			if (!prompted) {
				await deliverInbox(pi, ctx, { trigger: true }).catch(() => undefined);
			}
		}
	});

	pi.on("session_shutdown", async (event) => {
		stopInboxWatcher();
		// Clear for every replacement flow, not only quit. The replacement
		// extension instance writes a fresh marker from its own session_start;
		// if it fails to load, no stale capability survives to accept prompts.
		if (WRAPPED) {
			await mvctl(exec, ["clear-pi-prompt-consumer"], { timeout: 15_000 });
		}
		if (event.reason === "quit") {
			// Awaited: the runner's fallback also writes `ended`, but this is the
			// path that runs while pi is still alive to be waited on.
			await setStatus("ended");
		} else {
			// `/new`, `/resume`, `/fork`, and reload replace the session inside
			// the same live pi process. Marking the wrapper ended here would make
			// Python's terminal-status guard reject the replacement's idle write.
			await statusChain;
		}
	});

	// ----- inter-agent tools ----------------------------------------------
	//
	// Only registered under the wrapper. Standalone (`pi --extension …`) the
	// state dir the tools coordinate through does not exist, so offering them
	// would just hand the model tools that always fail.

	if (!WRAPPED) return;

	pi.registerTool({
		name: "set_description",
		label: "Set description",
		description:
			"Set or update YOUR OWN very short, concise agent description. Summarize " +
			"responsibilities, completed tasks, ongoing implementations, or expertise " +
			"so other agents and the user can see at first glance what to delegate to you. " +
			"Hard limit: 60 Unicode characters, one line, no control characters. " +
			"Over-limit calls fail explicitly and leave the existing description unchanged. " +
			"Shown in list_agents and the dashboard. Call at your discretion when ready " +
			"and whenever your work changes to keep it updated. New agents have none. " +
			"Empty string clears it. You cannot change another agent's description.",
		promptSnippet: "Keep your short work/role description current for delegation",
		parameters: Type.Object({
			description: Type.String({
				description: "Concise work/role summary, at most 60 characters; empty clears it.",
			}),
		}),
		async execute(_id, params, signal) {
			return toolText((await mvctl(exec, [
				"set-description", `--description=${params.description}`,
			], { signal })).text);
		},
	});

	pi.registerTool({
		name: "list_agents",
		label: "List agents",
		description:
			"List the named agents in your Multiverse workspace — the ones you can " +
			"reach with message_agent and ask_agent. Shows each agent's short description " +
			"(or None when unset), and status " +
			"(idle / working / listening / ended / retired / dormant). Your own entry is " +
			"marked. Pass working_directory to list a different workspace instead.",
		promptSnippet: "Discover other agents you can talk to",
		parameters: Type.Object({
			max_agents: Type.Optional(
				Type.Integer({ description: "Entries before truncating. Default 15." }),
			),
			working_directory: Type.Optional(
				Type.String({
					description:
						"Absolute path of another workspace to list. Omit for your own.",
				}),
			),
		}),
		async execute(_id, params, signal) {
			const args = ["list-agents", "--max", String(params.max_agents ?? 15)];
			if (params.working_directory) args.push("--cwd", params.working_directory);
			return toolText((await mvctl(exec, args, { signal })).text);
		},
	});

	pi.registerTool({
		name: "message_agent",
		label: "Message agent",
		description:
			"Send a durable message to another named agent. Use this for " +
			"instructions, in-depth questions, or anything the recipient should be " +
			"able to use tools to act on — they get the message in their " +
			"conversation and can take their time. Delivery is active: a dormant " +
			"recipient is woken and a live one receives it once its current turn settles. " +
			"A retired recipient rejects the message and identifies its successor. " +
			"Fire-and-forget — this returns as soon as the message is queued. To " +
			"reply to someone, call message_agent back at them; there is no " +
			"separate respond tool. Pass working_directory to reach an agent in a " +
			"different workspace.",
		promptSnippet: "Send another agent a message they can act on with tools",
		parameters: Type.Object({
			target_name: Type.String({ description: "Name of the recipient agent." }),
			message: Type.String({ description: "What to send." }),
			working_directory: Type.Optional(
				Type.String({
					description:
						"Absolute path of the recipient's workspace. Omit for your own.",
				}),
			),
		}),
		async execute(_id, params, signal) {
			const args = ["message", "--to", params.target_name, "--body", params.message];
			if (params.working_directory) args.push("--cwd", params.working_directory);
			return toolText((await mvctl(exec, args, { signal })).text);
		},
	});

	pi.registerTool({
		name: "ask_agent",
		label: "Ask agent",
		description:
			"Ask another agent a quick question they can answer from what they " +
			"already know. The answer comes back inline. The target answers from " +
			"its existing context with NO tools, its own session is not touched, " +
			"and nothing is added to its history — so this works whether it is " +
			"idle, mid-task, or dormant. For anything needing tools or follow-up, " +
			"use message_agent instead.",
		promptSnippet: "Ask another agent a question answerable from their context",
		parameters: Type.Object({
			target_name: Type.String({ description: "Name of the agent to ask." }),
			question: Type.String({ description: "The question." }),
			timeout_seconds: Type.Optional(
				Type.Integer({ description: "Seconds to wait. Default 60." }),
			),
			working_directory: Type.Optional(
				Type.String({
					description:
						"Absolute path of the target's workspace. Omit for your own.",
				}),
			),
		}),
		async execute(_id, params, signal) {
			const timeout = params.timeout_seconds ?? 60;
			const args = [
				"ask",
				"--to", params.target_name,
				"--question", params.question,
				"--timeout", String(timeout),
			];
			if (params.working_directory) args.push("--cwd", params.working_directory);
			return toolText(
				(
					await mvctl(exec, args, {
						signal,
						// Past mvctl's own deadline, so we surface the target's
						// timeout message instead of killing the bridge process.
						timeout: (timeout + 30) * 1000,
					})
				).text,
			);
		},
	});

	pi.registerTool({
		name: "check_inbox",
		label: "Check inbox",
		description:
			"Read and clear any inter-agent messages waiting for you, without " +
			"waiting for one to be delivered at the end of your turn. Useful right " +
			"after you sent a message and suspect a reply landed, or to drain what " +
			"piled up during a long task.",
		promptSnippet: "Read pending inter-agent messages now",
		parameters: Type.Object({}),
		async execute(_id, _params, signal) {
			return toolText((await mvctl(exec, ["check-inbox"], { signal })).text);
		},
	});

	pi.registerTool({
		name: "monitor_inbox",
		label: "Monitor inbox",
		description:
			"Block until a new inter-agent message arrives, then tell you to read " +
			"it with check_inbox. Park here when you have finished your work but " +
			"are expecting a reply, instead of polling check_inbox. Returns " +
			"immediately if something is already pending. While parked your status " +
			"reads 'listening', so the user and other agents can see you are " +
			"waiting rather than busy.",
		promptSnippet: "Wait for the next inter-agent message",
		parameters: Type.Object({
			timeout_seconds: Type.Optional(
				Type.Integer({ description: "Seconds to wait. Default 300." }),
			),
		}),
		async execute(_id, params, signal) {
			const timeout = params.timeout_seconds ?? 300;
			// The watcher would otherwise claim the very message this call is
			// parked on, and monitor_inbox would time out while the message sat
			// already delivered in the transcript.
			suspendWatcher();
			try {
				const r = await mvctl(exec, ["monitor-inbox", "--timeout", String(timeout)], {
					signal,
					timeout: (timeout + 30) * 1000,
				});
				return toolText(r.text);
			} finally {
				resumeWatcher();
			}
		},
	});

	pi.registerTool({
		name: "spawn_agent",
		label: "Spawn agent",
		description:
			"Start a brand-new agent with a fresh context and hand it a starting " +
			"task. Use this to delegate to a clean slate rather than interrupt an " +
			"existing agent. The new agent has NO memory of your conversation, so " +
			"put everything it needs in the instruction. The user approves and " +
			"names it from the monitor before it is created; this blocks until " +
			"they decide, however long that takes.",
		promptSnippet: "Delegate a task to a brand-new agent with a fresh context",
		parameters: Type.Object({
			instruction: Type.String({
				description: "The new agent's first message. Self-contained.",
			}),
			name: Type.Optional(
				Type.String({ description: "Suggested name; the user may change it." }),
			),
		}),
		async execute(_id, params, signal) {
			const args = ["spawn", "--instruction", params.instruction];
			if (params.name) args.push("--name", params.name);
			// No deadline: approval waits for a human, and main removed the
			// spawn timeout precisely so a slow approval is not an error. A
			// client-side timeout here would reintroduce it, and would orphan a
			// spawn request the user is still looking at.
			return toolText((await mvctl(exec, args, { signal, timeout: null })).text);
		},
	});

	// ----- commands --------------------------------------------------------

	pi.registerCommand("mv-inbox", {
		description: "Show pending Multiverse inter-agent messages",
		handler: async (_args, ctx) => {
			const r = await mvctl(exec, ["check-inbox"]);
			ctx.ui.notify(r.text || "(nothing pending)", r.ok ? "info" : "warning");
		},
	});

	pi.registerCommand("mv-agents", {
		description: "List agents in this Multiverse workspace",
		handler: async (args, ctx) => {
			const r = await mvctl(exec, ["list-agents", "--max", args?.trim() || "15"]);
			ctx.ui.notify(r.text, r.ok ? "info" : "warning");
		},
	});
}

// ---------------------------------------------------------------------------
// SecondBrain prompt delivery
// ---------------------------------------------------------------------------

/**
 * Deliver claimed SecondBrain actions as actual pi user messages.
 *
 * This is deliberately separate from the inter-agent inbox: SecondBrain acts
 * on the user's behalf, so its prompt must have role=user. Claims are committed
 * only after sendUserMessage accepts them, with immediate release on failure.
 */
async function deliverSecondBrainPrompts(
	pi: ExtensionAPI,
	ctx: ExtensionContext,
): Promise<boolean> {
	const exec = pi.exec.bind(pi);
	const claimed = await mvctl(exec, ["claim-pi-prompts"], { timeout: 30_000 });
	if (!claimed.ok || !claimed.text) return false;

	let pending: { count: number; items: Array<{ text: string; claim: string }> };
	try {
		pending = JSON.parse(claimed.text);
	} catch {
		return false;
	}
	if (!pending.count || !pending.items.length) return false;

	const committed: string[] = [];
	let failedAt = pending.items.length;
	try {
		for (let index = 0; index < pending.items.length; index += 1) {
			const item = pending.items[index];
			if (index === 0 && ctx.isIdle()) {
				pi.sendUserMessage(item.text);
			} else {
				pi.sendUserMessage(item.text, { deliverAs: "followUp" });
			}
			committed.push(item.claim);
		}
	} catch (err) {
		failedAt = committed.length;
		if (ctx.hasUI) {
			ctx.ui.notify(
				`multiverse: could not deliver a SecondBrain prompt (${String(err)})`,
				"warning",
			);
		}
	}

	if (committed.length) {
		const args = ["commit-pi-prompts"];
		for (const claim of committed) args.push("--claim", claim);
		await mvctl(exec, args, { timeout: 15_000 });
	}
	const remaining = pending.items.slice(failedAt).map((item) => item.claim);
	if (remaining.length) {
		const args = ["release-pi-prompts"];
		for (const claim of remaining) args.push("--claim", claim);
		await mvctl(exec, args, { timeout: 15_000 });
	}
	if (committed.length && ctx.hasUI) {
		ctx.ui.notify(
			`multiverse: delivered ${committed.length} SecondBrain prompt(s)`,
			"info",
		);
	}
	return committed.length > 0;
}

// ---------------------------------------------------------------------------
// Inbox delivery
// ---------------------------------------------------------------------------

/**
 * Claim pending messages, inject them, then commit the claim.
 *
 * The ordering matters. `claim-inbox` stages messages rather than deleting
 * them, and only `commit-inbox` removes them after `pi.sendMessage` accepts
 * the injection call. A synchronous injection failure releases this
 * deliverer's claims immediately; process death leaves them for stale-claim
 * recovery. If the commit itself fails, the safe failure mode is a duplicate
 * delivery. pi's fire-and-forget API provides no later acknowledgement, so
 * asynchronous failures beyond that call are not observable here.
 *
 * Delivered as a `custom` message rather than a user message: it participates
 * in LLM context, which is the point, but is not attributed to the user, who
 * did not say it.
 */
async function deliverInbox(
	pi: ExtensionAPI,
	ctx: ExtensionContext,
	opts: { trigger: boolean },
): Promise<boolean> {
	const exec = pi.exec.bind(pi);
	const claimed = await mvctl(exec, ["claim-inbox"], { timeout: 30_000 });
	if (!claimed.ok || !claimed.text) return false;

	let pending: { count: number; claims: string[]; text: string };
	try {
		pending = JSON.parse(claimed.text);
	} catch {
		return false;
	}
	if (!pending.count) return false;

	try {
		pi.sendMessage(
			{
				customType: "multiverse-inbox",
				content:
					"[Multiverse Inter-Agent] You have new messages from other agents.\n\n" +
					pending.text +
					"\n\nYou may use any tools to act on these. To reply, call " +
					"message_agent with the sender's name. Replies are optional — only " +
					"respond if it is useful.",
				display: true,
			},
			// While a turn is running, followUp queues the delivery until the
			// model has finished with its current tool results, so we never
			// interleave an inbox message with a half-finished chain of
			// thought. Idle, triggerTurn starts a turn so the agent actually
			// acts on it — this is what replaces Claude Code's tmux send-keys
			// wake.
			ctx.isIdle() ? { triggerTurn: opts.trigger } : { deliverAs: "followUp" },
		);
	} catch (err) {
		// claim-inbox already staged these out of the pending pool, so they are
		// invisible to every other deliverer. Swallowing the throw would leave
		// them that way until the 60s stale-claim sweep — with the sender
		// blocked on a reply the whole time. Undo our own claim instead, and
		// the 2s watcher poll retries almost immediately.
		if (pending.claims.length) {
			const args = ["release-inbox"];
			for (const claim of pending.claims) args.push("--claim", claim);
			await mvctl(exec, args, { timeout: 15_000 });
		}
		if (ctx.hasUI) {
			ctx.ui.notify(
				`multiverse: could not deliver ${pending.count} message(s), returned them ` +
					`to the inbox for retry (${String(err)})`,
				"warning",
			);
		}
		return false;
	}

	if (pending.claims.length) {
		const args = ["commit-inbox"];
		for (const claim of pending.claims) args.push("--claim", claim);
		await mvctl(exec, args, { timeout: 15_000 });
	}
	if (ctx.hasUI) {
		ctx.ui.notify(
			`multiverse: delivered ${pending.count} inter-agent message(s)`,
			"info",
		);
	}
	return true;
}

// ---------------------------------------------------------------------------
// Inbox watcher
// ---------------------------------------------------------------------------
//
// `agent_settled` covers messages that arrive while we are working. It does
// not cover an agent sitting idle at the prompt: nothing fires again until the
// user types. That is the gap Claude Code fills by typing a wake prompt into
// the recipient's tmux pane — a hack that is skipped entirely when a user is
// attached, since it would overwrite what they were typing.
//
// pi can deliver into a live session directly, so the wake path here is a
// cheap poll plus `sendMessage({ triggerTurn: true })`. No terminal
// manipulation, and it works the same whether or not someone is attached.

const WATCH_INTERVAL_MS = 2000;

let watchTimer: ReturnType<typeof setInterval> | undefined;
let watchSuspended = false;
let watchInFlight = false;

function startInboxWatcher(pi: ExtensionAPI, ctx: ExtensionContext): void {
	stopInboxWatcher();
	watchTimer = setInterval(() => {
		// Only deliver into a quiet agent. Mid-turn arrivals are picked up by
		// `agent_settled`, which avoids interleaving a delivery with tool
		// results the model is still reasoning about.
		if (watchSuspended || watchInFlight || !ctx.isIdle()) return;
		watchInFlight = true;
		void (async () => {
			const prompted = await deliverSecondBrainPrompts(pi, ctx).catch(() => false);
			if (!prompted) {
				await deliverInbox(pi, ctx, { trigger: true }).catch(() => undefined);
			}
		})().finally(() => {
			watchInFlight = false;
		});
	}, WATCH_INTERVAL_MS);
	// Never hold the process open on account of the poll.
	(watchTimer as unknown as { unref?: () => void }).unref?.();
}

function stopInboxWatcher(): void {
	if (watchTimer) clearInterval(watchTimer);
	watchTimer = undefined;
}

function suspendWatcher(): void {
	watchSuspended = true;
}

function resumeWatcher(): void {
	watchSuspended = false;
}
