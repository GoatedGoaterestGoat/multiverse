"""Trust for a universe's own plugins covers every file that can run, and what runs is the trusted copy (PR #20, item 1)."""
import argparse
import importlib.util
import os
from pathlib import Path
import py_compile
import subprocess
import sys
import time
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from multiverse import plugins
from multiverse.plugins import harness, loader, trust
from multiverse.plugins.testing import PluginTestCase
from multiverse.tools import plugins as tool

SOURCE = Path(__file__).resolve().parents[1] / "src"
HARMLESS = "def register(mv):\n    pass\n"
HARMFUL = ("import os\nopen(os.environ['MARKER'], 'w').write('ran')\n"
           "def register(mv):\n    pass\n")


class TrustTest(PluginTestCase):
    def setUp(self):
        super().setUp()
        self.marker = self.work / "marker"
        os.environ["MARKER"] = str(self.marker)
        self.directory = self.write_plugin("local", {"module": "plugin", "extends": ["launch"],
                                                     "permissions": ["harness.claude"],
                                                     "harnesses": {"claude": [{"dir": "claude"}]}},
                                           {"plugin.py": HARMLESS, "claude/hooks/index.ts": "export default {}\n"},
                                           where=self.root / "plugins")
        self.enable("local")

    def plant_bytecode(self):
        """A .pyc beside plugin.py that Python would trust over the source (unchecked-hash)."""
        source = self.work / "harmful.py"
        source.write_text(HARMFUL)
        cached = Path(importlib.util.cache_from_source(str(self.directory / "plugin.py")))
        cached.parent.mkdir(parents=True, exist_ok=True)
        py_compile.compile(str(source), cfile=str(cached), dfile=str(self.directory / "plugin.py"),
                           invalidation_mode=py_compile.PycInvalidationMode.UNCHECKED_HASH)
        return cached

    def test_planted_bytecode_never_runs(self):
        """An unchecked-hash .pyc planted beside a trusted plugin.py never runs; fragile because Python prefers such a
        .pyc to the source, so any import from the plugin directory instead of the trusted copy reopens the hole,
        silently."""
        self.plant_bytecode()
        trust.trust(self.directory)
        loaded = loader.load(self.entry("local"))
        self.assertFalse(self.marker.exists(), "the trusted plugin.py, not the planted bytecode, must run")
        self.assertIn("plugin-cache", loaded.module.__file__)

    def test_trust_fingerprint_lifecycle(self):
        """An untrusted plugin refuses to load and trust turns it on; editing a file, adding node_modules JS or a .so,
        or an edit keeping size and mtime makes it changed so it never runs, while bytecode and test caches, a new
        process and the plugin's own test run leave it on; fragile because the stat cache keys on metadata and the
        runnable file kinds grow, and an edited plugin running as trusted is invisible by design."""
        found = self.entry("local")
        self.assertEqual(found.state, "untrusted")
        self.assertIn("mv2 plugins trust local", found.problem)
        with self.assertRaises(plugins.PluginError):
            loader.load(found)
        value = trust.trust(self.directory)
        with self.subTest("an edited plugin.py has no trusted copy and never runs"):
            (self.directory / "plugin.py").write_text(HARMFUL)  # changed after trust: not copied, not run
            with self.assertRaises(ValueError):
                trust.cached(self.directory, value)
            self.assertEqual(self.entry("local").state, "changed")
            self.assertFalse(self.marker.exists())
            (self.directory / "plugin.py").write_text(HARMLESS)
            loader.reset()
        self.assertEqual(self.entry("local").state, "on")
        self.assertTrue((self.home / ".multiverse" / trust.FILE).is_file())
        hook = self.directory / "claude" / "hooks" / "index.ts"
        with self.subTest("a new process does not hash an unchanged plugin again, and still sees a change"):
            loader.reset()  # as a new mv2 process starts
            with mock.patch.object(trust, "fingerprint", side_effect=AssertionError("hashed again")):
                self.assertEqual(self.entry("local").state, "on")
            hook.write_text("export default {changed: true}\n")
            loader.reset()
            self.assertEqual(self.entry("local").state, "changed")
            hook.write_text("export default {}\n")
            loader.reset()
            self.assertEqual(self.entry("local").state, "on")
        with self.subTest("bytecode and test caches are not part of the plugin"):
            for added in ("__pycache__/extra.cpython-312.pyc", "tests/__pycache__/t.cpython-313.pyc", "stray.pyc",
                          ".pytest_cache/v/cache/lastfailed", ".coverage"):
                path = self.directory / added
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text("x")
            loader.reset()
            self.assertEqual(self.entry("local").state, "on")
            copy = Path(loader.load(self.entry("local")).module.__file__).parent
            for added in ("__pycache__/extra.cpython-312.pyc", "stray.pyc", ".pytest_cache", ".coverage"):
                self.assertFalse((copy / added).exists(), added)  # never copied, so never run
        for added in ("node_modules/dep/index.js", "native.so"):
            with self.subTest(added):
                path = self.directory / added
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text("x")
                loader.reset()
                self.assertEqual(self.entry("local").state, "changed")
                path.unlink()
                loader.reset()
                self.assertEqual(self.entry("local").state, "on")
        with self.subTest("an edit keeping size and mtime"):
            loader.reset()
            status = hook.stat()
            hook.write_text("export default []\n")  # same size; even with the old mtime the change time differs
            os.utime(hook, ns=(status.st_atime_ns, status.st_mtime_ns))
            self.assertEqual(trust.state(self.directory), "changed")
            hook.write_text("export default {}\n")
        with self.subTest("a symbolic link is refused"):
            (self.directory / "link").symlink_to(self.home)
            with self.assertRaisesRegex(ValueError, "symbolic links"):
                trust.fingerprint(self.directory)
        with self.subTest("a test run in the plugin folder keeps its trust"):
            tool.scaffold("mine", self.root / "plugins")
            directory = self.root / "plugins" / "mine"
            trust.trust(directory)
            self.assertEqual(trust.state(directory), "trusted")
            # What a developer runs in the plugin's folder (its README says so), with mv2 importable.
            run = subprocess.run([sys.executable, "-m", "unittest", "discover", "-s", "tests"], cwd=directory,
                                 env={**os.environ, "PYTHONPATH": str(SOURCE)}, capture_output=True, text=True,
                                 timeout=120)
            self.assertEqual(run.returncode, 0, run.stderr[-2000:])
            written = sorted(str(path.relative_to(directory)) for path in directory.rglob("*")
                             if path.is_file() and path.stat().st_mtime > (directory / "plugin.json").stat().st_mtime)
            self.assertEqual(trust.state(directory), "trusted", f"the test run wrote {written}")

    def test_claude_runs_the_trusted_copy_and_its_writes_never_change_trust(self):
        """Claude and the catalog load a universe plugin from the trusted cache copy, and Claude's own writes there
        never change trust; fragile because Claude Code writes files into directories it loads, and a break flips
        plugins to changed after every launch or runs the untrusted directory."""
        trust.trust(self.directory)
        plan = harness.launch_plan(harness.LaunchInfo(self.root, "a", "w", "spawn", "claude", "claude",
                                                      (2, 1, 296), True))
        [claude_dir] = plan.claude_plugin_dirs
        self.assertIn("plugin-cache", claude_dir)
        (Path(claude_dir) / "tsconfig.json").write_text("{}")  # what Claude writes into a directory it loads
        loader.reset()
        self.assertEqual(self.entry("local").state, "on")
        self.assertFalse((self.directory / "claude" / "tsconfig.json").exists())
        with self.subTest("catalog entries"):
            directory = self.write_plugin("guide", {"extends": ["catalog"]}, {
                "skills/howto/entry.json": '{"description": "How to"}', "skills/howto/SKILL.md": "# How to\n"},
                where=self.root / "plugins")
            trust.trust(directory)
            self.enable("guide")
            [found] = plugins.catalog_entries(self.root)
            self.assertTrue(Path(found["path"]).is_relative_to(trust.user_directory() / trust.CACHE), found)


class CheckTest(PluginTestCase):
    """mv2 plugins check never imports an untrusted universe plugin (PR #20, item 2)."""

    def test_check_never_imports_an_untrusted_plugin(self):
        """mv2 plugins check never imports an untrusted universe plugin unless --load is given, before or after the
        directory (PR #20 re-check: Python 3.11's argparse gives the directory to --load), and checks a trusted one
        from its copy; a break goes unnoticed because check still says ok after the untrusted code ran."""
        marker = self.work / "marker"
        os.environ["MARKER"] = str(marker)
        directory = self.write_plugin("local", {"module": "plugin"}, {"plugin.py": HARMFUL},
                                      where=self.root / "plugins")
        result = tool.run(self.root, plugin_action="check", name=str(directory))
        self.assertFalse(marker.exists(), "check imported untrusted code")
        self.assertTrue(result["ok"], result)
        self.assertIn("its code was not imported: it is not trusted", " ".join(result["notes"]))
        parser = argparse.ArgumentParser(prog="plugins")
        tool.configure(parser)
        for argv in (["check", "--load", str(directory)], ["check", str(directory), "--load"], None):
            with self.subTest(argv or "what 3.11 parses"):
                marker.unlink(missing_ok=True)
                loader.reset()
                if argv is None:
                    arguments = {"plugin_action": "check", "load": str(directory)}
                else:
                    arguments = vars(parser.parse_args(argv))
                    arguments.pop("json")
                self.assertTrue(tool.run(None, **arguments)["ok"], argv)
                self.assertTrue(marker.exists(), "--load was explicitly asked for")
        with self.subTest("trusted"):
            (directory / "plugin.py").write_text(HARMLESS)
            trust.trust(directory)
            loader.reset()
            result = tool.run(self.root, plugin_action="check", name=str(directory))
            self.assertTrue(result["ok"], result)
            self.assertNotIn("not imported", " ".join(result["notes"]))


class CacheCleanupTest(PluginTestCase):
    def test_only_copies_nothing_trusts_and_nothing_used_lately_go(self):
        """Trust-cache pruning keeps copies still trusted, used lately (perhaps by a running Claude through
        --plugin-dir) or still being copied; a break goes unnoticed until a live agent's plugin directory disappears
        mid-session."""
        directory = self.write_plugin("kept", where=self.root / "plugins")
        value = trust.trust(directory)
        kept = trust.cached(directory, value)
        cache = kept.parent
        old = time.time() - 30 * 86400

        def copy(name, age=None):
            path = cache / name
            path.mkdir(parents=True)
            (path / trust.COMPLETE).write_text("sha256:" + name + "\n")
            if age is not None:
                os.utime(path / trust.COMPLETE, (age, age))
                os.utime(path, (age, age))
            return path
        stale = copy("a" * 64, old)  # trusted by nothing, unused for 30 days
        recent = copy("b" * 64)  # trusted by nothing, but used just now: perhaps by a running agent
        leftover = cache / ".copy-x"
        leftover.mkdir()
        os.utime(leftover, (old, old))
        starting = cache / ".copy-y"  # another process is copying right now
        starting.mkdir()
        trust.prune_cache()
        self.assertEqual(sorted(path.name for path in cache.iterdir()), sorted([kept.name, recent.name, starting.name]))
        self.assertFalse(stale.exists())
        # Using a copy marks it used: one resolved lately survives untrusting its plugin.
        os.utime(kept / trust.COMPLETE, (old, old))
        trust.cached(directory, value)
        trust.forget(directory)
        self.assertTrue(kept.exists())
        os.utime(kept / trust.COMPLETE, (old, old))
        trust.prune_cache()
        self.assertFalse(kept.exists())
