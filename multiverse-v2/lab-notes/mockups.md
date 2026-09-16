# Mockups builder log

2026-09-16. Base tested CLI: `3c7de7ed69f732a0e70362b69e3127022cf3e587`.
Native session bound before work; V2 coordination only; no additional agents.
Commit identity: shaoxiongduan <shaoxiongduan@gmail.com>.

## Delivered

Reusable definitions, fictional data, static previews and standard-library
installer: `multiverse-v2/examples/mockups/`. See its README for portable commands.
Agreed directly with ui-dev: `title`, `description`, `columns`, `widgets` with
markdown/table/artifacts. All artifact paths are relative to and inside the local
universe directory. Local `demo` prompts and review workflows exist at all levels.

Live parent: `/Users/admin/Documents/Github/Multiverse-Lab/universe/subverses/mockups`.
V2-created descendants, all from `LAB/templates/lab`:

- `subverses/stock-trading`: simulated $40,000 NAV, $579 unrealized P/L;
  fictional companies and illustrative unfilled orders, no trading connection.
- `subverses/cad`: MP-100 mounting plate, SVG/HTML/SCAD/JSON artifacts;
  calculated mass 94.983 g, edge clearance 8.7 mm.
- `subverses/cad/subverses/verification`: three analytical regression cases;
  nominal passes, oversized holes and thin stock fail as expected.

Distinct nested workspaces live under
`LAB/worktrees/mockups/multiverse-v2/examples/mockups/runtime/`:
`stock-trading`, `cad`, `cad/verification`. Runtime is ignored. Root workspace
remains the assigned mockups worktree. No live IDs or session state are committed.

## Validation and evidence

- `python3 multiverse-v2/examples/mockups/demo.py verify` passes portfolio
  reconciliation, all CAD expected outcomes, nominal mass and artifact links.
- Clean temporary install through V2, then reinstallation: all four IDs unchanged,
  state sentinel preserved, four workspace paths distinct, artifact paths valid.
- Conflicting workspace root correctly rejected without repointing the child.
- SVG parsed successfully as XML. Browser visual inspection was unavailable
  because the computer-use tool reported no browser available.
- Full commands and output: `LAB/artifacts/mockups/reproducibility.txt`.
- Confirmed baseline empty-inbox ack failure and sent report to testing-manager:
  `LAB/artifacts/mockups/empty-inbox-ack.md`. No claim of a fix from this role.

Live install command (use the same workspace root from any integrated checkout):

```sh
python3 multiverse-v2/examples/mockups/demo.py install \
  --universe /Users/admin/Documents/Github/Multiverse-Lab/universe/subverses/mockups \
  --template /Users/admin/Documents/Github/Multiverse-Lab/templates/lab \
  --workspace-root /Users/admin/Documents/Github/Multiverse-Lab/worktrees/mockups/multiverse-v2/examples/mockups/runtime \
  --cli /Users/admin/Documents/Github/Multiverse-Lab/repo/multiverse-v2/bin/mv2
```

UI tester received exact live paths for independent multilevel enter/back and
artifact activation checks once ui-dev supplies its implementation. Static
fixture previews are not live terminal evidence. This initial commit supplies
the demo data; portal validation and any follow-up evidence will be recorded
separately when the actual UI revision is ready.

## Committed portal follow-up

Demo commit `ff8a9934decf0b72251a89e2a29c6fe9406a7c56` was reviewed and
integrated by director. On UI commit
`bfa7790ba62f08632710a13c8d72b5681060925c`, a read-only Textual Pilot run
against the actual installed universe files passed:

- Select CAD, `Enter`; select verification, `Enter`.
- Activate the verification report artifact; no artifact error.
- `Escape`, `Backspace`: CAD scope and verification selection restored.
- `Backspace`: mockups scope and CAD selection restored.
- Select stock-trading, `Enter`: local Northstar title and content displayed.
- Resize from 120×36 to 80×24: stock scope preserved.

Evidence: `LAB/artifacts/mockups/verified-portal.json`,
`verified-portal-report.svg`, `verified-portal-stock.svg`, and
`verified-portal-stock-80.svg`. These are headless Pilot exports, not live tmux
captures. Independent live terminal validation remains assigned to testing-ui.
All evidence paths and tested SHAs were reported to director and testing-manager.
