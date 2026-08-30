from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import call, patch

from src import codex_startup_input


class CodexStartupInputTests(unittest.TestCase):
    def test_names_live_tui_before_delivering_initial_prompt(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            prompt_path = root / "initial-prompt.txt"
            prompt_path.write_text("Do the spawned work.")
            native_id_path = root / "runtime-session-id"
            native_id_path.write_text("thread-id\n")

            with (
                patch.object(
                    codex_startup_input,
                    "_send_startup_input",
                    side_effect=[(True, ""), (True, "")],
                ) as send,
                patch.object(
                    codex_startup_input,
                    "_wait_for_empty_composer",
                    return_value=(True, ""),
                ),
                patch.object(
                    codex_startup_input,
                    "_wait_for_native_name",
                    return_value=True,
                ) as named,
            ):
                errors = codex_startup_input.deliver_startup_input(
                    "multiverse-wrapper",
                    "named-child",
                    prompt_path,
                    native_id_path,
                    root / "codex-home",
                )
            prompt_exists = prompt_path.exists()

        self.assertEqual(errors, [])
        self.assertEqual(
            send.call_args_list,
            [
                call(
                    "multiverse-wrapper",
                    "/rename named-child",
                    composer_ready=True,
                ),
                call(
                    "multiverse-wrapper",
                    "Do the spawned work.",
                    allow_opaque=True,
                ),
            ],
        )
        named.assert_called_once_with(
            native_id_path,
            root / "codex-home" / "session_index.jsonl",
            "named-child",
            index_offset=0,
        )
        self.assertFalse(prompt_exists)

    def test_prompt_is_still_delivered_when_rename_ack_fails(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            prompt_path = root / "initial-prompt.txt"
            prompt_path.write_text("Keep working.")
            native_id_path = root / "runtime-session-id"
            native_id_path.write_text("thread-id\n")
            with (
                patch.object(
                    codex_startup_input,
                    "_send_startup_input",
                    side_effect=[(True, ""), (True, "")],
                ),
                patch.object(
                    codex_startup_input,
                    "_wait_for_empty_composer",
                    return_value=(True, ""),
                ),
                patch.object(
                    codex_startup_input,
                    "_wait_for_native_name",
                    return_value=False,
                ),
            ):
                errors = codex_startup_input.deliver_startup_input(
                    "multiverse-wrapper",
                    "named-child",
                    prompt_path,
                    native_id_path,
                    root / "codex-home",
                )
            prompt_exists = prompt_path.exists()

        self.assertEqual(errors, ["native rename was not acknowledged"])
        self.assertFalse(prompt_exists)

    def test_records_visible_thread_id_before_first_turn_hook(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            native_id_path = Path(raw) / "runtime-session-id"
            pane = (
                "› Try something\n\n"
                "  gpt-5.6-sol xhigh · Approve for me · Context 0% used · "
                "01a021d1-5d8d-7bc2-9738-0b195f9efd67\n"
            )
            with patch.object(
                codex_startup_input,
                "capture_pane",
                return_value=(True, pane),
            ):
                recorded = codex_startup_input._record_visible_native_id(
                    "multiverse-wrapper",
                    native_id_path,
                )

            value = native_id_path.read_text()

        self.assertTrue(recorded)
        self.assertEqual(value, "01a021d1-5d8d-7bc2-9738-0b195f9efd67\n")

    def test_opaque_paste_is_accepted_only_when_character_count_matches(self) -> None:
        with patch.object(
            codex_startup_input,
            "capture_focused_codex_composer_draft",
            return_value=(
                False,
                "[Pasted Content 13 chars]",
                "Codex input contains pasted content",
            ),
        ):
            matching = codex_startup_input._loaded_prompt_state(
                "multiverse-wrapper", "twelve chars!", allow_opaque=True
            )
            mismatched = codex_startup_input._loaded_prompt_state(
                "multiverse-wrapper", "short", allow_opaque=True
            )

        self.assertEqual(matching, ("opaque", ""))
        self.assertEqual(mismatched[0], "unavailable")

    def test_submit_retries_enter_until_exact_prompt_clears(self) -> None:
        states = iter(
            [
                ("exact", ""),
                ("empty", ""),
            ]
        )
        with (
            patch.object(
                codex_startup_input,
                "_run_tmux",
                return_value=(True, ""),
            ) as run,
            patch.object(
                codex_startup_input,
                "_loaded_prompt_state",
                side_effect=lambda *args, **kwargs: next(states),
            ),
            patch.object(codex_startup_input.time, "sleep"),
            patch.object(
                codex_startup_input.time,
                "monotonic",
                side_effect=[0.0, 0.0, 1.0, 1.0, 1.0],
            ),
        ):
            submitted = codex_startup_input._submit_loaded_prompt(
                "multiverse-wrapper", "prompt", allow_opaque=False
            )

        self.assertEqual(submitted, (True, ""))
        self.assertEqual(run.call_count, 2)
        run.assert_has_calls(
            [
                call(["send-keys", "-t", "multiverse-wrapper", "Enter"]),
                call(["send-keys", "-t", "multiverse-wrapper", "Enter"]),
            ]
        )

    def test_wait_for_native_name_uses_latest_index_record(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            native_id_path = root / "runtime-session-id"
            index_path = root / "session_index.jsonl"
            native_id_path.write_text("thread-id\n")
            index_path.write_text(
                json.dumps({"id": "thread-id", "thread_name": "old"})
                + "\n"
                + json.dumps({"id": "thread-id", "thread_name": "named-child"})
                + "\n"
            )

            self.assertTrue(
                codex_startup_input._wait_for_native_name(
                    native_id_path, index_path, "named-child"
                )
            )

    def test_wait_for_native_name_uses_only_new_index_rows_as_id_fallback(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            native_id_path = root / "runtime-session-id"
            index_path = root / "session_index.jsonl"
            old = json.dumps(
                {"id": "old-thread", "thread_name": "named-child"}
            ) + "\n"
            index_path.write_text(old)
            offset = index_path.stat().st_size
            with index_path.open("a") as handle:
                handle.write(
                    json.dumps(
                        {"id": "new-thread", "thread_name": "named-child"}
                    )
                    + "\n"
                )

            self.assertTrue(
                codex_startup_input._wait_for_native_name(
                    native_id_path,
                    index_path,
                    "named-child",
                    index_offset=offset,
                )
            )
            recorded_id = native_id_path.read_text()

        self.assertEqual(recorded_id, "new-thread\n")


if __name__ == "__main__":
    unittest.main()
