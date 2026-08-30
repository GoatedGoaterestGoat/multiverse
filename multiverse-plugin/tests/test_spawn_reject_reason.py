"""Rejecting a spawn request can carry an explanation back to the agent.

Without one the requesting agent only learns "rejected", and its usual next
move is to ask again. The reason is what lets it adjust or drop the idea.
"""

from __future__ import annotations

import tempfile
import unittest
from unittest.mock import patch

from textual.app import App
from textual.widgets import Input

from src import monitor, server


REQUEST = {
    "id": "request-id",
    "from": "caller",
    "instruction": "Spin up a second indexer.",
    "default_name": "indexer-2",
    "caller_runtime": monitor.RUNTIME_CLAUDE,
}


class SpawnRejectWidgetTests(unittest.IsolatedAsyncioTestCase):
    async def _drive(self, actions) -> list:
        """Run the modal, apply ``actions(screen, pilot)``, return dismissals."""
        results: list = []
        with tempfile.TemporaryDirectory() as cwd, patch.object(
            monitor, "all_session_names_in_cwd", return_value=set()
        ):
            app = App()
            async with app.run_test() as pilot:
                screen = monitor.SpawnRequestScreen(cwd, REQUEST)
                app.push_screen(screen, results.append)
                await pilot.pause()
                await actions(screen, pilot)
                await pilot.pause()
        return results

    async def test_escape_sends_the_typed_reason(self) -> None:
        async def act(screen, pilot):
            screen.query_one("#spawn-reject-reason", Input).value = (
                "we already have an indexer running"
            )
            await pilot.press("escape")

        results = await self._drive(act)
        self.assertEqual(
            results,
            [
                monitor.SpawnDecision(
                    accepted=False, reason="we already have an indexer running"
                )
            ],
        )

    async def test_escape_with_no_reason_still_rejects(self) -> None:
        async def act(screen, pilot):
            await pilot.press("escape")

        results = await self._drive(act)
        self.assertEqual(results, [monitor.SpawnDecision(accepted=False, reason="")])

    async def test_reason_is_whitespace_trimmed(self) -> None:
        async def act(screen, pilot):
            screen.query_one("#spawn-reject-reason", Input).value = "   nope   "
            await pilot.press("escape")

        results = await self._drive(act)
        self.assertEqual(results[0].reason, "nope")

    async def test_enter_in_the_reason_box_rejects_rather_than_creating(self) -> None:
        """The footgun this guards: Enter is bound screen-wide with priority,
        so without the focus check, typing a rejection and hitting Enter would
        create the very agent you were declining."""

        async def act(screen, pilot):
            reason = screen.query_one("#spawn-reject-reason", Input)
            reason.value = "not now"
            reason.focus()
            await pilot.pause()
            await pilot.press("enter")

        results = await self._drive(act)
        self.assertEqual(len(results), 1)
        self.assertFalse(results[0].accepted)
        self.assertEqual(results[0].reason, "not now")

    async def test_enter_in_the_name_box_still_accepts(self) -> None:
        async def act(screen, pilot):
            screen.query_one("#spawn-name", Input).value = "indexer-2"
            screen.query_one("#spawn-reject-reason", Input).value = "ignored on accept"
            screen.query_one("#spawn-name", Input).focus()
            await pilot.pause()
            await pilot.press("enter")

        results = await self._drive(act)
        self.assertTrue(results[0].accepted)
        self.assertEqual(results[0].name, "indexer-2")


class SpawnRejectResolutionTests(unittest.IsolatedAsyncioTestCase):
    """What the monitor writes for each kind of dismissal."""

    async def _resolve(self, decision):
        callbacks: list = []
        with tempfile.TemporaryDirectory() as cwd, patch.dict(
            "os.environ", {"MULTIVERSE_ORIG_CWD": cwd}
        ):
            app = monitor.MonitorApp([])
            with (
                patch.object(
                    app,
                    "push_screen",
                    side_effect=lambda screen, callback: callbacks.append(callback),
                ),
                patch.object(app, "notify") as notify,
                patch.object(
                    monitor, "write_resolution", return_value=True
                ) as write_resolution,
            ):
                app._show_spawn_modal(REQUEST)
                callbacks[0](decision)
        return write_resolution, notify

    async def test_reason_reaches_the_resolution(self) -> None:
        write_resolution, notify = await self._resolve(
            monitor.SpawnDecision(accepted=False, reason="wrong workspace")
        )
        write_resolution.assert_called_once_with(
            "request-id", status="rejected", reason="wrong workspace"
        )
        self.assertIn("wrong workspace", str(notify.call_args))

    async def test_bare_rejection_keeps_the_cancelled_marker(self) -> None:
        """An unexplained Esc must read exactly as it did before this feature."""
        write_resolution, _ = await self._resolve(
            monitor.SpawnDecision(accepted=False, reason="")
        )
        write_resolution.assert_called_once_with(
            "request-id", status="rejected", reason="cancelled"
        )

    async def test_external_dismiss_is_still_cancelled(self) -> None:
        """None means another monitor resolved it — nobody here to explain."""
        write_resolution, _ = await self._resolve(None)
        write_resolution.assert_called_once_with(
            "request-id", status="rejected", reason="cancelled"
        )


class SpawnToolResultTests(unittest.TestCase):
    """What the requesting agent actually reads back."""

    def _reject_result(self, reason):
        fn = getattr(server.spawn_agent, "fn", server.spawn_agent)
        import asyncio

        with (
            patch.object(
                server, "_resolve_self_identity", return_value=("caller", "/tmp/ws")
            ),
            patch.object(server, "all_session_names_in_cwd", return_value=set()),
            patch.object(server, "pick_random_name", return_value="child"),
            patch.object(server, "write_spawn_request"),
            patch.object(server, "cleanup_request"),
            patch.object(server, "create_session") as create,
            patch.object(
                server,
                "read_resolution",
                return_value={"status": "rejected", "reason": reason},
            ),
        ):
            out = asyncio.run(fn("do a thing"))
        return out, create

    def test_user_reason_is_quoted_back_to_the_agent(self) -> None:
        out, create = self._reject_result("we already have an indexer running")
        create.assert_not_called()
        self.assertEqual(
            out,
            'Spawn request rejected by the user with the message '
            '"we already have an indexer running"\n'
            "No subagent was created.",
        )

    def test_cancelled_carries_no_message_clause(self) -> None:
        """The internal bare-Esc marker must not surface as a quoted message."""
        out, _ = self._reject_result("cancelled")
        self.assertEqual(
            out,
            "Spawn request rejected by the user\nNo subagent was created.",
        )
        self.assertNotIn("cancelled", out)
        self.assertNotIn("with the message", out)

    def test_missing_reason_carries_no_message_clause(self) -> None:
        out, _ = self._reject_result(None)
        self.assertEqual(
            out,
            "Spawn request rejected by the user\nNo subagent was created.",
        )

    def test_blank_reason_carries_no_message_clause(self) -> None:
        out, _ = self._reject_result("   ")
        self.assertNotIn("with the message", out)


if __name__ == "__main__":
    unittest.main()
