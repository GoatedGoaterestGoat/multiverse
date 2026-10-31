"""Views written before the dashboard's columns were renamed still show those columns."""
import asyncio
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from types import SimpleNamespace
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from multiverse.plugins.testing import PluginTestCase
from multiverse.runtime import native
from multiverse.ui import app as appmod
from multiverse.ui.backend import usage_identity, usage_key
from multiverse.ui.views import view
from multiverse.universe import templates
from isolation import isolate


class OldViewTests(unittest.TestCase):
    def setUp(self):
        isolate(self)

    def test_old_view_column_names_still_resolve(self):
        """A view written before the tokens-to-context and activity-to-status renames still loads those columns; a
        break goes unnoticed because views.view() silently drops unknown names, and only universes older than the
        rename have them."""
        with tempfile.TemporaryDirectory() as temp:
            root = templates.create(Path(temp) / ".multiverse", "top", workdir=Path(temp))
            (root / "views").mkdir(exist_ok=True)
            (root / "views" / "default.json").write_text(json.dumps({"columns": ["name", "tokens", "activity", "cost"]}))
            self.assertEqual(view(root)["columns"], ["name", "context", "status", "cost"])


def reading(tokens):
    """What lifecycle.row_usage gives for a Claude agent whose latest request was `tokens` of a 200k window."""
    usage = {"state": "ok", "value": {"context": {"tokens": tokens, "window": 200000}}}
    return {"usage": usage, "tokens": "-", "usage_detail": ""}


QUITS = """
import sys, threading
sys.path[:0] = [{src!r}, {examples!r}]
from tui_demo import DemoBackend
from multiverse.ui.app import MultiverseApp

class Slow(DemoBackend):
    def read_usage(self, rows):
        print("reading", flush=True)
        threading.Event().wait()  # a usage read that never lands, as a cold parse of every transcript, made endless

mounted = MultiverseApp.on_mount

def on_mount(self):  # not a subclass: Textual finds app.tcss next to the class's own file
    mounted(self)
    self.set_interval(0.05, lambda: self.usage_reading and self.action_quit())

MultiverseApp.on_mount = on_mount
backend = Slow()
MultiverseApp(backend.root, backend, refresh_interval=0).run(headless=True)
print("returned", flush=True)
"""


class UsageLaterTests(PluginTestCase, unittest.IsolatedAsyncioTestCase):
    def cells(self, d):
        """Each agent's Context cell, by name."""
        headers, rows = d.table()
        return {row[0].split()[-1]: row[headers.index("Context")] for row in rows if row[0].split()[-1] in ("ada", "bob")}

    async def test_the_agents_show_before_their_usage_and_nothing_waits_for_it(self):
        """The dashboard shows its agents before their usage is read, fills the cells when the read lands, never waits
        for it on a refresh, keeps the last values across refreshes and drops a read for a launch that has moved on;
        fragile because reading usage parses every Claude transcript in a new process (about 6.5 s here), so a scan
        that waits for it again leaves the dashboard empty for seconds, with nothing wrong in what it finally shows."""
        self.agent("ada")
        self.agent("bob")
        gate, calls = threading.Event(), []
        self.addCleanup(gate.set)

        def usage_snapshot(scope, properties, facts, reconciled=False):  # the slow read: every transcript, cold
            calls.append(properties["name"])
            gate.wait(30)
            return reading(82000)["usage"]

        with mock.patch.object(native, "usage_snapshot", usage_snapshot):
            async with self.dashboard() as d:
                with self.subTest("the agents show while their usage is still being read"):
                    await d.until(lambda: calls, "the usage read")
                    self.assertEqual(self.cells(d), {"ada": "…", "bob": "…"})
                with self.subTest("a refresh doesn't wait for it, and starts no second read"):
                    await d.refresh()
                    self.assertEqual((self.cells(d), len(calls)), ({"ada": "…", "bob": "…"}, 1))
                with self.subTest("the cells fill when it lands, and keep it while the next read runs"):
                    gate.set()
                    await d.until(lambda: self.cells(d) == {"ada": "82k/200k", "bob": "82k/200k"}, "the usage")
                    await d.until(lambda: not d.app.usage_reading, "the first read to finish")
                    gate.clear()  # the next read hangs: what shows meanwhile is the last one
                    await d.refresh()
                    await d.until(lambda: d.app.usage_reading, "the next read")
                    self.assertEqual(self.cells(d), {"ada": "82k/200k", "bob": "82k/200k"})
                with self.subTest("an agent whose binding fails shows that error, never its older reading"):
                    real = native.session_snapshot

                    def session_snapshot(scope, directory, properties, facts, *args, **kwargs):
                        if properties.get("name") == "bob":
                            raise ValueError("bound to another conversation")
                        return real(scope, directory, properties, facts, *args, **kwargs)

                    with mock.patch.object(native, "session_snapshot", session_snapshot):
                        await d.refresh()
                        bob = next(member for member in d.app.snapshot["agents"] if member["name"] == "bob")
                        self.assertIn("identity_error", bob)
                        self.assertEqual(self.cells(d)["bob"], "N/A")
                        d.app.apply_usage({usage_key(bob): (usage_identity(bob), reading(1000))})  # a read that was under way
                        await d.settle()
                        self.assertEqual(self.cells(d)["bob"], "N/A")
                    await d.refresh()
                with self.subTest("a read for a launch that has moved on is dropped"):
                    row = next(member for member in d.app.snapshot["agents"] if member["name"] == "ada")
                    then = (*usage_identity(row)[:2], "an earlier launch", *usage_identity(row)[3:])
                    d.app.apply_usage({usage_key(row): (then, reading(1000))})
                    await d.settle()
                    self.assertEqual(self.cells(d)["ada"], "82k/200k")

    async def test_the_refresh_timer_starts_no_listing_during_the_first_usage_read_for_at_most_its_bound(self):
        """While the first usage read of a dashboard runs, the refresh timer starts no listing, for at most
        FIRST_USAGE_PATIENCE, and F5 still refreshes at once; fragile because a listing started then competes with
        that read for the interpreter and quitting waits for it, which shows only as a slow quit on a busy machine."""
        self.agent("ada")
        gate, calls, listings, ticks = threading.Event(), [], [], []
        self.addCleanup(gate.set)
        clock = [100.0]  # the app's monotonic clock, moved by hand: the bound never depends on how busy the machine is

        def usage_snapshot(scope, properties, facts, reconciled=False):  # a first read that never lands
            calls.append(properties["name"])
            gate.wait(30)
            return reading(82000)["usage"]

        with mock.patch.object(native, "usage_snapshot", usage_snapshot), \
                mock.patch.object(appmod, "time", SimpleNamespace(monotonic=lambda: clock[0])):
            d = self.dashboard()
            d.app.refresh_interval = 0.2  # the driver runs without a timer; this test is about the timer
            listing, tick = d.app.backend.snapshot, d.app.timed_refresh

            def snapshot(root):
                listings.append(clock[0])
                return listing(root)

            def timed_refresh():
                ticks.append(clock[0])
                tick()

            d.app.backend.snapshot, d.app.timed_refresh = snapshot, timed_refresh  # before the timer is set up
            async with d:
                await d.until(lambda: calls, "the first usage read")
                self.assertEqual(d.app.first_usage_started, 100.0)
                with self.subTest("the timer starts no listing while the read runs"):
                    before, seen = len(listings), len(ticks)
                    await d.until(lambda: len(ticks) >= seen + 5, "five timer ticks")
                    self.assertEqual(len(listings), before)
                with self.subTest("F5 still refreshes at once"):
                    before = len(listings)
                    await d.press("f5")  # the clock stays inside the bound, so this listing can only be F5's
                    await d.until(lambda: len(listings) > before, "F5's listing")
                with self.subTest("past the bound, the timer starts listings again while the read still hangs"):
                    clock[0] += appmod.FIRST_USAGE_PATIENCE - 0.5
                    before, seen = len(listings), len(ticks)
                    await d.until(lambda: len(ticks) >= seen + 3, "three timer ticks just inside the bound")
                    self.assertEqual(len(listings), before)
                    clock[0] += 1.0
                    await d.until(lambda: len(listings) > before, "a timer listing after the bound")

    def test_quitting_never_waits_for_a_usage_read(self):
        """q ends the process while a usage read is still running, leaving that read behind; fragile because a read
        run where Python waits for it at exit (asyncio's executor, a non-daemon thread) makes every quit wait for a
        parse of every Claude transcript, seconds long, and nothing else says why."""
        root = Path(__file__).resolve().parents[1]
        script = self.work / "quits.py"  # a file, not -c: Textual reads an App subclass's source file
        script.write_text(QUITS.format(src=str(root / "src"), examples=str(root / "examples")))
        started = time.time()
        done = subprocess.run([sys.executable, str(script)], capture_output=True, text=True, timeout=60,
                              env={**os.environ, "PYTHONPATH": str(root / "src")})
        self.assertEqual((done.returncode, done.stdout.split()), (0, ["reading", "returned"]), done.stderr[-2000:])
        self.assertLess(time.time() - started, 30)


if __name__ == "__main__":
    unittest.main()
