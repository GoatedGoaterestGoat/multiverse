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

## Integrated acceptance

Reviewed and cherry-picked the department commits into `lab/integration`. Production source at final regression revision `7b0f13f` is identical to `c006133`, the running dashboard build. Key integrated commits:

| Outcome | Developer commit | Integration commit |
| --- | --- | --- |
| Socket propagation + empty inbox | 14ff690 | 08da915 |
| Failed launch recovery / UUID guidance | adfcdb7 | e008ace |
| Nested stock/CAD/verification fixtures | ff8a993 | 0b069cf |
| Portals and declarative local widgets | bfa7790 | 780674f |
| Lazy testing/development templates | 6b4baca | 43ce8c1 |
| Cross-harness native marker cleanup | 1fe2fe4 | c707bb1 |
| Attach error restoration / current cursor activation | 65f228f | c006133 |

Independent testing closed eight confirmed issues: LAB-SOCKET-001, LAB-INBOX-001, LAB-SPAWN-003, LAB-UI001, LAB-VIEW-004, LAB-UI002, LAB-SESSION-004 and LAB-UI003. Detailed exact reproductions, before/after results and evidence are in the committed department notes and `LAB/artifacts/testing*/`. The director independently reproduced the blank terminal on a real failed attach and verified restored input after the fix. Cursor activation has a separate deterministic regression; director row-counting attempts do not prove that race.

Final combined regression at `7b0f13f`: **55 tests pass in 42.784 s**. Command:

```sh
PYTHONPATH=multiverse-v2/src:multiverse-v2/tests multiverse-v2/.venv/bin/python -m unittest test_tui test_portal test_ui_independent test_launch_isolation test_templates test_lab_harness_contracts test_lab_session_isolation test_multiverse.FilesTest test_harnesses.HarnessFilesTest -v
```

Log: `LAB/artifacts/director/final-integration-tests.log`. This final run excludes the seven original tests that create separate temporary tmux servers; the actual lab socket workflow was independently exercised instead. The earlier baseline full suite passed 32 checks with temporary-socket access. No later full-suite pass is claimed.

### Actual model/session evidence

Seven ordinary Codex/Astra roles were bound to distinct native sessions and matched their lab pane agent/universe/launch tags. Only one disposable slot was used at a time. Actual Codex probe confirmed child socket, empty inbox ack, durable reply, idle native queue-only nonce, exact native resume under a new launch ID, and another nonce response. Probe stopped; pane absent. Evidence `LAB/artifacts/testing-harnesses/{queue-actual-response.json,resume-actual-response.json,final-observations.json}`.

A bounded explicitly named Claude test returned an actual assistant nonce and was stopped after 42.6 seconds. Pi live smoke was skipped because its default Google provider credentials are not configured; offline contracts passed. No successful live Pi turn is claimed. See `LAB/artifacts/testing-harnesses/session-004-alternate-report.md`.

### Actual dashboard demonstration

Restarted only `multiverse-lab:dashboard` on the lab socket after reviewed tests. The recorder and ordinary agents stayed available. Live main pane `%0` at 180×48 showed root → Mockups → CAD → Verification, opened local results.json by mouse-equivalent click, restored CAD/Verification selection then Mockups/CAD selection with Backspace, opened Stock-trading, and opened the distinct Testing queue. Exact commands and frames: `LAB/artifacts/director/live-portal-steps.json`.

Verified main-dashboard capture folders under `LAB/artifacts/terminal/`:

- `20260916T103121Z-integrated-attach-error-recovered`
- `20260916T103130Z-integrated-mockups-confirmed`
- `20260916T103148Z-integrated-cad-confirmed`
- `20260916T103159Z-integrated-verification-confirmed`
- `20260916T103232Z-integrated-verification-artifact`
- `20260916T103300Z-integrated-back-cad`
- `20260916T103318Z-integrated-back-mockups`
- `20260916T103329Z-integrated-stock-confirmed`
- `20260916T103500Z-integrated-testing-queue`

The earlier `102803Z-integrated-mockups-portal` and `102935Z-integrated-mockups-paced-success` folders contain **failed blank-terminal evidence despite premature labels**, not successful portal proof. Testing-ui also corrected initially mislabeled frames; its final note records replacements reviewed by mockups-builder and testing-manager. Headless exports are explicitly distinguished from live terminal frames.

### Lab configuration handoff

After independent marker-fix verification, applied 33 reviewed installed/template lab manifests with exact before/after hash checks and semantic validation. Only native marker cleanup plus lab-only Astra default/explicit override settings changed. Original files and rollout plan: `LAB/artifacts/director/manifest-rollout-20260916T103355Z/`. Developer independently verified all installed hashes in `LAB/artifacts/template-dev/installed-manifests-verified.json`.

Blank model uses Astra; explicit model is passed as matching --model and later config value, avoiding conflicting native precedence. Actual no-turn config/read diagnostics checked default and override resolution; 44 request-construction combinations passed. Reusable source templates remain model-neutral and their role details lazy (176-word automatic bootstrap). Live agent environments/sessions were not rewritten or restarted for this definition update.

`LAB/START-HERE.md` documents landed keys and defaults. `LAB/STATUS.md` is the current handoff; all commits use shaoxiongduan <shaoxiongduan@gmail.com>, remain local, and the original source checkout was not modified.

## Remaining follow-up

Bounded independent testing reproduced **LAB-UI004 (medium, OPEN)** at `65f228f`: a single terminal batch containing slash + `Testing queue` + Enter leaves the filter blank and can invoke query-letter dashboard shortcuts, including a Stop confirmation. No submit occurred and no agent was stopped; tester canceled the dialog. Separate slash, wait for the filter, then query works. Evidence `LAB/artifacts/testing-ui/UI004-{before,batched,paced-filter-ready,paced-query,paced-after-enter}.txt`. The eight prior verified repairs remain closed. This ninth confirmed issue is recorded for follow-up, with no claim of a fix and no further source expansion during the finite milestone.
