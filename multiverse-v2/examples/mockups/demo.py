#!/usr/bin/env python3
"""Build and install an offline, standard-library-only multilevel V2 demo."""
import argparse
import copy
from decimal import Decimal
import html
import json
import math
import os
from pathlib import Path
import shutil
import subprocess

HERE = Path(__file__).resolve().parent
DEFINITIONS = HERE / "definitions"
FIXTURES = HERE / "fixtures"
SCOPES = {"mockups": "", "stock-trading": "subverses/stock-trading",
          "cad": "subverses/cad", "verification": "subverses/cad/subverses/verification"}


def read(path):
    return json.loads(path.read_text())


def write(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, indent=2) + "\n" if not isinstance(value, str) else value)


def geometry(model):
    p, holes, limits = model["plate"], model["holes"], model["constraints"]
    radius = holes["diameter"] / 2
    edge = min(min(x, y, p["length"] - x, p["width"] - y) - radius
               for x, y in holes["centers"])
    mass = (p["length"] * p["width"] - len(holes["centers"]) * math.pi * radius ** 2) * p["thickness"] * model["material"]["density_kg_m3"] / 1e6
    pairs = [(a, b) for i, a in enumerate(holes["centers"]) for b in holes["centers"][i + 1:]]
    separated = all(math.dist(a, b) > radius * 2 for a, b in pairs)
    checks = {"edge-clearance": edge >= limits["min_edge_clearance"],
              "minimum-thickness": p["thickness"] >= limits["min_thickness"],
              "mass-budget": 0 < mass <= limits["max_mass_g"],
              "hole-separation": separated}
    return {"mass_g": round(mass, 3), "edge_clearance_mm": round(edge, 3), "checks": checks,
            "result": "pass" if all(checks.values()) else "fail"}


def verification(model, cases):
    results = []
    for case in cases["cases"]:
        candidate = copy.deepcopy(model)
        overrides = case["overrides"]
        candidate["holes"]["diameter"] = overrides.get("hole_diameter", model["holes"]["diameter"])
        candidate["plate"]["thickness"] = overrides.get("thickness", model["plate"]["thickness"])
        result = geometry(candidate)
        matches = result["result"] == case["expected"]
        if "expected_failure" in case:
            matches = matches and result["checks"][case["expected_failure"]] is False
        results.append({**case, **result, "expectation_met": matches})
    return {"method": "Analytical dimensions and cylindrical through-hole volume; no FEA",
            "all_expectations_met": all(r["expectation_met"] for r in results), "cases": results}


def table(title, columns, rows):
    return {"type": "table", "title": title, "columns": columns, "rows": rows}


def markdown(title, text):
    return {"type": "markdown", "title": title, "text": text}


def artifacts(*items):
    return {"type": "artifacts", "title": "Local artifacts", "items": [
        {"label": label, "path": "artifacts/" + path, "description": description}
        for label, path, description in items]}


def page(title, body):
    return '<!doctype html><html lang="en"><meta charset="utf-8"><meta name="viewport" content="width=device-width"><title>' + title + '</title><style>body{background:#101923;color:#e4edf6;font:17px system-ui;max-width:1000px;margin:50px auto;padding:24px}h1{color:#7ed9c7}a{color:#9ec7ff}table{border-collapse:collapse;width:100%}td,th{text-align:left;padding:14px;border-bottom:1px solid #344251}.badge{color:#ffd38a}svg{max-width:100%;background:#172735;border-radius:12px}code{color:#7ed9c7}</style><h1>' + title + '</h1>' + body + '</html>\n'


def scope(name, title, description, widgets, prompt):
    destination = DEFINITIONS / name
    write(destination / "views/default.json", {"title": title, "description": description,
          "columns": ["name", "phase", "summary"], "widgets": widgets})
    write(destination / "commands/demo/entry.json", {"description": description})
    write(destination / "commands/demo/prompt.md", prompt + "\n")
    write(destination / "workflows/review/prompt.md", "Read the local demo command and artifacts. Check that source fixtures agree with calculated results. Report evidence paths and limitations. Do not claim a static preview is a live terminal capture.\n")


def build():
    portfolio = read(FIXTURES / "stock-trading/portfolio.json")
    model = read(FIXTURES / "cad/model.json")
    cases = read(FIXTURES / "cad/verification/cases.json")
    d = Decimal
    positions = portfolio["positions"]
    market = sum(d(p["mark"]) * p["shares"] for p in positions)
    pnl = sum((d(p["mark"]) - d(p["average_cost"])) * p["shares"] for p in positions)
    nav = market + d(portfolio["cash"])
    summary = {"simulated": True, "as_of": portfolio["as_of"], "cash_usd": portfolio["cash"],
               "market_value_usd": str(market), "nav_usd": str(nav), "unrealized_pnl_usd": str(pnl),
               "gross_exposure_pct": str(market / nav * 100)}
    rows = [[p["symbol"], str(p["shares"]), "$" + p["mark"], f'${d(p["mark"]) * p["shares"]:,.2f}'] for p in positions]
    scope("stock-trading", "Northstar | Paper trading", "Fictional portfolio • fixed 2026-09-15 snapshot • USD",
          [markdown("SIMULATION ONLY", f'NAV ${nav:,.2f} · cash ${d(portfolio["cash"]):,.2f} · unrealized P/L +${pnl:,.2f}. Orders are illustrative and never submitted.'),
           table("Positions", ["Symbol", "Shares", "Mark", "Value"], rows),
           artifacts(("Portfolio preview", "portfolio.html", "Offline static dashboard"), ("Portfolio data", "portfolio.json", "Fictional source fixture"), ("Calculated totals", "summary.json", "Decimal arithmetic"))],
          "Review the fictional Northstar paper portfolio. Read artifacts/portfolio.json and summary.json. Reconcile cash plus marked positions to NAV and distinguish unfilled sample orders from holdings. This demo has no live prices, brokerage connection, or order execution. Never access financial accounts.")
    stock = DEFINITIONS / "stock-trading/artifacts"
    write(stock / "portfolio.json", portfolio)
    write(stock / "summary.json", summary)
    write(stock / "portfolio.html", page("Northstar / paper desk", '<p class="badge">SIMULATED · fictional securities · fixed snapshot</p>' + f'<h2>${nav:,.2f} NAV</h2><p>Cash ${d(portfolio["cash"]):,.2f} · Unrealized P/L +${pnl:,.2f} · Exposure {market/nav*100:.2f}%</p><table><tr><th>Symbol</th><th>Shares</th><th>Mark</th><th>Value</th></tr>' + ''.join('<tr>' + ''.join('<td>' + html.escape(c) + '</td>' for c in row) + '</tr>' for row in rows) + '</table><p>Snapshot 2026-09-15 16:00 UTC. Two unfilled illustrative limit orders. No live feed or trading connection.</p>'))
    result = geometry(model)
    report = verification(model, cases)
    scope("cad", "CAD | MP-100 mounting plate", "Rev B • aluminum • millimetres • analytical geometry",
          [table("Design constraints", ["Parameter", "Measured", "Requirement"], [["Mass", f'{result["mass_g"]} g', "≤ 110 g"], ["Edge clearance", f'{result["edge_clearance_mm"]} mm', "≥ 8 mm"], ["Thickness", "6 mm", "≥ 5.5 mm"]]),
           markdown("Verification portal", "Enter the verification child for nominal and intentionally invalid regression cases. These checks cover geometry, not load capacity."),
           artifacts(("Plate drawing", "plate.svg", "Dimensioned top view"), ("CAD preview", "plate.html", "Browser preview"), ("Parametric source", "plate.scad", "OpenSCAD source; no install required"), ("Model specification", "model.json", "Dimensions, material and constraints"))],
          "Inspect the MP-100 rev B sensor mounting plate in artifacts/model.json, plate.svg and plate.scad. Dimensions use mm. Enter verification for calculated constraints and regression cases. Do not describe geometry checks as structural analysis or manufacturing approval. No large CAD installation is needed.")
    cad = DEFINITIONS / "cad/artifacts"
    write(cad / "model.json", model)
    p, holes = model["plate"], model["holes"]
    circles = ''.join(f'<circle cx="{60+x*6}" cy="{90+y*6}" r="{holes["diameter"]*3}" fill="#101923" stroke="#7ed9c7" stroke-width="2"/>' for x,y in holes["centers"])
    svg = f'<svg xmlns="http://www.w3.org/2000/svg" width="760" height="560" viewBox="0 0 760 560"><rect width="760" height="560" fill="#172735"/><g font-family="sans-serif" fill="#e4edf6"><text x="60" y="40" font-size="23">MP-100 · REV B · TOP VIEW</text><rect x="60" y="90" width="600" height="360" rx="0" fill="#305267" stroke="#7ed9c7" stroke-width="3"/>{circles}<path d="M60 470v20h600v-20 M680 90h20v360h-20" fill="none" stroke="#ffd38a"/><text x="330" y="520" font-size="21">100 mm</text><text x="710" y="290" font-size="20" transform="rotate(-90 710 290)">60 mm</text><text x="70" y="70" font-size="17">4 × Ø6.6 THRU · centres 12 mm from edges · 6 mm thick</text></g></svg>\n'
    write(cad / "plate.svg", svg)
    write(cad / "plate.html", page("MP-100 / mounting plate", '<p>Revision B · Aluminum 6061-T6 · analytical geometry fixture</p>' + svg + f'<p>Calculated mass {result["mass_g"]} g. Minimum hole-edge clearance {result["edge_clearance_mm"]} mm.</p><p>No structural certification or manufacturing release.</p>'))
    write(cad / "plate.scad", '// MP-100 rev B; mm; geometry demo only\n$fn=64;\ndifference() {\n  cube([100,60,6]);\n  for (x=[12,88], y=[12,48]) translate([x,y,-1]) cylinder(d=6.6,h=8);\n}\n')
    check_rows = [[r["id"], r["name"], r["result"].upper(), "matched" if r["expectation_met"] else "MISMATCH"] for r in report["cases"]]
    scope("verification", "Verification | MP-100 regression suite", "Three analytical cases • two expected failures • no FEA",
          [table("Constraint results", ["Case", "Scenario", "Geometry", "Expectation"], check_rows),
           markdown("Result interpretation", "3/3 expectations met. Nominal geometry passes. Oversized holes fail edge clearance; thin stock fails minimum thickness. Expected failures demonstrate that checks detect invalid geometry."),
           artifacts(("Test results", "results.json", "Calculated checks and expected outcomes"), ("Case definitions", "cases.json", "Nominal and invalid inputs"), ("Verification report", "report.md", "Human-readable method and limitations"))],
          "Review artifacts/cases.json and results.json. Confirm nominal passes, the 10 mm hole mutation fails edge-clearance, and 5 mm stock fails minimum-thickness. A detected expected failure is a passing regression test. Use the reusable demo.py verify command to recalculate; no FEA or CAD engine is involved.")
    verify = DEFINITIONS / "verification/artifacts"
    write(verify / "results.json", report)
    write(verify / "cases.json", cases)
    write(verify / "report.md", '# MP-100 verification\n\nMethod: rectangular prism minus four cylindrical through-holes; nominal density 2700 kg/m³. Edge clearance is nearest plate edge minus hole radius.\n\n' + '\n'.join(f'- {r["id"]}: {r["name"]}; geometry {r["result"]}; expected {r["expected"]}; expectation met = {r["expectation_met"]}.' for r in report["cases"]) + '\n\nScope: analytical geometry only. No tolerance stack, FEA, fatigue, tooling, or manufacturing approval.\n')
    scope("mockups", "Mockups | Working worlds", "Offline fixtures • nested local workspaces • no extra agents",
          [markdown("Choose a world", "Enter stock-trading for the fictional Northstar paper desk, or cad for a mounting plate. Inside cad, enter verification for calculated regression tests."),
           table("Demo map", ["Universe", "Preview", "Evidence"], [["stock-trading", "Paper portfolio", "$40,000 NAV"], ["cad", "MP-100 plate", "94.983 g"], ["cad / verification", "Constraint suite", "3/3 expectations met"]]),
           artifacts(("Demo guide", "guide.md", "Navigation and artifact map"))],
          "Explore stock-trading and cad, then cad/verification. Each universe has local prompts, declarative views and lightweight artifacts; agents are not required for these static worlds. Market data is fictional. Read artifacts/guide.md for the walkthrough.")
    write(DEFINITIONS / "mockups/artifacts/guide.md", '# Working worlds\n\nSelect a child universe and use the dashboard enter action. Use its parent/back action to return with navigation context preserved.\n\n- stock-trading: fixed fictional portfolio, JSON and offline HTML.\n- cad: mounting-plate dimensions, SVG, HTML and OpenSCAD text.\n- cad/verification: analytical results with two intentional geometry failures.\n\nLocal command: `mv2 -u UNIVERSE command demo`. Artifact paths are relative to each universe root. Preview files are static fixtures, not live terminal evidence.\n')
    return summary, report


def install(args):
    build()
    root = Path(args.universe).resolve()
    template = Path(args.template).resolve()
    runtime = Path(args.workspace_root).resolve()
    if not (root / "universe.json").is_file():
        raise SystemExit("Create the parent mockups universe with V2 first.")
    if not (template / "universe.json").is_file():
        raise SystemExit("Template must contain universe.json")
    workspaces = {"stock-trading": runtime / "stock-trading", "cad": runtime / "cad", "verification": runtime / "cad/verification"}
    for name, relative in SCOPES.items():
        target = root / relative
        if name != "mockups":
            workspace = workspaces[name]
            workspace.mkdir(parents=True, exist_ok=True)
            if not target.exists():
                parent = root / "subverses/cad" if name == "verification" else root
                subprocess.run([args.cli, "-u", str(parent), "subverse", name, "--template", str(template), "--workspace", str(workspace)], check=True)
            meta = read(target / "universe.json")
            actual_workspace = (target / meta["workspace"]).resolve()
            if actual_workspace != workspace:
                raise SystemExit(f"Existing {name} points to {actual_workspace}; refusing to change its workspace")
            fixture = FIXTURES / ("cad/verification" if name == "verification" else name)
            shutil.copytree(fixture, workspace, dirs_exist_ok=True)
        shutil.copytree(DEFINITIONS / name, target, dirs_exist_ok=True)
        print(f"Installed {name}: {target}")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("build")
    sub.add_parser("verify")
    install_parser = sub.add_parser("install")
    install_parser.add_argument("--universe", required=True)
    install_parser.add_argument("--template", required=True)
    install_parser.add_argument("--workspace-root", default=str(HERE / "runtime"))
    install_parser.add_argument("--cli", default=os.environ.get("MV2_CLI", "mv2"))
    args = parser.parse_args()
    if args.command == "install":
        install(args)
    else:
        summary, report = build()
        assert summary["nav_usd"] == "40000.00", summary
        assert summary["unrealized_pnl_usd"] == "579.00", summary
        assert report["all_expectations_met"], report
        assert report["cases"][0]["mass_g"] == 94.983
        for scope_dir in DEFINITIONS.iterdir():
            view = read(scope_dir / "views/default.json")
            for widget in view["widgets"]:
                for item in widget.get("items", []):
                    assert (scope_dir / item["path"]).is_file(), item
        print("PASS: portfolio reconciles; 3 CAD expectations met; every view artifact resolves.")


if __name__ == "__main__":
    main()
