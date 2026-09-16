# Director integration log

Lab checkout: `/Users/admin/Documents/Github/Multiverse-Lab/repo`, branch `lab/integration`, baseline `3c7de7e`. All development stays in this independent clone. Commit identity is shaoxiongduan <shaoxiongduan@gmail.com>.

## Department setup

Created V2 testing, template-dev, ui-dev and mockups subverses from `LAB/templates/lab`, each backed by a distinct `LAB/worktrees/ROLE` checkout and `lab/ROLE` branch. Added local instructions, lazy `workflows/department` catalog entries, distinct default titles, columns and colors. Launched department agents with Codex / gpt-6-astra; testing-manager owns creation of its nested UI and harness workers. Mockups owns sequential stock-trading, CAD and CAD/verification construction. Seven ordinary agents, one bounded probe slot controlled by testing.

Director session binding occurred before setup. All department launch commands explicitly set the isolated `LAB/run/tmux.sock`. Milestone terminal capture: `LAB/artifacts/terminal/20260916T102015Z-departments-launched/` (actual tmux frames).

## Reproduction and verification queue

- LAB-SOCKET-001 (testing alias LAB-H001): launch request omits MV2_TMUX_SOCKET and runner strips inherited MV2_*; testing-manager independently printed `<unset>` in a real launched child. Report: `LAB/artifacts/director/LAB-SOCKET-001.md`.
- LAB-INBOX-001 (testing alias LAB-H002): fresh `mv2 inbox` returns empty messages and cursor 0; `mv2 ack --cursor 0` fails opening absent inbox.jsonl. Independently reproduced by director, testing-manager and template-dev. Report: `LAB/artifacts/director/LAB-INBOX-001.md`.
- Roster audit discovered phantom properties from failed UI/harness launches alongside live replacement agents. Testing notified to preserve evidence and use authoritative IDs.

Initial sandbox baseline suite could not create its temporary tmux sockets; `baseline-tests.log` records environmental failures, not product regressions. Authorized rerun at baseline `3c7de7e`: 32 tests passed in 35.903 s. Command: `multiverse-v2/.venv/bin/python -m unittest discover -s multiverse-v2/tests -v`; evidence: `LAB/artifacts/director/baseline-tests-unsandboxed.log`.

## Acceptance criteria

Only integrate reviewed task commits. Independent testing must identify exact fix SHA, repro, expected/actual behavior and evidence before closing reports. Portal acceptance requires real dashboard enter/back through mockups → CAD → verification with local views, restored selection, usable testing queue, and terminal snapshots at integrated revision. Session acceptance requires distinct bound native IDs, lab socket ownership tags, durable inbox read/ack and proven queue response; acceptance alone is not delivery.

Initial isolation audit (`LAB/artifacts/director/session-audit-initial.json`): all seven live agents have distinct bound native sessions, the expected lab socket and matching agent/universe/launch tmux tags. Two additional failed launch records have no panes and do not count as active. This proves pane identity isolation, while child environment propagation remains a separate failing test.
