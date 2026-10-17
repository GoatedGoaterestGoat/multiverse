# Codex plugin contract investigation

Design and evidence only. Read `docs/plugins.md` first on `plugin-system` at
`0b87ba5`, then the Codex adapter documentation and code. Investigated on
2026-10-11. **Installed CLI reports `codex-cli 0.162.1`**, not the expected
0.159.x. Below, native execution evidence is 0.162.1; 0.159.3 claims are from
OpenAI's pinned source/schema, not a locally executed 0.159.3 binary.

All native probes used a fresh temporary `CODEX_HOME`, startup update checks off
and `app-server-daemon/settings.json` with `updater.autoUpdateEnabled=false`,
following `tests/native_safety.py`. Probes launched a private stdio app-server,
not the daemon. No credentials were copied, no model turn was requested, no
real Codex home/service/session was inspected or modified. The private server
was terminated after each probe. No implementation or shared design doc edited.

## Proposed replacement: Pi and Codex

### Pi and Codex

- **Pi:** `harnesses.pi` entries are extension files, added as `--extension
  PATH` beside mv2's own `extension.ts` when the bridge is on. A Pi extension
  reaches mv2 through the CLI and helper.
- **Codex:** Codex has native plugins, skills, MCP servers and lifecycle hooks.
  It does not expose a Pi-style extension or Claude-style `--plugin-dir` in the
  checked CLI. mv2's first Codex integration is a declarative launch contribution,
  with explicit handling of plain TUI versus remote app-server launches. Native
  plugin installation and native hook trust remain Codex-owned operations.

The adapter's documented baseline is 0.159.3. This investigation found 0.162.1
installed. Both versions' pinned source have the capabilities below; only
0.162.1 was executed here. A config-schema field is evidence of a recognized
shape, not proof that the TUI forwards it or that every shape is usable.

#### What Codex actually offers

| Mechanism | Native capability and mv2 consequence |
| --- | --- |
| Launch config | `-c` / `--config key=value` accepts dotted paths and TOML values, with a raw-string fallback. Examples include model/effort/verbosity, TUI presentation, developer instructions, MCP configuration, hook configuration, skills selectors, plugin enablement and environment policy. mv2 accepts a small typed allowlist, rather than letting a plugin change arbitrary config. |
| `notify` | A single command argv array. Codex spawns it after an agent turn and appends one JSON argument with `type: agent-turn-complete`, thread/turn IDs, cwd, input messages and the last assistant message. It is fire-and-forget: output is discarded, and spawn errors do not fail the turn. It neither supplies a general lifecycle API nor replaces approvals or compaction. It can expose conversation text to its command. Preserve the user's notifier; v1 plugins use mv2 lifecycle events instead of claiming this singleton. |
| Lifecycle hooks | `SessionStart`, `SessionEnd`, `UserPromptSubmit`, `PreToolUse`, `PermissionRequest`, `PostToolUse`, `PreCompact`, `PostCompact`, `SubagentStart`, `SubagentStop`, `Stop`, `Interrupt`. Sources include config TOML, `.codex/hooks.json` layers and native plugin bundles. Command and MCP tool handlers exist; schema presence of `prompt`/`agent` handlers does not mean they execute. Non-managed hooks require native review against their current hash. mv2 trust does not satisfy Codex trust. Never synthesize `hooks.state.*.trusted_hash`, weaken native policy or add `--dangerously-bypass-hook-trust` on a plugin's behalf. |
| MCP | `mcp_servers.NAME` supports local stdio (`command`, `args`, `cwd`, `env`/`env_vars`) and HTTP (`url`, header/environment/auth settings), tool allow/deny lists and timeouts. Native plugin bundles can also contribute MCP servers. A plugin-owned MCP server is the practical way to give the Codex model tools; it does not need relay callbacks for tool execution. Authentication remains user-owned. |
| Skills and instructions | Native skills are discovered under repository `.agents/skills`, trusted project `.codex/skills`, user `.agents/skills`, the backward-compatible `$CODEX_HOME/skills`, system roots and native plugin skills. `skills.config` enables/disables discovered skills; it is not an arbitrary skill-root registration mechanism. `AGENTS.override.md`, `AGENTS.md` and fallback filenames participate in project instructions. `developer_instructions` adds developer text; `model_instructions_file` replaces the built-in model instructions and is unsuitable for plugin defaults. mv2 catalog skills remain available through the existing bootstrap/CLI without installing files in the user's home or repo. Custom prompts are deprecated in favor of skills and are not a v1 distribution target. |
| Native plugin packages | The checked CLI has `codex plugin add/list/remove` and marketplace management. A package can use `.codex-plugin/plugin.json`, skills, `.mcp.json` and hooks, or the portable Agent Plugins layout. `plugins.<name@marketplace>.enabled` selects a resolvable package; setting it does not create a package directory loader. mv2 v1 does not install marketplaces/packages, write `.agents`/`.codex` in a workspace, or modify `$CODEX_HOME`. A later native-package contribution must handle installation identity, refresh, trust and exact resume before offering `dir` as a supported field. |
| Profiles | Checked help says `--profile NAME` layers `$CODEX_HOME/NAME.config.toml`. The 0.159.3 loader explicitly rejects legacy `profile = "NAME"`, despite the schema retaining `profile`/`profiles`. Profiles belong to the user. Plugins neither create nor select them; existing mv2 bridge resume rejects profile overrides (auto chooses plain, required bridge fails). |

Sources: [0.159.3 config schema](https://github.com/openai/codex/blob/rust-v0.159.3/codex-rs/core/config.schema.json),
[notify implementation](https://github.com/openai/codex/blob/rust-v0.159.3/codex-rs/hooks/src/legacy_notify.rs),
[hook discovery and trust](https://github.com/openai/codex/blob/rust-v0.159.3/codex-rs/hooks/src/engine/discovery.rs),
[native plugin loader](https://github.com/openai/codex/blob/rust-v0.159.3/codex-rs/core-plugins/src/loader.rs),
[skill roots](https://github.com/openai/codex/blob/rust-v0.159.3/codex-rs/ext/skills/src/host_roots.rs),
[profile loader](https://github.com/openai/codex/blob/rust-v0.159.3/codex-rs/core/src/config/mod.rs),
[official hooks guide](https://learn.chatgpt.com/docs/hooks),
[official native package guide](https://developers.openai.com/plugins/build/plugins),
[deprecated prompts](https://learn.chatgpt.com/docs/custom-prompts).

#### The v1 launch contract

A proposed `harnesses.codex` entry is:

```json
{
  "min_version": "0.162.1",
  "max_version": "0.162.1",
  "instructions": "codex/instructions.md",
  "config": {"model_verbosity": "low"},
  "mcp_servers": {
    "tools": {
      "command": "python3",
      "args": ["{plugin_dir}/codex/server.py"],
      "enabled_tools": ["lookup"],
      "startup_timeout_sec": 10
    }
  }
}
```

This example pins the executed version, not a claim that the mechanism first
appeared there. Lower bounds can widen after native tests on those releases.
Entries require `harness.codex`; a local MCP command also declares
`process.spawn`. Relative instruction paths resolve inside the trusted plugin
root. Only documented placeholders are expanded, as argv elements, with no
shell evaluation. Snapshot instruction content and plugin provenance at launch.

- **`config`:** typed config values, not raw argument fragments. Initially allow
  `model_reasoning_effort`, `model_reasoning_summary`, `model_verbosity` and a
  versioned set of TUI presentation keys such as `tui.animations`,
  `tui.alternate_screen`, `tui.notifications`, `tui.notification_method`,
  `tui.status_line`. A plugin changing an array like `tui.status_line` supplies
  an entire default array; this is not an additive native status-item API.
  Require a known version/type/value for each key. Keep model/provider/service
  tier, credentials/endpoints, permissions/sandbox/approvals, environment
  policy, persistence/history, project trust, updates, compaction thresholds,
  features, profile selection and global skill/plugin/hook settings reserved
  to core and the user. No plugin gets arbitrary extra CLI args.
- **`instructions`:** optional UTF-8 file added to mv2's generated bootstrap in
  deterministic plugin-name order, with the plugin named in its wrapper. User
  explicit `developer_instructions` retains its existing replacement behavior;
  record when it suppresses the plugin text. Do not replace AGENTS files or
  model instructions. This reuses the existing bootstrap path in both modes.
- **`mcp_servers`:** server definitions under adapter-generated names
  `mv2_<plugin>_<local-name>` (normalize hyphens consistently and refuse
  collisions). Validate transport fields, timeouts and tool lists against the
  supported version. Default to optional startup (`required=false`) and keep
  approval decisions/authentication native. Do not permit inline bearer tokens
  or OAuth client secrets in manifests; use user-supplied environment references.
  Pass per-launch identity explicitly to a stdio server that needs it; a shared
  daemon does not inherit the TUI's whole environment. This field is the only
  v1 way a plugin contributes MCP config; generic `config.mcp_servers` is refused.

**Plain and bridge must have equivalent intent.** Plain mode encodes contributions
as `-c` overrides. Bridge mode needs a narrow, adapter-owned launch transform:
merge the approved session config into the exact `thread/start` or
`thread/resume` request, not daemon config. The remote TUI forwards only selected
keys; `notify`, `mcp_servers`, `hooks`, `skills`, `plugins` are missing from its
forwarding function in both 0.159.3 and 0.162.1. `-c mcp_servers...` by itself
therefore does not establish a bridge contribution. This launch transform is
part of the harness-side phase; it does not introduce plugin middleware or
import plugin Python into the runner.

Sources: [0.159.3 TUI request construction](https://github.com/openai/codex/blob/rust-v0.159.3/codex-rs/tui/src/app_server_session.rs),
[0.162.1 TUI request construction](https://github.com/openai/codex/blob/rust-v0.162.1/codex-rs/tui/src/app_server_session.rs).
The isolated 0.162.1 probe passed a namespaced local MCP server in
`thread/start.config`; `mcpServerStatus/list` returned `runtimeStatus=connected`
and the server's `ping` tool, without a model turn. A further isolated probe persisted a harmless raw user item via
`thread/inject_items`, then resumed the same loaded thread with a new MCP server
in `config`. Resume succeeded, but that server was absent from its MCP inventory.
Thus v1 promises new MCP activation on spawn only; on resume it can observe
retained servers but cannot promise an added/changed contribution took effect.
Cold-resume activation needs separate native proof before widening that contract.
Record requested versus observed state and never restart/unload a user's shared
service to force plugin config through.

**Merge and precedence.** Resolve core-owned launch settings separately. Conflicting
non-additive defaults from two plugins lose the conflicting contribution with
an actionable diagnostic; plugin-name order is not a silent winner. Distinct
MCP namespaces compose. Insert remaining plugin defaults before the user's
applicable `args` and `spawn_args`/`resume_args`, never after `--`. Recognize
`-c KEY=VALUE`, attached `-cKEY=VALUE`, `--config KEY=VALUE`, `--config=KEY=VALUE`
and native feature aliases. A user override of an ancestor, descendant or the
same key suppresses the overlapping plugin default. Interpret paths the way
that Codex version does (0.159.3 splits CLI paths on literal dots; do not invent
quoted-path semantics). Preserve explicit flag behavior, including `--model`
and `--profile`; do not reorder user's arguments. For bridge-only forwarding,
carry user session overrides of the contributed namespace as the highest
layer too, since the TUI may omit them. Incoming native request values beat
plugin defaults, and retained explicit user values beat both. The existing
`check_for_update_on_startup` scan stays authoritative; plugins cannot undo it.
Native managed requirements remain enforced by Codex.

**Gating and evidence.** `min_version` and optional `max_version` are inclusive
semantic-version bounds. An unknown/prerelease version needs explicit support,
not lexical comparison. Check the selected executable, not hardcoded `codex`.
CLI-only settings gate on the CLI version; bridge-side config also gates on the
actual daemon version, which mv2 already records separately and may differ.
Version compatibility does not replace protocol validation. An unsupported
entry is skipped with a reason; a broken plugin never prevents the core launch.
Keep a valid plain fallback and report contributions again after mode selection.

`runtime.json.plugins` records each plugin name/version/fingerprint and entry,
CLI/daemon versions, selected mode, accepted instruction snapshot/hash, applied
config **key names**, MCP server names, user-suppressed fields and reasons for
skips/conflicts/version failures. Distinguish `prepared`/`requested` from native
`confirmed`; a rendered argv or successful thread reply alone does not prove
an MCP connection or trusted hook execution. Record fallback/resume limitations
against the current launch ID. Never dump credentials, hook payloads,
conversation text, or raw merged config into this record.

Native hooks and native package directories are **recognized future integration
points**, not fields silently forwarded in v1. A later `hooks` declaration must
compose native hook sources without erasing user handlers, preserve native trust
and policy, and expose skipped/untrusted/disabled status. An mv2-enabled hook
plugin still cannot promise enforcement until native execution is verified.
A notifier needing only lifecycle observations already has mv2 events.

#### Relay observations and later requests

Today `runtime/rpc.py::LaunchProxy` transparently forwards native requests,
notifications and replies in both directions, with adapter launch preparation,
exact binding, naming and usage observation. Separate control RPCs initialize
with experimental API support and operate on the saved thread/workspace.
`status.py` consumes server-wide `thread/status/changed` broadcasts;
`usage.py` consumes exact-thread `thread/tokenUsage/updated` on the TUI stream.
There is no plugin relay subscription or generic RPC API today.

| Native surface | Possible plugin use; ownership rule |
| --- | --- |
| `turn/started`, `turn/completed`; `item/started`, `item/completed`; `thread/queue/changed`; `hook/started`, `hook/completed`; `thread/compacted` | Future passive events. Observe the existing bound TUI connection: turn/item events are not supplied by a passive server-wide status observer. Emit only after exact binding, filter thread IDs and include launch/thread/turn/item identity. Turn completion includes failure/interruption; it does not mean success. |
| `thread/status/changed`, `thread/tokenUsage/updated` | Existing mv2 status/context paths can feed the proposed event journal. Normalize payloads and publish metadata, rather than copying raw native frames. |
| `thread/read`, `thread/turns/list`, `thread/items/list`, `thread/queue/list`, `thread/backgroundTerminals/list` | Later bounded read API for the exact agent. Paginate, revalidate native ID and workspace, enforce permissions and avoid loading/resuming threads just to observe. |
| `thread/queue/add`, `thread/queue/start`, `turn/steer`, `turn/interrupt`, queue deletion and background terminal cleanup | Existing send/stop operations already own these semantics. Plugins call the public agent API, retaining durable message IDs and acceptance/receipt rules. Do not offer a raw alternative that bypasses them. |
| `item/commandExecution/requestApproval`, `item/fileChange/requestApproval`, `item/permissions/requestApproval`, `item/tool/requestUserInput`, `mcpServer/elicitation/request` | Server **requests**, carrying an `id`, not notifications. The TUI owns answers. Observers must neither answer nor swallow them. |
| `item/tool/call` and `dynamicTools` | Native dynamic tools require a client that answers tool calls on the owning connection. A passive plugin observer cannot implement them; v1 model tools use MCP. |
| `skills/extraRoots/set`, `plugin/install`, config write methods | Real native APIs, but not appropriate per-agent plugin operations. Extra skill roots are replaced on the shared skills service, and install/config APIs affect native persistent/global state. Do not expose them through a blanket relay permission. |

The installed `generate-json-schema --experimental` output and
[0.159.3 method definitions](https://github.com/openai/codex/blob/rust-v0.159.3/codex-rs/app-server-protocol/src/protocol/common.rs)
provide the concrete protocol inventory. The current relay already carries
these messages, but it does not expose them to plugin code. Add a small passive
observer later, with bounded queues/drop accounting and no runner-side plugin
imports, so a slow host cannot stall the TUI or an approval. Shared account,
skill and MCP notifications may lack a thread ID; they must not be attributed
to whichever agent happens to have a connection. Conversation/tool payloads
require a separate read permission and do not enter the ordinary event journal.

There is also a native `thread/start.selectedCapabilityRoots` API for selected
package roots (0.159.3 source and 0.162.1 schema). Its local MCP contribution
path has native tests. It is worth investigating for a future bridge-only
package loader, but it is not a TUI CLI flag and the checked resume params have
no matching field. Skill/hook loading, thread persistence, exact resume and
client feature gating need proof before promising `harnesses.codex.dir`.

#### Tree compaction: analysis only

Codex could eventually implement `CompactionSource`, but lifecycle signals alone
are insufficient. It needs stable completed-turn IDs from `turn/completed`, a
whole-conversation reader over paginated native turns/items (including tool
calls/results and images), consistent filtering of previous install rows, a
cancellable model builder with typed error/rate-limit mapping, and a renderer
that emits a valid Codex conversation with the protocol's required `messages`
and `count`. Modern native threads can have `path=null`; a guessed rollout
JSONL path is not an adequate transcript contract.

Most critically, it needs an atomic native **replacement** of the model-visible
history for the same thread, with persistence and ownership/idle locking. Native
`thread/compact/start` invokes Codex compaction, not a tree install;
`thread/inject_items` appends raw items, not replacement. The unstable Cloud-only
`thread/resume.history` is explicitly not a supported general install contract.
Editing SQLite/rollouts behind a loaded daemon or swapping to a fork would
violate exact identity and persistence. There is no supported tree-install API
established by this investigation.

Native `PreCompact` can stop compaction with `continue:false`; a Claude-style
`decision:block` is not accepted for this event. That is useful but is not a
replacement payload. Native hook trust, disablement, policy and failure behavior
must support a verified fail-closed guard for auto and manual compaction before
mv2 advertises tree compaction. Untrusted/missing hooks skip execution and cannot
enforce that guard. Keep current Codex tree-compaction capability unavailable.

Sources: [PreCompact implementation/tests](https://github.com/openai/codex/blob/rust-v0.159.3/codex-rs/hooks/src/events/compact.rs),
[history injection](https://github.com/openai/codex/blob/rust-v0.159.3/codex-rs/core/src/codex_thread.rs),
[thread resume fields](https://github.com/openai/codex/blob/rust-v0.159.3/codex-rs/app-server-protocol/src/protocol/v2/thread.rs),
and mv2's `src/multiverse/compaction/protocol.py`.

## Additional evidence and review checklist

- Installed isolated help: `--remote`, `-c`, `--strict-config`, `--enable` /
  `--disable`, `--profile` with `NAME.config.toml`, and
  `--dangerously-bypass-hook-trust`; no `--plugin-dir`. `codex plugin --help`
  lists add/list/remove/marketplace. Help/schema generation ran with no daemon.
- Private stdio `config/read` returned `model_verbosity=high` after consecutive
  `-c model_verbosity="low"` then `-c model_verbosity="high"`, and preserved
  `notify=["/bin/true"]`. Native config read succeeds without credentials.
- Private ephemeral `thread/start` accepted developer text and an MCP server
  definition. Enabling the tiny stdio server returned connected status and a
  discovered `ping` tool through `mcpServerStatus/list` with exact `threadId`.
- Private hooks probe supplied an inline SessionStart command through `-c`.
  `hooks/list` returned source `sessionFlags`, `enabled=true`, a current hash,
  and `trustStatus=untrusted`. The marker command did not run. This establishes
  discoverability/trust diagnostics, not execution of a trusted hook.
- 0.159.3 `config/src/overrides.rs` establishes ordered last-value override
  application and literal-dot path splitting. Do not treat JSON objects as
  TOML inline tables: serialize typed values with a TOML writer; JSON is usable
  for many string/array scalars but not general object config.
- In source, TUI remote config forwarding excludes MCP/hook/notify/plugin/skill
  keys in both inspected versions. Core/user existing config still loads from
  native layers; it is the per-launch CLI override propagation that is missing.
- Current mv2 `remote_arguments()` already moves bootstrap to
  `developerInstructions`, and `LaunchProtocol.outgoing()` handles only exact
  start/resume, protects workspace/permissions and refuses native fork. Extend
  that narrow transform rather than introducing a second plugin transport.
- Loaded-thread resume was probed after model-free history injection: the same
  native ID resumed successfully, but a newly requested MCP server did not
  appear. This is positive evidence of a resume limitation, not activation.
- No claim of tested remote-TUI plugin end-to-end operation, cold MCP resume,
  trusted lifecycle execution, native package install or tree replacement is made.
  Tests before implementation should cover plain/bridge/fallback, parent/child
  user config overrides, `--` prompt boundaries, duplicate plugins, daemon/CLI
  version mismatch and failed native activation. Use isolated native homes only.

### Complete 0.159.3 top-level config schema key inventory

These are recognized schema keys, **not the proposed plugin allowlist**. Some
are legacy, constrained, experimental, local-only or unforwarded. Dotted
subfields are described by the linked versioned schema definitions.

`agents`, `allow_login_shell`, `allow_symlinked_codex_home`, `analytics`,
`approval_policy`, `approvals_reviewer`, `apps`, `apps_mcp_product_sku`, `audio`,
`auto_review`, `background_terminal_max_timeout`, `browser_use`,
`chatgpt_base_url`, `check_for_update_on_startup`, `cli_auth_credentials_store`,
`cloud`, `compact_prompt`, `computer_use`, `default_permissions`, `desktop`,
`developer_instructions`, `disable_paste_burst`, `experimental_compact_prompt_file`,
`experimental_realtime_start_instructions`,
`experimental_realtime_webrtc_call_base_url`,
`experimental_realtime_ws_backend_prompt`, `experimental_realtime_ws_base_url`,
`experimental_realtime_ws_model`, `experimental_realtime_ws_startup_context`,
`experimental_thread_store`, `experimental_use_unified_exec_tool`, `features`,
`feedback`, `file_opener`, `forced_chatgpt_workspace_id`, `forced_login_method`,
`ghost_snapshot`, `goals`, `hide_agent_reasoning`, `history`, `hooks`,
`include_apps_instructions`, `include_collaboration_mode_instructions`,
`include_environment_context`, `include_permissions_instructions`, `instructions`,
`log_dir`, `marketplaces`, `mcp_enterprise_managed_auth`,
`mcp_oauth_callback_port`, `mcp_oauth_callback_url`, `mcp_oauth_credentials_store`,
`mcp_optional_startup_grace_ms`, `mcp_servers`, `memories`, `model`,
`model_auto_compact_token_limit`, `model_auto_compact_token_limit_scope`,
`model_catalog_json`, `model_context_window`, `model_instructions_file`,
`model_post_turn_compact_threshold_percent`, `model_provider`, `model_providers`,
`model_reasoning_effort`, `model_reasoning_summary`, `model_verbosity`, `notice`,
`notify`, `openai_base_url`, `orchestrator`, `oss_provider`, `otel`, `permissions`,
`personality`, `plan_mode_reasoning_effort`, `plugins`, `profile`, `profiles`,
`project_doc_fallback_filenames`, `project_doc_max_bytes`, `project_root_markers`,
`projects`, `realtime`, `responses_api_metadata`, `review_model`, `sandbox_mode`,
`sandbox_workspace_write`, `service_tier`, `shell_environment_policy`,
`show_raw_agent_reasoning`, `skills`, `sqlite_home`,
`suppress_unstable_features_warning`, `thread_unload_delay_secs`,
`tool_output_token_limit`, `tool_suggest`, `tools`, `tui`, `web_search`, `windows`.
