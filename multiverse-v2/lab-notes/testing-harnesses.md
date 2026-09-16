# Independent harness verification

Tester: testing-harnesses (`c5b473e2-9e29-4a73-96d6-5801994ac937`), native session `01a0a9bc-1ec2-7ae2-a267-4d325da53841`.

Baseline `3c7de7ed69f732a0e70362b69e3127022cf3e587`: independent offline checks reproduce LAB-SOCKET-001 (alias LAB-H001), missing socket in runner request, and LAB-INBOX-001 (alias LAB-H002), missing-file error acknowledging fresh cursor zero. Actual worker fresh-inbox command also reproduced H002.

Reviewed committed developer fix `14ff6901570233a63dcdfd5b7786d91c5521bba4`, cherry-picked as `da3c863` with identical source tree. Independent five-test file `tests/test_lab_harness_contracts.py` passes after both failures on baseline. Combined 18 offline tests pass:

```sh
PYTHONPATH=multiverse-v2/src:multiverse-v2/tests LAB/repo/multiverse-v2/.venv/bin/python -m unittest test_multiverse.FilesTest test_harnesses.HarnessFilesTest test_launch_isolation test_lab_harness_contracts -v
```

New coverage uses fake terminal backend and a real bounded runner subprocess with no model calls. Checks socket propagation, removal of parent native/MV2 identity, Codex/Claude/Pi exact resume arguments, new launch IDs, stale launch rejection, unbound Codex refusal, empty inbox ack, message/reply IDs, cursor boundaries and nonexistent-agent rejection. Existing terminal suites were not run: they create other tmux sockets and kill-server, prohibited in this lab.

Configured CLI versions: Codex 0.154.0, Claude Code 2.1.231, Pi 0.84.3. All executable/manifests present. Claude/Pi only have offline adapter-contract coverage; no authentication or successful live alternate model turn is claimed. Manager explicitly allocated one ordinary Codex/Astra probe and prohibited additional live alternatives.

Evidence directory: `/Users/admin/Documents/Github/Multiverse-Lab/artifacts/testing-harnesses/`. Baseline `offline-baseline.txt`; fixed independent run `offline-fix-14ff690.txt`; combined `offline-suite-fix.txt`; catalog `configured-harnesses.json`.

## Live probe

Manager allocation message `3ac16e1e-f798-4303-9e10-14c8a84c7390` authorized one ordinary Codex/Astra probe. Spawn explicitly set `MV2_TMUX_SOCKET=LAB/run/tmux.sock`, using fixed worktree CLI and `--model gpt-6-astra --task-file LAB/artifacts/testing-harnesses/probe-task.txt`. Agent `d091e3c8-3827-4b35-948f-9a2ced62a69d`, native `01a0a9bd-9ed2-73c0-862a-c4c06cb59fd3`, first launch `0eb97b73-d844-4dfe-9453-deffa6c55ba4`, pane `%9`. Live child evidence confirms own native identity, assigned workspace, correct actual socket, fresh empty inbox and successful cursor-zero ack. Pane tags captured on lab socket match agent/universe/launch.

Idle queue delivery is proven by actual response, not receipt: initial idle pane saved; durable challenge `5a80936c-58ab-4106-8954-253d357a8054` deliberately contains no nonce; native queue `01a0a9be-84e8-7df1-81d9-cfe33e1f1154` sends `HQUEUE-7dd863c8-6b50`; actual response `44e531b5-f49f-466e-9ca1-e852f436b742` returns exact nonce, durable ID, native ID and launch ID. Evidence: `queue-actual-response.json`, `queue-native-receipt.json`, `probe-idle-before-queue.txt`, `probe-after-queue.txt`.

Stopped the idle disposable probe and ran root integrated CLI `08da915ec07522a145a219b40df8a27ee4713690` with explicit lab socket: `mv2 resume d091e3c8-3827-4b35-948f-9a2ced62a69d --model gpt-6-astra`. Root agents/store source matches developer fix. `resume-contract.json` verifies exact recorded native UUID in argv, no latest-session fallback, changed launch, same socket and root CLI environment. Actual post-resume queue `01a0a9bf-aeea-7381-8840-817cce4ca50a` yielded response `a2926d0c-bda4-4be7-9d50-2b79e1b762c5` with exact nonce `HRESUME-a82e9323-075c`, D2 message `dd2205c4-5ce2-4c0b-b4c8-96cd015d98f8`, unchanged native UUID, new launch `d33eac8c-06f2-430b-9263-2a2b250187b1`, lab socket and root CLI. Rebinding and inbox ack cursor 967 succeeded in the actual resumed model turn. Evidence: `probe-resumed-turn.json`, `resume-actual-response.json`, `probe-post-resume-turn.txt`, `pane-tags-resume.txt`.

Final disposable probe stop succeeded; `final-observations.json` confirms stopped and recorded pane absent. No extra agent or alternative live provider was launched. **LAB-SOCKET-001 and LAB-INBOX-001 independently VERIFIED on committed developer fix and integrated root CLI.** Harness worker remains available for retests.

## LAB-SESSION-004 retest and finite alternate smoke

LAB-SESSION-004: VERIFIED against developer commit `1fe2fe4ec139ccc0d308dabf12c51b6377321ca5`, cherry-picked as `b6ebbd9` with identical source. Independent `test_lab_session_isolation.py` launches the actual runner with synthetic parent CODEX_THREAD_ID and CLAUDECODE values and captures only marker names and fresh MV2 identity. All five manifests fail at pre-fix `6b4baca`; all five pass after fix. Combined offline suite: 25 tests pass. No model-misbinding or global native-ID uniqueness claim.

Evidence: `session-004-before.txt`, `session-004-after.txt`, `session-004-suite.txt`. Repro: `MV2_SESSION_TEST_SOURCE=<exact snapshot>/multiverse-v2/src LAB/repo/multiverse-v2/.venv/bin/python -m unittest discover -s multiverse-v2/tests -p test_lab_session_isolation.py -v`. Source snapshots were read from committed Git objects; original source repository untouched.

Installed universe manifests are separate from bundled templates. They were not edited or implicitly upgraded by this retest; rollout remains director-controlled.

Manager allocation `ed257b2a-afc7-4205-a553-ceb628516a2a` authorized at most one finite smoke per usable alternate provider, sequential, approximately 60 seconds initial attempt, no auth setup or retries. Native readiness check found Claude authenticated via claude.ai. Sandbox-only check falsely reported logged out; native readiness check corrected that. No credentials were printed. Pi default google reports `not_ready / credentials_not_configured`; its auth provider list is empty, no custom models file or credential environment keys detected. Pi live smoke was therefore skipped; offline adapter/isolation coverage passes. Evidence `alternate-readiness-native.json`.

Claude smoke agent `54eda697-0f9b-4a47-a964-8077953031cf`, assigned native UUID identical to agent ID, launch `8b5882cd-3b7f-4036-9475-149e205ccb45`, pane `%17`. Command: explicit `MV2_TMUX_SOCKET=LAB/run/tmux.sock mv2 spawn --name harness-test-claude-smoke --harness claude --task <single response nonce>`, using configured default model (pane reports Fable 5). Actual assistant response `CLAUDE_SMOKE_OK_6dcb842a` captured, distinct from prompt echo. Probe stopped after 42.6 seconds; final observations verify recorded pane absent. No retry, alternate auth setup, extra workers, or second simultaneous probe. This establishes one real Claude model response; it does not claim Claude queue/resume verification.

Evidence: `claude-smoke-spawn.json`, `claude-smoke-pane.txt` (account banner redacted), `claude-smoke-stop.json`, `alternate-final-observations.json`. Bounded slot released; testing-harnesses remains available for retests.
