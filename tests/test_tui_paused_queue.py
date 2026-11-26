"""The inspector's paused-queue line, from core's listing row."""
import unittest
from unittest import mock
from uuid import uuid4

from textual.widgets import Static

from multiverse.plugins.testing import PluginTestCase
from multiverse.runtime import lifecycle, native
from multiverse.universe import mailbox

PAUSED = "Queue paused (1 message)"
RESUME = ": send a new steer or follow-up, or type a terminal prompt, to resume."


class PausedQueueTests(PluginTestCase, unittest.IsolatedAsyncioTestCase):
    def paused(self, name, harness, queue, queued):
        """An agent whose native queue is paused, with `queued` accepted follow-ups behind it at its launch."""
        conversation, launch = str(uuid4()), str(uuid4())
        bridged = {"native_mode": {"mode": "bridge", "policy": "bridge"},  # the routine lines a cut must take first
                   "native_versions": {"cli": "1.5.14", "daemon": "1.5.14"}} if harness == "agy" else {}
        directory = self.agent(name, harness, native_id=conversation, runtime={
            "launch_id": launch, **bridged, "queue_state": {"native_session_id": conversation, "state": "paused",
                                                            **({"launch_id": launch} if harness == "agy" else {}),
                                                            **queue}})
        for _ in range(queued):
            message = mailbox.append_message(directory, "saved input", {}, delivery="follow-up",
                                             target_session=conversation)
            mailbox.update_message(directory, message["id"], submission_state="accepted",
                                   attempts=[{"launch_id": launch, "native_session_id": conversation}],
                                   native_receipt={"queued": True})

    async def test_the_inspector_says_a_queue_is_paused_as_mv2_inspect_does(self):
        """p on an agent whose native queue paused with accepted messages behind it says so in mv2 inspect's words,
        for any harness, and an Esc with nothing queued says nothing. Fragile because the count comes from core's
        listing row and the inspector draws only the facts it's given, so the pause showed in mv2 list and inspect
        but not here (BUG-DASHBOARD-026)."""
        self.paused("codexer", "codex", {}, 1)
        self.paused("agyer", "agy", {"reason": "cancel"}, 1)
        self.paused("quiet", "agy", {"reason": "cancel"}, 0)
        expected = {"codexer": PAUSED + RESUME, "agyer": PAUSED + " after turn cancellation" + RESUME, "quiet": None}
        # The records' terminals don't exist, so they stand in as live; the dashboard must never ask the harness.
        with mock.patch.object(lifecycle, "observe", return_value={"live": "alive"}), \
                mock.patch.object(lifecycle, "terminal_action", return_value=""), \
                mock.patch.object(native, "deliver", side_effect=AssertionError("the dashboard queried delivery")):
            async with self.dashboard(size=(140, 36)) as d:
                for name, line in expected.items():
                    with self.subTest(name):
                        d.select(name)
                        await d.settle()
                        if not d.app.has_class("inspecting"):
                            await d.press("p")
                        await d.until(lambda: name in str(d.app.query_one("#inspect-name", Static).render()),
                                      f"the inspector to show {name}")
                        state = str(d.app.query_one("#inspect-state", Static).render())
                        if line is None:
                            self.assertNotIn("Queue paused", state)
                        else:
                            self.assertIn(line, " ".join(state.split()))
            with self.subTest("it fits at 60x20"):
                async with self.dashboard(size=(60, 20)) as d:
                    d.select("agyer")
                    await d.press("p")
                    await d.until(lambda: "agyer" in str(d.app.query_one("#inspect-name", Static).render()),
                                  "the inspector to show agyer")
                    frame = " ".join(strip.text for strip in d.app.screen._compositor.render_strips())
                    self.assertIn("after turn cancellation", " ".join(frame.split()))


if __name__ == "__main__":
    unittest.main()
