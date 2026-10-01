import { execFile } from "node:child_process";
import { promisify } from "node:util";
import { createServer, type Server } from "node:net";
import { resolve } from "node:path";
import { TranscriptReceipts } from "./transcript.ts";

const exec = promisify(execFile);

// Runs inside the native interactive Pi process. No polling, inbox access,
// or model tools: core owns all delivery records.
export default function multiverse(pi: any, run = exec) {
  const env = { ...process.env };
  if (!env.MV2_AGENT_ID || !env.MV2_LAUNCH_ID || !env.MV2_CONTROL_SOCKET) return;
  let context: any;
  let server: Server | undefined;
  let closed = true;
  let serial = Promise.resolve();
  let transcript = new TranscriptReceipts();
  const accepted = new Map<string, string>();

  async function cli(...args: string[]) {
    const result = await run(env.MV2_PYTHON!, [env.MV2_CLI!, "-u", env.MV2_UNIVERSE!, ...args],
      { env, timeout: 10000, maxBuffer: 2 * 1024 * 1024 });
    return result.stdout;
  }
  // Not an mv2 command: agents report their phase, never their status.
  async function runtimeUpdate(...args: string[]) {
    await run(env.MV2_PYTHON!, ["-m", "multiverse.tools.runtime_update", "-u", env.MV2_UNIVERSE!, ...args],
      { env, timeout: 10000, maxBuffer: 2 * 1024 * 1024 });
  }
  function report(error: unknown) { context?.ui?.notify(`Multiverse: ${error}`, "warning"); }
  async function reportStatus(value: string) {
    if (!closed) try { await runtimeUpdate("--status", value); } catch (error) { report(error); }
  }
  function close() {
    closed = true;
    server?.close();
    server = undefined;
  }
  async function request(input: any) {
    const identity = { launch_id: env.MV2_LAUNCH_ID, native_id: context.sessionManager.getSessionId(),
                       message_id: input.message_id };
    const reply = (state: string, reason: string, value?: any) => ({ ...identity, state, reason, value });
    if (closed || input.launch_id !== identity.launch_id || input.native_id !== identity.native_id ||
        resolve(context.sessionManager.getSessionFile() || "") !== resolve(env.MV2_AGENT_DIR!, "session.jsonl")) {
      return reply("error", "Request does not match this native session and launch");
    }
    if (input.operation === "get_delivery") {
      const persisted = await transcript.scan(context.sessionManager.getSessionFile(), identity.native_id);
      if (!persisted.complete) return reply("unknown", "Transcript scan incomplete");
      if (persisted.ids.has(input.message_id)) return reply("native_queued", "Message observed in transcript",
        { message_id: input.message_id, transcript_confirmed: true });
      if (accepted.get(input.message_id) === "accepted") return reply("native_queued", "Accepted; persistence unconfirmed",
        { message_id: input.message_id, transcript_confirmed: false });
      return reply("unknown", "No acceptance evidence; absence is not proof of failed submission");
    }
    if (input.operation !== "send") return reply("unavailable", "Unknown operation");
    if (typeof input.message_id !== "string" || !input.message_id || typeof input.text !== "string" ||
        !["steer", "follow-up"].includes(input.mode)) return reply("error", "Invalid delivery request");
    const persisted = await transcript.scan(context.sessionManager.getSessionFile(), identity.native_id);
    if (!persisted.complete) return reply("error", "Cannot verify session transcript before submission");
    if (accepted.has(input.message_id) || persisted.ids.has(input.message_id)) {
      if (accepted.get(input.message_id) === "unknown" && !persisted.ids.has(input.message_id))
        return reply("unknown", "Previous submission acceptance remains unknown");
      return reply("native_queued", "Message already accepted", { message_id: input.message_id,
        transcript_confirmed: persisted.ids.has(input.message_id) });
    }
    // Pi queues this at the requested native boundary; this is not an answer.
    accepted.set(input.message_id, "unknown");
    try {
      pi.sendMessage({ customType: "multiverse-inbox", content: input.text, display: true,
        details: { ids: [input.message_id] } }, // Persisted metadata, excluded from Pi's model input.
        { triggerTurn: true, deliverAs: input.mode === "follow-up" ? "followUp" : "steer" });
    } catch (error) {
      // Retain uncertain IDs: retrying after a partially successful throw can duplicate.
      return reply("unknown", `Pi submission failed; acceptance unknown: ${error}`);
    }
    accepted.set(input.message_id, "accepted");
    return reply("native_queued", "Pi accepted native input; consumption unconfirmed", { message_id: input.message_id });
  }

  pi.on("session_start", async (_event: any, ctx: any) => {
    close();
    context = ctx;
    accepted.clear();
    transcript = new TranscriptReceipts();
    if (!ctx.sessionManager.getSessionFile() ||
        resolve(ctx.sessionManager.getSessionFile()) !== resolve(env.MV2_AGENT_DIR!, "session.jsonl")) {
      try { await runtimeUpdate("--status", "unknown", "--bridge", "none"); } catch (error) { report(error); }
      return;
    }
    try {
      await cli("session-bind", "--session-id", ctx.sessionManager.getSessionId());
      server = createServer(socket => {
        socket.setEncoding("utf8");
        socket.setTimeout(10000, () => socket.destroy());
        socket.on("error", report);
        let data = "";
        let handled = false;
        socket.on("data", chunk => {
          if (handled) return;
          data += chunk.toString();
          if (Buffer.byteLength(data) > 1024 * 1024) { socket.destroy(); return; }
          if (!data.includes("\n")) return;
          handled = true;
          serial = serial.then(async () => {
            try { socket.end(JSON.stringify(await request(JSON.parse(data.split("\n", 1)[0]))) + "\n"); }
            catch (error) { report(error); socket.destroy(); }
          });
        });
      });
      await new Promise<void>((resolve, reject) => {
        server!.once("error", reject);
        server!.listen(env.MV2_CONTROL_SOCKET, resolve);
      });
      server.on("error", report);
      server.unref();
      closed = false;
      await runtimeUpdate("--status", "idle", "--bridge", "pi-socket-v1");
    } catch (error) { close(); report(error); }
  });
  pi.on("agent_start", async () => { await reportStatus("working"); });
  pi.on("agent_settled", async () => { await reportStatus("idle"); });
  pi.on("session_shutdown", async () => {
    close();
    try { await runtimeUpdate("--status", "unknown", "--bridge", "none"); } catch (error) { report(error); }
  });
  pi.registerCommand("multiverse", {
    description: "List capabilities or load a universe command",
    handler: async (input: string, ctx: any) => {
      try {
        const [name, ...rest] = input.trim().split(/\s+/);
        if (!name || name === "list") ctx.ui.notify(await cli("catalog"), "info");
        else pi.sendUserMessage(await cli("command", name, rest.join(" ")), { deliverAs: "followUp" });
      } catch (error) { ctx.ui.notify(String(error), "error"); }
    },
  });
}
