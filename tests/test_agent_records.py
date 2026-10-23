"""One damaged agent record never stops the others: the validator, then listing over real files on disk."""
import json
import os
from pathlib import Path
import sys
from types import SimpleNamespace
import unittest
from unittest import mock
from uuid import uuid4

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from multiverse import harnesses
from multiverse.plugins.testing import PluginTestCase
from multiverse.runtime import lifecycle, native
from multiverse.store import read, write
from multiverse.terminal.tmux import Tmux
from multiverse.tools import list_agents
from multiverse.universe import records, templates

UNREADABLE_WORKS = os.geteuid() != 0  # root reads a mode-000 file


FORMS = ("list", "truncated", "empty", "name-list", "harness-list", "unreadable", "directory", "garbage")


class DamagedListingTests(PluginTestCase):
    def setUp(self):
        super().setUp()
        self.healthy = self.agent("healthy", summary="HEALTHY-MARKER", phase="working")
        self.other = self.agent("other")
        self.victim = self.agent("victim", summary="the victim's own summary", phase="blocked")
        self.originals = {name: (self.victim / name).read_bytes() for name in ("properties.json", "runtime.json")}
        self.addCleanup(self.restore)

    def restore(self):
        for name, data in self.originals.items():
            path = self.victim / name
            if path.is_dir():
                path.rmdir()
            path.exists() and path.chmod(0o600)
            path.write_bytes(data)

    def damage(self, file, form):
        path = self.victim / file
        value = json.loads(self.originals[file])
        if form == "list":
            path.write_text("[]")
        elif form == "truncated":
            path.write_text('{"id":')
        elif form == "empty":
            path.write_text("")
        elif form == "name-list":
            path.write_text(json.dumps({**value, "name": ["not-a-name"]}))
        elif form == "harness-list":
            path.write_text(json.dumps({**value, "harness": ["not-a-harness"]}))
        elif form == "unreadable":
            path.chmod(0)
        elif form == "directory":
            path.unlink()
            path.mkdir()
        elif form == "garbage":
            path.write_bytes(b"\x80\x81 not json")

    def damaged(self, path, function=records.properties):
        with self.assertRaises(records.RecordError) as raised:
            function(path)
        error = raised.exception
        self.assertEqual(error.file, path)
        self.assertNotIn("\n", error.reason)
        self.assertNotIn("Traceback", str(error))
        self.assertTrue(str(error).startswith(path.name + ": "), str(error))
        return error

    def rows(self, **options):
        return {row["id"]: row for row in lifecycle.listing(self.root, **options)}

    def healthy_rows_are_whole(self, rows):
        for directory in (self.healthy, self.other):
            row = rows[directory.name]
            self.assertNotIn("damaged", row)
            self.assertIn("usage", row)  # the full observed row, not a fallback
        self.assertEqual(rows[self.healthy.name]["summary"], "HEALTHY-MARKER")

    def check(self, file, form):
        if form == "unreadable" and not UNREADABLE_WORKS:
            self.skipTest("root reads every file")
        self.damage(file, form)
        rows = self.rows()
        self.assertEqual(len(rows), 3)
        self.healthy_rows_are_whole(rows)
        row = rows[self.victim.name]
        self.assertEqual(row["live"], "unknown")
        self.assertEqual(row["status"], "unknown")
        self.assertTrue(all(isinstance(row[key], str) for key in ("id", "name", "harness", "phase", "summary", "universe")), row)
        damaged = row["damaged"]
        self.assertEqual(damaged["file"], f"state/agents/{self.victim.name}/{file}")
        self.assertNotIn("\n", damaged["reason"])
        self.assertIn(file, row["summary"])
        self.assertTrue(row["summary"].startswith("damaged: "))
        if file == "properties.json" and form != "harness-list":
            self.assertEqual(row["name"], self.victim.name)  # nothing readable to name it by
        elif file == "properties.json":  # only the harness is of the wrong type: the valid name still shows, the harness does not
            self.assertEqual((row["name"], row["harness"]), ("victim", ""))
        else:  # the properties are good: the agent keeps its name, harness and reported phase
            self.assertEqual((row["name"], row["harness"], row["phase"]), ("victim", "claude", "blocked"))
        # Putting the file back puts the row back.
        self.restore()
        again = self.rows()[self.victim.name]
        self.assertNotIn("damaged", again)
        self.assertEqual(again["name"], "victim")

    def test_each_form_of_damage_names_its_file_and_reason(self):
        """Each form of record damage (truncated, wrong types, binary, a directory, unreadable, null identity) names its
        file and a one-line reason, while a good record keeps whatever it holds and null reported fields list as empty
        text; fragile because every new text or identity field must join the validator's lists, or a wrong type
        crashes a reader far away (51537ad, LC-16)."""
        path = self.work / "records" / "properties.json"
        path.parent.mkdir()
        with self.subTest("a good record"):
            path.write_text(json.dumps({"id": "1", "name": "w", "harness": "claude", "phase": None,
                                        "future": [1, {"x": 2}]}))
            self.assertEqual(records.properties(path)["future"], [1, {"x": 2}])
            path.write_text("{}")
            self.assertEqual(records.properties(path), {})  # a record that merely lacks fields is still readable
        forms = {"[]": "must be a JSON object, not a list", "7": "not a number", '"text"': "not a string",
                 "null": "not null", '{"id":': "is not valid JSON", "": "is empty", "  \n": "is empty",
                 "{": "is not valid JSON"}
        for text, reason in forms.items():
            with self.subTest(text=text):
                path.write_text(text)
                self.assertIn(reason, self.damaged(path).reason)
        for key in records.TEXT_FIELDS:
            for value in (["x"], {"a": 1}, 3, True):
                with self.subTest(key=key, value=value):
                    path.write_text(json.dumps({"name": "ok", key: value}))
                    error = self.damaged(path)
                    self.assertIn(f"{key} must be a string", error.reason)
                    self.assertEqual(error.data[key], value)
        path.write_text(json.dumps({"phase": None, "summary": None, "task": None}))
        records.properties(path)  # a reported field that is null is "not set"
        for key in records.IDENTITY_FIELDS:  # the fields that identify the agent are never null
            with self.subTest(null=key):
                path.write_text(json.dumps({"name": "ok", key: None}))
                error = self.damaged(path)
                self.assertIn(f"{key} must be a string, not null", error.reason)
                self.assertIn(key, error.data)
        with self.subTest("binary garbage, a deep nest, a huge file and a directory"):
            path.write_bytes(b"\xff\xfe\x00\x01")
            self.assertIn("not text", self.damaged(path).reason)
            path.write_text("[" * 200000)
            self.assertIn("nested too deeply", self.damaged(path).reason)
            path.write_text('{"name": "' + "x" * 64 + '"}')
            with mock.patch.object(records, "MAX_BYTES", 32):
                self.assertIn("larger than", self.damaged(path).reason)
            path.unlink()
            path.mkdir()
            self.assertIn("cannot be read", self.damaged(path).reason)
            path.rmdir()
        with self.subTest("unreadable"):
            if not UNREADABLE_WORKS:
                self.skipTest("root reads every file")
            path.write_text("{}")
            path.chmod(0)
            self.addCleanup(path.chmod, 0o600)
            self.assertIn("cannot be read: Permission denied", self.damaged(path).reason)
        runtime = path.with_name("runtime.json")
        with self.subTest("runtime.json"):
            self.assertEqual(records.runtime(runtime), {})
            for text in ("[]", "", "{", '"x"'):
                runtime.write_text(text)
                self.damaged(runtime, records.runtime)
            runtime.write_text('{"state": "running", "name": ["fine here"]}')
            self.assertEqual(records.runtime(runtime)["state"], "running")  # only properties' text fields are checked
        data = json.loads(self.originals["properties.json"])
        with self.subTest("null reported fields in a listing"):
            (self.victim / "properties.json").write_text(json.dumps({**data, "phase": None, "summary": None,
                                                                     "task": None}))
            row = self.rows()[self.victim.name]
            self.assertNotIn("damaged", row)
            self.assertEqual((row["phase"], row["summary"], row["task"]), ("", "", ""))
            self.assertIn("victim", list_agents.run(self.root))  # the text listing formats every row
            self.assertEqual(len(list_agents.run(self.root, json=True)), 3)
        for key in ("id", "name", "harness"):
            with self.subTest(null_in_listing=key):
                (self.victim / "properties.json").write_text(json.dumps({**data, key: None}))
                rows = self.rows()
                self.assertEqual(len(rows), 3)
                damaged = rows[self.victim.name]
                self.assertEqual(damaged["damaged"]["reason"].split(';', 1)[0], f"{key} must be a string, not null")
                self.assertIsInstance(damaged["name"], str)
                self.assertIn("damaged: properties.json", list_agents.run(self.root))

    def test_a_damaged_agent_record_leaves_every_other_row_whole_and_names_the_file(self):
        """One damaged record, or one raising observer, is one marked row naming its file while every other row,
        subverses included, stays whole; fragile because every new field read or per-row adapter call is a new crash
        point, and a break empties the agent list for the whole universe (LC-9, 00c689d)."""
        for file in ("properties.json", "runtime.json"):
            for form in FORMS:
                if file == "runtime.json" and form in ("name-list", "harness-list"):
                    continue  # those are properties fields
                with self.subTest(file=file, form=form):
                    self.check(file, form)
        for field in ({"tmux": []}, {"tmux": {"socket": 3, "pane": 4}}, {"tmux": "text"}, {"native_relay": []},
                      {"state": ["x"]}):
            with self.subTest(runtime_field=field):
                path = self.victim / "runtime.json"
                path.write_text(json.dumps({**json.loads(self.originals["runtime.json"]), **field}))
                rows = self.rows()
                self.assertEqual(len(rows), 3)
                self.healthy_rows_are_whole(rows)
                self.assertIsInstance(rows[self.victim.name]["name"], str)
                self.restore()
        with self.subTest("listing one agent by id"):
            self.damage("properties.json", "truncated")
            only = lifecycle.listing(self.root, only=self.victim.name)
            self.assertEqual([row["id"] for row in only], [self.victim.name])
            self.assertIn("damaged", only[0])
            self.assertEqual(lifecycle.listing(self.root, only=self.healthy.name)[0]["summary"], "HEALTHY-MARKER")
            self.restore()
        with self.subTest("a subverse"):
            inner = self.subverse("inner")
            mine = self.agent("deep", root=inner)
            self.damage("properties.json", "list")
            rows = self.rows(recursive=True)
            self.assertEqual(len(rows), 4)
            self.assertEqual(rows[mine.name]["universe"], str(inner))
            self.assertIn("damaged", rows[self.victim.name])
            self.restore()
        root = templates.create(self.work / "observed" / ".multiverse", "observed", workdir=self.work)
        identity, launch = str(uuid4()), str(uuid4())
        directory = root / "state" / "agents" / identity
        properties = {"id": identity, "name": "worker", "harness": "pi", "workspace": str(self.work),
                      "native_session_id": str(uuid4()), "native_session_home": str(self.work),
                      "native_session_provider": "fixture", "universe_id": read(root / "universe.json")["id"],
                      "task": ""}
        facts = {"launch_id": launch, "state": "running", "pid": 123,
                 "tmux": {"socket": str(self.work / "absent.sock"), "session": "$1", "pane": "%1"}}
        write(directory / "properties.json", properties)
        write(directory / "runtime.json", facts)

        def broken(session):
            raise TypeError("bad native observation")
        with self.subTest("an observer that raises"), \
                mock.patch.object(harnesses, "load", return_value=SimpleNamespace(observed_binding=broken)), \
                mock.patch.object(lifecycle, "observe", return_value={"live": "alive"}):
            row, = lifecycle.listing(root)
            self.assertEqual(row["name"], "worker")
            self.assertEqual(row["connection_error"], "bad native observation")
        calls = []

        def problem(session):
            calls.append(session)
            return "Native inbox disconnected; restart this agent"
        adapter = SimpleNamespace(connection_problem=problem)
        with self.subTest("a connection problem is asked of live agents only, and a relay error wins"):
            with mock.patch.object(harnesses, "load", return_value=adapter), \
                    mock.patch.object(native, "usage_snapshot", return_value={}), \
                    mock.patch.object(lifecycle, "observe", return_value={"live": "alive"}):
                self.assertIn("inbox disconnected", lifecycle.listing(root)[0]["connection_error"])
                write(directory / "runtime.json", {**facts, "native_relay": {"state": "lost",
                                                                             "reason": "relay disconnected"}})
                self.assertEqual(lifecycle.listing(root)[0]["connection_error"], "relay disconnected")
                self.assertEqual(len(calls), 1)
            write(directory / "runtime.json", facts)
            with mock.patch.object(harnesses, "load", return_value=adapter), \
                    mock.patch.object(native, "usage_snapshot", return_value={}), \
                    mock.patch.object(lifecycle, "observe", return_value={"live": "dormant"}):
                self.assertIsNone(lifecycle.listing(root)[0]["connection_error"])
                self.assertEqual(len(calls), 1)
            for adapter in (SimpleNamespace(), SimpleNamespace(connection_problem=lambda session: 1 / 0)):
                with mock.patch.object(harnesses, "load", return_value=adapter), \
                        mock.patch.object(native, "usage_snapshot", return_value={}), \
                        mock.patch.object(lifecycle, "observe", return_value={"live": "alive"}):
                    row = lifecycle.listing(root)[0]
                    self.assertEqual(row["name"], "worker")
                    self.assertIsNone(row["connection_error"])
        with self.subTest("bad properties during early observation"):
            write(directory / "properties.json", {key: value for key, value in properties.items()
                                                  if key != "universe_id"})
            pane = {"pane_id": "%1", "session_id": "$1", "pane_pid": "123", "pane_dead": "0",
                    "@mv2_agent_id": identity, "@mv2_universe_id": "private-universe", "@mv2_launch_id": launch}
            with mock.patch.object(Tmux, "snapshot", return_value=({"%1": pane}, None)), \
                    mock.patch.object(harnesses, "load", return_value=SimpleNamespace()):
                rows = lifecycle.listing(root)
            self.assertEqual(len(rows), 1)
            self.assertIn("damaged", rows[0], "malformed records must remain isolated to their own row")


if __name__ == "__main__":
    unittest.main()
