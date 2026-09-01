/**
 * Test-only pi extension: registers the mock provider from
 * `tests/mock_openai_server.py` so the multiverse extension can be driven
 * end-to-end without a real model.
 *
 *   pi -e tests/mock-provider.ts -e pi-extension/index.ts \
 *      --provider mock --model mock-model -p "..."
 */

import type { ExtensionAPI } from "@earendil-works/pi-coding-agent";

export default function mockProvider(pi: ExtensionAPI) {
	pi.registerProvider("mock", {
		baseUrl: process.env.MOCK_OPENAI_URL ?? "http://127.0.0.1:8123/v1",
		apiKey: "mock-key",
		api: "openai-completions",
		models: [
			{
				id: "mock-model",
				name: "Mock Model",
				reasoning: false,
				input: ["text"],
				cost: { input: 0, output: 0, cacheRead: 0, cacheWrite: 0 },
				contextWindow: 128000,
				maxTokens: 4096,
			},
		],
	});
}
