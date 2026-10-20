"""First-use conversion of a plugin's pre-plugin settings file: once, where mv2 starts work in a universe."""
import json
import os
from pathlib import Path
import subprocess
import sys
import time
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from multiverse import plugins
from multiverse.plugins import harness, loader, trust
from multiverse.plugins.testing import PluginTestCase
from multiverse.universe import templates

SRC = Path(__file__).resolve().parents[1] / "src"
NOTES = """
def register(mv):
    def migrate(old, source):
        if source == "legacy":
            if not isinstance(old.get("colour"), str):
                raise ValueError("colour must be text")
            return {"schema": 2, "colour": old["colour"]}, bool(old.get("enabled", True))
        return old
    mv.settings.migrate(migrate)
"""
CONVERT = """
def register(mv):
    @mv.settings.migrate
    def convert(data, source):
        if source == "legacy":
            return {"schema": 2, "budget": data.get("budget", 1)}, bool(data.get("enabled"))
        return data
"""
COMPACTION = {"module": "plugin", "settings": {"version": 2, "legacy": "compaction.json"}}
NOT_INSTALLED = "compaction.json is here but the compaction plugin isn't installed"


def hold_lock(path, seconds):
    """A separate process holding mv2's file lock at `path` for `seconds`; returns it once the lock is held."""
    process = subprocess.Popen([sys.executable, "-c", (
        "import sys, time; sys.path.insert(0, sys.argv[3]); from multiverse.store import lock\n"
        "with lock(sys.argv[1]):\n    print('held', flush=True); time.sleep(float(sys.argv[2]))"),
        str(path), str(seconds), str(SRC)], stdout=subprocess.PIPE, text=True)
    assert process.stdout.readline().strip() == "held"
    return process


class FirstUseTest(PluginTestCase):
    def setUp(self):
        super().setUp()
        self.write_plugin("notes", {"module": "plugin", "settings": {"version": 2, "legacy": "notes.json"}},
                          {"plugin.py": NOTES})

    def universe(self, name):
        """A universe tree of its own: a first use converts the whole tree it is called for."""
        return templates.create(self.work / name / ".multiverse", name, workdir=self.work / name)

    def legacy(self, data, root=None):
        path = Path(root or self.root) / "notes.json"
        path.write_text(json.dumps(data))
        return path

    def converted(self, root=None):
        path = Path(root or self.root) / "plugin-settings" / "notes.json"
        return json.loads(path.read_text()) if path.exists() else None

    def backups(self, root=None):
        return sorted((Path(root or self.root) / "trash").glob("*-plugins-migrate/notes.json"))

    def test_two_processes_at_once_convert_once(self):
        """Two processes converting at once, or migrate --apply beside a command, convert once under the migrate
        lock; fragile because every mv2 command can trigger conversion, and a race gives a double conversion or
        backup nobody notices."""
        self.legacy({"colour": "teal"})
        environment = {**os.environ, "PYTHONPATH": str(SRC)}
        runs = [subprocess.Popen([sys.executable, "-m", "multiverse.cli", "-u", str(self.root), "list"], env=environment,
                                 stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True) for _ in range(2)]
        outputs = [run.communicate(timeout=60) for run in runs]
        self.assertEqual([run.returncode for run in runs], [0, 0])
        self.assertEqual(sum("converted notes.json" in stderr for _, stderr in outputs), 1, outputs)
        self.assertEqual(len(self.backups()), 1)
        with self.subTest("migrate --apply waits for a conversion holding the lock"):
            from multiverse.tools import plugins as tool
            from multiverse.universe import trash
            root = self.universe("beside")
            self.legacy({"colour": "teal"}, root)
            holder = hold_lock(root / "state" / "plugins" / plugins.MIGRATE_LOCK, 1.0)
            self.addCleanup(holder.wait, 10)
            start = time.monotonic()
            result = tool.run(root, plugin_action="migrate", apply=True)
            self.assertGreater(time.monotonic() - start, 0.6)  # waited for the first-use conversion holding the lock
            self.assertEqual([item["plugin"] for item in result["applied"]], ["notes"])
            listed = trash.contents(root)["entries"]
            self.assertEqual([(item["kind"], item["files"]) for item in listed], [("plugins-migrate", ["notes.json"])])

    def test_a_conversion_completes_or_leaves_files_exactly_as_they_were(self):
        """With failures injected at each step (broken plugins.json, set_enabled or os.link failing, a target
        appearing, the file edited or replaced meanwhile, the old file not removable) a conversion completes or leaves
        the user's files as they were, and says which; fragile because these paths never run in normal use, and a
        break destroys someone's settings."""
        with self.subTest("a broken switch file stops the conversion before anything changes"):
            root = self.universe("broken-switch")
            self.legacy({"colour": "teal"}, root)
            (root / "plugins.json").write_text("{bad-json")
            [line] = plugins.migrate_on_first_use(root)
            self.assertIn("plugins.json cannot be used", line)
            self.assertIn("it is unchanged", line)
            self.assertTrue((root / "notes.json").exists())
            self.assertIsNone(self.converted(root))
            self.assertEqual(self.backups(root), [])
            self.assertEqual((root / "plugins.json").read_text(), "{bad-json")
        with self.subTest("a failure after writing puts everything back"):
            root = self.universe("failed-switch")
            self.legacy({"colour": "teal"}, root)
            with mock.patch.object(plugins.config, "set_enabled", side_effect=OSError("disk full")):
                [line] = plugins.migrate_on_first_use(root)
            self.assertIn("could not be set: disk full); it is unchanged", line)
            self.assertTrue((root / "notes.json").exists())
            self.assertIsNone(self.converted(root))
            self.assertEqual(self.backups(root), [])
        with self.subTest("a file changed meanwhile is left alone and said so"):
            root = self.universe("edited")
            self.legacy({"colour": "teal"}, root)
            target = root / "plugin-settings" / "notes.json"

            def someone_edits(*arguments):
                target.write_text('{"schema": 2, "colour": "edited"}')
                raise OSError("disk full")
            with mock.patch.object(plugins.config, "set_enabled", side_effect=someone_edits):
                [line] = plugins.migrate_on_first_use(root)
            self.assertIn("check plugin-settings/notes.json and plugins.json by hand", line)
            self.assertNotIn("unchanged", line)
            self.assertEqual(self.converted(root), {"schema": 2, "colour": "edited"})
            self.assertTrue((root / "notes.json").exists())
        with self.subTest("a target that appears while writing is never replaced"):
            root = self.universe("appeared")
            self.legacy({"colour": "teal"}, root)
            target = root / "plugin-settings" / "notes.json"
            real = os.link

            def someone_writes_first(source, destination, *arguments, **options):
                Path(destination).write_text('{"schema": 2, "colour": "theirs"}')
                return real(source, destination, *arguments, **options)  # fails: a file is there now
            with mock.patch.object(plugins.os, "link", someone_writes_first):
                [line] = plugins.migrate_on_first_use(root)
            self.assertIn("appeared meanwhile); it is unchanged", line)
            self.assertEqual(self.converted(root), {"schema": 2, "colour": "theirs"})
            self.assertTrue((root / "notes.json").exists())
            self.assertEqual(list(target.parent.glob(".migrate-*")), [])
        with self.subTest("a write that fails publishes nothing and touches no other file"):
            root = self.universe("unwritable")
            self.legacy({"colour": "teal"}, root)
            with mock.patch.object(plugins.os, "link", side_effect=OSError("I/O error")):  # publishing fails
                [line] = plugins.migrate_on_first_use(root)
            self.assertIn("could not be written: I/O error); it is unchanged", line)
            self.assertIsNone(self.converted(root))
            self.assertTrue((root / "notes.json").exists())
            self.assertEqual(list((root / "plugin-settings").glob(".migrate-*")), [])
        with self.subTest("a file replaced before the undo is left alone"):
            root = self.universe("replaced")
            self.legacy({"colour": "teal"}, root)
            target = root / "plugin-settings" / "notes.json"

            def someone_replaces(*arguments):
                other = target.with_name("other.json")
                other.write_text('{"schema": 2, "colour": "replaced"}')
                os.replace(other, target)
                raise OSError("disk full")
            with mock.patch.object(plugins.config, "set_enabled", side_effect=someone_replaces):
                [line] = plugins.migrate_on_first_use(root)
            self.assertIn("check plugin-settings/notes.json and plugins.json by hand", line)
            self.assertEqual(self.converted(root), {"schema": 2, "colour": "replaced"})
        with self.subTest("an old file that cannot be removed is said"):
            root = self.universe("read-only")
            self.legacy({"colour": "teal"}, root)
            real = Path.unlink

            def refuse(path, *arguments, **options):
                if path.name == "notes.json" and path.parent == root:
                    raise PermissionError("read-only")
                return real(path, *arguments, **options)
            with mock.patch.object(Path, "unlink", refuse):
                [line] = plugins.migrate_on_first_use(root)
            self.assertIn("converted notes.json to plugin-settings/notes.json", line)
            self.assertIn("but notes.json could not be moved to trash (read-only): remove it yourself", line)
            self.assertEqual(self.converted(root), {"schema": 2, "colour": "teal"})
            self.assertEqual(plugins.migrate_on_first_use(root), [])  # converted: not pending again

    def test_a_failed_conversion_is_said_once_and_retried_when_the_file_changes(self):
        """A failed conversion, or one not yet possible because the plugin is not installed, is said once, reported
        by doctor (and for compaction in Claude launch plans) and tried again when the file changes or the plugin
        arrives, even past a record an older mv2 left; a break goes unnoticed because the universe stays unconverted
        forever and nothing more is said (PR #20 re-check item 2)."""
        with self.subTest("the plugin's migration fails"):
            path = self.legacy({"colour": 3})
            [line] = plugins.migrate_on_first_use(self.root)
            self.assertIn("could not convert notes.json (notes's migration failed: ValueError: colour must be text); "
                          "it is unchanged", line)
            self.assertTrue(path.exists())
            self.assertIsNone(self.converted())
            loader.reset()  # as the next command's process starts
            self.assertEqual(plugins.migrate_on_first_use(self.root), [])  # said once
            self.assertTrue(any("notes.json could not be converted" in problem
                                for problem in plugins.problems(self.root)))
            self.legacy({"colour": "blue"})
            loader.reset()
            self.assertIn("converted notes.json", " ".join(plugins.migrate_on_first_use(self.root)))
            self.assertEqual(self.converted(), {"schema": 2, "colour": "blue"})
            loader.reset()
            self.assertFalse(any("could not be converted" in problem for problem in plugins.problems(self.root)))
        with self.subTest("the plugin is installed after the notice"):
            # PluginTestCase hides the checkout's plugins: compaction is not installed here.
            root = self.universe("before-install")
            (root / "plugin-settings" / "compaction.json").unlink(missing_ok=True)  # made before plugins
            (root / "compaction.json").write_text(json.dumps({"enabled": True, "budget": 7}))
            self.assertTrue(any(NOT_INSTALLED in problem for problem in plugins.problems(root)))
            [line] = plugins.migrate_on_first_use(root)
            self.assertIn(NOT_INSTALLED, line)
            self.assertEqual(plugins.migrate_on_first_use(root), [])  # said once for this file
            plan = harness.launch_plan(harness.LaunchInfo(root, "a", "w", "spawn", "claude", "claude",
                                                          (2, 1, 299), True))
            self.assertTrue(any(NOT_INSTALLED in problem for problem in plan.problems))
            codex = harness.launch_plan(harness.LaunchInfo(root, "a", "w", "spawn", "codex", "codex", None, True))
            self.assertFalse(any(NOT_INSTALLED in problem for problem in codex.problems))  # never in Codex launches
            # Installed now (a checkout's plugins/compaction, here on the plugin path).
            self.write_plugin("compaction", COMPACTION, {"plugin.py": CONVERT})
            loader.reset()
            self.assertTrue(any("compaction.json is still here and not converted yet" in problem
                                for problem in plugins.problems(root)))
            lines = plugins.migrate_on_first_use(root)
            self.assertEqual(len(lines), 1, lines)
            self.assertIn("converted compaction.json to plugin-settings/compaction.json, turned it on", lines[0])
            self.assertEqual(json.loads((root / "plugin-settings" / "compaction.json").read_text()),
                             {"schema": 2, "budget": 7})
            self.assertFalse((root / "compaction.json").exists())
            self.assertEqual([problem for problem in plugins.problems(root) if "compaction" in problem], [])
        with self.subTest("a not-installed record an older mv2 left"):
            root = self.universe("older-record")
            (root / "plugin-settings" / "compaction.json").unlink(missing_ok=True)
            legacy = root / "compaction.json"
            legacy.write_text(json.dumps({"enabled": False}))
            status = legacy.stat()
            state = root / "state" / "plugins"
            state.mkdir(parents=True, exist_ok=True)
            (state / plugins.MIGRATE_FAILED).write_text(json.dumps({"compaction": {
                "file": "compaction.json", "fingerprint": [status.st_mtime_ns, status.st_size],
                "reason": "not installed", "not_installed": True}}))
            self.write_plugin("compaction", COMPACTION, {"plugin.py": CONVERT})
            loader.reset()
            self.assertIn("converted compaction.json", " ".join(plugins.migrate_on_first_use(root)))

    def test_an_untrusted_universe_plugin_is_never_converted_or_imported(self):
        """First-use conversion, which runs on every command, never imports an untrusted universe plugin, nor any
        plugin under MV2_PLUGINS=off, and doctor says to trust it before its old file can be converted; a break goes
        unnoticed because conversion just works while untrusted code, or a plugin safe mode should keep out, runs on
        any mv2 call."""
        self.write_plugin("local", {"module": "plugin", "settings": {"version": 2, "legacy": "local.json"}},
                          {"plugin.py": "raise SystemExit('imported')\n"}, where=self.root / "plugins")
        (self.root / "local.json").write_text("{}")
        self.assertEqual(plugins.migrate_on_first_use(self.root), [])
        self.assertTrue((self.root / "local.json").exists())
        self.assertFalse((self.root / "state" / "plugins" / plugins.MIGRATE_LOCK).exists())
        with self.subTest("MV2_PLUGINS=off: no plugin's migrate() runs, nothing is converted"):
            self.legacy({"colour": "teal"})
            loader.reset()
            with mock.patch.dict(os.environ, {"MV2_PLUGINS": "off"}):
                self.assertEqual(plugins.migrate_on_first_use(self.root), [])
            self.assertTrue((self.root / "notes.json").exists())
            self.assertIsNone(self.converted())
            self.assertEqual(loader._loaded, {})  # notes' code (and its migrate()) never imported
        with self.subTest("doctor says to trust it first"):
            directory = self.write_plugin("forked", {"settings": {"version": 2, "legacy": "forked.json"}},
                                          where=self.root / "plugins")
            (self.root / "forked.json").write_text("{}")
            found = [problem for problem in plugins.problems(self.root) if "forked.json" in problem]
            self.assertTrue(found, plugins.problems(self.root))
            self.assertIn("mv2 plugins trust forked", found[0])
            self.assertNotIn("converted the next time mv2 runs here,", found[0])
            trust.trust(directory)
            loader.reset()
            found = [problem for problem in plugins.problems(self.root) if "forked.json" in problem]
            self.assertIn("converted the next time mv2 runs here", found[0])
