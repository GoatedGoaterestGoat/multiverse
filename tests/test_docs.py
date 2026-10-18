"""Docs stay true: relative links resolve, and the CLI reference matches the parser."""
import argparse
from pathlib import Path
import re
import subprocess
import sys
import unittest
from urllib.parse import unquote

from multiverse import cli

ROOT = Path(__file__).resolve().parents[1]
REFERENCE = ROOT / "docs" / "cli" / "reference.md"
# Design records may cite files that have since moved; they are not checked.
SKIPPED = (ROOT / "docs" / "design",)

FENCE = re.compile(r"^ {0,3}(`{3,}|~{3,}).*?^ {0,3}\1[`~]*[ \t]*$", re.MULTILINE | re.DOTALL)
CODE_SPAN = re.compile(r"(`+)(?!`).*?(?<!`)\1(?!`)")
INLINE = re.compile(r"!?\[[^\]]*\]\(\s*(<[^>]*>|[^)\s]+)(?:\s+(?:\"[^\"]*\"|'[^']*'|\([^)]*\)))?\s*\)")
DEFINITION = re.compile(r"^ {0,3}\[[^\]]+\]:\s*(<[^>]*>|\S+)", re.MULTILINE)
HTML = re.compile(r"<(?:a|img)\b[^>]*?\b(?:href|src)\s*=\s*[\"']([^\"']+)[\"']", re.IGNORECASE)
SCHEME = re.compile(r"^[a-zA-Z][a-zA-Z0-9+.-]*:")


def pages():
    found = [ROOT / name for name in ("README.md", "AGENTS.md") if (ROOT / name).is_file()]
    for path in sorted((ROOT / "docs").rglob("*.md")):
        if not any(path.is_relative_to(skipped) for skipped in SKIPPED):
            found.append(path)
    return found


def targets(text):
    """Link and image targets outside code blocks and code spans."""
    text = CODE_SPAN.sub("", FENCE.sub("", text))
    for pattern in (INLINE, DEFINITION, HTML):
        for match in pattern.finditer(text):
            yield match.group(1).strip("<>")


def missing_submodules():
    """Optional plugin submodules this clone hasn't fetched: links into them can't be checked here."""
    found = []
    modules = ROOT / ".gitmodules"
    if modules.is_file():
        for line in modules.read_text().splitlines():
            key, _, value = line.strip().partition("=")
            if key.strip() == "path":
                directory = ROOT / value.strip()
                if not directory.is_dir() or not any(directory.iterdir()):
                    found.append(directory)
    return found


def anchors(text):
    """The heading anchors GitHub makes for a Markdown page: lower case, punctuation dropped,
    spaces as hyphens, and -1, -2 after a repeated one."""
    found, seen = set(), {}
    for match in re.finditer(r"^#{1,6}[ \t]+(.+?)[ \t#]*$", FENCE.sub("", text), re.MULTILINE):
        title = re.sub(r"\[([^\]]*)\]\([^)]*\)", r"\1", match.group(1))
        slug = re.sub(r"[^\w\- ]", "", title.lower().replace("`", "")).replace(" ", "-")
        count = seen.get(slug, 0)
        seen[slug] = count + 1
        found.add(slug if count == 0 else f"{slug}-{count}")
    return found


def broken_links(page):
    problems = []
    unfetched = missing_submodules()
    text = page.read_text(encoding="utf-8")
    for target in targets(text):
        if SCHEME.match(target) or target.startswith("//"):
            continue
        path, _, fragment = unquote(target.split("?", 1)[0]).partition("#")
        if not path:
            if fragment and fragment not in anchors(text):
                problems.append(f"{page.relative_to(ROOT)}: {target} (no such heading)")
            continue
        resolved = (ROOT / path.lstrip("/")) if path.startswith("/") else (page.parent / path)
        if any(resolved.resolve().is_relative_to(directory.resolve()) for directory in unfetched):
            continue
        if path.startswith("/") or not resolved.exists():
            problems.append(f"{page.relative_to(ROOT)}: {target}")
        elif fragment and resolved.suffix == ".md" and fragment not in anchors(resolved.read_text(encoding="utf-8")):
            problems.append(f"{page.relative_to(ROOT)}: {target} (no such heading)")
    return problems


def headings(text):
    text = FENCE.sub("", text)
    return {match.group(1).replace("`", "").strip()
            for match in re.finditer(r"^#{1,6}[ \t]+(.+?)[ \t#]*$", text, re.MULTILINE)}


class LinkTest(unittest.TestCase):
    def test_relative_links_and_images_resolve(self):
        """Relative links, images and anchors in README, AGENTS and docs resolve; a break goes unnoticed because nobody
        clicks a link broken by a moved file until a user hits it."""
        with self.subTest("anchors follow GitHub's rules"):
            text = ("# Getting started\n## Live connection or launch-only\n## `mv2 send` (CLI)\n## Keys\n## Keys\n"
                    "```\n# not a heading\n```\n")
            self.assertEqual(anchors(text),
                             {"getting-started", "live-connection-or-launch-only", "mv2-send-cli", "keys", "keys-1"})
        with self.subTest("links in code are ignored, external ones are found"):
            text = ("[a](one.md) ![b](img/two.png \"t\") [c](https://x.org) [d](#here)\n"
                    "`[e](code.md)`\n```sh\n[f](block.md)\n```\n[g]: ref.md\n<img src=\"three.png\">\n")
            self.assertEqual(list(targets(text)),
                             ["one.md", "img/two.png", "https://x.org", "#here", "ref.md", "three.png"])
        problems = [problem for page in pages() for problem in broken_links(page)]
        if problems:
            self.fail(f"{len(problems)} broken relative links; fix the path, or link a page or heading that exists:\n"
                      + "\n".join(problems))


class CliReferenceTest(unittest.TestCase):
    def test_generated_reference_is_current(self):
        """docs/cli/reference.md matches the parser, with a heading for every command and alias; a break goes unnoticed
        because regenerating it is a manual step, and agents learn the flags from it."""
        parser = cli.parser()
        commands = next(action for action in parser._actions if isinstance(action, argparse._SubParsersAction))
        found = headings(REFERENCE.read_text(encoding="utf-8"))
        missing = [name for name in commands.choices if name not in found]
        self.assertEqual(missing, [], "Run python scripts/gen_cli_docs.py")
        result = subprocess.run([sys.executable, str(ROOT / "scripts" / "gen_cli_docs.py"), "--check"],
                                capture_output=True, text=True, timeout=60)
        self.assertEqual(result.returncode, 0, result.stderr)


if __name__ == "__main__":
    unittest.main()
