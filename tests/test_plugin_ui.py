"""multiverse.plugins.ui: the names and call shapes a plugin's field screens may rely on."""
import asyncio
import inspect
from pathlib import Path
import subprocess
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "examples"))
from textual.widgets import Input, Static
from multiverse.plugins import ui
from multiverse.ui.app import MultiverseApp
from tui_demo import DemoBackend
from isolation import isolate


class ContractTests(unittest.TestCase):
    def setUp(self):
        isolate(self)

    def test_exactly_these_names_are_exported(self):
        self.assertEqual(sorted(ui.__all__), ["BOOL_OPTIONS", "Field", "FieldScreen", "MISSING", "convert", "field"])
        for name in ui.__all__:
            self.assertTrue(hasattr(ui, name))

    def test_call_shapes(self):
        sig = lambda function: str(inspect.signature(function))
        self.assertEqual(sig(ui.FieldScreen.__init__), "(self, root, backend)")
        self.assertEqual(sig(ui.field), "(key, path, label, kind, data, placeholder='', options=(), **extra)")
        self.assertEqual(sig(ui.convert), "(item, value)")
        self.assertEqual([name for name in ui.Field.__dataclass_fields__],
                         ["key", "path", "label", "kind", "options", "placeholder", "initial", "group", "role", "help"])
        for helper in ("field_rows", "values", "error", "refresh_rows", "applies", "changed", "action_close"):
            self.assertTrue(callable(getattr(ui.FieldScreen, helper)), helper)
        self.assertEqual(sig(ui.FieldScreen.field_rows), "(self, items)")
        self.assertEqual(sig(ui.FieldScreen.applies), "(self, item, values)")
        self.assertEqual(ui.BOOL_OPTIONS, ((True, "true"), (False, "false")))

    def test_field_and_convert_behave_as_documented(self):
        made = ui.field("a.b", ("a", "b"), "B", "int", {"a": {"b": 3}}, 7)
        self.assertEqual((made.initial, made.placeholder, made.path), ("3", "7", ("a", "b")))
        self.assertEqual(ui.convert(made, " 5 "), 5)
        self.assertIs(ui.convert(made, "  "), ui.MISSING)
        with self.assertRaisesRegex(ValueError, "B must be a whole number"):
            ui.convert(made, "lots")
        choice = ui.field("c", ("c",), "C", "choice", {}, "x", ui.BOOL_OPTIONS)
        self.assertEqual([value for value, _ in choice.options], [None, True, False])
        self.assertIs(ui.convert(choice, None), ui.MISSING)

    def test_the_command_line_never_imports_it_or_textual(self):
        code = ("import sys, multiverse.cli, multiverse.plugins, multiverse.plugins.api; "
                "print('multiverse.plugins.ui' in sys.modules, 'textual' in sys.modules)")
        result = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True,
                                cwd=Path(__file__).resolve().parents[1], env={"PYTHONPATH": "src", "PATH": "/usr/bin:/bin"})
        self.assertEqual(result.stdout.strip(), "False False", result.stderr)


class Settings(ui.FieldScreen):
    """A plugin's screen written only against multiverse.plugins.ui."""
    saved = None

    def compose(self):
        yield Static("", classes="cerror")
        yield Static("Budget", classes="csection")
        for row in self.field_rows([ui.field("budget", ("budget",), "Budget", "int", {"budget": 4}, 0),
                                    ui.field("mode", ("mode",), "Mode", "choice", {}, "fast",
                                             (("fast", "fast"), ("slow", "slow"))),
                                    ui.field("extra", ("extra",), "Extra", "text", {})]):
            yield row

    def applies(self, item, values):
        return item.key != "extra" or values["mode"] == "slow"

    def changed(self):
        type(self).seen = self.values()

    def action_save(self):
        try:
            budget = ui.convert(self.fields[0], self.values()["budget"])
        except ValueError as error:
            self.error(str(error))
            return
        self.dismiss(f"saved budget {budget}")


class ScreenTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        isolate(self)

    async def test_a_screen_on_the_contract_edits_validates_hides_rows_and_reports_back(self):
        backend = DemoBackend()
        app = MultiverseApp(backend.root, backend, refresh_interval=0)
        results = []
        async with app.run_test(size=(100, 30)) as pilot:
            for _ in range(100):
                await app.workers.wait_for_complete()
                if app.snapshot is not None:
                    break
                await pilot.pause(0.02)
            app.push_screen(Settings(root=Path("/demo"), backend=backend), results.append)
            await pilot.pause(0.2)
            screen = app.screen
            self.assertEqual((screen.root, screen.backend), (Path("/demo"), backend))
            self.assertEqual(screen.widgets["budget"].value, "4")
            self.assertFalse(screen.rows["extra"].display)  # applies() hides it
            screen.widgets["mode"].action_step(1)
            await pilot.pause(0.1)
            self.assertEqual(screen.widgets["mode"].value, "fast")
            screen.widgets["mode"].action_step(1)
            await pilot.pause(0.1)
            self.assertTrue(screen.rows["extra"].display)
            self.assertEqual(Settings.seen["mode"], "slow")
            # The look ships with the class.
            self.assertEqual(screen.query_one(".frow").styles.height.value, 1)
            self.assertEqual(screen.query_one(".flabel").styles.width.value, 22)
            screen.widgets["budget"].value = "lots"
            await pilot.press("ctrl+enter")
            await pilot.pause(0.1)
            self.assertIn("Budget must be a whole number", str(screen.query_one(".cerror", Static).render()))
            self.assertIs(app.screen, screen)
            screen.widgets["budget"].value = "9"
            await pilot.press("ctrl+enter")
            await pilot.pause(0.2)
            self.assertEqual(results, ["saved budget 9"])
            app.push_screen(Settings(root=Path("/demo"), backend=backend), results.append)
            await pilot.pause(0.2)
            await pilot.press("escape")
            await pilot.pause(0.2)
            self.assertEqual(results, ["saved budget 9", None])


if __name__ == "__main__":
    unittest.main()
