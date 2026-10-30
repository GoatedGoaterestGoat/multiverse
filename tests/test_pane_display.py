"""The dashboard's live pane without what the harness draws around its input: the pane display protocol, its guards
and its call site."""
import json
from pathlib import Path
import sys
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "examples"))
from textual.widgets import Static

from multiverse import harnesses
from multiverse.tools import inspect
from multiverse.ui.app import MultiverseApp
from isolation import isolate
from test_multiverse import BaseTest
from tui_demo import DemoBackend

FIXTURES = Path(__file__).parent / "fixtures"
# One capture set per checked release, with the lines the live pane shows; a release whose screen differs fails here,
# not as an untidy pane. Codex's mid-conversation screens are its own renderer's snapshots, not tmux captures.
SCREENS = {"claude": FIXTURES / "claude/screens-2.1.314.json",
           "codex": FIXTURES / "codex/upstream-screens-0.164.0.json"}
# Screens that stay exactly as they are: dialogs, which the person answers in the pane, and screens whose end the
# harness can't be sure is its input.
UNCHANGED = {"model dialog", "trust dialog", "permission dialog", "question dialog", "command approval",
             "permissions selection", "config dialog", "reconnecting with draft", "history search"}
# Built from Codex 0.164.0's source and its reviewers' probes, not captured: the end of the screen under a reply, and
# what is left of the screen, None when it stays unchanged.
CONVERSATION = "• Previous conversation\n\n"
CODEX_BUILT = {
    "a draft between blank rows": ("› Ask Codex to do anything\n\n  100% context left\n\n"
                                   "  gpt-5 default · /tmp/project\n", "• Previous conversation\n"),
    "a draft on the footer": ("› fix it\n  100% context left\n", None),
    "no footer": ("› Ask Codex to do anything\n", None),
    "three rows that look like footer": ("› fix it\n\n  ? for shortcuts\n  100% context left\n"
                                         "  gpt-5 default · /tmp\n", None),
    "a copy result": ("› Ask Codex to do anything\n\n  Copy unconfirmed; /export saves chat\n", None),
    "a search with no match": ("› Ask Codex to do anything\n\n  reverse-i-search: needle  no match\n", None),
    "a sent message over a dialog": ("› fix the bug\n\n  🔒  This conversation is open in another app\n\n"
                                     "   r retry   f fork   esc/⌃c/q exit\n", None),
}
CAPTURE = "conversation\n  ----------\n\n────────\n❯\xa0\n────────\n  status"


class Tidy:
    def __init__(self, display_pane):
        self.display_pane = display_pane


class InspectOnly:
    """A backend from before live_pane, such as a plugin's test double."""
    def __init__(self, backend):
        self.backend = backend

    def __getattr__(self, name):
        if name == "live_pane":
            raise AttributeError(name)
        return getattr(self.backend, name)


class PaneDisplayTest(BaseTest):
    def test_screens_lose_only_the_input_and_what_is_under_it(self):
        """Each screen loses exactly the harness's input box and the rows under it, a dialog or a screen the harness
        can't be sure of stays as it is, and a line of dashes in the conversation stays; a harness release that changes
        its screen breaks only this."""
        for harness, path in SCREENS.items():
            adapter = harnesses.load(self.root, harness)
            for frame in json.loads(path.read_text()):
                with self.subTest(harness=harness, screen=frame["name"]):
                    self.assertEqual(adapter.display_pane(frame["text"]), frame["shown"])
                    self.assertEqual(frame["shown"] == frame["text"], frame["name"] in UNCHANGED)
                    if frame["name"] == "idle":
                        self.assertIn("  ---", frame["shown"].split("\n"))
        codex = harnesses.load(self.root, "codex")
        for name in ("started", "started-layouts", "loading", "resuming", "refusal"):
            for number, frame in enumerate(json.loads((FIXTURES / f"codex/{name}-0.163.0.json").read_text())):
                with self.subTest(harness="codex", screen=f"{name} {number}"):
                    # Real tmux captures: the composer is the cursor's row. A refusal has none, a loading screen has
                    # no footer yet, and a lone status item could be a message.
                    rows = frame["text"].split("\n")
                    kept = name in ("refusal", "loading") or frame.get("footer") == "one"
                    shown = frame["text"] if kept else "\n".join(rows[:frame["cursor_y"]])
                    self.assertEqual(codex.display_pane(frame["text"]), shown)
        for name, (end, shown) in CODEX_BUILT.items():
            with self.subTest(harness="codex", screen=name):
                self.assertEqual(codex.display_pane(CONVERSATION + end), shown or CONVERSATION + end)

    def test_a_broken_adapter_never_blanks_or_rewrites_the_pane(self):
        """The inspector shows the raw capture whatever the adapter returns or raises; a break would blank the pane or
        show text the agent never wrote, with nothing to say the adapter is at fault."""
        def fail(text):
            raise RuntimeError("broken")
        cases = [
            ("no display_pane", object(), CAPTURE),
            ("raises", Tidy(fail), CAPTURE),
            ("not a string", Tidy(lambda text: text.split("\n")), CAPTURE),
            ("nothing left", Tidy(lambda text: ""), CAPTURE),
            ("lines out of order", Tidy(lambda text: "\n".join(reversed(text.split("\n")))), CAPTURE),
            ("a line rewritten", Tidy(lambda text: text.replace("conversation", "conversion")), CAPTURE),
            ("some lines, in order", Tidy(lambda text: "conversation\n  ----------"), "conversation\n  ----------"),
        ]
        for name, adapter, shown in cases:
            with self.subTest(name), patch.object(harnesses, "load", return_value=adapter):
                self.assertEqual(inspect.display(self.root, "claude", CAPTURE), shown)
        for harness in ("pi", "missing", None):
            with self.subTest(harness=harness):
                self.assertEqual(inspect.display(self.root, harness, CAPTURE), CAPTURE)


class LivePaneTest(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        isolate(self)

    async def preview(self, backend):
        app = MultiverseApp(backend.root, backend, refresh_interval=0)
        async with app.run_test(size=(140, 32)) as pilot:
            await pilot.pause()
            await app.workers.wait_for_complete()
            await pilot.press("p")
            await pilot.pause()
            await app.workers.wait_for_complete()
            await pilot.pause()
            return str(app.query_one("#preview", Static).render())

    async def test_the_inspector_shows_the_tidied_pane(self):
        """The inspector shows live_pane's text, and the terminal itself from a backend without live_pane; a break
        shows the chrome again, or turns every capture from such a backend into an error."""
        backend = DemoBackend()
        with patch.object(backend, "inspect", return_value="RAW PANE"), \
                patch.object(backend, "live_pane", return_value="TIDY PANE"):
            self.assertEqual(await self.preview(backend), "TIDY PANE")
        backend = DemoBackend()
        with patch.object(backend, "inspect", return_value="RAW PANE"):
            self.assertEqual(await self.preview(InspectOnly(backend)), "RAW PANE")


if __name__ == "__main__":
    unittest.main()
