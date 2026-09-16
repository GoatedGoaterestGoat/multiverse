# testing-ui independent verification

Native session: `01a0a9bc-1ebd-78b2-8ff1-3ec97d645c62`; agent `e5d01748-d437-485f-a64c-08357284bc6f`.

Baseline implementation: `3c7de7ed69f732a0e70362b69e3127022cf3e587`.
Only independent tests and this note are owned here; ui-dev owns its separate `tests/test_portal.py`.

Commands use `/Users/admin/Documents/Github/Multiverse-Lab/repo/multiverse-v2/.venv/bin/python` from the assigned testing-ui worktree:

- `-m unittest discover -s multiverse-v2/tests -p test_tui.py -v`: 17 pass.
- `-m unittest discover -s multiverse-v2/tests -p test_ui_independent.py -v`: 1 pass, 1 fail. LAB-UI001: Enter on group folds it and leaves root unchanged, so local portal and multilevel navigation unavailable. Hover selection, live demo inspection, scoped draft cancel through 60×20 resize pass.

Evidence: `/Users/admin/Documents/Github/Multiverse-Lab/artifacts/testing-ui/{baseline-existing-tests.log,baseline-independent-tests.log,LAB-UI001.md}`.
LAB-UI001 high severity; proposed fix: local portal Enter/double-click with Backspace restoring parent context. Actual committed fix has not yet been independently retested. Demo inspection above is headless simulated output, not live terminal capture.

Expanded acceptance checks also cover mouse double-click entry with a temporary local universe, artifact URL preview and parent search restoration; baseline has 2 navigation failures and 1 passing check (`baseline-expanded-tests.log`). Nested draft checks prevent Backspace from accidentally leaving a draft's scope.

Actual tmux baseline: director-approved non-agent session `testing-ui-evidence:baseline`, pane `%10`, on lab socket. `terminal-baseline.md` in role artifacts records exact keys, SGR click/hover and resize sequence. Group Enter reproduces LAB-UI001. Real terminal demo hover preserves CAD selection while showing reviewer; draft cancel and 60×20 resize pass. Labeled SVG: `artifacts/terminal/20260916T102413Z-testing-ui-baseline-hover/pane-10.svg`. This terminal is real, but its backend preview payload is simulated and labeled as such.

Queued independent verification: actual portal fix SHA; mockups → CAD → verification and stock views/artifacts; local-view/inspector layout at 120×32, 80×24, 60×20; LAB-VIEW-004 testing view correction **814e775** (supersedes mistaken manager prefix); LAB-UI002 nested tmux return hint.

## Portal retest and new attach failure

Portal implementation `bfa7790` cherry-picked unchanged as `0ad33a3` (production source diff empty). Three independent acceptance tests pass; 17 existing UI and 6 developer portal tests pass. Lab-local `artifacts/testing-ui/fixture_retest.py` passes installed mockups/CAD/verification/testing title/columns/artifact preview and inspector layout at 120×32, 80×24, 60×20; stock view loads. JSON/log and explicitly headless SVGs saved under role artifacts.

Actual pane `%14`, `testing-ui-evidence:portal`: CAD entry verified, but **prior full multilevel/back success claim is retracted**. Builder inspected `portal-verification.txt` and the mislabeled `20260916T102811Z-testing-ui-bfa7790-verification/pane-14.svg`: header remained CAD with Verification only selected. `portal-back-mockups.txt` still contained a draft. Those files are failed/inconclusive sequence evidence, not proof of successful entry/back. Likely stale activation plus Escape batched with following keys affected the sequence. Pilot LAB-UI001 acceptance passed bfa7790; fresh actual verification is below.

LAB-VIEW-004: corrected view 814e775 opens the real local initial-bugs.md in Pilot and actual pane `%16`, SGR click x90,y17. Text `testing-artifact-preview.txt`; actual SVG `artifacts/terminal/20260916T103026Z-testing-ui-814e775-artifact/pane-16.svg`. Verified with bfa7790 renderer.

LAB-UI003 high independently reproduced on bfa7790: deterministic failing attach backend (`artifacts/testing-ui/attach_failure_repro.py`) in actual pane `%15`; Enter blanks terminal. No model or client switching. `UI003-before.txt`, `UI003-after.txt`, and actual SVG `artifacts/terminal/20260916T102916Z-testing-ui-UI003-reproduced/pane-15.svg`. Independent normal-suspend-exit regression fails; cursor activation regression added. Fix 65f228f received, not yet retested at this checkpoint.

## Fixed attach and activation verification

`65f228f` cherry-picked unchanged as `848d508`. Five independent tests pass; both normal-suspend-exit and synchronous-cursor checks failed on bfa7790. Full discovery confirms all 30 UI tests pass (19 existing/developer UI, 6 portal, 5 independent); seven non-UI test errors are sandbox denials attempting temporary tmux sockets. No escalation/retry of those harness tests; evidence in `retest-65f228f-full-suite.log` is not an all-suite pass.

LAB-UI003 verified in actual pane `%18`: same deterministic failing backend, Enter now renders `Attach failed: LAB-UI003 deterministic attach failure`; `n` creates a draft; standalone Escape cancels; then batched Down Down Down Enter opens PR Review. `UI003-fixed-error.txt`, `UI003-fixed-draft.txt`, `UI003-fixed-batch.txt` prove recovery and subsequent input. The same-loop Pilot test separately isolates current-cursor activation.

Fresh actual portal pane `%19`, 65f228f: explicit SGR CAD selection, Enter, standalone Down, standalone Enter. `fixed-02-cad.txt` header is CAD; `fixed-04-verification.txt` header is **Verification | MP-100 regression suite**, with full mockups/CAD/verification breadcrumb and Constraint results. Actual SVG: `artifacts/terminal/20260916T103248Z-testing-ui-65f228f-verification-confirmed/pane-19.svg`.

Frame-confirmed continuation: `fixed-05-draft-60x20.txt` shows Verification draft; standalone Escape removes it (`fixed-06-canceled-60x20.txt`). Restore120×32; standalone Backspace returns CAD with Verification selected (`fixed-07-back-cad.txt`); another Backspace returns Mockups with CAD selected and collapsed fold restored (`fixed-08-back-mockups.txt`). SGR hover at x10,y4 inspects actual mockups-builder pane while CAD stays selected (`fixed-09-live-hover.txt`); labeled actual SVG `artifacts/terminal/20260916T103449Z-testing-ui-65f228f-back-and-live-hover/pane-19.svg`. LAB-UI001 now independently verified in both Pilot and actual multilevel terminal on 65f228f. Builder reviewed corrected Verification/cancellation frames; initial mismatch retained in EVIDENCE-CORRECTION.md.

LAB-UI002 help text verified in actual pane `%16` with bfa7790, expanded to 120×80 so complete help is visible: `help-bfa7790-full.txt` explicitly distinguishes Ctrl-b L previous session and Ctrl-b d whole-client detach. This verifies instruction rendering; no real user client was switched by this tester.

## Final bounded comparison

Manager requested one LAB-UI004 buffered-versus-paced search comparison, with no further expansion. On65f228f, actual pane20 starts at observed root/director selection with filter hidden. One `send-keys / "Testing queue" Enter` produces empty filter plus **Stop director?** confirmation (`UI004-batched.txt`); canceled with standalone Escape, never submitted. Paced slash → visible filter → literal query → Enter succeeds (`UI004-paced-query.txt`, `UI004-paced-after-enter.txt`). LAB-UI004 reproduced; proposed medium severity, suspected focus/event queue delay dispatches text as global shortcuts. Full repro/proposal in `artifacts/testing-ui/LAB-UI004.md`; this remaining finding does not undo verified UI001/UI003 fixes. Test fixture overrides only attach to protect real user clients. No model workers created.
