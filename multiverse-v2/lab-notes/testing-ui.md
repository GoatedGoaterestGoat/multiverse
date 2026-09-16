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
