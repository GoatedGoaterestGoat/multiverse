// Runs inside real Pi. No network, account, credentials, or model spend.
import { appendFileSync } from "node:fs";
import { join } from "node:path";
import { createAssistantMessageEventStream } from "@earendil-works/pi-ai";

export default function mock(pi: any) {
  pi.registerProvider("mv2-test", {
    api: "mv2-test", apiKey: "test", baseUrl: "http://unused.invalid",
    models: [{ id: "mv2-test", name: "Test", reasoning: false, input: ["text"],
      cost: { input: 0, output: 0, cacheRead: 0, cacheWrite: 0 }, contextWindow: 128000, maxTokens: 4096 }],
    streamSimple(model: any, context: any) {
      appendFileSync(join(process.env.MV2_AGENT_DIR!, "mock-requests.jsonl"), JSON.stringify(context) + "\n");
      const stream = createAssistantMessageEventStream();
      queueMicrotask(() => {
        const message: any = { role: "assistant", content: [{ type: "text", text: "Mock completed." }],
          api: model.api, provider: model.provider, model: model.id, stopReason: "stop", timestamp: Date.now(),
          usage: { input: 1, output: 1, cacheRead: 0, cacheWrite: 0, totalTokens: 2,
            cost: { input: 0, output: 0, cacheRead: 0, cacheWrite: 0, total: 0 } } };
        stream.push({ type: "start", partial: message });
        stream.push({ type: "text_start", contentIndex: 0, partial: message });
        stream.push({ type: "text_delta", contentIndex: 0, delta: "Mock completed.", partial: message });
        stream.push({ type: "text_end", contentIndex: 0, content: "Mock completed.", partial: message });
        stream.push({ type: "done", reason: "stop", message });
        stream.end();
      });
      return stream;
    },
  });
}
