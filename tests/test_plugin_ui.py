"""multiverse.plugins.ui: the names and call shapes a plugin's field screens may rely on."""
import inspect
from pathlib import Path
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from multiverse.plugins import ui
from isolation import isolate


class ContractTests(unittest.TestCase):
    def setUp(self):
        isolate(self)

    def test_public_ui_api_names_and_shapes(self):
        """multiverse.plugins.ui exports exactly the names and call shapes GOAT's screens import; a break goes
        unnoticed in mv2's suite because only plugins in other repositories call them."""
        with self.subTest("exactly these names are exported"):
            self.assertEqual(sorted(ui.__all__),
                             ["BOOL_OPTIONS", "Field", "FieldScreen", "MISSING", "convert", "field"])
            for name in ui.__all__:
                self.assertTrue(hasattr(ui, name))
        with self.subTest("call shapes"):
            sig = lambda function: str(inspect.signature(function))
            self.assertEqual(sig(ui.FieldScreen.__init__), "(self, root, backend)")
            self.assertEqual(sig(ui.field), "(key, path, label, kind, data, placeholder='', options=(), **extra)")
            self.assertEqual(sig(ui.convert), "(item, value)")
            self.assertEqual([name for name in ui.Field.__dataclass_fields__],
                             ["key", "path", "label", "kind", "options", "placeholder", "initial", "group", "role",
                              "help"])
            for helper in ("field_rows", "values", "error", "refresh_rows", "applies", "changed", "action_close"):
                self.assertTrue(callable(getattr(ui.FieldScreen, helper)), helper)
            self.assertEqual(sig(ui.FieldScreen.field_rows), "(self, items)")
            self.assertEqual(sig(ui.FieldScreen.applies), "(self, item, values)")
            self.assertEqual(ui.BOOL_OPTIONS, ((True, "true"), (False, "false")))


if __name__ == "__main__":
    unittest.main()
