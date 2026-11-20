import os
import threading
import unittest

from multiverse.plugins.testing import PluginTestCase
from multiverse.ui import views
from multiverse.ui.columns_screen import ColumnsScreen
from textual.widgets import DataTable, Static

VIEW = """{
  "columns": ["name", "unread", "status", "model", "effort", "phase", "blocked_on", "summary"],
  "accent": "auto",
  "description": "Kept as written."
}
"""


class ColumnsScreenTests(PluginTestCase, unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        super().setUp()
        self.view = self.root / "views" / "default.json"
        self.agent("ada", model="opus", effort="xhigh")

    async def open(self, d):
        await d.press("C")
        await d.until(lambda: isinstance(d.screen, ColumnsScreen), "the Columns screen")
        return d.screen

    async def pick(self, d, key):
        screen = d.screen
        keys = [row[0] for row in screen.rows()]
        screen.query_one("#columns-table", DataTable).move_cursor(row=keys.index(key))
        await d.settle()

    def note(self, screen):
        return str(screen.query_one(".cerror", Static).render())

    async def test_the_columns_screen_toggles_orders_and_writes_only_the_views_columns(self):
        """The Columns screen turns columns on and off and orders them, keeps a mark with its column, and writes only
        the columns value of the view file; fragile because one wrong write loses the view's other settings or a mark,
        and the agent list would then quietly show other columns."""
        self.view.write_text(VIEW)
        async with self.dashboard(size=(160, 40)) as d:
            screen = await self.open(d)
            self.assertEqual([row[0] for row in screen.rows()][:2], ["name", "unread"])  # a mark under its column
            for key in ("status", "model"):  # off
                await self.pick(d, key)
                await d.press("space")
            await self.pick(d, "phase")
            await d.press("shift+up")  # above Model
            await self.pick(d, "name")
            await d.press("space", "shift+down")  # Universe / agent stays on and first
            self.assertIn("always shown", self.note(screen))
            drawn = {key: (on, title) for key, on, title in screen.rows()}
            self.assertIn("(needs Model)", drawn["effort"][1])  # kept, under its column, while the column is off
            await d.press("enter")
            await d.until(lambda: not isinstance(d.screen, ColumnsScreen), "the screen to close")
            expected = ["name", "unread", "phase", "summary", "blocked_on", "effort"]
            self.assertEqual(self.view.read_text(), VIEW.replace(
                '["name", "unread", "status", "model", "effort", "phase", "blocked_on", "summary"]',
                '["' + '", "'.join(expected) + '"]'))
            await d.until(lambda: d.table()[0][1:] == ["Phase", "Working on"], "the table to follow the file")

            with self.subTest("turning Model back on puts it at its usual place, with its effort"):
                screen = await self.open(d)
                await self.pick(d, "model")
                await d.press("space", "enter")
                await d.until(lambda: not isinstance(d.screen, ColumnsScreen), "the screen to close")
                self.assertIn('"columns": ["name", "unread", "model", "effort", "phase", "summary", "blocked_on"]',
                              self.view.read_text())

            with self.subTest("a mark sits under core's own column where that one is shown"):
                self.view.write_text('{"columns": ["name", "core:model", "effort", "core:summary", "blocked_on"]}\n')
                screen = await self.open(d)
                rows = screen.rows()
                keys, titles = [key for key, _, _ in rows], {key: title for key, _, title in rows}
                for mark, host in (("effort", "core:model"), ("blocked_on", "core:summary")):
                    self.assertEqual(keys.index(mark), keys.index(host) + 1, mark)
                    self.assertNotIn("(needs", titles[mark])
                await self.pick(d, "status")
                await d.press("space", "enter")
                await d.until(lambda: not isinstance(d.screen, ColumnsScreen), "the screen to close")
                self.assertIn('"core:model", "effort"', self.view.read_text())

    async def test_the_columns_screen_never_overwrites_a_view_it_did_not_read(self):
        """Esc, a view changed on disk meanwhile and a damaged view each leave the file as it is; fragile because the
        screen writes a file people also edit by hand, and an overwrite would lose their edit without a word."""
        async with self.dashboard(size=(160, 40)) as d:
            with self.subTest("Esc discards"):
                self.view.write_text(VIEW)
                screen = await self.open(d)
                await self.pick(d, "status")
                await d.press("space", "escape")
                await d.until(lambda: not isinstance(d.screen, ColumnsScreen), "the screen to close")
                self.assertEqual(self.view.read_text(), VIEW)

            with self.subTest("a view edited on disk while the screen is open is read again, not overwritten"):
                screen = await self.open(d)
                await self.pick(d, "status")
                await d.press("space")
                edited = VIEW.replace('"status", ', "")
                self.view.write_text(edited)
                await d.press("enter")
                self.assertIs(d.screen, screen)
                self.assertEqual(self.view.read_text(), edited)
                self.assertIn("changed on disk", self.note(screen))
                shown = [key for key, on, _ in screen.rows() if on == "●"]
                self.assertNotIn("status", shown)  # the file as it is now
                await d.press("escape")

            with self.subTest("a column the list never draws is kept"):
                self.view.write_text('{"columns": ["name", "universe", "status", "summary"]}\n')
                screen = await self.open(d)
                await self.pick(d, "status")
                await d.press("space", "enter")
                await d.until(lambda: not isinstance(d.screen, ColumnsScreen), "the screen to close")
                self.assertIn('"universe"', self.view.read_text())

            plain = '{"columns": ["name", "status"]}'
            damaged = (("not JSON", b'{\n  "columns": ["name",\n', "isn't valid JSON"),
                       ("columns not a list", b'{"columns": "name"}\n', "aren't a list of names"),
                       ("a byte-order mark", plain.encode("utf-8-sig"), "byte-order mark"),
                       ("UTF-16", plain.encode("utf-16"), "isn't UTF-8"))
            for name, text, why in damaged:
                with self.subTest(f"a damaged view is read-only: {name}"):
                    self.view.write_bytes(text)
                    screen = await self.open(d)
                    self.assertIn("can't be edited", self.note(screen))
                    self.assertIn(why, self.note(screen))
                    await self.pick(d, "status")
                    before = screen.rows()
                    await d.press("space")
                    self.assertEqual(screen.rows(), before)  # nothing to save
                    await d.press("enter")
                    self.assertIs(d.screen, screen)
                    self.assertEqual(self.view.read_bytes(), text)
                    await d.press("escape")

    def test_a_view_that_is_a_fifo_is_refused_without_waiting(self):
        """The view reader refuses a FIFO at once instead of opening it; fragile because a plain open waits for a
        writer, and the Columns screen reads the file on the dashboard's thread, so the whole dashboard would hang."""
        path = self.root / "views" / "default.json"
        path.unlink(missing_ok=True)
        os.mkfifo(path)
        outcome = []

        def read():
            try:
                views.editable_columns(self.root)
                outcome.append("read")
            except (OSError, ValueError) as error:
                outcome.append(error)

        reader = threading.Thread(target=read, daemon=True)
        reader.start()
        reader.join(2)
        if reader.is_alive():  # let the stuck open return, so the thread ends
            os.close(os.open(path, os.O_WRONLY | os.O_NONBLOCK))
            reader.join(2)
            self.fail("reading the view waited on a FIFO")
        self.assertIsInstance(outcome[0], ValueError)
        self.assertIn("isn't a regular file", str(outcome[0]))


if __name__ == "__main__":
    unittest.main()
