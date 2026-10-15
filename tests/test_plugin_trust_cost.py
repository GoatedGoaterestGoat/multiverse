"""Trust stays cheap and stays put (PR #20 re-check, items 3 and 4)."""
import os
from pathlib import Path
import sys
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from multiverse.plugins import loader, trust
from multiverse.plugins.testing import PluginTestCase

LAZY = """
def register(mv):
    @mv.helper.action("go")
    def go(arguments):
        from . import later  # imported when used, not when loaded
        return later.VALUE
"""


class TrustCostTest(PluginTestCase):
    def setUp(self):
        super().setUp()
        self.directory = self.write_plugin("local", {"module": "plugin", "extends": ["helper"]},
                                           {"plugin.py": LAZY, "later.py": "VALUE = 42\n",
                                            "node_modules/dep/index.js": "module.exports = 1;\n"},
                                           where=self.root / "plugins")
        for index in range(200):
            path = self.directory / "node_modules" / "dep" / f"file{index}.js"
            path.write_text(f"// {index}\n")
        trust.trust(self.directory)
        self.enable("local")

    def test_an_unchanged_plugin_is_not_hashed_again_by_the_next_process(self):
        self.assertEqual(loader.entry(self.root, "local").state, "on")
        loader.reset()  # as a new mv2 process starts
        with mock.patch.object(trust, "fingerprint", side_effect=AssertionError("hashed again")):
            self.assertEqual(loader.entry(self.root, "local").state, "on")
        (self.directory / "node_modules" / "dep" / "file3.js").write_text("// changed\n")
        loader.reset()
        self.assertEqual(loader.entry(self.root, "local").state, "changed")  # a change is still seen

    def test_a_copy_of_unchanged_metadata_is_never_trusted_without_its_bytes(self):
        loader.reset()
        path = self.directory / "later.py"
        status = path.stat()
        path.write_text("VALUE = 43\n")  # same size; even with the old mtime the change time differs
        os.utime(path, ns=(status.st_atime_ns, status.st_mtime_ns))
        self.assertEqual(trust.state(self.directory), "changed")

    def test_importing_from_a_universe_plugin_directory_writes_no_bytecode_there(self):
        previous = sys.pycache_prefix
        self.addCleanup(setattr, sys, "pycache_prefix", previous)
        from multiverse.tools import plugins as tool
        result = tool.check(self.directory, load=True)
        self.assertTrue(result["ok"], result)
        self.assertEqual(trust.state(self.directory), "trusted")  # check --load runs from the trusted copy
        # A developer running the plugin's own tests points MV2_PLUGIN_PATH at it: it loads from the directory.
        with mock.patch.dict(os.environ, {"MV2_PLUGIN_PATH": str(self.root / "plugins")}):
            loader.reset()
            found = loader.entry(None, "local")
            self.assertEqual(Path(found.directory).resolve(), self.directory.resolve())
            loaded = loader.load(found, off=True)
            self.assertEqual(loaded.registrations.helpers["go"]([]), 42)
        self.assertEqual(list(self.directory.rglob("__pycache__")), [])
        self.assertEqual(trust.state(self.directory), "trusted")
