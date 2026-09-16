# UI portal work log

Workspace: `LAB/worktrees/ui-dev`, branch `lab/ui-dev`. Base: `3c7de7e`.
Author: shaoxiongduan <shaoxiongduan@gmail.com>.

Implemented local portal navigation and declarative Markdown/table/artifact
widgets. Agreed schema with mockups-builder and testing-manager before fixtures
were written; template-dev owns all non-UI sources/templates. Schema and controls
are documented in `docs/views.md`. No additional workers launched.

Initial verification on the implementation worktree:

```sh
PY=/Users/admin/Documents/Github/Multiverse-Lab/repo/multiverse-v2/.venv/bin/python
$PY -m unittest discover -s multiverse-v2/tests -p test_tui.py
$PY -m unittest discover -s multiverse-v2/tests -p test_portal.py
git diff --check
```

Results: 17 existing UI tests and 6 portal tests pass. Portal tests exercise real
on-disk schemas, title/column changes, nested Enter and Backspace, breadcrumb,
fold/search/selection restoration, mouse double-click, keyboard and mouse artifact
activation, refresh stability, resize, stale-scan suppression, malformed views,
missing files and confined artifact paths. The inspector temporarily replaces
the local view, preserving usable hierarchy width on large and small terminals.
Help also addresses LAB-UI002 by explaining previous-session vs whole-client
detach behavior.

Broader suite attempt: 37 tests, 30 passed and 7 non-UI errors because sandboxing
denied creation of private test tmux sockets. Evidence: `LAB/artifacts/ui-dev/full-tests.log`.
Reported to testing-manager; not rerun outside the sandbox because lab operations
must use the lab socket. No successful full-suite result is claimed.

Evidence: `LAB/artifacts/ui-dev-initial-tests.log` and
`LAB/artifacts/ui-dev/portal-tests.log`. These are headless Pilot results, not live
terminal captures. Independent testing-ui owns `test_ui_independent.py` and its
dedicated lab-socket terminal; requested exact-commit retest for LAB-UI001.

The initial launch scope is the history boundary (documented). Back restores the
previous visited scope, including direct jumps to grandchildren. Artifacts render
UTF-8/Markdown previews; SVG/HTML source includes an explicit default-viewer file
link. No graphical rendering is claimed inside the terminal.
