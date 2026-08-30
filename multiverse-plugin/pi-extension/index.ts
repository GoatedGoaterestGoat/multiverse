/**
 * Multiverse for pi.
 *
 * pi's counterpart to the Claude Code plugin and the Codex plugin: it exposes
 * the inter-agent tools, writes the session status the monitor dashboard
 * reads, records the native session id, and delivers inbox messages.
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

	const setStatus = (status: string) => mvctlDetached(exec, ["status", status]);

	pi.on("session_start", async (_event, ctx) => {
		const id = ctx.sessionManager.getSessionId();
		if (id) mvctlDetached(exec, ["record-session", id]);
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

	pi.on("before_agent_start", async () => setStatus("working"));
	pi.on("agent_start", async () => setStatus("working"));
	pi.on("tool_execution_start", async () => setStatus("working"));

	pi.on("agent_settled", async (_event, ctx) => {
		setStatus("idle");
		// End-of-turn is the natural delivery point: anything that arrived
		// while we were working gets picked up without interrupting the run.
		if (WRAPPED && isLongLived(ctx)) await deliverInbox(pi, ctx, { trigger: true });
	});

	pi.on("session_shutdown", async () => {
		stopInboxWatcher();
		setStatus("ended");
	});

	// ----- inter-agent tools ----------------------------------------------
	//
	// Only registered under the wrapper. Standalone (`pi --extension …`) the
	// state dir the tools coordinate through does not exist, so offering them
	// would just hand the model six tools that always fail.

	if (!WRAPPED) return;

	pi.registerTool({
		name: "list_agents",
		label: "List agents",
		description:
			"List the named agents in your Multiverse workspace — the ones you can " +
			"reach with message_agent and ask_agent. Shows each agent's status " +
			"(idle / working / listening / ended / dormant). Your own entry is " +
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
// Inbox delivery
// ---------------------------------------------------------------------------

/**
 * Claim pending messages, inject them, then commit the claim.
 *
 * The ordering matters. `claim-inbox` stages messages rather than deleting
 * them, and only `commit-inbox` removes them after `pi.sendMessage` accepts
 * the injection call. A synchronous injection failure or process exit before
 * that point leaves the claim for stale-claim recovery. If the commit itself
 * fails, the safe failure mode is a duplicate delivery. pi's fire-and-forget
 * API provides no later acknowledgement, so asynchronous failures beyond
 * that call are not observable here.
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
		// While a turn is running, followUp queues the delivery until the model
		// has finished with its current tool results, so we never interleave an
		// inbox message with a half-finished chain of thought. Idle,
		// triggerTurn starts a turn so the agent actually acts on it — this is
		// what replaces Claude Code's tmux send-keys wake.
		ctx.isIdle() ? { triggerTurn: opts.trigger } : { deliverAs: "followUp" },
	);

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
		void deliverInbox(pi, ctx, { trigger: true })
			.catch(() => undefined)
			.finally(() => {
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
