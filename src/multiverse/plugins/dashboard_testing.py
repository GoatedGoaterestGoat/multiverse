"""Drive the real dashboard in a plugin's tests: the 1.x contract for dashboard tests (API 1.9).

A plugin whose screens, columns or panels are shown by the dashboard tests them
against the dashboard itself, on the temporary universe `PluginTestCase`
makes, with the plugin on and really loaded. There is no fake backend and no fake
plugin service to keep in step with mv2: the dashboard reads the universe's
files, and a test arranges them (`self.agent(...)`, `self.enable(...)`,
`self.subverse(...)`, a view file) before or while it runs.

    class MyScreenTest(PluginTestCase, unittest.IsolatedAsyncioTestCase):
        plugin = Path(__file__).parents[1]

        async def test_configure(self):
            self.agent("worker")
            async with self.dashboard() as d:
                screen = await d.open_settings("my-plugin")   # P, the plugin, Configure
                await d.until(lambda: screen.query("#table"), "its table")
                await d.press("ctrl+enter")                   # whatever the screen does
                self.assertIn("saved", d.status)

`self.dashboard(size=(120, 40), root=None)` returns a `Dashboard`, used with
`async with`. It imports Textual, so it is imported only when a test asks for it
and never by the command line. The refresh timer is off (`d.refresh()` reads the
universe again) and the plugin host is never started.

Promised from API 1.9, by adding only:

- `await d.ready()` waits until the first refresh is drawn and the palette is read once;
  the dashboard waits only half a second for a plugin to import, so on a busy machine
  a plugin's entries can come later: `d.until(...)` and `d.run_action(...)` read the palette
  again as soon as a plugin has loaded. `async with` calls it. `d.until(check, what, timeout=8)` waits
  for `check()` to be true, failing the test with `what` after `timeout` seconds;
  prefer it to fixed pauses. `await d.press(*keys)` presses keys as a person
  would, `await d.settle()` waits for pending background work and `await
  d.refresh()` reads the universe again. Leaving the `async with` first waits (at
  most 10 s) until the dashboard has nothing running, scans an action started included,
  so no thread of it outlives the test and writes under the home the test restores.
- `d.screen` is the screen in front; `d.status` the dashboard's status line;
  `d.header` its header counts (agents, plugin errors, `host off`).
- `await d.open_plugins()` presses P and returns the Plugins screen once its list
  is read. `await d.open_settings(name)` goes on to Configure the named plugin and
  returns the screen it opened (its own, or the one generated from its fields or
  schema); the test fails with the screen's message if the plugin has no settings.
  `await d.close_screen()` presses Escape and waits for the screen to go.
  A screen that dismisses with a string leaves it on `d.status`.
- `await d.run_action(title)` runs a command palette entry by its title
  (`"my-plugin: Say hello"`, as a person sees it) and waits for it to finish; it
  returns the new screen when the entry opens one. `d.palette()` lists the titles.
- `d.select(name)` puts the cursor on the agent or group of that name, as the table
  shows it (a subverse is shown under its view's title).
  `await d.enter(name)` enters that subverse's portal and `await d.back()`
  returns. `d.root` is the scope on show.
- `d.table()` gives `(headers, rows)` of the agent table as text, and
  `d.local_view()` the texts of the local view (a portal's widgets, plugin panels
  among them) in order.
- `d.app` is the Textual app, for anything else; what it contains (widget ids,
  its attributes) is not promised.

Not promised: any other attribute of the dashboard, the shape of its rows, and
the backend's methods. A screen receives `backend=app.backend` as its second
argument and should not call it; keep what a screen needs in the plugin.
"""
import asyncio
from pathlib import Path
from types import SimpleNamespace

from textual.pilot import WaitForScreenTimeout
from textual.widgets import DataTable, Label, Markdown, Static

from . import loader
from ..ui.app import MultiverseApp
from ..ui.backend import LocalBackend
from ..ui.plugin_service import PluginService
from ..ui.plugins_screen import PluginsScreen

TIMEOUT = 8.0


class _Service(PluginService):
    """The real plugin service, except that a test never starts the plugin host."""

    def host_state(self, root):
        return None


async def quiet(app, timeout=10.0):
    """Wait until `app` has nothing running: no scan, no action, no other worker. Leave a dashboard only then.

    Waiting for the workers that exist is not enough: an action ends by starting a scan, and the scan's thread, still
    running when a test's cleanups restore the real HOME, writes mv2's state there. Bounded, so a test that leaves
    a plugin action hanging on purpose is not held up for ever; the wait just ends."""
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while loop.time() < deadline:
        try:
            await asyncio.wait_for(app.workers.wait_for_complete(), max(0.01, deadline - loop.time()))
        except Exception:  # timed out, or a worker was cancelled: either way there is nothing more to wait for
            return
        await asyncio.sleep(0)  # a worker that has just ended may have started another
        if not app.scanning and all(worker.is_finished for worker in app.workers):
            return


class Dashboard:
    def __init__(self, case, size=(120, 40), root=None):
        self.case, self.size = case, size
        self.app = MultiverseApp(Path(root or case.root), LocalBackend(), refresh_interval=0, plugin_service=_Service())
        self._context = None
        self.pilot = None

    async def __aenter__(self):
        self._context = self.app.run_test(size=self.size)
        self.pilot = await self._context.__aenter__()
        await self.ready()
        return self

    async def __aexit__(self, *exception):
        await quiet(self.app)
        return await self._context.__aexit__(*exception)

    # Waiting -------------------------------------------------------------

    async def until(self, check, what, timeout=TIMEOUT):
        deadline = asyncio.get_running_loop().time() + timeout
        while True:
            await self.app.workers.wait_for_complete()
            if loader.generation() != self.app.registry_generation:
                # A plugin has finished loading since the palette was read, which waits for a plugin only briefly:
                # read it again, as the dashboard does on its next refresh, which this driver does not schedule.
                self.app.load_plugin_registry()
                await self.app.workers.wait_for_complete()
            try:
                if check():
                    return
            except Exception:  # a widget that is not there yet
                pass
            if asyncio.get_running_loop().time() > deadline:
                self.case.fail(f"timed out waiting for {what}")
            await self._pause(0.02)

    async def settle(self):
        for _ in range(3):
            await self._pause()
            await self.app.workers.wait_for_complete()

    async def _pause(self, delay=0.03):
        """Let the dashboard draw. A plugin screen that has just failed and is being closed never answers, so
        Textual's own wait is cut short rather than left to run for its 30 seconds."""
        try:
            await asyncio.wait_for(self.pilot.pause(delay), 2.0)
        except (asyncio.TimeoutError, WaitForScreenTimeout):
            pass

    async def ready(self):
        await self.until(lambda: self.app.snapshot is not None and not self.app.scanning and self.app.visible_rows,
                         "the dashboard's first refresh")
        await self.settle()

    async def refresh(self):
        before = self.app.scan_count
        self.app.refresh_snapshot()
        await self.until(lambda: self.app.scan_count > before and not self.app.scanning, "a refresh")
        await self.settle()

    async def press(self, *keys):
        await self.pilot.press(*keys)
        await self.settle()

    # What is on show -------------------------------------------------------

    @property
    def screen(self):
        return self.app.screen

    @property
    def root(self):
        return self.app.root

    @property
    def status(self):
        return str(self.app.query_one("#status", Static).render())

    @property
    def header(self):
        return str(self.app.query_one("#totals", Static).render())

    def table(self):
        table = self.app.query_one("#agents", DataTable)
        headers = [str(column.label) for column in table.columns.values()]
        return headers, [[getattr(cell, "plain", str(cell)) for cell in table.get_row_at(index)]
                         for index in range(table.row_count)]

    def local_view(self):
        found = []
        for widget in self.app.query_one("#local-view").query("Label, Static, Markdown"):
            text = widget.source if isinstance(widget, Markdown) else str(widget.render())
            if text and not (isinstance(widget, Static) and widget.parent is not None and isinstance(widget.parent, Markdown)):
                found.append(text)
        return found

    def palette(self):
        return [command.title for command in self.app.get_system_commands(self.app.screen)] + \
               [title for title, _, _ in self.app.plugin_palette()]

    # Doing what a person does ------------------------------------------------

    def select(self, name):
        for key, row in self.app.visible_rows.items():
            if row.get("name") == name:
                self.app.select_key(key)
                return
        self.case.fail(f"no agent or group named {name!r} is on show: {[r.get('name') for r in self.app.visible_rows.values()]}")

    async def open_plugins(self):
        await self.press("P")
        await self.until(lambda: isinstance(self.app.screen, PluginsScreen)
                         and self.app.screen.items and self.app.screen.query_one("#plugins-table", DataTable).row_count,
                         "the Plugins screen")
        return self.app.screen

    async def open_settings(self, name):
        plugins = await self.open_plugins()
        names = [item["name"] for item in plugins.items]
        if name not in names:
            self.case.fail(f"the Plugins screen does not list {name!r}: {names}")
        plugins.query_one("#plugins-table", DataTable).move_cursor(row=names.index(name), animate=False)
        await self.settle()
        await self.pilot.press("enter")
        await self.until(lambda: self.app.screen is not plugins or str(plugins.query_one(".cerror", Static).render()),
                         f"{name}'s settings screen")
        if self.app.screen is plugins:
            self.case.fail(str(plugins.query_one(".cerror", Static).render()))
        await self.settle()
        return self.app.screen

    async def close_screen(self):
        before = self.app.screen
        await self.pilot.press("escape")
        await self.until(lambda: self.app.screen is not before, "the screen to close")
        await self.settle()

    def entry(self, title):
        found = next((command for command in self.app.get_system_commands(self.app.screen) if command.title == title), None)
        return found or next((SimpleNamespace(callback=callback) for name, _, callback in self.app.plugin_palette()
                              if name == title), None)

    async def run_action(self, title):
        if self.entry(title) is None:  # a plugin still importing adds its entries once it has loaded
            try:
                await self.until(lambda: self.entry(title) is not None, f"the command palette to offer {title!r}")
            except AssertionError:
                pass
        entry = self.entry(title)
        if entry is None:
            self.case.fail(f"the command palette has no {title!r}: {self.palette()}")
        before, opens = self.app.screen, title in {f"{plugin}: {name}" for plugin, name in self.app.plugin_screens}
        entry.callback()
        if opens:
            await self.until(lambda: self.app.screen is not before or "failed" in self.status or "off in" in self.status,
                             f"{title} to open")
        await self.until(lambda: not self.app.operating, f"{title} to finish")
        await self.settle()
        return self.app.screen if self.app.screen is not before else None

    async def enter(self, name):
        self.select(name)
        before = self.app.root
        row = self.app.visible_rows[self.app.selected_key]
        self.case.assertEqual(row.get("kind"), "group", f"{name!r} is not a subverse")
        self.app.enter_scope(Path(row["path"]))
        await self.until(lambda: self.app.root != before and self.app.snapshot is not None and self.app.visible_rows,
                         f"{name}'s portal")
        await self.settle()

    async def back(self):
        before = self.app.root
        self.app.action_back()
        await self.until(lambda: self.app.root != before and self.app.snapshot is not None and self.app.visible_rows,
                         "the previous scope")
        await self.settle()
