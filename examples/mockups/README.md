# Nested working worlds

An offline Multiverse V2 demonstration with no additional agents or large tools.
All market data, companies and orders are fictional. The CAD checks calculate
geometry; they do not perform structural analysis or approve manufacturing.

```
mockups                 local guide and demo map
├── stock-trading       Northstar paper portfolio ($40,000 NAV)
└── cad                 MP-100 rev B mounting plate (94.983 g)
    └── verification    nominal + two invalid geometry regression cases
```

`fixtures/` holds editable source data. `definitions/` holds reusable local
commands, review prompts, views and generated artifacts. `demo.py` uses only the
Python standard library. `runtime/` is ignored and is the default destination
for separate nested workspaces. Live universe IDs and agent state are never
stored in this example.

Build or recalculate:

```sh
python3 examples/mockups/demo.py verify
```

Install into an existing parent universe, creating missing descendants through
the V2 CLI. Pass a clean template explicitly:

```sh
python3 examples/mockups/demo.py install \
  --universe /absolute/path/to/mockups \
  --template /absolute/path/to/templates/lab \
  --workspace-root /absolute/path/to/mockups-worktree/demo-workspaces \
  --cli /absolute/path/to/multiverse-v2/bin/mv2
```

The installer refreshes these example definitions and fixture files, retaining
existing universe IDs and runtime state. It rejects an existing child whose
workspace differs from the requested location. Use the same explicit workspace
root when running an integrated copy against an already-installed demo. No
agents are spawned or resumed.

Views use the agreed `title`, `description`, `columns`, `widgets` schema. Widget
types are `markdown`, `table` and `artifacts`; artifact paths are relative to each
universe root, so every installed link stays local. The portal UI implementation
is supplied separately by ui-dev. Enter `cad`, enter `verification`, then use the
parent/back action twice to demonstrate three-level navigation. Each universe
also exposes `mv2 -u PATH command demo` without loading unrelated prompts.

The HTML and SVG files are static previews. They are not terminal captures. Open
`definitions/stock-trading/artifacts/portfolio.html` or
`definitions/cad/artifacts/plate.html` in a browser. `plate.scad` is provided as
readable source; OpenSCAD is not needed to inspect the drawing or run the checks.

Verification covers exact Decimal portfolio reconciliation, cylindrical-hole
volume and mass, edge clearance, thickness, hole separation, all three expected
case outcomes, and artifact path existence. The deliberately oversized-hole and
thin-stock cases must fail their respective geometry constraints; both count as
successful regression expectations.
