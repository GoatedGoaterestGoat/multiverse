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

## First committed portal and live failure/fix cycle

Portal commit `bfa7790` was sent to director and testing-ui for independent review.
The worker independently passed three Pilot checks and installed fixture checks
at 120×32, 80×24 and 60×20. Director integrated it as `780674f` and reported a
combined 37-test pass under the integration environment.

Supplemental actual terminal session `ui-dev-evidence`, pane `%13`, uses only
`LAB/run/tmux.sock` and the ui-dev source path. It is a non-agent UI process.
On `bfa7790`, batched `Down Enter` from the initial live-agent selection produced
a blank cooked terminal. Report: `LAB/artifacts/ui-dev/LAB-UI003.md`. Failure
capture: `LAB/artifacts/terminal/20260916T102633Z-ui-dev-bfa7790-mockups/pane-13.*`.
Despite the old label, this is explicitly FAILED navigation evidence.
Director independently reproduced the failure and assigned the bounded fix.

Fix resolves Enter from the actual table cursor before queued highlight delivery,
and catches attach errors inside Textual's suspension context so terminal resume
runs before reporting the error. Two regressions cover same-event-loop activation
and normal suspension-context exit on backend failure. All 19 UI tests pass;
log: `LAB/artifacts/ui-dev/ui003-tests.log`. Independent actual-terminal failure
and fixed-commit verification are delegated to the existing testing-ui worker.

After replacing only the supplemental UI process, direct mockups launch and
separate `Down`, inspected selected CAD row, then `Enter` showed both local
portals correctly. Successful mockups live capture:
`LAB/artifacts/terminal/20260916T102813Z-ui-dev-bfa7790-mockups-rendered/pane-13.*`.
No main-dashboard interaction was performed by ui-dev.

## Verified fix and terminal handoff

Fix commit: `65f228f` (clean UI worktree). Independent testing-ui cherry-pick
`848d508` has identical production source. Its five independent checks, nineteen
UI checks and six portal checks pass (30 total). Same-loop cursor activation and
suspend-error checks fail before the fix and pass after it. On 2026-09-16,
testing-manager closed LAB-UI003 after independent **actual-terminal** verification:

- `LAB/artifacts/testing-ui/UI003-fixed-error.txt`: dashboard restored after error.
- `LAB/artifacts/testing-ui/UI003-fixed-draft.txt`: input/draft still usable.
- `LAB/artifacts/testing-ui/UI003-fixed-batch.txt`: correct batched navigation.

My own actual terminal sequence on `65f228f`, including exact commands, is in
`LAB/artifacts/ui-dev/terminal-65f228f.md`. Confirmed each frame before continuing:
Lab → Mockups → CAD → Verification, then Backspace restoring CAD/Verification
selection, Mockups/CAD selection, and Lab/Mockups selection. Archives:

| Result | Folder under `LAB/artifacts/terminal/` (pane-13.*) |
| --- | --- |
| Original batched navigation now passes | `20260916T103100Z-ui-dev-65f228f-batched-portal-pass` |
| Grandchild local view and four-scope breadcrumb | `20260916T103143Z-ui-dev-65f228f-verification-pass` |
| Back restores CAD and selected Verification | `20260916T103205Z-ui-dev-65f228f-back-cad-pass` |
| Back restores Mockups and selected CAD | `20260916T103234Z-ui-dev-65f228f-back-mockups-pass` |
| Real detached attach error restores dashboard | `20260916T103349Z-ui-dev-65f228f-attach-error-restored` |
| Distinct testing queue and evidence link | `20260916T103425Z-ui-dev-65f228f-testing-queue-pass` |

All are live tmux frames, not headless fixture images. The supplemental non-agent
`ui-dev-evidence` session showed Testing queue, then was closed at the director's
request after verifying all six archived captures. The ordinary agent remains available. The
director controls the user-facing dashboard and integration. No extra workers,
external messages, pushes, or edits outside the lab were made.

One low, unconfirmed buffered slash/search-focus observation was sent to
testing-manager as `LAB/artifacts/ui-dev/LAB-UI004-investigation.md`. No ordinary
paced-search failure is established; this is separate from verified LAB-UI003.

## Continued integration: LAB-UI004 buffered search

The independent bounded comparison subsequently confirmed LAB-UI004 at medium
severity: a single terminal batch `/Testing queue` followed by Enter opened a
Stop confirmation instead of editing the query. Nothing was confirmed or stopped.
User/director authorized this bounded fix on 2026-09-16; existing worker roles and
worktree were reused, with no main-dashboard interaction.

The opening Slash now uses a priority binding, before Textual forwards the rest
of the terminal input batch to the old widget. It focuses the filter synchronously
and closes the inspector without queuing a later table refocus. The binding is
disabled for editors, drafts, modal screens and in-progress operations, preserving
literal slashes and normal form behavior.

Three behavior regressions exercise a raw event batch (without Pilot's per-key
waits), absence of global action callbacks, an open inspector, continued buffered
action-letter text, and literal slashes in search/draft/message editors. The first
test fails before the fix with `action_stop` invoked; evidence is
`LAB/artifacts/ui-dev/ui004-before.log`. Fixed-suite results are recorded in
`ui004-ui-tests.log` (22 UI checks) and `ui004-portal-tests.log` (6 portal checks)
under the same artifacts directory. Independent testing-ui owns its separate
Driver.send_message regression and exact-commit actual-terminal retest.
