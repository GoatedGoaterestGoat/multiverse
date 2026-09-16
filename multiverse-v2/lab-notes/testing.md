# Testing coordination

Baseline: `3c7de7ed69f732a0e70362b69e3127022cf3e587`. Native manager session: `01a0a9ba-de48-7101-9318-4d2ccde753a8`.

## Queue

| ID | Severity | Reproduction | Fix owner | Independent verifier | Status |
|---|---|---|---|---|---|
| LAB-SOCKET-001 (LAB-H001) | high | Actual child `MV2_TMUX_SOCKET` unset | template-dev | testing-harnesses | Pending independent retest of `14ff690` |
| LAB-INBOX-001 (LAB-H002, MV2-EMPTY-INBOX-ACK) | medium | Empty inbox cursor 0 then ack exits 1 ENOENT | template-dev | testing-harnesses | Pending independent retest of `14ff690` |
| LAB-SPAWN-003 | medium | Failed sandbox spawn + same-name retry makes send ambiguous | template-dev | testing-manager | Open; exact-ID workaround |
| Portal behavior | normal | Baseline + Pilot + live tmux coverage | ui-dev | testing-ui | Await actual fix commit |

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
