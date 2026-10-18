"""One damaged agent record never stops the others: the validator, then listing over real files on disk."""
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from multiverse.plugins.testing import PluginTestCase
from multiverse.runtime import lifecycle
from multiverse.universe import records

UNREADABLE_WORKS = os.geteuid() != 0  # root reads a mode-000 file


class RecordsTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / "properties.json"

    def damaged(self, function=records.properties):
        with self.assertRaises(records.RecordError) as raised:
            function(self.path)
        error = raised.exception
        self.assertEqual(error.file, self.path)
        self.assertNotIn("\n", error.reason)
        self.assertNotIn("Traceback", str(error))
        self.assertTrue(str(error).startswith(self.path.name + ": "), str(error))
        return error

    def test_a_good_record_is_returned_as_written_with_whatever_else_it_holds(self):
        self.path.write_text(json.dumps({"id": "1", "name": "w", "harness": "claude", "phase": None, "future": [1, {"x": 2}]}))
        self.assertEqual(records.properties(self.path)["future"], [1, {"x": 2}])
        self.path.write_text("{}")
        self.assertEqual(records.properties(self.path), {})  # a record that merely lacks fields is still readable

    def test_each_form_of_damage_names_its_file_and_gives_one_line_reason(self):
        forms = {"[]": "must be a JSON object, not a list", "7": "not a number", '"text"': "not a string", "null": "not null",
                 '{"id":': "is not valid JSON", "": "is empty", "  \n": "is empty", "{": "is not valid JSON"}
        for text, reason in forms.items():
            with self.subTest(text=text):
                self.path.write_text(text)
                self.assertIn(reason, self.damaged().reason)

    def test_values_of_the_wrong_type_name_the_field_and_keep_what_was_parsed(self):
        for key in records.TEXT_FIELDS:
            for value in (["x"], {"a": 1}, 3, True):
                with self.subTest(key=key, value=value):
                    self.path.write_text(json.dumps({"name": "ok", key: value}))
                    error = self.damaged()
                    self.assertIn(f"{key} must be a string", error.reason)
                    self.assertEqual(error.data[key], value)
        self.path.write_text(json.dumps({"phase": None, "summary": None, "task": None}))
        records.properties(self.path)  # a reported field that is null is "not set"
        for key in records.IDENTITY_FIELDS:  # the fields that identify the agent are never null
            with self.subTest(null=key):
                self.path.write_text(json.dumps({"name": "ok", key: None}))
                error = self.damaged()
                self.assertIn(f"{key} must be a string, not null", error.reason)
                self.assertIn(key, error.data)

    def test_binary_garbage_a_deep_nest_a_huge_file_and_a_directory_are_damage_not_crashes(self):
        self.path.write_bytes(b"\xff\xfe\x00\x01")
        self.assertIn("not text", self.damaged().reason)
        self.path.write_text("[" * 200000)
        self.assertIn("nested too deeply", self.damaged().reason)
        self.path.write_text('{"name": "' + "x" * 64 + '"}')
        with mock.patch.object(records, "MAX_BYTES", 32):
            self.assertIn("larger than", self.damaged().reason)
        self.path.unlink()
        self.path.mkdir()
        self.assertIn("cannot be read", self.damaged().reason)

    @unittest.skipUnless(UNREADABLE_WORKS, "root reads every file")
    def test_an_unreadable_file_says_so(self):
        self.path.write_text("{}")
        self.path.chmod(0)
        self.addCleanup(self.path.chmod, 0o600)
        self.assertIn("cannot be read: Permission denied", self.damaged().reason)

    def test_a_missing_runtime_is_empty_and_a_damaged_one_is_reported(self):
        runtime = self.path.with_name("runtime.json")
        self.assertEqual(records.runtime(runtime), {})
        for text in ("[]", "", "{", '"x"'):
            runtime.write_text(text)
            with self.assertRaises(records.RecordError) as raised:
                records.runtime(runtime)
            self.assertEqual(raised.exception.file, runtime)
        runtime.write_text('{"state": "running", "name": ["fine here"]}')
        self.assertEqual(records.runtime(runtime)["state"], "running")  # only properties' text fields are checked


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

    def test_a_damaged_agent_record_leaves_every_other_row_whole_and_names_the_file(self):
        for file in ("properties.json", "runtime.json"):
            for form in FORMS:
                if file == "runtime.json" and form in ("name-list", "harness-list"):
                    continue  # those are properties fields
                with self.subTest(file=file, form=form):
                    self.check(file, form)

    def test_null_reported_fields_list_as_empty_text_and_null_identity_is_damage(self):
        from multiverse.tools import list_agents
        data = json.loads(self.originals["properties.json"])
        (self.victim / "properties.json").write_text(json.dumps({**data, "phase": None, "summary": None, "task": None}))
        row = self.rows()[self.victim.name]
        self.assertNotIn("damaged", row)
        self.assertEqual((row["phase"], row["summary"], row["task"]), ("", "", ""))
        self.assertIn("victim", list_agents.run(self.root))  # the text listing formats every row
        self.assertEqual(len(list_agents.run(self.root, json=True)), 3)
        for key in ("id", "name", "harness"):
            with self.subTest(null=key):
                (self.victim / "properties.json").write_text(json.dumps({**data, key: None}))
                rows = self.rows()
                self.assertEqual(len(rows), 3)
                damaged = rows[self.victim.name]
                self.assertEqual(damaged["damaged"]["reason"], f"{key} must be a string, not null")
                self.assertIsInstance(damaged["name"], str)
                self.assertIn("damaged: properties.json", list_agents.run(self.root))
        self.restore()

    def test_a_wrong_type_inside_a_runtime_that_parses_never_stops_the_listing(self):
        for field in ({"tmux": []}, {"tmux": {"socket": 3, "pane": 4}}, {"tmux": "text"}, {"native_relay": []}, {"state": ["x"]}):
            with self.subTest(field=field):
                path = self.victim / "runtime.json"
                path.write_text(json.dumps({**json.loads(self.originals["runtime.json"]), **field}))
                rows = self.rows()
                self.assertEqual(len(rows), 3)
                self.healthy_rows_are_whole(rows)
                self.assertIsInstance(rows[self.victim.name]["name"], str)
                self.restore()

    def test_every_agent_of_a_subverse_lists_when_one_of_the_tree_is_damaged(self):
        inner = self.subverse("inner")
        mine = self.agent("deep", root=inner)
        self.damage("properties.json", "list")
        rows = self.rows(recursive=True)
        self.assertEqual(len(rows), 4)
        self.assertEqual(rows[mine.name]["universe"], str(inner))
        self.assertIn("damaged", rows[self.victim.name])

    def test_listing_one_agent_by_id_reports_its_damage_and_a_healthy_neighbours_is_untouched(self):
        self.damage("properties.json", "truncated")
        only = lifecycle.listing(self.root, only=self.victim.name)
        self.assertEqual([row["id"] for row in only], [self.victim.name])
        self.assertIn("damaged", only[0])
        self.assertEqual(lifecycle.listing(self.root, only=self.healthy.name)[0]["summary"], "HEALTHY-MARKER")

    def test_a_damaged_row_has_the_keys_the_text_listing_and_the_dashboard_read(self):
        from multiverse.tools import list_agents
        self.damage("properties.json", "name-list")
        text = list_agents.run(self.root)
        self.assertIn("healthy", text)
        self.assertIn("damaged: properties.json", text)


if __name__ == "__main__":
    unittest.main()
