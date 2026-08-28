/**
 * Multiverse for pi.
 *
 * This is pi's counterpart to the Claude Code plugin: it provides the
 * inter-agent tools, writes the session status the monitor dashboard reads,
 * delivers inbox messages, and replaces pi's compaction with multiverse's
 * frontier-coherent tree compaction.
 *
 * ## How it talks to multiverse
 *
 * All shared state — the inbox, spawn-request brokering, session metadata,
 * ask-forks, the summary-tree cache — lives on disk under `~/.multiverse` and
 * is implemented once, in Python, in `src/`. Rather than write a second
 * implementation of that on-disk contract in TypeScript (two copies, free to
 * drift), this extension shells out to `mvctl`, a thin CLI over the exact
 * functions the Claude Code MCP server calls. See `src/mvctl.py`.
 *
 * The cost is one short-lived process per tool call, which is invisible next
 * to a model round-trip. The benefit is that a fix to push-notification
 * routing or inbox claiming lands on both harnesses at once.
 *
 * ## Three places pi beats the Claude Code integration
 *
 * 1. **Delivery into a live turn.** Claude Code can only inject context when
 *    a hook fires, so waking an idle recipient means typing into its tmux
 *    pane with `send-keys` — which is refused outright when a user is
 *    attached, because it would clobber their input. pi has
 *    `pi.sendMessage(..., { triggerTurn: true })`, so a push-notified message
 *    reaches a running agent directly. No tmux, no attached-user carve-out.
 * 2. **One process.** Claude Code spawns a hook process per lifecycle event.
 *    Here every event is a callback in the pi process; only the mvctl calls
 *    leave it.
 * 3. **Compaction is an entry, not a rewrite.** `session_before_compact` lets
 *    us hand pi a summary and a keep-boundary. pi appends one `compaction`
 *    entry and leaves the original entries in the file, so the Claude path's
 *    whole two-JSONL mirror (full + live + uuid marker) is unnecessary.
 *
 * Loaded automatically by `bin/multiverse-session` when the session's harness
 * is pi; standalone use is `pi --extension <plugin_root>/pi-extension/index.ts`.
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

type Exec = ExtensionAPI["exec"];

/**
 * Build the argv for an mvctl invocation.
 *
 * Prefers the plugin's own virtualenv interpreter over `uv run`: `uv run`
 * re-resolves the lockfile on every call, which turns a ~40ms exec into a
 * ~400ms one, and these run on the interactive path.
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

async function mvctl(
	exec: Exec,
	args: string[],
	opts: { timeout?: number; signal?: AbortSignal } = {},
): Promise<MvctlResult> {
	if (!PLUGIN_ROOT) {
		return { ok: false, text: "multiverse: MULTIVERSE_PLUGIN_ROOT is not set" };
	}
	const { cmd, argv } = mvctlArgv(args);
	try {
		const r = await exec(cmd, argv, {
			cwd: PLUGIN_ROOT,
			timeout: opts.timeout ?? 120_000,
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

/** Fire-and-forget mvctl call; never let bookkeeping break a turn. */
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
	// Claude Code a shell script writes it from six different hooks; here the
	// same values come from the agent lifecycle directly.
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
		if (WRAPPED) startInboxWatcher(pi, ctx);
	});

	pi.on("before_agent_start", async () => setStatus("working"));
	pi.on("agent_start", async () => setStatus("working"));
	pi.on("tool_execution_start", async () => setStatus("working"));

	pi.on("agent_settled", async (_event, ctx) => {
		setStatus("idle");
		// End-of-turn is the natural delivery point: anything that arrived
		// while we were working gets picked up without interrupting the run.
		if (WRAPPED) await deliverInbox(pi, ctx, { trigger: true });
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

	if (WRAPPED) {
		pi.registerTool({
			name: "list_agents",
			label: "List agents",
			description:
				"List the named agents in your multiverse workspace — the ones you can " +
				"reach with message_agent and ask_agent. Shows each agent's status " +
				"(idle / working / listening / ended / dormant). Your own entry is " +
				"marked. Pass working_directory to inspect a different workspace.",
			promptSnippet: "Discover other agents you can talk to",
			parameters: Type.Object({
				max_agents: Type.Optional(
					Type.Integer({ description: "Entries before truncating. Default 15." }),
				),
				working_directory: Type.Optional(
					Type.String({ description: "Absolute path of another workspace to list." }),
				),
			}),
			async execute(_id, params, signal) {
				const args = ["list-agents", "--max", String(params.max_agents ?? 15)];
				if (params.working_directory) args.push("--cwd", params.working_directory);
				const r = await mvctl(exec, args, { signal });
				return toolText(r.text);
			},
		});

		pi.registerTool({
			name: "message_agent",
			label: "Message agent",
			description:
				"Send a durable message to another named agent. Use this for " +
				"instructions, in-depth questions, or anything the recipient should be " +
				"able to use tools to act on — they get the message in their " +
				"conversation and can take their time. Fire-and-forget: this returns as " +
				"soon as the message is queued. To reply to someone, call message_agent " +
				"back at them; there is no separate respond tool. Set push_notification " +
				"when you are blocked on the recipient acting, which wakes a dormant " +
				"agent or delivers into a live one's current turn.",
			promptSnippet: "Send another agent a message they can act on with tools",
			parameters: Type.Object({
				target_name: Type.String({ description: "Name of the recipient agent." }),
				message: Type.String({ description: "What to send." }),
				push_notification: Type.Optional(
					Type.Boolean({ description: "Wake the recipient now. Default false." }),
				),
			}),
			async execute(_id, params, signal) {
				const args = ["message", "--to", params.target_name, "--body", params.message];
				if (params.push_notification) args.push("--push");
				const r = await mvctl(exec, args, { signal });
				return toolText(r.text);
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
			}),
			async execute(_id, params, signal) {
				const timeout = params.timeout_seconds ?? 60;
				const r = await mvctl(
					exec,
					["ask", "--to", params.target_name, "--question", params.question,
						"--timeout", String(timeout)],
					// Give mvctl room past its own deadline so we report the
					// agent's timeout message rather than killing the process.
					{ signal, timeout: (timeout + 30) * 1000 },
				);
				return toolText(r.text);
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
				const r = await mvctl(exec, ["check-inbox"], { signal });
				return toolText(r.text);
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
				// The watcher would otherwise claim the very message this call
				// is parked on, and monitor_inbox would time out while the
				// message sat delivered in the transcript.
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
				"names it from the monitor before it is created; this blocks until they " +
				"decide.",
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
				const r = await mvctl(exec, args, { signal, timeout: 330_000 });
				return toolText(r.text);
			},
		});
	}

	// ----- compaction ------------------------------------------------------
	//
	// pi asks before it compacts and will take a summary from us, so goat
	// compaction slots in without touching pi's own trigger logic: pi decides
	// WHEN, multiverse decides WHAT.
	//
	// The budget is derived from pi's own preparation rather than configured
	// separately, so `keepRecentTokens` and `reserveTokens` keep meaning what
	// the user set them to.

	pi.on("session_before_compact", async (event, ctx) => {
		const sessionFile = ctx.sessionManager.getSessionFile();
		if (!sessionFile) return; // ephemeral session — let pi handle it
		const { preparation, reason } = event;

		const budget = compactionBudget(preparation);
		ctx.ui.setStatus("multiverse", "compacting…");
		try {
			const r = await mvctl(
				exec,
				[
					"compact-plan",
					"--session", sessionFile,
					"--session-id", ctx.sessionManager.getSessionId() ?? "",
					"--budget", String(budget),
				],
				{ signal: event.signal, timeout: 300_000 },
			);
			if (!r.ok) {
				// Falling through to pi's built-in compaction is the right
				// failure mode: the session still gets compacted, just flatly.
				ctx.ui.notify(`multiverse compaction unavailable (${r.text}); using pi's`, "warning");
				return;
			}
			const plan = JSON.parse(r.text) as {
				summary: string;
				firstKeptEntryId: string | null;
				tokensBefore: number;
				details: Record<string, unknown>;
				_multiverse?: { turns: number; turnsWithTree: number };
			};
			const built = plan._multiverse?.turnsWithTree ?? 0;
			const turns = plan._multiverse?.turns ?? 0;
			ctx.ui.notify(
				`multiverse compaction (${reason}): ${turns} turns, ` +
					`${built} with summary trees, budget ${budget.toLocaleString()}t`,
				"info",
			);
			return {
				compaction: {
					summary: plan.summary,
					// Fall back to pi's own cut point when every turn was
					// summarized: pi requires a valid keep boundary.
					firstKeptEntryId: plan.firstKeptEntryId ?? preparation.firstKeptEntryId,
					tokensBefore: plan.tokensBefore || preparation.tokensBefore,
					details: plan.details,
				},
			};
		} finally {
			ctx.ui.setStatus("multiverse", "");
		}
	});

	// ----- commands --------------------------------------------------------

	pi.registerCommand("mv-inbox", {
		description: "Show pending multiverse inter-agent messages",
		handler: async (_args, ctx) => {
			const r = await mvctl(exec, ["check-inbox"]);
			ctx.ui.notify(r.text || "(nothing pending)", r.ok ? "info" : "warning");
		},
	});

	pi.registerCommand("mv-agents", {
		description: "List agents in this multiverse workspace",
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
 * Claim pending messages and inject them into the conversation.
 *
 * `check-inbox` is the claiming read: it returns the messages and removes
 * them in one atomic step, so two deliveries can never double-inject. That
 * makes the ordering here important — we only ever call it when we intend to
 * put the result in the transcript.
 *
 * Delivered as a `custom` message rather than a user message: it participates
 * in LLM context (which is the point) but is not attributed to the user, who
 * did not say it.
 */
async function deliverInbox(
	pi: ExtensionAPI,
	ctx: ExtensionContext,
	opts: { trigger: boolean },
): Promise<boolean> {
	const exec = pi.exec.bind(pi);
	const peeked = await mvctl(exec, ["peek-inbox"], { timeout: 15_000 });
	if (!peeked.ok || !peeked.text) return false;

	let pending: { count: number; ids: string[]; text: string };
	try {
		pending = JSON.parse(peeked.text);
	} catch {
		return false;
	}
	if (!pending.count) return false;

	pi.sendMessage(
		{
			customType: "multiverse-inbox",
			content:
				"[Multiverse Inter-Agent] You have new messages from other agents in " +
				"your workspace.\n\n" +
				pending.text +
				"\n\nYou may use any tools to act on these. To reply, call " +
				"message_agent with the sender's name. Replies are optional — only " +
				"respond if it is useful.",
			display: true,
		},
		// While a turn is running, followUp queues the delivery until the
		// model has finished with its current tool results, so we never
		// interleave an inbox message with a half-finished chain of thought.
		// Idle, triggerTurn starts a turn so the agent actually acts on it —
		// this is what replaces Claude Code's tmux send-keys wake.
		ctx.isIdle() ? { triggerTurn: opts.trigger } : { deliverAs: "followUp" },
	);

	// Ack only after the send. `pi.sendMessage` is fire-and-forget, so a
	// claim-then-send ordering could lose a message when the send is rejected;
	// this ordering costs at most a duplicate if we die in between.
	await mvctl(exec, ["ack-inbox", "--ids", pending.ids.join(",")], { timeout: 15_000 });
	ctx.ui.notify(
		`multiverse: delivered ${pending.count} inter-agent message(s)`,
		"info",
	);
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

// ---------------------------------------------------------------------------
// Budget
// ---------------------------------------------------------------------------

/**
 * Token budget for the compacted output, derived from pi's own preparation.
 *
 * pi has already decided which messages to summarize and how much recent
 * context to keep; `tokensBefore` is the size of what it wants replaced. We
 * target a fraction of that, floored so a small compaction still produces
 * something usable and capped so a huge one does not simply reproduce itself.
 */
function compactionBudget(preparation: { tokensBefore: number }): number {
	const before = Math.max(0, preparation.tokensBefore || 0);
	const target = Math.floor(before * COMPACTION_RATIO);
	return Math.min(Math.max(target, MIN_BUDGET_TOKENS), MAX_BUDGET_TOKENS);
}

/** Keep ~30% of the pre-compaction context. */
const COMPACTION_RATIO = Number(process.env.MULTIVERSE_COMPACT_RATIO ?? "0.3");
const MIN_BUDGET_TOKENS = Number(process.env.MULTIVERSE_COMPACT_MIN ?? "4000");
const MAX_BUDGET_TOKENS = Number(process.env.MULTIVERSE_COMPACT_MAX ?? "120000");
