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
