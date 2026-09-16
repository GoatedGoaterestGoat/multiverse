# Template development work log

Workspace: `LAB/worktrees/template-dev`, branch `lab/template-dev`.
Baseline tested: `3c7de7ed69f732a0e70362b69e3127022cf3e587`.
Commit identity: shaoxiongduan <shaoxiongduan@gmail.com>.

## LAB-SOCKET-001 / LAB-INBOX-001

Testing-manager assigned both bugs after actual native Codex reproduction.
The first local checkpoint independently reproduced the missing-inbox exception.
Offline launch tests reproduced the missing socket in both spawn/resume requests.

- `agents.start` now writes the actual socket returned by tmux into request.env
  before releasing the runner's `ready` barrier. Other inherited MV2 identity
  continues to be removed by the runner.
- `ack` handles a missing inbox as empty, matching `inbox`; invalid, backwards,
  and incomplete-record cursors remain rejected. CLI lookup still rejects an
  unknown agent without creating state.
- No workers or additional tmux servers were created for these tests. Mocked
  launch regressions exercise request construction and runner subprocess env.

Validation from the workspace (Python = `LAB/repo/multiverse-v2/.venv/bin/python`):

```
python -m unittest discover -s multiverse-v2/tests -p test_launch_isolation.py -v
PYTHONPATH=multiverse-v2/src:multiverse-v2/tests python -m unittest test_multiverse.FilesTest test_harnesses.HarnessFilesTest -v
```

Evidence: `LAB/artifacts/template-dev/regressions-before.txt` (two reproduced
errors), `regressions-after.txt` (four passing regressions), and
`files-harness-after.txt` (nine existing checks pass). Independent live committed
fix retest is assigned to testing-manager/harness worker; local checks are not
claimed as that independent verification.

Fix commit: `14ff6901570233a63dcdfd5b7786d91c5521bba4`.

## LAB-SPAWN-003

Testing-manager reported repeated names after a sandbox-denied spawn followed by
a successful retry. A mocked failure/retry reproduced the missing recovery
guidance at `14ff690`. Failed records remain intact and duplicate labels stay
supported. Launch errors now expose the preserved UUID and suggest either exact
resume (file/bound native session) or unique-name spawn (unbound native session).
Ambiguous resolution lists candidate UUIDs and still refuses to choose one.
Recovery details live in `docs/launch-recovery.md`.

Evidence: `LAB/artifacts/template-dev/spawn-before.txt` (expected failing new
regression) and `spawn-after.txt` (five launch/inbox checks pass). Independent
committed-fix retest requested separately.

Recovery commit: `adfcdb78f620187defe6a7120b7f6d9283ce534a`. Testing-manager
independently verified it against archived source and real persisted duplicate
records; evidence `LAB/artifacts/testing/LAB-SPAWN-003-retest.txt`. Closed as
recovery/diagnostics, not deduplication or deletion.

## Reusable templates

Bundled `testing` and `development` templates include a short bootstrap pointer,
lazy role/task workflows, the unchanged declarative Codex adapter, blank evidence
forms and distinct local views. Schema coordinated with UI development: markdown,
inline table, and artifacts widgets. No workers or fake results are preinstalled.
See `docs/role-templates.md` for installation and usage.

Validation: `python -m unittest discover -s multiverse-v2/tests -p test_templates.py -v`.
Tests exercise fresh IDs and empty-state cloning, adapter consistency, lazy prompt
loading, prompt size, and artifact paths. Evidence:
`LAB/artifacts/template-dev/templates-tests.txt`.

Template commit: `6b4baca39901700942ece73fab399a44228a25a4`.

## Independent results and session audit

Harness tester independently VERIFIED `14ff690` (identical-source cherry-pick
`da3c863`) with 18 offline checks and an authorized actual Codex/Astra probe:
lab socket propagation, fresh-inbox ack, native binding, exact resume and actual
idle/resumed native queue responses. Root integrated `08da915` passed the live
resume check. Evidence: `LAB/artifacts/testing-harnesses/verification-report.md`,
tester tests/log commit `91d1d7f`. Disposable probe was stopped by its owner.

Local session audit verifies stale CLI callbacks leave current inbox, binding,
status and runtime unchanged. Combined 18 launch/template/file/harness checks pass:
`PYTHONPATH=multiverse-v2/src:multiverse-v2/tests python -m unittest test_launch_isolation test_templates test_multiverse.FilesTest test_harnesses.HarnessFilesTest -v`.
Evidence: `LAB/artifacts/template-dev/combined-checks.txt`.

New report LAB-SESSION-004 records synthetic cross-harness marker leakage; sent
to testing-manager/director with reproduction script and evidence under
`LAB/artifacts/template-dev/session-audit.*`. Documented explicit binding's
caller-trust/global-uniqueness limitation without claiming an automatic live
misbinding. See `docs/session-isolation.md`. No session registry or additional
workers were introduced.

Testing-manager independently verified template commit `6b4baca` with fresh CLI
installs and all three template tests; evidence
`LAB/artifacts/testing/templates-6b4baca-retest.txt`.

LAB-SESSION-004 assigned bounded cleanup: all five bundled manifests now unset
both known markers, preserving custom MV2 launch identity. New regression fails
in all five cases before the fix and passes afterward. Combined 19 checks pass;
evidence `markers-before.txt` (synthetic environment only) and `markers-after.txt`.
No live-manifest rewrite or additional live probe performed; director owns
reviewed installed-manifest deployment after independent offline retest.

Marker cleanup commit: `1fe2fe4ec139ccc0d308dabf12c51b6377321ca5`.
Harness tester independently VERIFIED all five adapters with actual runner
subprocesses: each fails at `6b4baca` and passes at this commit; combined 25
independent offline checks pass. Evidence:
`LAB/artifacts/testing-harnesses/session-004-before.txt`, `session-004-after.txt`,
`session-004-suite.txt`; tester test/log commit `d7e9b40`.

## Lab-only default model proposal

Director requested an Astra default for blank inline model fields, retaining an
explicit override and leaving reusable templates free of a forced model.
Prepared a review-only bundle at
`LAB/artifacts/template-dev/live-manifest-proposal/`: 33 exact current manifest
paths, before/after hashes, staged JSON, full diff and deployment/rollback notes.
No installed manifests were changed by this worker. Director owns rollout.

Lab Codex spawn/resume add `-c model="gpt-6-astra"`. Explicit `model_args` add both
`--model {model}` and a later `-c model="{model}"`, so the flag/config values agree.
All 44 blank/explicit spawn/resume request-construction combinations pass. An
actual no-model-turn Codex `config/read` diagnostic confirms a blank/default
config resolves Astra and later explicit config wins. It created no threads,
turns, workers or tmux sessions; SQLite/log paths were scoped to LAB and both
short-lived stdio processes were stopped. Sandbox escalation was approved after
OS denied startup. Evidence: `live-manifest-proposal-result.txt`,
`model-config-resolution.txt`, with exact scripts beside them.

Official configuration sources consulted: [configuration basics](https://learn.chatgpt.com/docs/config-file/config-basic)
and [configuration reference](https://learn.chatgpt.com/docs/config-file/config-reference).
The proposal avoids relying on undocumented precedence between a conflicting
`--model` flag and `-c model` setting; no model turn is claimed for this diagnostic.

Director subsequently deployed the reviewed bundle. Final read-only verification
confirmed all 33 installed files exactly match their reviewed after hashes and
marker/default/override assertions. Reusable source adapters remain model-neutral.
Evidence: `LAB/artifacts/template-dev/installed-manifests-verified.json`. No
processes, agents or restarts were needed for this deployment verification.
