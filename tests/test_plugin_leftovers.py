"""The PR #20 re-check's smaller leftovers: settings, CLI, trust cache, catalog, migration."""
import json
import os
from pathlib import Path
import sys
import time
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from multiverse.plugins import settings, trust
from multiverse.plugins.testing import PluginTestCase


class SettingsTypesTest(PluginTestCase):
    def test_enum_and_const_never_take_true_for_1_or_1_for_true(self):
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


class CheckArgumentsTest(PluginTestCase):
    def test_load_before_or_after_the_directory(self):
        import argparse
        from multiverse.tools import plugins as tool
        directory = self.write_plugin("chk")
        parser = argparse.ArgumentParser(prog="plugins")
        tool.configure(parser)
        for argv in (["check", "--load", str(directory)], ["check", str(directory), "--load"]):
            arguments = vars(parser.parse_args(argv))
            arguments.pop("json")
            self.assertTrue(tool.run(None, **arguments)["ok"], argv)
        self.assertTrue(tool.run(None, plugin_action="check", load=str(directory))["ok"])  # what 3.11 parses


class CacheCleanupTest(PluginTestCase):
    def test_only_copies_nothing_trusts_and_nothing_used_lately_go(self):
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


CONVERT = """
def register(mv):
    @mv.settings.migrate
    def convert(data, source):
        return ({"schema": 2, "budget": data.get("budget", 1)}, True) if source == "legacy" else data
"""


def hold_lock(path, seconds):
    """A separate process holding mv2's file lock at `path` for `seconds`; returns it once the lock is held."""
    import subprocess
    process = subprocess.Popen([sys.executable, "-c", (
        "import sys, time; sys.path.insert(0, sys.argv[3]); from multiverse.store import lock\n"
        "with lock(sys.argv[1]):\n    print('held', flush=True); time.sleep(float(sys.argv[2]))"),
        str(path), str(seconds), str(Path(__file__).resolve().parents[1] / "src")], stdout=subprocess.PIPE, text=True)
    assert process.stdout.readline().strip() == "held"
    return process


class MigrationLeftoversTest(PluginTestCase):
    def setUp(self):
        super().setUp()
        (self.root / "plugin-settings" / "compaction.json").unlink(missing_ok=True)
        self.write_plugin("tree", {"module": "plugin", "settings": {"version": 2, "legacy": "tree.json"}},
                          {"plugin.py": CONVERT})
        (self.root / "tree.json").write_text('{"budget": 5}')

    def test_migrate_apply_takes_the_migrate_lock_and_its_trash_entry_is_listed(self):
        from multiverse import plugins
        from multiverse.tools import plugins as tool
        from multiverse.universe import trash
        holder = hold_lock(self.root / "state" / "plugins" / plugins.MIGRATE_LOCK, 1.0)
        self.addCleanup(holder.wait, 10)
        start = time.monotonic()
        result = tool.run(self.root, plugin_action="migrate", apply=True)
        self.assertGreater(time.monotonic() - start, 0.6)  # waited for the first-use conversion holding the lock
        self.assertEqual([item["plugin"] for item in result["applied"]], ["tree"])
        listed = trash.contents(self.root)["entries"]
        self.assertEqual([(item["kind"], item["files"]) for item in listed], [("plugins-migrate", ["tree.json"])])


class LogRotationLockTest(PluginTestCase):
    def test_a_log_write_waits_for_another_process_rotating_it(self):
        from multiverse.plugins import loader
        path = self.work / "state" / "plugin.log"
        path.parent.mkdir(parents=True)
        holder = hold_lock(loader.log_lock_path(path), 1.0)
        self.addCleanup(holder.wait, 10)
        start = time.monotonic()
        loader.append_log(path, "line")
        self.assertGreater(time.monotonic() - start, 0.6)
        self.assertEqual(path.read_text(), "line\n")


class EventsAgainTest(PluginTestCase):
    def test_turning_a_plugin_on_again_starts_after_what_happened_while_it_was_off(self):
        from multiverse import plugins
        from multiverse.plugins import events, host
        for name in ("listener", "other"):
            self.write_plugin(name, {"extends": ["events"], "events": ["agent.*"]})
            plugins.set_enabled(self.root, name, True)
        events.emit(self.root, "agent.status", {"id": "a", "name": "w"})
        self.assertEqual(host._read_cursor(self.root, "listener"), 0)
        plugins.set_enabled(self.root, "listener", False)
        for _ in range(3):  # "other" keeps the journal recording
            events.emit(self.root, "agent.status", {"id": "a", "name": "w"})
        plugins.set_enabled(self.root, "listener", True)
        self.assertEqual(host._read_cursor(self.root, "listener"), 4)
        plugins.set_enabled(self.root, "listener", True)  # on already: its cursor is left alone
        self.assertEqual(host._read_cursor(self.root, "listener"), 4)


class TrustedCopyTest(PluginTestCase):
    def test_catalog_entries_come_from_the_trusted_copy(self):
        from multiverse import plugins
        directory = self.write_plugin("guide", {"extends": ["catalog"]}, {
            "skills/howto/entry.json": '{"description": "How to"}', "skills/howto/SKILL.md": "# How to\n"},
            where=self.root / "plugins")
        trust.trust(directory)
        self.enable("guide")
        [found] = plugins.catalog_entries(self.root)
        self.assertTrue(Path(found["path"]).is_relative_to(trust.user_directory() / trust.CACHE), found)

    def test_an_edited_trusted_copy_that_another_source_replaces_says_so(self):
        from multiverse import plugins
        from multiverse.plugins import loader
        from multiverse.tools import plugins as tool
        self.write_plugin("fork", where=self.home / ".multiverse" / "plugins")  # the personal copy
        directory = self.write_plugin("fork", {"description": "my fork"}, where=self.root / "plugins")
        trust.trust(directory)
        self.enable("fork")
        self.assertEqual(plugins.entry(self.root, "fork").source, "universe")  # the trusted fork overrides it
        (directory / "plugin.json").write_text((directory / "plugin.json").read_text().replace("my fork", "edited"))
        loader.reset()
        self.assertEqual(plugins.entry(self.root, "fork").source, "personal")
        expected = ("fork: this universe's copy changed since it was trusted, so the personal fork runs instead; "
                    "review it, then run mv2 plugins trust fork")
        self.assertIn(expected, plugins.problems(self.root))
        self.assertIn(f"! {expected}", tool.text("list", tool.run(self.root)))
