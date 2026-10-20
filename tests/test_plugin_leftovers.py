"""The PR #20 re-check's smaller leftovers: settings types."""
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from multiverse.plugins import settings
from multiverse.plugins.testing import PluginTestCase


class SettingsTypesTest(PluginTestCase):
    def test_enum_and_const_never_take_true_for_1_or_1_for_true(self):
        """Settings validation of enum and const never accepts True for 1 or 1 for True; fragile because 1 == True in
        Python makes the obvious value-in-enum check wrong, and wrong-typed settings pass silently."""
        with self.assertRaisesRegex(ValueError, "must be one of"):
            settings.validate(True, {"enum": [1, 2]})
        with self.assertRaisesRegex(ValueError, "must be one of"):
            settings.validate(1, {"enum": [True, False]})
        with self.assertRaisesRegex(ValueError, "must be"):
            settings.validate(True, {"const": 1})
        with self.assertRaisesRegex(ValueError, "must be"):
            settings.validate(0, {"const": False})
        settings.validate(1, {"enum": [1, 2]})
        settings.validate(True, {"enum": [True]})
        settings.validate({"a": [1, True]}, {"const": {"a": [1, True]}})
        with self.assertRaisesRegex(ValueError, "must be"):
            settings.validate({"a": [1, 1]}, {"const": {"a": [1, True]}})
