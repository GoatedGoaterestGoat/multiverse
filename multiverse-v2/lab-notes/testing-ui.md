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

Actual pane `%14`, `testing-ui-evidence:portal`: SGR click CAD x10,y5 then Enter; Down Enter to verification; `n`, Ctrl-U, `independent-cancel`, resize60×20, Escape cancels; restore120×32; Backspace twice returns CAD then mockups with CAD selection. `portal-*.txt` frames and actual SVG `artifacts/terminal/20260916T102811Z-testing-ui-bfa7790-verification/pane-14.svg`. LAB-UI001 verified on bfa7790.

LAB-VIEW-004: corrected view 814e775 opens the real local initial-bugs.md in Pilot and actual pane `%16`, SGR click x90,y17. Text `testing-artifact-preview.txt`; actual SVG `artifacts/terminal/20260916T103026Z-testing-ui-814e775-artifact/pane-16.svg`. Verified with bfa7790 renderer.

LAB-UI003 high independently reproduced on bfa7790: deterministic failing attach backend (`artifacts/testing-ui/attach_failure_repro.py`) in actual pane `%15`; Enter blanks terminal. No model or client switching. `UI003-before.txt`, `UI003-after.txt`, and actual SVG `artifacts/terminal/20260916T102916Z-testing-ui-UI003-reproduced/pane-15.svg`. Independent normal-suspend-exit regression fails; cursor activation regression added. Fix 65f228f received, not yet retested at this checkpoint.
