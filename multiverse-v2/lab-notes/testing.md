# Independent testing record

Baseline `3c7de7ed69f732a0e70362b69e3127022cf3e587`. Manager native session `01a0a9ba-de48-7101-9318-4d2ccde753a8` was bound before V2 worker launch. All commits use shaoxiongduan <shaoxiongduan@gmail.com>. Evidence paths below are relative to `/Users/admin/Documents/Github/Multiverse-Lab/`.

## Reproduction → fix → independent retest queue

| ID | Severity | Reproduction / outcome | Fix commit | Verifier / status |
|---|---|---|---|---|
| LAB-SOCKET-001 (LAB-H001) | high | Actual Codex child socket missing; fixed spawn/resume now retain actual lab socket | `14ff690` | testing-harnesses: VERIFIED, including live root `08da915` |
| LAB-INBOX-001 (LAB-H002, MV2-EMPTY-INBOX-ACK) | medium | Fresh inbox cursor 0 ack raised ENOENT; fixed live child ack succeeds | `14ff690` | testing-harnesses: VERIFIED |
| LAB-SPAWN-003 | medium | Failed spawn + same-name retry ambiguous; actionable UUID/recovery guidance added | `adfcdb7` | testing-manager: VERIFIED; labels may still repeat |
| LAB-SESSION-004 | medium | Cross-harness parent native markers survive; five manifests now clear both | `1fe2fe4` | testing-harnesses: VERIFIED; director deployed installed manifests |
| LAB-UI001 | high | Enter folded group instead of entering local scope | `bfa7790` + `65f228f` | testing-ui: VERIFIED Pilot + actual multilevel/back/resize/cancel |
| LAB-VIEW-004 | low | External artifact path rejected; named accent invalid | `814e775` | testing-ui: VERIFIED actual local artifact modal |
| LAB-UI002 | low | Nested tmux detach/return hint misleading | `bfa7790` | testing-ui: VERIFIED rendered return/detach guidance |
| LAB-UI004 | medium | Buffered slash/query/Enter invokes global shortcut before input focus; paced search works | none | Reproduced; OPEN finite-milestone follow-up |
| LAB-UI003 | high | Attach error escapes suspend, leaving blank/cooked terminal | `65f228f` | testing-ui: VERIFIED actual terminal recovery + separate same-loop cursor regression |

Bugs close only after independent verification of the actual fix commit. Developer self-tests alone do not close the queue. Initial exact commands/expected/actual are preserved in `testing-initial-bugs.md` and `artifacts/testing/initial-bugs.md`.

## Harness evidence

- Manager independently reproduced missing `MV2_TMUX_SOCKET` and empty inbox `ack --cursor 0` on baseline. Developer `14ff690` was tested from tree-identical cherry-pick `da3c863`; root CLI `08da915` was exercised by actual native resume. Five independent contracts and 18 combined safe offline tests passed. Test/evidence commit `91d1d7f`.
- Probe `d091e3c8-3827-4b35-948f-9a2ced62a69d`, native `01a0a9bd-9ed2-73c0-862a-c4c06cb59fd3`: `probe-start.json` records correct socket, workspace, fresh identity and successful empty ack. Actual pane tags match. Evidence under `artifacts/testing-harnesses/`.
- Idle queue: durable challenge `5a80936c-58ab-4106-8954-253d357a8054` contains no nonce. Native queue `01a0a9be-84e8-7df1-81d9-cfe33e1f1154` supplies `HQUEUE-7dd863c8-6b50`; actual V2 response `44e531b5-f49f-466e-9ca1-e852f436b742` returns it. `probe-idle-before-queue.txt`, `queue-native-receipt.json`, `queue-actual-response.json` distinguish acceptance from actual delivery/response.
- Exact resume retained that native UUID, changed launch from `0eb97b73-d844-4dfe-9453-deffa6c55ba4` to `d33eac8c-06f2-430b-9263-2a2b250187b1`, retained lab socket, and used root CLI. Actual response `a2926d0c-bda4-4be7-9d50-2b79e1b762c5` returns queue-only `HRESUME-a82e9323-075c`, new launch and successful ack. See `resume-contract.json`, `resume-actual-response.json`, `probe-resumed-turn.json`. Final probe stopped; pane absent (`final-observations.json`).
- LAB-SPAWN-003: exact `adfcdb7` source archived with `git archive`; read-only resolution against actual failed/live duplicate records enumerates UUIDs and resolves exact IDs. Five launch regression checks pass. `artifacts/testing/LAB-SPAWN-003-retest.txt`.
- LAB-SESSION-004: independent real runner subprocess with synthetic markers fails all five manifests at `6b4baca`, passes exact `1fe2fe4` (tree-identical cherry `b6ebbd9`). 25 combined offline tests pass. `session-004-before.txt`, `session-004-after.txt`, `session-004-suite.txt`; test/evidence commit `d7e9b40`. This proves marker cleanup, not automatic model misbinding or global native-ID uniqueness.
- Alternate finite smoke, explicitly allocated after Codex probe stopped: authenticated Claude produced actual assistant nonce `CLAUDE_SMOKE_OK_6dcb842a`; test agent `54eda697-0f9b-4a47-a964-8077953031cf` stopped after 42.6 seconds, pane absent. No Claude resume/queue claim. Pi live skipped because default google credentials are not configured; offline adapter contracts pass. `alternate-readiness-native.json`, redacted `claude-smoke-pane.txt`, `alternate-final-observations.json`. No active probes remain; no further probes authorized.

## UI evidence and correction

Baseline existing UI tests: 17 pass; independent portal test fails while hover/draft/resize passes (`artifacts/testing-ui/baseline-independent-tests.log`). Independent test commits `e7cddc9`, `750c727`. Portal exact `bfa7790` source (test cherry `0ad33a3`) passes three independent Pilot checks and installed fixture checks at 120×32, 80×24, 60×20. Headless SVGs are explicitly not terminal captures.

LAB-UI003 actual deterministic OSError backend reproduces blank terminal on `bfa7790` in lab pane `%15`; `UI003-before.txt`, `UI003-after.txt`, terminal `20260916T102916Z-testing-ui-UI003-reproduced/pane-15.svg`. Exact `65f228f` restores error rendering, subsequent draft/cancel and batched navigation: `UI003-fixed-error.txt`, `UI003-fixed-draft.txt`, `UI003-fixed-batch.txt` reviewed by manager. Both independent same-loop cursor and suspend regressions fail prior source and pass fix; 30 UI tests pass. Director failed navigation frames prove attach suspension only, not stale selection.

Evidence correction: preliminary `portal-verification.txt` and `20260916T102811Z-testing-ui-bfa7790-verification/pane-14.svg` show CAD with Verification selected, not entered; `portal-back-mockups.txt` contains a draft. Those live success claims were retracted. Corrected `fixed-04-verification.txt` and terminal `20260916T103248Z-testing-ui-65f228f-verification-confirmed/pane-19.svg` show entered Verification masthead; corrected back/resize/cancel and artifact preview acceptance verified. Director independently captured correct entered scope at integrated `c006133`: terminal `20260916T103159Z-integrated-verification-confirmed/pane-0.txt`, also reviewed by manager.

## Reusable templates and topology

Exact `6b4baca` CLI fresh `init --template testing` and `init --template development` installations pass: distinct identities, empty agents/queues, local readable forms, catalog and explicit lazy role commands. All three template tests pass. `artifacts/testing/templates-6b4baca-retest.txt`. Installed manifest upgrades remain explicit director-controlled actions.

V2-created nested universes use `LAB/templates/lab` and separate worktrees/branches: `testing/subverses/ui` → `worktrees/testing-ui` / `lab/testing-ui`; `testing/subverses/harnesses` → `worktrees/testing-harnesses` / `lab/testing-harnesses`. Both workers use Codex `gpt-6-astra` and explicit lab socket on every spawn/resume.

- UI worker `e5d01748-d437-485f-a64c-08357284bc6f`, native `01a0a9bc-1ebd-78b2-8ff1-3ec97d645c62`.
- Harness worker `c5b473e2-9e29-4a73-96d6-5801994ac937`, native `01a0a9bc-1ec2-7ae2-a267-4d325da53841`.
- Seven ordinary agents max; only one bounded test occupied the eighth slot at any time. Both disposable probes stopped. UI evidence sessions contain no agents.
- Initial sandbox-denied spawn records retained and archived under `artifacts/testing/failed-spawn-ID`; real worker IDs used for disambiguation. Superseded records phase done, runtime failed preserved.
- No implementation edits in testing-manager scope. Only testing notes/view/evidence committed. Inbox checked and returned cursors acknowledged at checkpoints.

Install `testing-view.json` as testing universe `views/default.json`; copy `testing-initial-bugs.md` to local `artifacts/initial-bugs.md`. The local artifact path and `#f9e2af` accent are deliberate. Safe offline selections avoid existing terminal suites that create other tmux servers and kill-server; sandbox errors in broader discovery are not hidden as passes.

LAB-VIEW-004 actual artifact modal verified: `artifacts/testing-ui/testing-artifact-preview.txt`, terminal `20260916T103026Z-testing-ui-814e775-artifact/pane-16.svg`. LAB-UI002 rendered help verified: `help-bfa7790-full.txt` lines 51–53 distinguish Ctrl-b L previous session from Ctrl-b d client detach; original host separately exercised both. Manager reviewed frame text. Director deployed 33 reviewed lab manifests; developer read-only hash/semantic verification is `artifacts/template-dev/installed-manifests-verified.json`. No new model calls/restarts were needed for rollout.

LAB-UI004 confirmed in one bounded comparison at exact `65f228f` source (test cherry `848d508`): `tmux -S LAB/run/tmux.sock send-keys -t %20 / \"Testing queue\" Enter` produced an empty filter and **Stop director?** confirmation. No Ctrl+Enter was sent; standalone Escape canceled; no agent stopped. Paced slash → wait for input focus → query → Enter works. Evidence `artifacts/testing-ui/UI004-before.txt`, `UI004-batched.txt`, `UI004-paced-{filter-ready,query,after-enter}.txt`; manager reviewed actual modal frame. Medium OPEN follow-up under director finite no-source-expansion scope; do not mark fixed. Workaround: press slash, allow filter focus, then type query.

Final LAB-UI001 frame QA: manager and mockups-builder reviewed `fixed-04-verification.txt`, `fixed-06-canceled-60x20.txt`, `fixed-07-back-cad.txt`, `fixed-08-back-mockups.txt`: entered Verification, draft canceled while preserving resized scope, restored CAD with Verification selection, restored Mockups with CAD folded state. These corrected `65f228f` frames replace the retracted earlier claim. Both stopped probe property summaries now phase done with verified outcomes; runtime and native evidence unchanged.

Final roster independently observed via V2 recursive JSON: seven live ordinary agents, seven distinct native IDs, all on lab socket; both disposable probes stopped/done. `artifacts/testing/final-roster.json`. Director integrated independent UI commits `e7cddc9`, `750c727`, `58e8c48`, `4706580`; combined integration revision `7b0f13f` reports 55/55 safe regression checks passing (`artifacts/director/final-integration-tests.log`), production source identical to running `c006133`. No broad rerun needed without a new change.

Final UI test/evidence series: `e7cddc9`, `750c727`, `58e8c48`, `4706580`, `bb0fed2`, `cf0b7f6` (do not duplicate production cherry-picks `0ad33a3`/`848d508`). Tester-owned `testing-ui-evidence` non-agent session removed after final capture `20260916T103716Z-testing-ui-final-before-cleanup`; `artifacts/testing-ui/cleanup-remaining-panes.txt` preserves cleanup audit. Ordinary tester Codex session remains available. Director explicitly accepted LAB-UI004 as medium OPEN follow-up without further source expansion for this milestone. Eight prior fixes remain verified.
