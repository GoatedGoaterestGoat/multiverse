"""Views written before the dashboard's columns were renamed still show those columns."""
import json
from pathlib import Path
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from multiverse.ui.views import view
from multiverse.universe import templates
from isolation import isolate


class OldViewTests(unittest.TestCase):
    def setUp(self):
        isolate(self)

    def test_old_view_column_names_still_resolve(self):
        """A view written before the tokens-to-context and activity-to-status renames still loads those columns; a
        break goes unnoticed because views.view() silently drops unknown names, and only universes older than the
        rename have them."""
        with tempfile.TemporaryDirectory() as temp:
            root = templates.create(Path(temp) / ".multiverse", "top", workdir=Path(temp))
            (root / "views").mkdir(exist_ok=True)
            (root / "views" / "default.json").write_text(json.dumps({"columns": ["name", "tokens", "activity", "cost"]}))
            self.assertEqual(view(root)["columns"], ["name", "context", "status", "cost"])


if __name__ == "__main__":
    unittest.main()
