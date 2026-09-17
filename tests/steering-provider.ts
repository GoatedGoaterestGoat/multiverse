// Offline provider: native Pi runs real shell tools, no API/network/model spend.
import { appendFileSync } from "node:fs";
import { join } from "node:path";
import { createAssistantMessageEventStream } from "@earendil-works/pi-ai";

export default function mock(pi: any) {
  let turn = 0;
  pi.registerProvider("mv2-steering", {
    api: "mv2-steering", apiKey: "test", baseUrl: "http://unused.invalid",
    models: [{ id: "test", name: "Test", reasoning: false, input: ["text"],
      cost: { input: 0, output: 0, cacheRead: 0, cacheWrite: 0 }, contextWindow: 128000, maxTokens: 4096 }],
    streamSimple(model: any, context: any) {
      turn++;
      appendFileSync(join(process.env.MV2_AGENT_DIR!, "timing.jsonl"), JSON.stringify({ at: Date.now(), turn, context }) + "\n");
      const stream = createAssistantMessageEventStream();
      const tool = turn <= 2;
      queueMicrotask(() => {
        const message: any = { role: "assistant", content: tool ? [{ type: "toolCall", id: `call-${turn}`, name: "bash",
          arguments: { command: `touch "$MV2_AGENT_DIR/tool-${turn}-start"; sleep 4; touch "$MV2_AGENT_DIR/tool-${turn}-end"` } }]
          : [{ type: "text", text: "Mock completed." }],
          api: model.api, provider: model.provider, model: model.id, stopReason: tool ? "toolUse" : "stop", timestamp: Date.now(),
          usage: { input: 1, output: 1, cacheRead: 0, cacheWrite: 0, totalTokens: 2,
            cost: { input: 0, output: 0, cacheRead: 0, cacheWrite: 0, total: 0 } } };
        stream.push({ type: "start", partial: message });
        stream.push({ type: "done", reason: tool ? "toolUse" : "stop", message });
        stream.end();
      });
      return stream;
    },
  });
}
