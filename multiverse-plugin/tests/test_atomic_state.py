from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest.mock import patch

from src import atomic_files, compact_state, full_jsonl, sb_connector, spawn_requests


class AtomicFilePrimitiveTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp_dir.cleanup)
        self.root = Path(self.temp_dir.name)

    def _temporary_siblings(self, path: Path) -> list[Path]:
        prefix = f".{path.name}."
        return [
            item
            for item in path.parent.iterdir()
            if item.name.startswith(prefix) and item.name.endswith(".tmp")
        ]

    def test_racing_replacements_never_expose_partial_json(self) -> None:
        path = self.root / "status.json"
        atomic_files.atomic_write_json(path, {"writer": -1, "body": "seed"})
        stop = threading.Event()
        failures: list[BaseException | str] = []

        def read_repeatedly() -> None:
            while not stop.is_set():
                try:
                    value = json.loads(path.read_text())
                    if len(value["body"]) not in (4, 50_000):
                        failures.append("truncated payload")
                except BaseException as exc:  # noqa: BLE001 - collect thread failures
                    failures.append(exc)

        readers = [threading.Thread(target=read_repeatedly) for _ in range(4)]
        for reader in readers:
            reader.start()
        try:
            with ThreadPoolExecutor(max_workers=12) as pool:
                futures = [
                    pool.submit(
                        atomic_files.atomic_write_json,
                        path,
                        {"writer": index, "body": str(index % 10) * 50_000},
                    )
                    for index in range(80)
                ]
                for future in futures:
                    future.result()
        finally:
            stop.set()
            for reader in readers:
                reader.join()

        self.assertEqual(failures, [])
        self.assertEqual(len(json.loads(path.read_text())["body"]), 50_000)
        self.assertEqual(self._temporary_siblings(path), [])

    def test_atomic_create_has_exactly_one_winner(self) -> None:
        path = self.root / "resolution.json"
        with ThreadPoolExecutor(max_workers=24) as pool:
            results = list(
                pool.map(
                    lambda index: atomic_files.atomic_create_json(
                        path, {"winner": index, "body": "x" * 20_000}
                    ),
                    range(48),
                )
            )

        self.assertEqual(results.count(True), 1)
        self.assertEqual(len(json.loads(path.read_text())["body"]), 20_000)
        self.assertEqual(self._temporary_siblings(path), [])

    def test_failed_replace_preserves_previous_value_and_cleans_temp(self) -> None:
        path = self.root / "state.json"
        atomic_files.atomic_write_text(path, "before")

        with patch.object(atomic_files.os, "replace", side_effect=OSError("boom")):
            with self.assertRaisesRegex(OSError, "boom"):
                atomic_files.atomic_write_text(path, "after")

        self.assertEqual(path.read_text(), "before")
        self.assertEqual(self._temporary_siblings(path), [])

    def test_failed_first_writer_publication_cleans_temp(self) -> None:
        path = self.root / "claim.json"
        with patch.object(atomic_files.os, "link", side_effect=OSError("boom")):
            with self.assertRaisesRegex(OSError, "boom"):
                atomic_files.atomic_create_json(path, {"value": 1})

        self.assertFalse(path.exists())
        self.assertEqual(self._temporary_siblings(path), [])

    def test_locked_append_keeps_every_subprocess_record_parseable(self) -> None:
        path = self.root / "events.jsonl"
        code = (
            "import json,sys\n"
            "from pathlib import Path\n"
            "from src.atomic_files import append_text_locked\n"
            "p=Path(sys.argv[1]); worker=int(sys.argv[2])\n"
            "for i in range(40):\n"
            " append_text_locked(p, json.dumps({'worker':worker,'i':i,'body':'x'*8000})+'\\n')\n"
        )
        processes = [
            subprocess.Popen(
                [sys.executable, "-c", code, str(path), str(worker)],
                cwd=Path(__file__).resolve().parents[1],
            )
            for worker in range(8)
        ]
        for process in processes:
            self.assertEqual(process.wait(timeout=20), 0)

        rows = [json.loads(line) for line in path.read_text().splitlines()]
        self.assertEqual(len(rows), 320)
        self.assertEqual(
            {(row["worker"], row["i"]) for row in rows},
            {(worker, index) for worker in range(8) for index in range(40)},
        )

    def test_racing_claude_status_hooks_never_expose_truncated_json(self) -> None:
        path = self.root / "status.json"
        path.write_text(json.dumps({"status": "starting", "ts": 0}))
        script = Path(__file__).resolve().parents[1] / "scripts" / "set-status.sh"
        env = {**os.environ, "MULTIVERSE_STATUS": str(path)}
        stop = threading.Event()
        failures: list[BaseException] = []

        def read_repeatedly() -> None:
            while not stop.is_set():
                try:
                    json.loads(path.read_text())
                except BaseException as exc:  # noqa: BLE001 - collect thread failures
                    failures.append(exc)

        reader = threading.Thread(target=read_repeatedly)
        reader.start()
        try:
            processes = [
                subprocess.Popen(
                    [str(script), "working" if index % 2 else "idle"],
                    env=env,
                )
                for index in range(48)
            ]
            for process in processes:
                self.assertEqual(process.wait(timeout=20), 0)
        finally:
            stop.set()
            reader.join()

        self.assertEqual(failures, [])
        self.assertIn(json.loads(path.read_text())["status"], {"idle", "working"})
        self.assertEqual(
            [item for item in self.root.iterdir() if item.name.endswith(".tmp")],
            [],
        )


class SharedStateRaceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp_dir.cleanup)
        self.root = Path(self.temp_dir.name)

    def test_spawn_resolution_has_one_complete_winner(self) -> None:
        request_dir = self.root / "spawn_requests"
        with (
            patch.object(spawn_requests, "SPAWN_REQUESTS_DIR", request_dir),
            patch.object(spawn_requests.sb_notify, "record_spawn_decision"),
            ThreadPoolExecutor(max_workers=24) as pool,
        ):
            results = list(
                pool.map(
                    lambda index: spawn_requests.write_resolution(
                        "request-id",
                        status="accepted",
                        name=f"agent-{index}",
                        runtime="codex",
                    ),
                    range(48),
                )
            )

        self.assertEqual(results.count(True), 1)
        resolution = json.loads(
            (request_dir / "request-id.resolved.json").read_text()
        )
        self.assertEqual(resolution["status"], "accepted")
        self.assertEqual(resolution["runtime"], "codex")
        self.assertTrue(resolution["name"].startswith("agent-"))

    def test_racing_spawn_request_replacements_remain_complete(self) -> None:
        request_dir = self.root / "spawn_requests"
        with (
            patch.object(spawn_requests, "SPAWN_REQUESTS_DIR", request_dir),
            patch.object(spawn_requests.sb_notify, "record_spawn_request"),
            ThreadPoolExecutor(max_workers=24) as pool,
        ):
            futures = [
                pool.submit(
                    spawn_requests.write_spawn_request,
                    "request-id",
                    f"caller-{index}",
                    str(self.root),
                    "x" * 20_000,
                    f"agent-{index}",
                    f"tmux-{index}",
                    "codex",
                )
                for index in range(48)
            ]
            for future in futures:
                future.result()

        request = json.loads((request_dir / "request-id.json").read_text())
        self.assertEqual(request["id"], "request-id")
        self.assertEqual(request["instruction"], "x" * 20_000)
        self.assertEqual(request["caller_runtime"], "codex")
        self.assertEqual(
            [path for path in request_dir.iterdir() if path.name.endswith(".tmp")],
            [],
        )

    def test_compaction_claim_has_one_live_winner(self) -> None:
        state_root = self.root / "state"
        with patch.dict(
            os.environ, {"MULTIVERSE_ROOT": str(state_root)}, clear=False
        ):
            with ThreadPoolExecutor(max_workers=20) as pool:
                results = list(
                    pool.map(
                        lambda _: compact_state.acquire_lock("claude-id"),
                        range(40),
                    )
                )
            self.assertEqual(results.count(True), 1)
            self.assertIsNotNone(compact_state.read_lock("claude-id"))
            compact_state.release_lock("claude-id")

    def test_concurrent_full_sync_does_not_duplicate_rows(self) -> None:
        state_root = self.root / "state"
        claude_root = self.root / "claude"
        cwd = str(self.root / "workspace")
        session_id = "session-id"
        live = (
            claude_root
            / "projects"
            / full_jsonl.cwd_slug(cwd)
            / f"{session_id}.jsonl"
        )
        live.parent.mkdir(parents=True)
        live.write_text(
            "".join(
                json.dumps(
                    {
                        "uuid": f"row-{index}",
                        "parentUuid": f"row-{index - 1}" if index else None,
                    }
                )
                + "\n"
                for index in range(120)
            )
        )
        env = patch.dict(
            os.environ,
            {
                "MULTIVERSE_ROOT": str(state_root),
                "CLAUDE_CONFIG_DIR": str(claude_root),
            },
            clear=False,
        )
        env.start()
        self.addCleanup(env.stop)

        with ThreadPoolExecutor(max_workers=16) as pool:
            results = list(
                pool.map(
                    lambda _: full_jsonl.sync_full(session_id, cwd), range(32)
                )
            )

        rows = [
            json.loads(line)
            for line in (state_root / "full_jsonls" / f"{session_id}.jsonl")
            .read_text()
            .splitlines()
        ]
        self.assertEqual([row["uuid"] for row in rows], [f"row-{i}" for i in range(120)])
        self.assertEqual(sum(results), 120)
        self.assertEqual(
            (state_root / "full_jsonls" / f"{session_id}.marker").read_text(),
            "row-119",
        )

    def test_concurrent_device_key_creation_returns_one_identity(self) -> None:
        key_path = self.root / "device.key"
        with ThreadPoolExecutor(max_workers=16) as pool:
            keys = list(
                pool.map(
                    lambda _: sb_connector.load_or_create_device_key(key_path)
                    .private_bytes_raw(),
                    range(32),
                )
            )

        self.assertEqual(len(set(keys)), 1)
        self.assertEqual(key_path.read_bytes(), keys[0])
        self.assertEqual(key_path.stat().st_mode & 0o777, 0o600)

    def test_secondbrain_connector_has_one_subprocess_lock_winner(self) -> None:
        state_root = self.root / "state"
        ready_dir = self.root / "ready"
        result_dir = self.root / "results"
        ready_dir.mkdir()
        result_dir.mkdir()
        start = self.root / "start"
        release = self.root / "release"
        code = (
            "import os,sys,time\n"
            "from pathlib import Path\n"
            "from src import sb_connector as c\n"
            "ready,result,start,release=map(Path,sys.argv[1:])\n"
            "ready.write_text('ready')\n"
            "while not start.exists(): time.sleep(.005)\n"
            "won=c.acquire_lock()\n"
            "result.write_text('1' if won else '0')\n"
            "if won:\n"
            " while not release.exists(): time.sleep(.005)\n"
            " c.release_lock()\n"
        )
        env = {
            **os.environ,
            "MULTIVERSE_ROOT": str(state_root),
        }
        processes = [
            subprocess.Popen(
                [
                    sys.executable,
                    "-c",
                    code,
                    str(ready_dir / str(index)),
                    str(result_dir / str(index)),
                    str(start),
                    str(release),
                ],
                cwd=Path(__file__).resolve().parents[1],
                env=env,
            )
            for index in range(8)
        ]

        def stop_workers() -> None:
            release.touch(exist_ok=True)
            for process in processes:
                if process.poll() is None:
                    process.terminate()
                try:
                    process.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait(timeout=5)

        self.addCleanup(stop_workers)
        deadline = time.monotonic() + 20
        while len(list(ready_dir.iterdir())) < len(processes):
            self.assertLess(time.monotonic(), deadline, "workers did not become ready")
            time.sleep(0.01)
        start.touch()
        while len(list(result_dir.iterdir())) < len(processes):
            self.assertLess(time.monotonic(), deadline, "workers did not resolve lock")
            time.sleep(0.01)

        self.assertEqual(
            sum(int(path.read_text()) for path in result_dir.iterdir()), 1
        )
        claim = state_root / "secondbrain" / "connector.lock"
        self.assertTrue(claim.is_symlink())
        self.assertIn("token", json.loads(claim.read_text()))
        release.touch()
        for process in processes:
            self.assertEqual(process.wait(timeout=20), 0)
        self.assertFalse(claim.exists())
        self.assertEqual(
            list((state_root / "secondbrain").glob(".connector-lock-*.json")),
            [],
        )


if __name__ == "__main__":
    unittest.main()
