"""``claude_statusline``: record the exact capacity, relay the user's statusline.

Covers the recorder (what gets stored, where, and what is refused), the
relay (the user's bytes, stdout and exit status are theirs), effective
settings resolution (Claude's source precedence, deep merge, ``--settings``,
``--setting-sources``, ``CLAUDE_CONFIG_DIR``, git-root local settings), and
the ``launch``/``render`` script end to end — including the fail-open paths
where instrumentation cannot be configured and Claude must start exactly as
it always did.
"""

from __future__ import annotations

import base64
import json
import os
import shlex
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from src import claude_statusline as cs
from src import claude_usage
from src.full_jsonl import cwd_slug

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "claude-statusline.py"
SESSION = "11111111-2222-3333-4444-555555555555"


def _payload(session_id: str = SESSION, size: object = 1_000_000, *, cwd: str, transcript: bool = True, **extra) -> dict:
    payload = {
        "hook_event_name": "Status",
        "session_id": session_id,
        "cwd": cwd,
        "model": {"id": "claude-opus-5", "display_name": "Opus 5 (1M context)"},
        "workspace": {"current_dir": cwd, "project_dir": cwd},
        "version": "2.1.263",
        "cost": {"total_cost_usd": 1.25},
        "rate_limits": {"seven_day": {"used_percentage": 10}},
        "context_window": {
            "total_input_tokens": 10, "total_output_tokens": 2,
            "context_window_size": size, "used_percentage": 1, "remaining_percentage": 99,
        },
    }
    if transcript:
        payload["transcript_path"] = f"/home/x/.claude/projects/{cwd_slug(cwd)}/{session_id}.jsonl"
    payload.update(extra)
    return payload


class StatuslineTestCase(unittest.TestCase):
    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory(prefix="mv statusline ")
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        self.workspace = self.root / "work space"
        self.workspace.mkdir()
        self.config = self.root / "cfg"
        self.config.mkdir()
        self.managed = self.root / "managed-settings.json"
        self.real_managed = cs.managed_settings_path()
        env = patch.dict(os.environ, {
            "MULTIVERSE_ROOT": str(self.root), "CLAUDE_CONFIG_DIR": str(self.config),
        })
        env.start()
        self.addCleanup(env.stop)
        for key in ("MULTIVERSE_STATUS", "MULTIVERSE_SESSION_CWD"):
            os.environ.pop(key, None)
        managed = patch.object(cs, "managed_settings_path", lambda: self.managed)
        managed.start()
        self.addCleanup(managed.stop)
        claude_usage._CACHE.clear()
        self.addCleanup(claude_usage._CACHE.clear)

    def payload(self, **kw) -> dict:
        kw.setdefault("cwd", str(self.workspace))
        return _payload(**kw)

    def write_json(self, path: Path, value: object) -> Path:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(value))
        return path

    def subprocess_env(self, **extra: str) -> dict[str, str]:
        env = {k: v for k, v in os.environ.items() if not k.startswith("MULTIVERSE_")}
        env.update({
            "MULTIVERSE_ROOT": str(self.root), "CLAUDE_CONFIG_DIR": str(self.config),
        })
        env.update(extra)
        return env


class RecordTests(StatuslineTestCase):
    def test_records_only_dashboard_telemetry_under_the_transcript_slug(self) -> None:
        path = cs.record(json.dumps(self.payload()).encode(), now=123.5)
        self.assertEqual(path, cs.record_path(SESSION, str(self.workspace)))
        self.assertEqual(path.parent.name, cwd_slug(str(self.workspace)))
        entry = json.loads(path.read_text())
        self.assertEqual(entry, {
            "recorded_at": 123.5,
            "session_id": SESSION,
            "workspace_slug": cwd_slug(str(self.workspace)),
            "transcript_path": self.payload()["transcript_path"],
            "cwd": str(self.workspace),
            "model_id": "claude-opus-5",
            "context_window_size": 1_000_000,
        })
        self.assertNotIn("cost", entry)
        self.assertNotIn("rate_limits", entry)
        self.assertEqual(oct(path.stat().st_mode & 0o777), oct(0o600))

    def test_transcript_slug_wins_over_the_sessions_current_directory(self) -> None:
        moved = self.payload(cwd=str(self.workspace / "sub" / "dir"))
        moved["transcript_path"] = self.payload()["transcript_path"]
        moved["workspace"] = {"current_dir": moved["cwd"], "project_dir": str(self.workspace)}
        path = cs.record(json.dumps(moved).encode())
        self.assertEqual(path, cs.record_path(SESSION, str(self.workspace)))

    def test_fallback_identity_order_without_a_transcript_path(self) -> None:
        # workspace.project_dir, then MULTIVERSE_SESSION_CWD, then cwd.
        payload = self.payload(transcript=False, cwd="/moved/elsewhere")
        payload["workspace"] = {"current_dir": "/moved/elsewhere", "project_dir": str(self.workspace)}
        self.assertEqual(
            cs.record(json.dumps(payload).encode()),
            cs.record_path(SESSION, str(self.workspace)),
        )
        del payload["workspace"]
        with patch.dict(os.environ, {"MULTIVERSE_SESSION_CWD": "/launched/here"}):
            self.assertEqual(
                cs.record(json.dumps(payload).encode()),
                cs.record_path(SESSION, "/launched/here"),
            )
        self.assertEqual(
            cs.record(json.dumps(payload).encode()),
            cs.record_path(SESSION, "/moved/elsewhere"),
        )
        del payload["cwd"]
        self.assertIsNone(cs.record(json.dumps(payload).encode()))

    def test_refuses_traversal_and_unknown_capacity(self) -> None:
        for session_id in ("../escape", "..", ".", "a/b", "id\x00x", "", "x" * 201, SESSION + "\n", "\n" + SESSION):
            self.assertIsNone(cs.record(json.dumps(self.payload(session_id=session_id)).encode()), session_id)
        escaping = self.payload()
        escaping["transcript_path"] = "/x/projects/../passwd.jsonl"
        escaping["cwd"] = "/moved/elsewhere"
        # An unusable transcript parent (here ``..``) falls through to the
        # project dir rather than to a path outside the store.
        path = cs.record(json.dumps(escaping).encode())
        self.assertEqual(path, cs.record_path(SESSION, str(self.workspace)))
        for size in (None, 0, -1, True, "1000000", 1e6):
            self.assertIsNone(cs.record(json.dumps(self.payload(size=size)).encode()), size)
        missing = self.payload()
        del missing["context_window"]
        self.assertIsNone(cs.record(json.dumps(missing).encode()))
        self.assertIsNone(cs.record(b"not json"))
        self.assertIsNone(cs.record(b"[1, 2]"))
        self.assertIsNone(cs.record(b"\xff"))
        stored = sorted(p.name for p in (self.root / "claude_statusline").rglob("*.json"))
        self.assertEqual(stored, [f"{SESSION}.json"])

    def test_known_capacity_is_never_replaced_by_unknown(self) -> None:
        cs.record(json.dumps(self.payload()).encode())
        gone = self.payload(size=None)
        self.assertIsNone(cs.record(json.dumps(gone).encode()))
        self.assertEqual(claude_usage.context_window_cached(SESSION, str(self.workspace)), 1_000_000)


class RelayTests(StatuslineTestCase):
    def render(self, stdin: bytes, command: str | None) -> subprocess.CompletedProcess:
        args = [sys.executable, str(SCRIPT), "render"]
        if command is not None:
            args += ["--relay-b64", base64.b64encode(command.encode()).decode()]
        return subprocess.run(
            args, input=stdin, capture_output=True, env=self.subprocess_env(),
            cwd=self.workspace, timeout=30, check=False,
        )

    def test_relays_bytes_stdout_stderr_and_exit_status(self) -> None:
        raw = json.dumps(self.payload()).encode() + b"\n"
        result = self.render(raw, "cat; printf ' | tail'; echo oops >&2; exit 3")
        self.assertEqual(result.returncode, 3)
        self.assertEqual(result.stdout, raw + b" | tail")
        self.assertEqual(result.stderr, b"oops\n")
        self.assertEqual(claude_usage.context_window_cached(SESSION, str(self.workspace)), 1_000_000)

    def test_no_original_command_prints_nothing(self) -> None:
        result = self.render(json.dumps(self.payload()).encode(), None)
        self.assertEqual((result.returncode, result.stdout, result.stderr), (0, b"", b""))
        self.assertEqual(claude_usage.context_window_cached(SESSION, str(self.workspace)), 1_000_000)

    def test_bad_payload_or_unwritable_store_still_relays(self) -> None:
        result = self.render(b"not json", "cat")
        self.assertEqual((result.returncode, result.stdout), (0, b"not json"))
        blocker = self.root / "blocker"
        blocker.write_text("not a directory")
        with patch.dict(os.environ, {"MULTIVERSE_ROOT": str(blocker)}):
            result = self.render(json.dumps(self.payload()).encode(), "printf relayed")
        self.assertEqual((result.returncode, result.stdout), (0, b"relayed"))

    def test_render_command_survives_the_shell_and_spaces(self) -> None:
        script = self.root / "plug in" / "claude-statusline.py"
        command = cs.render_command(script, "bash ~/.claude/status line.sh", python="/opt/py thon/bin/python3")
        parts = shlex.split(command)
        self.assertEqual(parts[:3], ["/opt/py thon/bin/python3", str(script), "render"])
        self.assertEqual(parts[3], "--relay-b64")
        self.assertEqual(base64.b64decode(parts[4]).decode(), "bash ~/.claude/status line.sh")
        self.assertEqual(shlex.split(cs.render_command(script, None))[2:], ["render"])
        self.assertEqual(cs._decode_relay("!!!"), None)
        self.assertEqual(cs._decode_relay(""), None)

    def test_import_failure_still_relays_and_launches(self) -> None:
        # A copy of the entry script whose ``src`` package is broken (a
        # half-updated checkout, a dependency the fallback interpreter lacks)
        # cannot import the recorder; it must still behave as a statusline
        # and as a launcher.
        orphan = self.root / "orphan" / "scripts" / "claude-statusline.py"
        orphan.parent.mkdir(parents=True)
        shutil.copy(SCRIPT, orphan)
        broken = self.root / "orphan" / "src"
        broken.mkdir()
        (broken / "__init__.py").write_text("")
        (broken / "claude_statusline.py").write_text("raise ImportError('simulated breakage')\n")
        env = self.subprocess_env()
        env.pop("PYTHONPATH", None)
        result = subprocess.run(
            [sys.executable, str(orphan), "render", "--relay-b64",
             base64.b64encode(b"cat; exit 4").decode()],
            input=b"payload", capture_output=True, env=env, timeout=30, check=False,
        )
        self.assertEqual((result.returncode, result.stdout), (4, b"payload"))
        fake, output = self.fake_claude()
        result = subprocess.run(
            [sys.executable, str(orphan), "launch", str(fake), "--resume", "u", "prompt"],
            capture_output=True, env={**env, "ARGV_OUTPUT": str(output)}, timeout=30, check=False,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(output.read_text())["args"], ["--resume", "u", "prompt"])

    def fake_claude(self) -> tuple[Path, Path]:
        fake = self.root / "fake claude"
        output = self.root / "argv.json"
        fake.write_text(
            "#!/usr/bin/env python3\n"
            "import json, os, sys\n"
            "from pathlib import Path\n"
            "Path(os.environ['ARGV_OUTPUT']).write_text(json.dumps({'args': sys.argv[1:]}))\n"
        )
        fake.chmod(0o755)
        return fake, output


class SettingsTests(StatuslineTestCase):
    def user(self, value: object) -> None:
        self.write_json(self.config / "settings.json", value)

    def project(self, value: object) -> None:
        self.write_json(self.workspace / ".claude" / "settings.json", value)

    def local(self, value: object) -> None:
        self.write_json(self.workspace / ".claude" / "settings.local.json", value)

    def effective(self, *args: str) -> object:
        return cs.effective_statusline(str(self.workspace), list(args))

    def test_layers_deep_merge_in_claudes_precedence(self) -> None:
        self.assertIsNone(self.effective())
        self.user({"statusLine": {"type": "command", "command": "user-cmd", "padding": 1}})
        self.project({"statusLine": {"padding": 0}})
        self.local({"statusLine": {"refreshInterval": 5}})
        self.assertEqual(self.effective(), {
            "type": "command", "command": "user-cmd", "padding": 0, "refreshInterval": 5,
        })
        self.local({"statusLine": {"command": "local-cmd"}})
        self.assertEqual(self.effective()["command"], "local-cmd")
        self.assertEqual(
            self.effective("--settings", '{"statusLine": {"command": "flag-cmd"}}')["command"],
            "flag-cmd",
        )
        self.write_json(self.managed, {"statusLine": {"command": "policy-cmd"}})
        self.assertEqual(self.effective("--settings", '{"statusLine": {"command": "flag-cmd"}}')["command"], "policy-cmd")

    def test_setting_sources_prunes_user_project_and_local(self) -> None:
        self.user({"statusLine": {"type": "command", "command": "user-cmd"}})
        self.project({"statusLine": {"padding": 0}})
        self.assertEqual(self.effective("--setting-sources", "project"), {"padding": 0})
        self.assertEqual(self.effective("--setting-sources=user")["command"], "user-cmd")
        self.assertIsNone(self.effective("--setting-sources", "local"))
        # Explicit --settings always applies.
        self.assertEqual(
            self.effective("--setting-sources", "local", "--settings", '{"statusLine": {"command": "f"}}'),
            {"command": "f"},
        )

    def test_explicit_settings_inline_relative_file_and_equals_form(self) -> None:
        self.write_json(self.workspace / "extra.json", {"statusLine": {"command": "file-cmd"}})
        self.assertEqual(self.effective("--settings", "extra.json")["command"], "file-cmd")
        self.assertEqual(self.effective("--settings=extra.json")["command"], "file-cmd")
        self.assertEqual(self.effective("--settings", str(self.workspace / "extra.json"))["command"], "file-cmd")
        # Positional text after ``--`` is a prompt, never an option.
        self.user({"statusLine": {"command": "user-cmd"}})
        self.assertEqual(self.effective("--", "--settings=extra.json")["command"], "user-cmd")
        self.assertEqual(self.effective("--", "--settings", "extra.json")["command"], "user-cmd")
        with self.assertRaises(ValueError):
            self.effective("--settings", "missing.json")
        with self.assertRaises(ValueError):
            self.effective("--settings", "[1]")
        with self.assertRaises(ValueError):
            self.effective("--settings", "{not json")

    def test_local_settings_come_from_the_git_root_of_a_subdirectory(self) -> None:
        if shutil.which("git") is None:
            self.skipTest("git not available")
        repo = self.root / "repo"
        sub = repo / "pkg" / "inner"
        sub.mkdir(parents=True)
        init = subprocess.run(["git", "init", "-q", str(repo)], capture_output=True, check=False)
        if init.returncode != 0:
            self.skipTest(f"git init failed: {init.stderr!r}")
        self.write_json(repo / ".claude" / "settings.local.json", {"statusLine": {"command": "root-local"}})
        self.write_json(repo / ".claude" / "settings.json", {"statusLine": {"padding": 3}})
        merged = cs.effective_statusline(str(sub))
        # Project settings are read from the cwd itself, local from the root.
        self.assertEqual(merged, {"command": "root-local"})
        self.assertEqual(cs.effective_statusline(str(repo)), {"command": "root-local", "padding": 3})
        with patch.object(Path, "home", lambda: repo):
            # Claude never treats $HOME as a project root.
            self.assertIsNone(cs.effective_statusline(str(sub)))

    def test_instrumented_settings_wraps_the_command_and_keeps_everything_else(self) -> None:
        self.user({"statusLine": {"type": "command", "command": "user-cmd", "padding": 1, "hideVimModeIndicator": True}})
        self.project({"statusLine": {"padding": 0, "refreshInterval": 2}})
        script = self.root / "scripts" / "claude-statusline.py"
        merged = cs.instrumented_settings(
            str(self.workspace), ["--settings", '{"env": {"X": "1"}, "statusLine": {"custom": "flag"}}'],
            script_path=script, python="/usr/bin/python3",
        )
        self.assertEqual(merged["env"], {"X": "1"})
        line = merged["statusLine"]
        self.assertEqual(
            {k: v for k, v in line.items() if k != "command"},
            {"type": "command", "padding": 0, "refreshInterval": 2, "hideVimModeIndicator": True, "custom": "flag"},
        )
        self.assertEqual(line["command"], cs.render_command(script, "user-cmd", python="/usr/bin/python3"))
        # No statusline anywhere: record silently, relay nothing.
        for path in (self.config / "settings.json", self.workspace / ".claude" / "settings.json"):
            path.unlink()
        merged = cs.instrumented_settings(str(self.workspace), script_path=script, python="/usr/bin/python3")
        self.assertEqual(merged, {"statusLine": {"type": "command", "command": cs.render_command(script, None, python="/usr/bin/python3")}})

    def test_instrumented_settings_fails_open(self) -> None:
        script = self.root / "scripts" / "claude-statusline.py"
        instrument = lambda *args: cs.instrumented_settings(str(self.workspace), list(args), script_path=script)
        self.user({"statusLine": {"type": "command", "command": "user-cmd"}})
        self.write_json(self.managed, {"statusLine": {"command": "policy-cmd"}})
        self.assertIsNone(instrument())
        self.write_json(self.managed, {"statusLine": {"type": "command"}})
        self.assertIsNone(instrument())
        self.write_json(self.managed, {"statusLine": {"padding": 2}})
        self.assertIsNotNone(instrument())
        self.managed.unlink()
        self.user({"statusLine": {"type": "prompt", "command": "user-cmd"}})
        self.assertIsNone(instrument())
        self.user({"statusLine": {"command": ["not", "a", "string"]}})
        self.assertIsNone(instrument())
        self.user({"statusLine": "cmd"})
        self.assertIsNone(instrument())
        self.user({"statusLine": {"command": "user-cmd"}})
        with self.assertRaises(ValueError):
            instrument("--settings", "{broken")
        (self.config / "settings.json").write_text("{broken")
        with self.assertRaises(ValueError):
            instrument()

    def test_prepare_launch_rewrites_argv_or_returns_it_untouched(self) -> None:
        self.user({"statusLine": {"type": "command", "command": "user-cmd"}})
        script = self.root / "scripts" / "claude-statusline.py"
        target = self.root / "sessions" / "s1" / "claude-settings.json"
        args = [
            "--resume", "u", "--settings", '{"env": {"A": "1"}}',
            '--settings={"env": {"B": "2"}}', "--", "--settings=prompt",
        ]
        rewritten = cs.prepare_launch(args, str(self.workspace), script_path=script, settings_file=target)
        self.assertEqual(rewritten, ["--settings", str(target), "--resume", "u", "--", "--settings=prompt"])
        self.assertEqual(oct(target.stat().st_mode & 0o777), oct(0o600))
        written = json.loads(target.read_text())
        self.assertEqual(written["env"], {"B": "2"})  # the last --settings wins, as for Claude
        self.assertEqual(written["statusLine"]["command"], cs.render_command(script, "user-cmd"))
        # Every failure returns the argv untouched: unparsable explicit
        # settings, an unwritable state dir, a managed statusline.
        self.assertEqual(cs.prepare_launch(["--settings", "{bad"], str(self.workspace), script_path=script, settings_file=target), ["--settings", "{bad"])
        blocker = self.root / "blocker"
        blocker.write_text("file")
        self.assertEqual(cs.prepare_launch(["x"], str(self.workspace), script_path=script, settings_file=blocker / "s.json"), ["x"])
        self.write_json(self.managed, {"statusLine": {"command": "policy"}})
        self.assertEqual(cs.prepare_launch(["x"], str(self.workspace), script_path=script, settings_file=target), ["x"])

    def test_settings_file_path_follows_the_monitor_session(self) -> None:
        with patch.dict(os.environ, {"MULTIVERSE_STATUS": str(self.root / "sessions" / "abc" / "status.json")}):
            self.assertEqual(cs.settings_file_path(), self.root / "sessions" / "abc" / "claude-settings.json")
        loose = cs.settings_file_path()
        self.assertEqual(loose.parent, self.root / "claude_statusline" / "settings")
        self.assertNotEqual(loose, cs.settings_file_path())


class LaunchTests(StatuslineTestCase):
    def setUp(self) -> None:
        super().setUp()
        # Subprocesses see the machine's real policy file; a managed
        # statusline there legitimately disables instrumentation.
        if self.real_managed.is_file() and "statusLine" in self.real_managed.read_text():
            self.skipTest("a managed statusline policy is installed on this machine")
        self.fake = self.root / "fake claude"
        self.output = self.root / "argv.json"
        self.fake.write_text(
            "#!/usr/bin/env python3\n"
            "import json, os, sys\n"
            "from pathlib import Path\n"
            "Path(os.environ['ARGV_OUTPUT']).write_text(json.dumps({"
            "'args': sys.argv[1:], 'argv0': sys.argv[0], 'cwd': os.getcwd()}))\n"
        )
        self.fake.chmod(0o755)
        self.status = self.root / "sessions" / "s1" / "status.json"
        self.status.parent.mkdir(parents=True)
        self.write_json(self.config / "settings.json", {
            "statusLine": {"type": "command", "command": "printf 'orig:'; cat", "padding": 0},
        })

    def launch(self, *args: str, **env: str) -> subprocess.CompletedProcess:
        env = {"ARGV_OUTPUT": str(self.output), "MULTIVERSE_STATUS": str(self.status), **env}
        return subprocess.run(
            [sys.executable, str(SCRIPT), "launch", str(self.fake), *args],
            capture_output=True, cwd=self.workspace, timeout=30, check=False,
            env=self.subprocess_env(**env),
        )

    def test_launch_instruments_then_records_and_relays_end_to_end(self) -> None:
        result = self.launch("--resume", "u", "--", "--settings=prompt")
        self.assertEqual(result.returncode, 0, result.stderr)
        captured = json.loads(self.output.read_text())
        settings_file = self.status.parent / "claude-settings.json"
        self.assertEqual(captured["args"], ["--settings", str(settings_file), "--resume", "u", "--", "--settings=prompt"])
        self.assertEqual(captured["argv0"], str(self.fake))
        self.assertEqual(Path(captured["cwd"]).resolve(), self.workspace.resolve())
        line = json.loads(settings_file.read_text())["statusLine"]
        self.assertEqual(line["padding"], 0)
        self.assertEqual(shlex.split(line["command"])[:3], [sys.executable, str(SCRIPT), "render"])
        # Now play Claude: pipe a statusline payload through the configured
        # command exactly as Claude would.
        raw = json.dumps(self.payload(size=1_000_000)).encode()
        rendered = subprocess.run(
            ["/bin/sh", "-c", line["command"]], input=raw, capture_output=True,
            cwd=self.workspace, env=self.subprocess_env(), timeout=30, check=False,
        )
        self.assertEqual((rendered.returncode, rendered.stdout), (0, b"orig:" + raw))
        self.assertEqual(claude_usage.context_window_cached(SESSION, str(self.workspace)), 1_000_000)
        # A /model switch shows up on the next render.
        raw = json.dumps(self.payload(size=200_000)).encode()
        subprocess.run(["/bin/sh", "-c", line["command"]], input=raw, capture_output=True, cwd=self.workspace, env=self.subprocess_env(), timeout=30, check=False)
        self.assertEqual(claude_usage.context_window_cached(SESSION, str(self.workspace)), 200_000)
        # The user's own settings were never touched.
        self.assertEqual(json.loads((self.config / "settings.json").read_text())["statusLine"]["command"], "printf 'orig:'; cat")

    def test_launch_falls_back_to_the_original_argv(self) -> None:
        blocker = self.root / "blocker"
        blocker.write_text("file")
        result = self.launch("--resume", "u", "prompt", MULTIVERSE_STATUS=str(blocker / "status.json"))
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(self.output.read_text())["args"], ["--resume", "u", "prompt"])
        result = self.launch("--settings", "{broken", "prompt")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(self.output.read_text())["args"], ["--settings", "{broken", "prompt"])

    def test_launch_reports_a_missing_binary(self) -> None:
        result = subprocess.run(
            [sys.executable, str(SCRIPT), "launch", str(self.root / "no such claude"), "x"],
            capture_output=True, env=self.subprocess_env(MULTIVERSE_STATUS=str(self.status)), timeout=30, check=False,
        )
        self.assertEqual(result.returncode, 127)
        self.assertIn(b"cannot exec", result.stderr)

    def test_usage_for_unknown_subcommands(self) -> None:
        for args in ([], ["launch"], ["bogus"]):
            result = subprocess.run(
                [sys.executable, str(SCRIPT), *args], capture_output=True, input=b"",
                env=self.subprocess_env(), timeout=30, check=False,
            )
            self.assertEqual(result.returncode, 2, args)
            self.assertIn(b"usage:", result.stderr)


if __name__ == "__main__":
    unittest.main()
