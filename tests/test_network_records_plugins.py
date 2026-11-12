"""Brief ENOENT during replacement must not reset plugin settings or delivery progress."""
from contextlib import contextmanager
import errno
import json
import os
from pathlib import Path
import sys
from types import SimpleNamespace
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from multiverse import plugins, store
from multiverse.plugins import api, config, events, host, loader, manifest, settings, trust
from multiverse.plugins.testing import PluginTestCase


class NetworkPluginRecordsTest(PluginTestCase):
    def setUp(self):
        super().setUp()
        pause = mock.patch.object(store.time, "sleep")
        pause.start()
        self.addCleanup(pause.stop)
        for table in (host._finished, host._saved, host._cursor_journals):
            before = dict(table)
            self.addCleanup(lambda table=table, before=before: (table.clear(), table.update(before)))
            table.clear()

    @contextmanager
    def missing(self, path, *, method="read_text", count=1, code=errno.ENOENT, after=0):
        """Inject errors at one pathname, keeping every write and unrelated read real."""
        original = getattr(Path, method)
        calls = []
        def read(current, *args, **kwargs):
            if current == path:
                calls.append(current)
                if len(calls) > after and (count is None or len(calls) <= after + count):
                    raise OSError(code, os.strerror(code), str(current))
            return original(current, *args, **kwargs)
        with mock.patch.object(Path, method, read):
            yield calls

    def settings_manifest(self):
        directory = self.write_plugin("listener", {"settings": {"version": 1, "legacy": "old.json"}})
        return manifest.load(directory)

    def test_optional_records_recover_but_keep_their_permanent_absence_policy(self):
        """A replacement gap must not become empty configuration, trust, journal identity or failure history."""
        directory = self.agent("worker")
        journal = events.directory(self.root)
        journal.mkdir(parents=True, exist_ok=True)
        state = self.root / "state" / "plugins"
        cases = [
            (config.path(self.root), {"schema": 1, "enabled": {"listener": False}},
             lambda: config._raw(self.root), {"schema": 1, "enabled": {"listener": False}}, None),
            (host.host_directory(self.root) / "status.json", {"state": "running", "socket": "saved"},
             lambda: host._recorded(self.root), ({"state": "running", "socket": "saved"}, None), ({}, None)),
            (trust.user_directory() / trust.FINGERPRINTS, {"saved": {"signature": "s"}},
             trust._fingerprints, {"saved": {"signature": "s"}}, {}),
            (trust.user_directory() / trust.FILE, {"schema": 1, "plugins": {"saved": {"fingerprint": "f"}}},
             trust._records, {"saved": {"fingerprint": "f"}}, {}),
            (state / plugins.MIGRATE_FAILED, {"listener": {"reason": "bad"}},
             lambda: plugins._failed_conversions(self.root), {"listener": {"reason": "bad"}}, {}),
            (state / plugins.NOTIFIED, {"listener": {"fingerprint": "f"}},
             lambda: plugins._notified(self.root), {"listener": {"fingerprint": "f"}}, {}),
            (directory / "properties.json", {"id": "worker", "name": "worker"},
             lambda: events.agent_of(directory), {"id": "worker", "name": "worker"}, None),
            (journal / events.ID, "a" * 32 + "\n", lambda: events.journal_id(self.root), "a" * 32, None),
            (journal / "seq", "10\n", lambda: events._counter(journal), 10, None),
        ]
        for path, value, read, recovered, absent in cases:
            with self.subTest(path=path.name):
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text(value) if isinstance(value, str) else store.write(path, value)
                with self.missing(path) as calls:
                    self.assertEqual(read(), recovered)
                self.assertEqual(len(calls), 2)
                with self.missing(path, count=None) as calls:
                    self.assertEqual(read(), absent)
                self.assertEqual(len(calls), store.MISSING_ATTEMPTS)
        (journal / events.ID).unlink()
        backup = journal / events.ID_COPY
        backup.write_text("a" * 32 + "\n")
        with self.missing(backup) as calls:
            self.assertEqual(events.journal_id(self.root), "a" * 32)
        self.assertEqual(len(calls), 2)
        with self.missing(backup, count=None):
            self.assertIsNone(events.journal_id(self.root))

    def test_configuration_edits_preserve_other_switches_and_access(self):
        """A transient missing configuration must not erase a disabled plugin or its access ceiling on edit."""
        path = config.path(self.root)
        original = {"schema": 1, "enabled": {"saved": False}, "access_ceiling": "observe",
                    "access": {"saved": "observe"}}
        for edit in (lambda: config.set_enabled(self.root, "listener", True),
                     lambda: config.set_grant(self.root, "listener", "observe")):
            store.write(path, original)
            with self.missing(path):
                edit()
            saved = store.read(path)
            self.assertFalse(saved["enabled"]["saved"])
            self.assertEqual(saved["access_ceiling"], "observe")
            self.assertEqual(saved["access"]["saved"], "observe")
            path.unlink()
            edit()
            self.assertTrue(config.load(self.root).exists)
        store.write(path, original)
        with self.missing(path, code=errno.EACCES) as calls:
            loaded = config.load(self.root)
        self.assertEqual(len(calls), 1)
        self.assertTrue(loaded.problem)
        self.assertFalse(loaded.is_on("listener", "on"))

    def test_settings_presence_and_content_do_not_fall_back_to_defaults(self):
        """Both the settings stat and open can miss during replacement, otherwise an edit silently loses values."""
        spec = self.settings_manifest()
        path = settings.path(self.root, spec.name)
        original = {"schema": 1, "kept": 41}
        for method in ("stat", "read_text"):
            with self.subTest(method=method):
                store.write(path, original)
                with self.missing(path, method=method):
                    self.assertEqual(settings.read(self.root, spec).values, original)
                with self.missing(path, method=method):
                    self.assertEqual(settings.edit(self.root, spec, {"new": 7}).values,
                                     {**original, "new": 7})
        store.write(path, original)
        with self.missing(path, count=None):
            self.assertTrue(settings.read(self.root, spec).problem)
        before = path.read_bytes()
        with self.missing(path, count=None) as calls:
            with self.assertRaises(ValueError):
                settings.edit(self.root, spec, {"new": 7})
        self.assertEqual(len(calls), store.MISSING_ATTEMPTS)
        self.assertEqual(path.read_bytes(), before)
        with self.missing(path, method="stat", count=None):
            self.assertEqual(settings.read(self.root, spec).values, {"schema": 1})
        with self.missing(path, method="stat", code=errno.ENOTDIR, count=None) as calls:
            self.assertEqual(settings.read(self.root, spec).values, {"schema": 1})
        self.assertEqual(len(calls), 1)
        with self.missing(path, method="stat"):
            self.assertFalse(settings.install_defaults(self.root, spec))
        self.assertEqual(store.read(path), original)
        path.unlink()
        self.assertEqual(settings.read(self.root, spec).values, {"schema": 1})
        self.assertTrue(settings.install_defaults(self.root, spec))
        path.unlink()
        self.assertEqual(settings.edit(self.root, spec, {"new": 7}).values, {"schema": 1, "new": 7})
        path.unlink()
        (self.root / "old.json").write_text("{}")
        self.assertFalse(settings.install_defaults(self.root, spec))
        with self.assertRaises(ValueError):
            settings.edit(self.root, spec, {"new": 7})

    def test_settings_migration_probes_keep_existing_settings_authoritative(self):
        """A settings replacement must not make migration planning select an obsolete legacy file."""
        spec = self.settings_manifest()
        found = SimpleNamespace(name=spec.name, manifest=spec, state="off", on=False, set_aside=())
        path = settings.path(self.root, spec.name)
        (self.root / "old.json").write_text("{}")
        read_cases = [
            (lambda: plugins._pending_legacy(self.root), [], [(found, self.root / "old.json", path)]),
            (lambda: plugins.waiting_settings(self.root), {}, {"plugin-settings/listener.json": "old.json"}),
            (lambda: plugins._waiting_old_file(self.root, found), None, "old.json"),
            (lambda: plugins.reappeared(self.root), [(spec.name, "old.json")], []),
            (lambda: any("not converted yet" in line for line in plugins.problems(self.root)), False, True),
        ]
        with mock.patch.object(plugins, "entries", return_value=[found]), mock.patch.object(plugins, "KNOWN", {}):
            for read, recovered, absent in read_cases:
                store.write(path, {"schema": 1})
                with self.missing(path, method="stat"):
                    self.assertEqual(read(), recovered)
                with self.missing(path, method="stat", count=None):
                    self.assertEqual(read(), absent)
            loaded = SimpleNamespace(registrations=SimpleNamespace(settings_migrate=lambda data: data))
            with mock.patch.object(plugins, "entry", return_value=found), mock.patch.object(loader, "load", return_value=loaded):
                store.write(path, {"schema": 0})
                for method in ("stat", "read_text"):
                    with self.missing(path, method=method):
                        self.assertEqual(plugins.migrations(self.root)[0]["kind"], "version")
                with self.missing(path, method="stat", count=None):
                    self.assertEqual(plugins.migrations(self.root)[0]["kind"], "legacy")
                with self.missing(path, count=None):
                    self.assertEqual(plugins.migrations(self.root), [])
        with mock.patch.object(plugins, "entries", return_value=[]), \
                mock.patch.object(plugins, "KNOWN", {spec.name: {"legacy": "old.json"}}):
            with self.missing(path, method="stat"):
                self.assertEqual(plugins.not_installed(self.root), [])
            with self.missing(path, method="stat", count=None):
                self.assertEqual(plugins.not_installed(self.root), [(spec.name, "old.json")])

    def test_cursor_and_generation_survive_brief_absence(self):
        """A cursor miss can jump to the journal end and permanently skip events, unlike a true first subscription."""
        directory = events.directory(self.root)
        directory.mkdir(parents=True, exist_ok=True)
        (directory / "000000000009.jsonl").write_text('{"seq": 9}\n')
        (directory / "seq").write_text("10\n")
        path = host._cursor_path(self.root, "listener")
        host._write_cursor(self.root, "listener", 3, "a" * 32)
        subscriber = SimpleNamespace(manifest=SimpleNamespace(events=["*"]))
        with mock.patch.object(plugins, "entry", return_value=subscriber):
            with self.missing(path, method="stat"):
                plugins.anchor_events(self.root, "listener")
            self.assertEqual(path.read_text(), "3\n")
            path.unlink()
            plugins.anchor_events(self.root, "listener")
            self.assertEqual(path.read_text(), "9\n")
        host._write_cursor(self.root, "listener", 3, "a" * 32)
        with self.missing(path):
            self.assertEqual(host._read_cursor(self.root, "listener", "a" * 32), 3)
        with self.missing(path, count=None):
            self.assertEqual(host._read_cursor(self.root, "listener", "a" * 32), 9)
        self.assertEqual(path.read_text(), "9\n")
        host._write_cursor(self.root, "listener", 3, "a" * 32)
        mark = host._cursor_journal_path(self.root, "listener")
        with self.missing(mark):
            self.assertEqual(host._read_cursor(self.root, "listener", "b" * 32), 0)
        with self.missing(mark, count=None):
            self.assertEqual(host._read_cursor(self.root, "listener", "b" * 32), 3)
        with self.missing(path):
            self.assertEqual(events.summary(self.root)["cursors"], {"listener": 3})
        with self.missing(path, count=None):
            self.assertEqual(events.summary(self.root)["cursors"], {})

    def test_handler_progress_and_give_up_history_survive_replacement(self):
        """Lost completion records repeat handler effects, and a missed give-up history erases earlier failures."""
        handler = lambda event: None
        loaded = SimpleNamespace(name="listener", registrations=SimpleNamespace(handlers=[("*", handler)]))
        path = self.root / "state" / "plugins" / "listener" / host.HANDLERS
        finished = {host._handler_id(0, "*", handler): 7}
        store.write(path, {"journal": "journal", "finished": finished})
        with self.missing(path):
            host._restore_progress(loaded, self.root, "listener", "journal")
        self.assertEqual(host._finished[("listener", str(self.root), 0)], 7)
        host._finished.clear()
        with self.missing(path, count=None):
            host._restore_progress(loaded, self.root, "listener", "journal")
        self.assertEqual(host._finished, {})
        path = path.with_name(host.DELIVERY)
        entry = {"seq": 1, "type": "event", "handler": "h", "why": "bad", "at": store.now()}
        store.write(path, {"gave_up": [entry]})
        with self.missing(path):
            self.assertEqual(len(host.gave_up(self.root, "listener")), 1)
        with self.missing(path, count=None):
            self.assertEqual(host.gave_up(self.root, "listener"), [])
        with self.missing(path):
            host._gave_up(self.root, "listener", 2, "event", "h", "bad")
        self.assertEqual([entry["seq"] for entry in store.read(path)["gave_up"]], [1, 2])
        path.unlink()
        host._gave_up(self.root, "listener", 3, "event", "h", "bad")
        self.assertEqual([entry["seq"] for entry in store.read(path)["gave_up"]], [3])

    def test_status_read_modify_write_and_restart_keep_published_records(self):
        """A missed host status read drops its terminal identity; a restart read must still consume only its request."""
        path = host.host_directory(self.root) / "status.json"
        original = {"state": "running", "pid": os.getpid(), "socket": "saved", "updated_at": "before"}
        store.write(path, original)
        with self.missing(path):
            host._write_status(self.root, "started", {}, {}, {})
        self.assertEqual(store.read(path)["socket"], "saved")
        path.unlink()
        host._write_status(self.root, "started", {}, {}, {})
        self.assertNotIn("socket", store.read(path))
        for present in (True, False):
            store.write(path, original)
            stop = SimpleNamespace(wait=mock.Mock(side_effect=[False, True]))
            with self.missing(path, count=1 if present else None):
                host._beat(self.root, stop)
            self.assertEqual(store.read(path)["updated_at"] == "before", not present)
        path = path.with_name(host.RESTART)
        for present in (True, False):
            store.write(path, {"for": "started", "pid": os.getpid()})
            with self.missing(path, count=1 if present else None):
                self.assertEqual(host._restart_requested(self.root, "started"), present)
            self.assertEqual(path.exists(), not present)

    def test_journal_repair_preserves_its_identity_and_monotonic_sequence(self):
        """A replacement gap must not manufacture a new journal while genuine missing metadata remains repairable."""
        directory = events.directory(self.root)
        directory.mkdir(parents=True, exist_ok=True)
        journal = "a" * 32
        for filename in (events.ID, events.ID_COPY):
            (directory / filename).write_text(journal + "\n")
        (directory / "seq").write_text("10\n")
        event = {"type": "event", "data": {}}
        # Pin the already-read journal identity so the injection reaches _append's repair read.
        with mock.patch.object(events, "journal_id", return_value=journal):
            for filename in (events.ID, events.ID_COPY):
                path = directory / filename
                with self.missing(path):
                    events._append(self.root, dict(event))
                self.assertEqual(path.read_text().strip(), journal)
                path.unlink()
                events._append(self.root, dict(event))
                self.assertEqual(path.read_text().strip(), journal)
        with self.missing(directory / "seq"):
            row = events._append(self.root, dict(event))
        self.assertEqual(row["seq"], 14)

    def test_caches_do_not_erase_other_entries_after_a_read_miss(self):
        """Cache rewrites must retain other plugins and environments when their existing record briefly vanishes."""
        directory = self.write_plugin("listener")
        path = trust.user_directory() / trust.FINGERPRINTS
        store.write(path, {"saved": {"fingerprint": "f"}})
        with self.missing(path):
            trust._remember(directory, "signature", "fingerprint")
        self.assertIn("saved", store.read(path))
        path.unlink()
        trust._remember(directory, "signature", "fingerprint")
        self.assertEqual(set(store.read(path)), {str(directory)})
        path = trust.user_directory() / trust.FILE
        store.write(path, {"plugins": {"saved": {"fingerprint": "f"}}})
        with self.missing(path):
            trust.trust(directory)
        self.assertIn("saved", store.read(path)["plugins"])
        with self.missing(path):
            self.assertTrue(trust.forget(directory))
        self.assertIn("saved", store.read(path)["plugins"])
        path.unlink()
        self.assertFalse(trust.forget(directory))
        trust.trust(directory)
        self.assertEqual(set(store.read(path)["plugins"]), {str(directory)})
        path = trust.user_directory() / loader.INSTALLED
        signature = [["site", 1]]
        saved = {"signature": signature, "directories": [str(directory)], "used": 0}
        with mock.patch.object(loader, "_environment_signature", return_value=signature), \
                mock.patch.object(loader, "_scan_installed", return_value=[]) as scan:
            store.write(path, {"environments": {sys.executable: saved, "other-python": saved}})
            with self.missing(path):
                self.assertEqual(loader._installed_remembered(), [directory])
            scan.assert_not_called()
            self.assertIn("other-python", store.read(path)["environments"])
            path.unlink()
            self.assertEqual(loader._installed_remembered(), [])
            scan.assert_called_once()

    def test_slow_load_cache_keeps_other_plugins_when_adding_or_forgetting(self):
        """A missed timeout cache would erase another plugin's load limit or leave a cleared timeout in effect."""
        directory = self.write_plugin("listener").resolve()
        found = SimpleNamespace(manifest=SimpleNamespace(directory=directory))
        path = trust.user_directory() / loader.LOAD_TIMEOUTS
        files = [("source", 1)]
        entry = {"files": loader._digest(files), "until": loader.time.time() + 600}
        with mock.patch.object(loader, "_files", return_value=files):
            store.write(path, {"plugins": {str(directory): entry, "saved": entry}})
            with self.missing(path):
                self.assertTrue(loader._known_slow(found))
            with self.missing(path, count=None):
                self.assertFalse(loader._known_slow(found))
            with self.missing(path):
                loader._remember_slow(directory, files)
            self.assertIn("saved", store.read(path)["plugins"])
            for after in (0, 1):
                store.write(path, {"plugins": {str(directory): entry, "saved": entry}})
                with self.missing(path, after=after):
                    loader._remember_slow(directory, None)
                self.assertEqual(set(store.read(path)["plugins"]), {"saved"})
            path.unlink()
            loader._remember_slow(directory, None)
            self.assertFalse(path.exists())
            loader._remember_slow(directory, files)
            self.assertEqual(set(store.read(path)["plugins"]), {str(directory)})

    def test_storage_selection_requires_the_existing_agent_records(self):
        """A transient missing runtime must not select the plugin process's home for a launched agent."""
        directory = self.agent("worker", native_session_home=str(self.work / "recorded-home"))
        row = {**store.read(directory / "properties.json"), "universe": str(self.root)}
        agent = api.Agent(row, None)
        path = directory / "properties.json"
        with self.missing(path):
            self.assertEqual(api._agent_storage(agent, "claude", "claude", ())[0], self.work / "recorded-home")
        with self.missing(path, count=None):
            self.assertEqual(api._agent_storage(agent, "claude", "claude", ()), (None, None))
        path = directory / "runtime.json"
        for method in ("stat", "read_text"):
            with self.missing(path, method=method):
                self.assertFalse(api._never_launched(directory, {}))
        with self.missing(path, count=None) as calls:
            self.assertFalse(api._never_launched(directory, {}))
        self.assertEqual(len(calls), store.MISSING_ATTEMPTS)
        path.unlink()
        self.assertTrue(api._never_launched(directory, {}))

    def test_non_missing_errors_and_external_files_are_not_retried(self):
        """Only owned replacement gaps qualify; corrupt records, denied reads and arbitrary plugin files stay immediate."""
        path = config.path(self.root)
        path.write_text("{")
        with mock.patch.object(Path, "read_text", autospec=True, return_value="{") as read:
            self.assertTrue(config.load(self.root).problem)
        self.assertEqual(read.call_count, 1)
        path = self.work / "plugin-data.json"
        store.write(path, {"value": 1})
        with self.missing(path) as calls:
            with self.assertRaises(api.Error):
                api.Files.read_json(path)
        self.assertEqual(len(calls), 1)
        with self.missing(path) as calls:
            with self.assertRaises(ValueError):
                settings._json(path)
        self.assertEqual(len(calls), 1)
