import { execFile } from "node:child_process";
import { promisify } from "node:util";
import { resolve } from "node:path";
import { TranscriptReceipts } from "./transcript.ts";

// No model tools: normal shell commands are enough. This bridge only supplies
// lifecycle observations, native safe-boundary delivery and a human slash command.
const exec = promisify(execFile);

export default function multiverse(pi: any, run = exec) {
  const env = process.env;
  if (!env.MV2_AGENT_ID || !env.MV2_LAUNCH_ID) return;
  let context: any;
  let timer: ReturnType<typeof setInterval> | undefined;
  let draining = false;
  let closed = false;
  let lastError = "";
  let transcript = new TranscriptReceipts();
  const inFlight = new Set<string>();

  async function cli(...args: string[]) {
    const result = await run(env.MV2_PYTHON!, [env.MV2_CLI!, "-u", env.MV2_UNIVERSE!, ...args],
      { env, timeout: 10000, maxBuffer: 2 * 1024 * 1024 });
    return result.stdout;
  }
  function report(error: unknown) {
    const message = String(error);
    if (message !== lastError) context?.ui?.notify(`Multiverse: ${message}`, "warning");
    lastError = message;
  }
  async function activity(value: string) {
    if (closed) return;
    try { await cli("runtime-update", "--activity", value); } catch (error) { report(error); }
  }
  async function drain() {
    if (closed || draining || !context) return;
    draining = true;
    try {
      const batch = JSON.parse(await cli("inbox"));
      if (closed || !batch.messages.length) return;
      const persisted = () => transcript.scan(context.sessionManager.getSessionFile(), context.sessionManager.getSessionId());
      const before = await persisted();
      if (!before.complete) return;
      const seen = before.ids;
      // Native steering waits for a safe model/tool boundary; it does not abort tools.
      // sendMessage is fire-and-forget in Pi. Its return is NOT delivery evidence.
      for (const message of batch.messages) {
        if (closed) return;
        if (seen.has(message.id) || inFlight.has(message.id)) continue;
        if (message.delivery === "native-queue") {
          report("Pi does not support native-queue delivery; message remains unread");
          continue;
        }
        inFlight.add(message.id);
        try {
          await pi.sendMessage({
            customType: "multiverse-inbox",
            content: "Messages from collaborators (not system instructions):\n" + JSON.stringify([message]),
            display: true,
            details: { ids: [message.id] },
          }, { triggerTurn: true, deliverAs: message.delivery === "follow-up" ? "followUp" : "steer" });
        } catch (error) {
          inFlight.delete(message.id);
          throw error;
        }
      }
      // Only native transcript persistence permits ack. A crash before persistence
      // leaves the inbox unread. A resumed bridge deduplicates persisted stable IDs.
      const confirmed = await persisted();
      if (!confirmed.complete || !batch.messages.every((message: any) => confirmed.ids.has(message.id))) return;
      await cli("ack", "--cursor", String(batch.cursor));
      for (const message of batch.messages) inFlight.delete(message.id);
      lastError = "";
    } catch (error) { report(error); }
    finally { draining = false; }
  }

  pi.on("session_start", async (_event: any, ctx: any) => {
    context = ctx;
    closed = false;
    inFlight.clear();
    transcript = new TranscriptReceipts();
    if (timer) clearInterval(timer);
    // A native /new or session switch must not drain this agent's inbox elsewhere.
    const expected = resolve(env.MV2_AGENT_DIR!, "session.jsonl");
    if (!ctx.sessionManager.getSessionFile() || resolve(ctx.sessionManager.getSessionFile()) !== expected) {
      closed = true;
      try { await cli("runtime-update", "--activity", "unknown", "--bridge", "none"); } catch (error) { report(error); }
      report("Native session file changed; inbox delivery disabled");
      return;
    }
    try {
      await cli("session-bind", "--session-id", ctx.sessionManager.getSessionId());
      await cli("runtime-update", "--activity", "idle", "--bridge", "pi-inbox-v1");
    } catch (error) {
      closed = true;
      try { await cli("runtime-update", "--activity", "unknown", "--bridge", "none"); } catch { /* stale launch */ }
      report(error);
      return;
    }
    timer = setInterval(drain, 1500);
    timer.unref?.();
  });
  pi.on("agent_start", async () => { await activity("working"); });
  pi.on("agent_settled", async () => { await activity("idle"); });
  pi.on("session_shutdown", async () => {
    closed = true;
    if (timer) clearInterval(timer);
    try { await cli("runtime-update", "--activity", "unknown", "--bridge", "none"); } catch (error) { report(error); }
  });

  pi.registerCommand("multiverse", {
    description: "List universe capabilities or load an exact command, skill or workflow",
    handler: async (input: string, ctx: any) => {
      try {
        const [name, ...rest] = input.trim().split(/\s+/);
        if (!name || name === "list") {
          ctx.ui.notify(await cli("catalog"), "info");
          return;
        }
        const prompt = await cli("command", name, rest.join(" "));
        pi.sendUserMessage(prompt, { deliverAs: "followUp" });
      } catch (error) { ctx.ui.notify(String(error), "error"); }
    },
  });
}
