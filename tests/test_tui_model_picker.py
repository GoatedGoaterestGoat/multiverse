"""n picks a model, and an effort where the model takes one, from the harness's list."""
import asyncio
from pathlib import Path
import sys
from threading import Event
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "examples"))
from textual import events
from textual.widgets import OptionList, Static

from multiverse.plugins.testing import PluginTestCase
from multiverse.store import read, write
from multiverse.ui.app import MultiverseApp
from multiverse.ui.tree import group_key
from isolation import isolate
from tui_demo import DemoBackend


class ModelPickerTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        isolate(self)
        self.backend = DemoBackend()
        self.app = MultiverseApp(self.backend.root, self.backend, refresh_interval=0)
        self.review = self.backend.root / "subverses/pr-review"

    async def settle(self, pilot):
        for _ in range(3):
            await pilot.pause()
            await self.app.workers.wait_for_complete()
        await pilot.pause()

    async def until(self, pilot, check, what, timeout=10.0):
        deadline = asyncio.get_running_loop().time() + timeout
        while not check():
            if asyncio.get_running_loop().time() > deadline:
                self.fail(f"timed out waiting for {what}")
            await pilot.pause(0.02)

    def menu(self):
        menu = self.app.query_one("#draft-menu", OptionList)
        return [str(menu.get_option_at_index(i).prompt) for i in range(menu.option_count)] if menu.display else []

    def selection(self):
        return str(self.app.query_one("#selection", Static).render())

    async def draft(self, pilot, harness):
        """A fresh n in the review group, its harness set to `harness`, the model field focused once its list is read."""
        await self.start(pilot, harness)
        await pilot.press("tab")
        await self.until(pilot, lambda: not self.app.draft.reading(), "the harness's model list")
        await self.settle(pilot)

    async def start(self, pilot, harness):
        if self.app.draft:
            await pilot.press("escape")
        self.app.select_key(group_key(self.review))
        await pilot.press("n", "tab")
        for _ in self.app.draft.harnesses:
            if self.app.draft.values["harness"] == harness:
                break
            await pilot.press("down")
        self.assertEqual(self.app.draft.values["harness"], harness)

    async def test_n_offers_the_harness_list_and_sends_exactly_what_was_picked(self):
        """With a model list, n's model is picked from it (neither typed nor pasted), an Effort field appears only for a model that takes one,
        what is created is exactly the picked model and effort, a harness or model change clears what no longer
        applies, there is no default where core won't allow one, with default the effort follows the model the settings
        configure, a model can be typed beside the list only where core takes unlisted
        IDs, a harness without a list (or whose list can't be read) takes a typed model, and nothing is created
        while the list is being read; fragile because what the picker shows and what spawn gets are kept apart, so
        an effort left from another model or a model from another harness would be sent where the person can't see
        it."""
        async with self.app.run_test(size=(120, 30)) as pilot:
            await self.settle(pilot)
            with self.subTest("the list, with the harness default first, and typing ignored"):
                await self.draft(pilot, "claude")
                self.assertEqual(self.menu(), ["default", "Opus 5.5 (claude-opus-5-5)", "Haiku 4.5 (claude-haiku-4-5)"])
                await pilot.press(*"gpt-5")
                self.app.query_one("#agents").post_message(events.Paste("gpt-5"))
                await pilot.pause()
                self.assertEqual(self.app.draft.values["model"], "")
                self.assertNotIn("effort", self.app.draft.fields)
            with self.subTest("a model that takes efforts adds the Effort field, with its own list"):
                await pilot.press("down")
                self.assertEqual(self.app.draft.values["model"], "claude-opus-5-5")
                self.assertIn("Model: Opus 5.5 (claude-opus-5-5) ▾", self.selection())
                self.assertEqual(self.menu()[0], "default")  # no configured model: a pick names no default
                self.app.query_one("#agents").post_message(events.Paste("stray"))  # never throws the pick away
                await pilot.pause()
                self.assertEqual(self.app.draft.values["model"], "claude-opus-5-5")
                await pilot.press("tab")
                await self.settle(pilot)
                self.assertEqual(self.app.draft.active, "effort")
                self.assertEqual(self.menu(), ["default", "low", "medium", "high", "xhigh"])
                await pilot.press("down", "down")
                self.assertEqual(self.app.draft.values["effort"], "medium")
            with self.subTest("a model that takes none drops the effort, and a harness change drops both"):
                await self.draft(pilot, "claude")
                await pilot.press("down", "tab", "down")  # Opus, effort low
                self.assertEqual(self.app.draft.values["effort"], "low")
                await pilot.press("shift+tab", "down")
                self.assertEqual(self.app.draft.values["model"], "claude-haiku-4-5")
                self.assertEqual(self.app.draft.values["effort"], "")
                self.assertNotIn("effort", self.app.draft.fields)
                await pilot.press("up")  # back to Opus: the effort starts from default again
                self.assertEqual(self.app.draft.values["effort"], "")
                await pilot.press("tab", "down", "shift+tab", "shift+tab", "up")  # effort low, then harness: to codex
                self.assertEqual(self.app.draft.values["harness"], "codex")
                self.assertEqual((self.app.draft.values["model"], self.app.draft.values["effort"]), ("", ""))
            with self.subTest("Enter creates exactly the picked model and effort"):
                await self.draft(pilot, "claude")
                await pilot.press("down", "tab", "down", "down", "down", "enter")
                await self.settle(pilot)
                self.assertIsNone(self.app.draft)
                created = self.backend.agents[-1]
                self.assertEqual((created["harness"], created["model"], created["effort"]),
                                 ("claude", "claude-opus-5-5", "high"))
            with self.subTest("where core won't let the model or effort be left unset, there's no default and Enter waits"):
                real = self.backend.model_choices
                self.backend.model_choices = lambda root, harness: {**real(root, harness), "unset": False, "models": [
                    {**model, "effort_unset": False} for model in real(root, harness)["models"]]}
                try:
                    agents = len(self.backend.agents)
                    await self.draft(pilot, "claude")
                    self.assertEqual(self.menu(), ["Opus 5.5 (claude-opus-5-5)", "Haiku 4.5 (claude-haiku-4-5)"])
                    self.assertIn("choose a model", self.selection())
                    await pilot.press("up")  # nothing chosen yet: the first step back is the last choice
                    self.assertEqual(self.app.draft.values["model"], "claude-haiku-4-5")
                    await pilot.press("escape")
                    await self.draft(pilot, "claude")
                    await pilot.press("enter")
                    await self.settle(pilot)
                    self.assertEqual(len(self.backend.agents), agents)
                    self.assertIn("Choose a model", str(self.app.query_one("#status", Static).render()))
                    await pilot.press("down", "tab")  # Opus: its effort must be chosen too
                    await self.settle(pilot)
                    self.assertEqual(self.menu(), ["low", "medium", "high", "xhigh"])
                    await pilot.press("enter")
                    await self.settle(pilot)
                    self.assertEqual(len(self.backend.agents), agents)
                    await pilot.press("down", "enter")
                    await self.settle(pilot)
                    self.assertEqual((self.backend.agents[-1]["model"], self.backend.agents[-1]["effort"]),
                                     ("claude-opus-5-5", "low"))
                finally:
                    self.backend.model_choices = real
            with self.subTest("a model the settings configure: default stays unset, and its effort follows it"):
                real = self.backend.model_choices
                self.backend.model_choices = lambda root, harness: {
                    **real(root, harness), "default_model": "claude-opus-5-5",
                    "models": [{**model, "effort_unset": model["id"] != "claude-opus-5-5"}
                               for model in real(root, harness)["models"]]}
                try:
                    agents = len(self.backend.agents)
                    await self.draft(pilot, "claude")
                    self.assertEqual(self.menu()[0], "default (Opus 5.5)")
                    self.assertIn("effort", self.app.draft.fields)  # Opus's, though the model is left unset
                    await pilot.press("tab")
                    await self.settle(pilot)
                    self.assertEqual(self.menu(), ["low", "medium", "high", "xhigh"])  # its effort must be chosen
                    await pilot.press("enter")
                    await self.settle(pilot)
                    self.assertEqual(len(self.backend.agents), agents)
                    self.assertIn("Choose an effort", str(self.app.query_one("#status", Static).render()))
                    await pilot.press("down", "enter")
                    await self.settle(pilot)
                    self.assertEqual((self.backend.agents[-1]["model"], self.backend.agents[-1]["effort"]), (None, "low"))
                    await self.draft(pilot, "claude")
                    await pilot.press("down", "down")  # Haiku picked: no effort, and none carried over
                    self.assertNotIn("effort", self.app.draft.fields)
                    self.assertEqual(self.menu()[0], "default (Opus 5.5)")  # what unset launches, not the pick
                finally:
                    self.backend.model_choices = real
            with self.subTest("with no subset, a model can also be typed beside the list"):
                real = self.backend.model_choices
                self.backend.model_choices = lambda root, harness: {**real(root, harness), "free_form": True}
                try:
                    agents = len(self.backend.agents)
                    await self.draft(pilot, "claude")
                    self.assertIn("or type a model ID", self.selection())
                    await pilot.press("down", "tab", "down", "shift+tab")  # Opus, effort low, back to the model
                    await pilot.press(*"claude-next-x", "backspace", "backspace")
                    self.assertEqual(self.app.draft.values["model"], "claude-next")
                    self.assertEqual(self.app.draft.values["effort"], "")  # a typed model has no effort list
                    self.assertNotIn("effort", self.app.draft.fields)
                    await pilot.press("down")  # the list again
                    self.assertEqual(self.app.draft.values["model"], "")
                    await pilot.press("down")
                    self.assertEqual(self.app.draft.values["model"], "claude-opus-5-5")
                    self.app.query_one("#agents").post_message(events.Paste("claude-pasted"))
                    await pilot.pause()
                    self.assertEqual(self.app.draft.values["model"], "claude-pasted")
                    await pilot.press("enter")
                    await self.settle(pilot)
                    self.assertEqual(len(self.backend.agents), agents + 1)
                    self.assertEqual((self.backend.agents[-1]["model"], self.backend.agents[-1]["effort"]),
                                     ("claude-pasted", None))
                finally:
                    self.backend.model_choices = real
            with self.subTest("a harness with no list takes a typed model"):
                await self.draft(pilot, "pi")
                await pilot.press(*"local-model")
                self.assertEqual(self.app.draft.values["model"], "local-model")
                self.assertEqual(self.menu(), [])
            with self.subTest("a list that can't be read: the model is typed, and the line says why"):
                real = self.backend.model_choices
                self.backend.model_choices = lambda root, harness: (_ for _ in ()).throw(OSError("no table"))
                try:
                    await self.draft(pilot, "claude")
                    await pilot.press(*"claude-x")
                    self.assertEqual(self.app.draft.values["model"], "claude-x")
                    self.assertIn("couldn't read its models: no table", self.selection())
                finally:
                    self.backend.model_choices = real
            with self.subTest("nothing is created while the list is being read"):
                gate, agents = Event(), len(self.backend.agents)
                self.backend.model_choices = lambda root, harness: gate.wait(10) and real(root, harness)
                try:
                    await self.start(pilot, "claude")
                    await pilot.press("tab", *"typed", "enter")
                    await pilot.pause()
                    self.assertIsNotNone(self.app.draft)
                    self.assertEqual(self.app.draft.values["model"], "")
                    self.assertEqual(len(self.backend.agents), agents)
                    self.assertIn("reading claude's models", self.selection())
                finally:
                    gate.set()
                    self.backend.model_choices = real
                await self.until(pilot, lambda: not self.app.draft.reading(), "the held list")
                await pilot.press("escape")



class NarrowedSettingsTests(PluginTestCase, unittest.IsolatedAsyncioTestCase):
    async def test_n_offers_what_the_universes_harness_settings_allow(self):
        """On a real universe, n's model list is exactly the harness settings' subset, in table order, with no
        default where the subset can't prove the harness's own choice allowed (and one, with its effort, where the
        settings configure a model in it), each model's efforts as narrowed, and
        a stale entry said on the line; fragile because the list comes from core across two modules and a real
        settings file, so a drift would offer a model spawn then refuses, or hide one the person allowed."""
        settings = self.root / "harnesses" / "claude" / "harness.json"
        base = read(settings)
        write(settings, {**base, "settings": {"models": {"sonnet": {"efforts": ["low", "medium"]}, "haiku": {}}}})
        async with self.dashboard(size=(130, 30)) as d:
            created = []
            d.app.backend.spawn = lambda root, values: created.append(dict(values)) or {"id": "new", "name": values["name"]}

            def menu():
                widget = d.app.query_one("#draft-menu", OptionList)
                return [str(widget.get_option_at_index(i).prompt) for i in range(widget.option_count)] if widget.display else []

            async def draft():
                if d.app.draft:
                    await d.press("escape")
                d.app.select_key(group_key(self.root))
                await d.press("n", "tab")
                for _ in d.app.draft.harnesses:
                    if d.app.draft.values["harness"] == "claude":
                        break
                    await d.press("down")
                await d.press("tab")
                await d.until(lambda: not d.app.draft.reading(), "claude's models")
                await d.settle()

            with self.subTest("the subset, in table order, an alias resolved, and no default for the model"):
                await draft()
                self.assertEqual(menu(), ["Haiku 5.5 (claude-haiku-5-5)", "Sonnet 5.5 (claude-sonnet-5-5)"])
                self.assertIn("narrowed in this universe's harness settings", str(d.app.query_one("#selection").render()))
                await d.press(*"claude-opus-5-5")
                self.assertEqual(d.app.draft.values["model"], "")
            with self.subTest("each model's efforts as narrowed: a list must be chosen from, {} leaves it to the harness"):
                await d.press("down", "down", "tab")  # Sonnet
                await d.settle()
                self.assertEqual(menu(), ["low", "medium"])
                await d.press("shift+tab", "up", "tab")  # Haiku
                await d.settle()
                self.assertEqual(menu(), ["default", "low", "medium", "high", "xhigh", "max"])
            with self.subTest("Enter sends exactly the picked model and effort to spawn"):
                await d.press("shift+tab", "down", "tab", "down", "down", "enter")  # Sonnet, medium
                await d.settle()
                self.assertEqual({k: created[-1][k] for k in ("harness", "model", "effort")},
                                 {"harness": "claude", "model": "claude-sonnet-5-5", "effort": "medium"})
            with self.subTest("settings.model in the subset: default leaves it unset, its effort still chosen"):
                write(settings, {**base, "settings": {"model": "sonnet", "models": {"sonnet": {"efforts": ["low"]}}}})
                await draft()
                self.assertEqual(menu(), ["default (Sonnet 5.5)", "Sonnet 5.5 (claude-sonnet-5-5)"])
                await d.press("tab")
                await d.settle()
                self.assertEqual(menu(), ["low"])
                await d.press("down", "enter")
                await d.settle()
                self.assertEqual({k: created[-1][k] for k in ("model", "effort")}, {"model": "", "effort": "low"})
            with self.subTest("a stale entry: no list, and the line names the settings file"):
                write(settings, {**base, "settings": {"models": {"no-such-model": {}}}})
                await draft()
                self.assertEqual(menu(), [])
                line = str(d.app.query_one("#selection").render())
                self.assertIn("couldn't read its models", line)
                self.assertIn("harness.json", line)


if __name__ == "__main__":
    unittest.main()
