"""Reading an agent's transcript through the plugin API (1.13), end to end over a real Claude transcript."""
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from multiverse.plugins import api, config, loader
from multiverse.plugins.testing import ClaudeRows, PluginTestCase

READER = {"module": "plugin", "api": "1.13", "permissions": ["agents.read", "transcripts.read"]}


class TranscriptApiTest(PluginTestCase):
    def setUp(self):
        super().setUp()
        self.write_plugin("reader", READER, {"plugin.py": "def register(mv): pass\n"})
        self.enable("reader")
        self.worker = self.agent("worker")
        rows = ClaudeRows()
        rows.prompt("First task")
        rows.response("I'll look", tool=("tu1", "Bash", {"command": "ls"}))
        rows.tool_result("tu1", "a.txt b.txt")
        rows.response("done")
        rows.turn_end()
        rows.prompt("Second task")
        rows.response("ok")
        self.rows = rows
        self.fake = self.transcript(self.worker, rows.rows)
        self.mv = api.Plugin(loader.load(self.entry("reader")))
        self.agent_ = self.mv.universe(self.root).agent("worker")

    def test_rows_and_turns_read_through_cursors_into_fresh_api_objects(self):
        """The API gives core's results as its own frozen types, pages by cursor to the end, keeps labels and
        refs, gives each read a fresh copy of a row's data and none with bodies=False; fragile because a plugin
        keeps cursors and rows between reads, so a shared dict or a lost page silently corrupts what it builds."""
        info = self.agent_.transcript()
        self.assertEqual((info.state, info.format, info.rows), ("supported", "claude-jsonl", 7))
        self.assertIsInstance(info, api.TranscriptInfo)
        page = self.agent_.transcript_rows(after=info.start, limit=3)
        self.assertIsInstance(page, api.RowPage)
        self.assertEqual([row.label for row in page.rows], ["prompt", "response", "tool_result"])
        self.assertEqual(page.rows[2].call_id, "tu1")
        seen = [row.ref for row in self.agent_.iter_transcript_rows(after=info.start, limit=2)]
        self.assertEqual(seen, [row["uuid"] for row in self.rows.rows])  # every page, in order, to the end
        first = page.rows[0]
        first.data["message"]["content"] = "changed by the plugin"
        again = self.agent_.transcript_rows(after=info.start, limit=1).rows[0]
        self.assertEqual(again.data["message"]["content"], "First task")  # a fresh copy per read
        self.assertIsNone(self.agent_.transcript_rows(after=info.start, limit=1, bodies=False).rows[0].data)
        turns = self.agent_.transcript_turns()
        self.assertIsInstance(turns, api.TurnPage)
        self.assertEqual([(turn.kind, turn.preview, turn.finished) for turn in turns.turns],
                         [("prompt", "First task", True), ("prompt", "Second task", False)])  # no end row yet
        rows = self.agent_.transcript_turn(turns.turns[0].id)
        self.assertEqual(len(rows.rows), 5)

    def test_reads_need_access_and_say_why_they_cannot_go_on(self):
        """A transcript read needs transcripts.read and observe where the plugin is on, checked for every page;
        a /clear under a cursor raises TranscriptChanged("replaced") with where to resume, and an unreadable
        store raises TranscriptUnavailable, all api.Error and never an OSError; fragile because transcripts hold
        tool output and secrets, and a read that quietly crosses a /clear or reads 'nothing' corrupts a plugin's
        own state."""
        with self.subTest("access"):
            self.write_plugin("nosy", {"module": "plugin", "api": "1.13", "permissions": ["agents.read"]},
                              {"plugin.py": "def register(mv): pass\n"})
            self.enable("nosy")
            nosy = api.Plugin(loader.load(self.entry("nosy"))).universe(self.root).agent("worker")
            with self.assertRaisesRegex(api.Error, "did not ask for permission 'transcripts.read'"):
                nosy.transcript()
            pages = self.agent_.iter_transcript_rows(limit=1)
            next(pages)
            config.set_enabled(self.root, "reader", False)  # turned off between pages
            with self.assertRaisesRegex(api.AccessDenied, "reader has no access here"):
                next(pages)
            config.set_enabled(self.root, "reader", True)
        with self.subTest("a /clear under a cursor"):
            saved = self.agent_.transcript().end
            fresh = ClaudeRows()
            fresh.prompt("After the clear")
            self.fake.clear(fresh.rows)
            with self.assertRaises(api.TranscriptChanged) as caught:
                self.agent_.transcript_rows(after=saved)
            self.assertEqual(caught.exception.reason, "replaced")
            self.assertNotIsInstance(caught.exception, OSError)
            resumed = self.agent_.transcript_rows(after=caught.exception.resume)
            self.assertEqual([row.label for row in resumed.rows], ["prompt"])
            self.assertEqual(len(self.agent_.transcript().earlier), 1)
        with self.subTest("an unreadable store"):
            self.fake.unreadable()
            with self.assertRaises(api.TranscriptUnavailable) as caught:
                self.agent_.transcript_rows()
            self.assertFalse(caught.exception.retry)
