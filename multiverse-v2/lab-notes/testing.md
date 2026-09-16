# Testing coordination

Baseline: `3c7de7ed69f732a0e70362b69e3127022cf3e587`. Native manager session: `01a0a9ba-de48-7101-9318-4d2ccde753a8`.

## Queue

| ID | Severity | Reproduction | Fix owner | Independent verifier | Status |
|---|---|---|---|---|---|
| LAB-SOCKET-001 (LAB-H001) | high | Actual child `MV2_TMUX_SOCKET` unset | template-dev | testing-harnesses | Verified `14ff690` + live root `08da915` |
| LAB-INBOX-001 (LAB-H002, MV2-EMPTY-INBOX-ACK) | medium | Empty inbox cursor 0 then ack exits 1 ENOENT | template-dev | testing-harnesses | Verified `14ff690` + live root `08da915` |
| LAB-SPAWN-003 | medium | Failed sandbox spawn + same-name retry makes send ambiguous | template-dev | testing-manager | Verified recovery fix `adfcdb7` |
| LAB-VIEW-004 | low | External report link rejected by portal containment; named accent invalid | testing-manager | testing-ui | Corrected `814e775`; independent portal retest pending |
| LAB-UI002 | low | Nested tmux detach exits client rather than returning to dashboard | ui-dev | testing-ui | Retesting help in `bfa7790` |
| LAB-SESSION-004 | medium | Cross-harness native markers survive child environment | template-dev | testing-harnesses | Assigned bounded manifest fix; synthetic runner repro/retest |
| LAB-UI001 | high | Enter CAD folds; scope unchanged; independent Pilot fails | ui-dev | testing-ui | Retesting portal `bfa7790` |

Reproduction commands and expected/actual: `/Users/admin/Documents/Github/Multiverse-Lab/artifacts/testing/initial-bugs.md`.
A fix is closed only after independent verification of the actual commit, with evidence.

## Worker topology

- Manager existed and bound before launching workers.
- `testing/subverses/ui`: testing-ui (`e5d01748-d437-485f-a64c-08357284bc6f`), worktree `testing-ui`, branch `lab/testing-ui`, lab socket pane `%7`.
- `testing/subverses/harnesses`: testing-harnesses (`c5b473e2-9e29-4a73-96d6-5801994ac937`), worktree `testing-harnesses`, branch `lab/testing-harnesses`, lab socket pane `%8`.
- Both V2 Codex `gpt-6-astra` spawns explicitly set lab `MV2_TMUX_SOCKET`; sandbox denied socket initially, approved escalated commands succeeded.
- Total seven ordinary agents; one bounded eighth-slot probe reserved, no further workers.
- Worker prompts: `artifacts/testing/ui-task.txt`, `artifacts/testing/harness-task.txt` under LAB.
- Actual dashboard input requires director coordination; Pilot can proceed independently.
- Queue acceptance alone is not native delivery proof; record target native ID and actual challenge response.

`testing-view.json` is the committed declarative view installed at testing universe `views/default.json`. Inbox checked and acknowledged at each coordination checkpoint.

Actual worker native sessions: UI `01a0a9bc-1ebd-78b2-8ff1-3ec97d645c62`; harness `01a0a9bc-1ec2-7ae2-a267-4d325da53841`. Fix commit `14ff6901570233a63dcdfd5b7786d91c5521bba4` assigned for independent harness retest. The single bounded live probe slot is allocated to harness worker; alternate executables are present but no live alternate success is claimed.

Failed spawn records retain runtime `failed` without panes. Their evidence is archived under `artifacts/testing/failed-spawn-ID`; no records deleted. Lab server roster is seven real ordinary agents. Commands using same names are ambiguous, so authoritative IDs are required.

Baseline independent UI: 17 existing tests pass; independent hover/draft/resize passes and portal navigation fails. `artifacts/testing-ui/baseline-independent-tests.log`. Director authorized separate `testing-ui-evidence` non-agent TUI on the lab socket. Root integrated core fix as `08da915`; harness cherry-pick `da3c863` has developer `14ff690` tree. Five independent offline harness contracts pass; actual probe pending.

View install: copy `testing-view.json` to testing universe `views/default.json`; copy `testing-initial-bugs.md` to local `artifacts/initial-bugs.md`. Local artifact containment is deliberate; external paths/symlinks are rejected. Accent uses `#f9e2af`. Independent UI baseline test commit: `e7cddc9`.

LAB-SPAWN-003 independent retest: exact source `adfcdb78f620187defe6a7120b7f6d9283ce534a` extracted with `git archive`; resolver tested read-only against actual failed/live duplicate records and reports both UUIDs; exact UUIDs still resolve. Five developer launch regression checks also pass. Evidence `artifacts/testing/LAB-SPAWN-003-retest.txt`. Closed as explicit recovery/diagnostics improvement; duplicate names remain intentional. Failed phantom summaries shortened via V2 status with their recorded launch IDs.

## Native queue proof

Harness tester used recorded probe native ID `01a0a9bd-9ed2-73c0-862a-c4c06cb59fd3`. Idle pane captured before queue. Durable challenge `5a80936c-58ab-4106-8954-253d357a8054` preceded native queue `01a0a9be-84e8-7df1-81d9-cfe33e1f1154`. Actual V2 response `44e531b5-f49f-466e-9ca1-e852f436b742` contains queue-only nonce `HQUEUE-7dd863c8-6b50`, exact native ID, original launch ID and successful ack. This proves response/delivery, beyond queue acceptance. Evidence: `artifacts/testing-harnesses/queue-native-receipt.json`, `queue-actual-response.json`, `probe-idle-before-queue.txt`.

Actual child `probe-start.json` confirms lab socket, independent native ID and empty inbox/ack cursor 0 success. Independent offline harness contracts: five pass at developer fix `14ff690` (tree-equivalent `da3c863`); broad safe offline selection 18 pass. Exact resume completed through integrated root CLI `08da915`: actual post-resume response `a2926d0c-bda4-4be7-9d50-2b79e1b762c5` returns queue-only nonce `HRESUME-a82e9323-075c`, unchanged native ID, new launch `d33eac8c-06f2-430b-9263-2a2b250187b1`, lab socket and successful ack. Final probe stop succeeded and pane is absent (`final-observations.json`). Both core bugs closed after actual committed-fix verification; test/evidence commit `91d1d7ffa5b1f2b8dda536ec7f6ee45b95856eff`. Alternate Claude/Pi executables exist but only offline adapter contracts have run; no successful live alternate turn claimed.

## Template smoke verification

Exact source `6b4baca39901700942ece73fab399a44228a25a4` installed through CLI into two fresh temporary workspaces with `init --template testing|development`. Distinct IDs, empty agent state/queue, local readable artifact forms, catalog discovery and explicit role command loading pass; all three template tests pass. Evidence `artifacts/testing/templates-6b4baca-retest.txt`.

LAB-SESSION-004 is a synthetic native environment marker leak, not reproduced model-session misbinding. Assigned declarative manifest cleanup plus offline independent harness repro/retest; no further live probes.
