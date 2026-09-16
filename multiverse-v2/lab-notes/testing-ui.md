# testing-ui independent verification

Native session: `01a0a9bc-1ebd-78b2-8ff1-3ec97d645c62`; agent `e5d01748-d437-485f-a64c-08357284bc6f`.

Baseline implementation: `3c7de7ed69f732a0e70362b69e3127022cf3e587`.
Only independent tests and this note are owned here; ui-dev owns its separate `tests/test_portal.py`.

Commands use `/Users/admin/Documents/Github/Multiverse-Lab/repo/multiverse-v2/.venv/bin/python` from the assigned testing-ui worktree:

- `-m unittest discover -s multiverse-v2/tests -p test_tui.py -v`: 17 pass.
- `-m unittest discover -s multiverse-v2/tests -p test_ui_independent.py -v`: 1 pass, 1 fail. LAB-UI001: Enter on group folds it and leaves root unchanged, so local portal and multilevel navigation unavailable. Hover selection, live demo inspection, scoped draft cancel through 60×20 resize pass.

Evidence: `/Users/admin/Documents/Github/Multiverse-Lab/artifacts/testing-ui/{baseline-existing-tests.log,baseline-independent-tests.log,LAB-UI001.md}`.
LAB-UI001 high severity; proposed fix: local portal Enter/double-click with Backspace restoring parent context. Actual committed fix has not yet been independently retested. Demo inspection above is headless simulated output, not live terminal capture.
