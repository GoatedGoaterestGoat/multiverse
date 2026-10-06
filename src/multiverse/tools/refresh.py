"""Update a universe's definition files from its template (dry run unless --apply)."""
import difflib
from pathlib import Path

from .. import universe
from ..universe import templates
from ..store import lock

LABELS = {"update": "update", "add": "add", "local": "keep", "conflict": "conflict", "unknown": "unknown",
          "deleted": "keep", "removed": "removed"}


def configure(parser):
    parser.add_argument("--apply", action="store_true", help="Write the planned changes (default: show them)")
    parser.add_argument("--diff", action="store_true", help="Show the differences for changed files")
    parser.add_argument("--take", action="append", default=[], metavar="PATH",
                        help="Replace this file with the template's version even if it differs locally (repeatable)")
    parser.add_argument("--template", help="Template to compare with (default: the one the universe was installed from)")
    parser.add_argument("--recursive", action="store_true", help="Also refresh every subverse")
    parser.add_argument("--json", action="store_true", help="Print the result as JSON")


def _diff(root, source, path):
    def lines(file):
        return file.read_text().splitlines(keepends=True) if file.is_file() else []
    return "".join(difflib.unified_diff(lines(Path(root) / path), lines(Path(source) / path),
                                        f"universe/{path}", f"template/{path}"))


def run(root=None, *, apply=False, diff=False, take=(), template=None, recursive=False, json=False):
    root = universe.resolve(root)
    scopes = list(universe.tree(root)) if recursive else [root]
    results = []
    for scope in scopes:
        relative = {Path(path).as_posix() for path in take}
        with lock(scope / ".refresh.lock"):
            plan = templates.refresh_plan(scope, template, relative if scope == root else ())
            if diff:
                for change in plan["changes"]:
                    if change["action"] in ("update", "conflict", "unknown", "local"):
                        change["diff"] = _diff(scope, plan["source"], change["path"])
            plan["applied"] = None
            if apply:
                plan["applied"] = templates.refresh_apply(scope, plan)
        results.append(plan)
    return {"universes": results, "apply": apply, "json": json, "ok": True}


def format_output(result):
    if result.get("json"):
        return {key: value for key, value in result.items() if key != "json"}
    lines = []
    for plan in result["universes"]:
        lines.append(f'{plan["name"]} ({plan["universe"]}) from template {plan["template"]}')
        if not plan["has_manifest"]:
            lines.append("  No install record yet: files that differ from the template are shown as unknown"
                         " and change only with --take PATH.")
        for change in plan["changes"]:
            lines.append(f'  {LABELS[change["action"]]:<9}{change["path"]}  ({change["reason"]})')
            if change.get("diff"):
                lines.extend("    " + line for line in change["diff"].rstrip("\n").splitlines())
        pending = [c for c in plan["changes"] if c["action"] in ("update", "add")]
        lines.append(f'  {plan["current"]} up to date' + (f', {len(pending)} to write' if pending else ''))
        if plan["applied"] is not None:
            written = plan["applied"]["written"]
            lines.append(f'  Wrote {len(written)} file(s)' + (
                f'; replaced files saved in {plan["applied"]["trash"]}' if plan["applied"]["trash"] else ''))
    if not result["apply"]:
        lines.append("Dry run: nothing written. Run again with --apply to write.")
    elif any((p["applied"] or {}).get("written") for p in result["universes"]):
        lines.append("Skills, commands and workflows apply immediately; running agents keep their old bootstrap until relaunched.")
    return "\n".join(lines)


if __name__ == "__main__":
    from ._common import main
    main(configure, run)
