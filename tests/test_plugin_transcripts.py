"""Reading an agent's transcript through the plugin API (1.13), end to end over a real Claude transcript."""
import json
from uuid import uuid4
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

    def test_a_history_read_says_when_it_starts_at_claudes_own_summary(self):
        """A history that starts at Claude's own compaction summary, because what came before was never stored, says
        so on the info, history pages and turn pages, and only there; fragile because the provider knows it and
        core dropped it once, so a plugin silently lost the only record of what came before (COMPACTION-8)."""
        for stored in (False, True):
            with self.subTest("what came before was " + ("stored" if stored else "never stored")):
                rows = ClaudeRows()
                if stored:
                    rows.prompt("Earlier task")
                    rows.response("earlier answer")
                summary = rows.compaction(stored=stored)
                rows.prompt("Go on")
                rows.response("ok")
                self.fake.rewrite(rows.rows)
                expected = None if stored else summary["uuid"]
                info = self.agent_.transcript()
                history = self.agent_.transcript_rows(after=info.start, history=True)
                self.assertEqual((info.summary, history.summary, self.agent_.transcript_turns().summary),
                                 (expected,) * 3)
                self.assertIsNone(self.agent_.transcript_rows(after=info.start).summary)  # not a history read
                if not stored:
                    self.assertEqual(history.rows[1].ref, expected)  # right after its boundary, labelled compaction
                    self.assertEqual(history.rows[1].label, "compaction")

    def test_a_history_row_names_its_turn(self):
        """Each history row names the turn it belongs to, the same id turn pages give, and an abandoned row none,
        also where turns interleave; fragile because turns interleave in history order, so a plugin slicing a
        history read by row order alone silently puts a mid-turn command's rows into the wrong turn."""
        rows = ClaudeRows()
        rows.prompt("First task")
        early = rows.response("working", tool=("tu1", "Bash", {"command": "ls"}))
        rows.command("/model", "haiku", "Set model to haiku")  # typed while the turn runs
        rows.tool_result("tu1", "a.txt")
        rows.response("done")
        rows.turn_end()
        rows.rewind(early)
        rows.prompt("A branch a rewind left")  # abandoned once the history goes on from the other branch
        rows.rewind(rows.rows[6])  # the second prompt goes on from the first turn's end
        rows.prompt("Second task")
        rows.response("ok")
        self.fake.rewrite(rows.rows)
        turns = {turn.id: turn for turn in self.agent_.transcript_turns().turns}
        info = self.agent_.transcript()
        history = self.agent_.transcript_rows(after=info.start, history=True).rows
        self.assertTrue(all(row.turn in turns for row in history))
        kinds = [turns[row.turn].kind for row in history]
        self.assertEqual(kinds.count("command"), 2)  # the /model row and its output, in the middle of the prompt turn
        self.assertEqual([row.turn for row in history].count(history[0].turn), 5)  # the first prompt turn, split
        raw = self.agent_.transcript_rows(after=info.start).rows
        abandoned = [row for row in raw if row.abandoned]
        self.assertEqual([row.data["message"]["content"] for row in abandoned], ["A branch a rewind left"])
        self.assertIsNone(abandoned[0].turn)
        self.assertEqual({row.ref: row.turn for row in raw if not row.abandoned},
                         {row.ref: row.turn for row in history})  # the same turn on a raw read

    def test_codex_rollout_turns_and_rollback_use_the_same_public_cursor_api(self):
        """Codex's event echoes and rollback markers must produce one public turn per native input, with diverged cursors."""
        from test_codex_transcript_reading import event, message
        native = str(uuid4())
        home = self.work / "codex-home"
        self.agent("codex-worker", harness="codex", native_id=native, native_session_home=str(home))
        path = home / "sessions/2026/10/25" / f"rollout-2026-10-25T10-00-00-{native}.jsonl"
        path.parent.mkdir(parents=True)
        rows = [{"type": "session_meta", "payload": {"id": native, "cli_version": "0.164.0"}},
                event("task_started", turn_id="a"), message("first", id="first"),
                event("user_message", message="first"), message("done", "assistant"),
                event("task_complete", turn_id="a"), event("task_started", turn_id="b"),
                message("second", id="second"), event("user_message", message="second")]
        path.write_text("".join(json.dumps(row) + "\n" for row in rows))
        agent = self.mv.universe(self.root).agent("codex-worker")
        info = agent.transcript()
        self.assertEqual((info.state, info.format), ("supported", "codex-jsonl"))
        turns = agent.transcript_turns().turns
        self.assertEqual([(turn.kind, turn.finished) for turn in turns],
                         [("preamble", None), ("prompt", True), ("prompt", False)])
        page = agent.transcript_rows(history=True, labels={"prompt"})
        self.assertEqual([row.ref for row in page.rows], ["first", "second"])
        saved = page.rows[-1].cursor
        with path.open("a") as stream:
            stream.write(json.dumps(event("thread_rolled_back", num_turns=1)) + "\n")
        current = agent.transcript_rows(history=True, labels={"prompt"})
        self.assertEqual([row.ref for row in current.rows], ["first"])
        with self.assertRaises(api.TranscriptChanged) as caught:
            agent.transcript_rows(after=saved, history=True)
        self.assertEqual(caught.exception.reason, "diverged")
        raw = agent.transcript_rows(labels={"prompt"})
        self.assertEqual([row.abandoned for row in raw.rows], [False, True])

    def test_reads_need_access_and_say_why_they_cannot_go_on(self):
        """A transcript read needs transcripts.read and observe where the plugin is on, checked for every page;
        a /clear under a cursor raises TranscriptChanged("replaced") with where to resume, malformed generation
        tokens raise api.Error, and unavailable generations return info but refuse data reads; fragile because
        transcripts hold tool output and secrets, and a read that quietly crosses a /clear or reads 'nothing' corrupts a plugin's
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
        other = self.agent("other")
        self.transcript(other, self.rows.rows)
        unknown = self.mv.universe(self.root).agent("other").transcript().generation
        turn_id = self.agent_.transcript_turns().turns[0].id
        for case, generation, expected in (
            ("malformed generation", "not-a-generation-token", api.Error),
            ("unknown generation", unknown, api.TranscriptUnavailable),
        ):
            for name, call in (
                ("info", self.agent_.transcript),
                ("rows", self.agent_.transcript_rows),
                ("iterator", lambda **kw: list(self.agent_.iter_transcript_rows(**kw))),
                ("turns", self.agent_.transcript_turns),
                ("turn", lambda **kw: self.agent_.transcript_turn(turn_id, **kw)),
            ):
                with self.subTest(case, read=name):
                    if expected is api.TranscriptUnavailable and name == "info":
                        info = call(generation=generation)
                        self.assertEqual(info.state, "unavailable")
                        self.assertIn("Unknown transcript generation", info.reason)
                        self.assertIsNone(info.start)
                        continue
                    with self.assertRaises(expected) as caught:
                        call(generation=generation)
                    self.assertIs(type(caught.exception), expected)
                    if expected is api.TranscriptUnavailable:
                        self.assertFalse(caught.exception.retry)
        with self.subTest("an unreadable store"):
            self.fake.unreadable()
            with self.assertRaises(api.TranscriptUnavailable) as caught:
                self.agent_.transcript_rows()
            self.assertFalse(caught.exception.retry)
