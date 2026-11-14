"""Update a universe's definition files from its template (dry run unless --apply)."""
import difflib
from pathlib import Path
import tempfile

from .. import universe
from ..universe import templates
from ..store import lock

LABELS = {"update": "update", "add": "add", "merge": "merge", "local": "keep", "conflict": "conflict",
          "unknown": "unknown", "deleted": "keep", "removed": "removed", "deferred": "deferred"}


def configure(parser):
    parser.add_argument("--apply", action="store_true", help="Write the planned changes (default: show them)")
    parser.add_argument("--diff", action="store_true", help="Show the differences for changed files")
    parser.add_argument("--take", action="append", default=[], metavar="PATH",
                        help="Replace this file with the template's version even if it differs locally (repeatable)")
    parser.add_argument("--template", help="Template to compare with (default: the one the universe was installed from)")
    parser.add_argument("--recursive", action="store_true", help="Also refresh every subverse")
    parser.add_argument("--json", action="store_true", help="Print the result as JSON")


def _diff(root, source, path, merged=None):
    def lines(file):
        return file.read_text().splitlines(keepends=True) if file.is_file() else []
    theirs = merged.splitlines(keepends=True) if merged is not None else lines(Path(source) / path)
    label = f"merged/{path}" if merged is not None else f"template/{path}"
    return "".join(difflib.unified_diff(lines(Path(root) / path), theirs, f"universe/{path}", label))


def _unusable(path, text):
    """Why a merged harness file couldn't be used, or None: it must load as its harness and its model settings
    must hold (a default inside the subset), or every launch with it would be refused."""
    from .. import harnesses
    from ..runtime import model_choices
    from ..runtime.run_environment import launch
    name = Path(path).parts[1]
    with tempfile.TemporaryDirectory(prefix=".mv2-refresh-") as scratch:
        target = Path(scratch) / "harnesses" / name / "harness.json"
        target.parent.mkdir(parents=True)
        target.write_text(text)
        try:
            adapter = harnesses.load(Path(scratch), name)
            model_choices.validate_settings(Path(scratch), adapter, environment=launch(adapter))
        except ValueError as error:
            return str(error).replace(scratch, "the merged file")
    return None


def run(root=None, *, apply=False, diff=False, take=(), template=None, recursive=False, json=False):
    root = universe.resolve(root)
    scopes = list(universe.tree(root)) if recursive else [root]
    results = []
    for scope in scopes:
        relative = {Path(path).as_posix() for path in take}
        with lock(scope / ".refresh.lock"):
            plan = templates.refresh_plan(scope, template, relative if scope == root else ())
            for change in plan["changes"]:
                why = _unusable(change["path"], change["_text"]) if change["action"] == "merge" else None
                if why:  # never published: the universe's file stays, as a conflict that says why
                    for key in ("_text", "_advance", "_base"):
                        change.pop(key, None)
                    change.update(action="conflict", reason=f"the merge would leave settings mv2 can't use "
                                                            f"({why}); yours kept")
            if diff:
                for change in plan["changes"]:
                    if change["action"] in ("update", "conflict", "unknown", "local", "merge"):
                        change["diff"] = _diff(scope, plan["source"], change["path"], change.get("_text"))
            plan["applied"] = None
            if apply:
                plan["applied"] = templates.refresh_apply(scope, plan)
        for change in plan["changes"]:  # the merged text and bookkeeping are the plan's own, not output
            for key in [key for key in change if key.startswith("_")]:
                del change[key]
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
        pending = [c for c in plan["changes"] if c["action"] in ("update", "add", "merge")]
        lines.append(f'  {plan["current"]} up to date' + (f', {len(pending)} to write' if pending else ''))
        if plan["applied"] is not None:
            written = plan["applied"]["written"]
            lines.append(f'  Wrote {len(written)} file(s)' + (
                f'; replaced files saved in {plan["applied"]["trash"]}' if plan["applied"]["trash"] else ''))
            for path in plan["applied"].get("skipped") or ():
                lines.append(f"  Left {path} as it is: it changed while refresh ran. Run refresh again.")
    if not result["apply"]:
        lines.append("Dry run: nothing written. Run again with --apply to write.")
    elif any((p["applied"] or {}).get("written") for p in result["universes"]):
        lines.append("Skills, commands and workflows apply immediately; running agents keep their old bootstrap until relaunched.")
    return "\n".join(lines)


if __name__ == "__main__":
    from ._common import main
    main(configure, run)
