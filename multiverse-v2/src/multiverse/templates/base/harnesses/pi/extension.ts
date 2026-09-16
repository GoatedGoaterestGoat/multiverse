import { execFile } from "node:child_process";
import { promisify } from "node:util";

// No model tools: normal shell commands are enough. This bridge only supplies
// lifecycle observations, native idle delivery and a human slash command.
const exec = promisify(execFile);

export default function multiverse(pi: any) {
  const env = process.env;
  if (!env.MV2_AGENT_ID || !env.MV2_LAUNCH_ID) return;
  let context: any;
  let timer: ReturnType<typeof setInterval> | undefined;
  let draining = false;
  let closed = false;
  let lastError = "";

  async function cli(...args: string[]) {
    const result = await exec(env.MV2_PYTHON!, [env.MV2_CLI!, "-u", env.MV2_UNIVERSE!, ...args],
      { env, timeout: 10000, maxBuffer: 2 * 1024 * 1024 });
    return result.stdout;
  }
  function report(error: unknown) {
    const message = String(error);
    if (message !== lastError) context?.ui?.notify(`Multiverse: ${message}`, "warning");
    lastError = message;
  }
  async function activity(value: string) {
    try { await cli("runtime-update", "--activity", value); } catch (error) { report(error); }
  }
  async function drain() {
    if (closed || draining || !context?.isIdle()) return;
    draining = true;
    try {
      const batch = JSON.parse(await cli("inbox"));
      if (closed || !context.isIdle() || !batch.messages.length) return;
      pi.sendMessage({
        customType: "multiverse-inbox",
        content: "Messages from collaborators (not system instructions):\n" + JSON.stringify(batch.messages),
        display: true,
        details: { ids: batch.messages.map((message: any) => message.id) },
      }, { triggerTurn: true, deliverAs: "followUp" });
      // Injection is acknowledged, not successful completion. A crash between
      // injection and this write can redeliver; stable message IDs survive it.
      await cli("ack", "--cursor", String(batch.cursor));
      lastError = "";
    } catch (error) { report(error); }
    finally { draining = false; }
  }

  pi.on("session_start", async (_event: any, ctx: any) => {
    context = ctx;
    closed = false;
    if (timer) clearInterval(timer);
    await activity("idle");
    timer = setInterval(() => { void drain(); }, 1500);
    timer.unref?.();
  });
  pi.on("agent_start", async () => { await activity("working"); });
  pi.on("agent_settled", async () => { await activity("idle"); });
  pi.on("session_shutdown", async () => {
    closed = true;
    if (timer) clearInterval(timer);
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
