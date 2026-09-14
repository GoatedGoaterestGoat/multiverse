from __future__ import annotations

import os
import subprocess
import tempfile
import unittest
from pathlib import Path


class MultiverseSetupTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp_dir.cleanup)
        self.root = Path(self.temp_dir.name)
        self.home = self.root / "home"
        self.home.mkdir()
        self.fake_bin = self.root / "bin"
        self.fake_bin.mkdir()
        self.plugin_root = Path(__file__).resolve().parents[1]
        self.wrapper = self.plugin_root / "bin" / "multiverse"
        self.codex_plugin_root = (
            self.plugin_root.parent / "plugins" / "multiverse-codex"
        )

        for name in ("tmux", "jq", "uv"):
            self._write_executable(name, "#!/bin/sh\nexit 0\n")

    def _write_executable(self, name: str, body: str) -> Path:
        path = self.fake_bin / name
        path.write_text(body)
        path.chmod(0o755)
        return path

    def _environment(self, state_root: Path) -> dict[str, str]:
        env = os.environ.copy()
        for name in (
            "CODEX_HOME",
            "MULTIVERSE_CODEX_BIN",
            "MULTIVERSE_CODEX_MODEL",
            "MULTIVERSE_CODEX_PLUGIN_ROOT",
            "MULTIVERSE_CODEX_REASONING_EFFORT",
            "MULTIVERSE_PYTHON_BIN",
            "MULTIVERSE_PI_BIN",
            "PI_CODING_AGENT_DIR",
            "PI_CODING_AGENT_SESSION_DIR",
            "MULTIVERSE_ROOT",
        ):
            env.pop(name, None)
        env.update(
            {
                "HOME": str(self.home),
                "MULTIVERSE_ROOT": str(state_root),
                "MULTIVERSE_CODEX_PLUGIN_ROOT": str(
                    self.codex_plugin_root
                ),
                "MULTIVERSE_SKIP_LOCAL_CONFIG": "1",
                "PATH": f"{self.fake_bin}:/usr/bin:/bin",
            }
        )
        return env

    def _run_setup(self, env: dict[str, str]) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [str(self.wrapper), "setup"],
            cwd=self.root,
            env=env,
            capture_output=True,
            text=True,
            timeout=20,
        )

    def test_launcher_defaults_codex_model_and_preserves_override(self) -> None:
        python_bin = self._write_executable(
            "python-custom",
            "#!/bin/sh\nprintf '%s\\n' \"$MULTIVERSE_CODEX_MODEL\"\n",
        )
        env = self._environment(self.root / "model-state")
        env["MULTIVERSE_PYTHON_BIN"] = str(python_bin)

        defaulted = subprocess.run(
            [str(self.wrapper)],
            cwd=self.root,
            env=env,
            capture_output=True,
            text=True,
            timeout=20,
        )
        self.assertEqual(defaulted.returncode, 0, defaulted.stderr)
        self.assertEqual(defaulted.stdout.strip(), "gpt-6-astra")

        env["MULTIVERSE_CODEX_MODEL"] = "gpt-explicit"
        overridden = subprocess.run(
            [str(self.wrapper)],
            cwd=self.root,
            env=env,
            capture_output=True,
            text=True,
            timeout=20,
        )
        self.assertEqual(overridden.returncode, 0, overridden.stderr)
        self.assertEqual(overridden.stdout.strip(), "gpt-explicit")

    def test_help_does_not_launch_the_monitor(self) -> None:
        marker = self.root / "uv-was-called"
        self._write_executable(
            "uv",
            f"#!/bin/sh\ntouch {marker}\nexit 99\n",
        )
        completed = subprocess.run(
            [str(self.wrapper), "--help"],
            cwd=self.root,
            env=self._environment(self.root / "help-state"),
            capture_output=True,
            text=True,
            timeout=20,
        )

        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assertIn("Usage: multiverse", completed.stdout)
        self.assertFalse(marker.exists())

    def test_setup_snapshots_and_restores_codex_environment(self) -> None:
        state_root = self.root / "state"
        codex_home = self.root / "custom-codex-home"
        codex_home.mkdir()
        self._write_executable(
            "codex-custom",
            "#!/bin/sh\nprintf 'codex-cli 9.8.7\\n'\n",
        )

        configured_env = self._environment(state_root)
        configured_env.update(
            {
                "CODEX_HOME": str(codex_home),
                "MULTIVERSE_CODEX_BIN": "codex-custom",
            }
        )
        first = self._run_setup(configured_env)
        first_output = first.stdout + first.stderr

        self.assertEqual(first.returncode, 0, first_output)
        self.assertIn(
            f"Codex CLI OK: {self.fake_bin / 'codex-custom'} "
            "(codex-cli 9.8.7)",
            first_output,
        )
        self.assertIn(f"Codex home: {codex_home}", first_output)
        self.assertIn(
            f"Codex plugin bundle OK: {self.codex_plugin_root}",
            first_output,
        )

        snapshot_path = state_root / "codex-env.sh"
        snapshot = snapshot_path.read_text()
        self.assertIn(f"export CODEX_HOME={codex_home}", snapshot)
        self.assertIn("export MULTIVERSE_CODEX_BIN=codex-custom", snapshot)
        self.assertIn(
            f'export PATH={self.fake_bin}:"${{PATH-}}"',
            snapshot,
        )
        self.assertEqual(list(state_root.glob("codex-env.sh.*.tmp")), [])

        unconfigured_env = self._environment(state_root)
        unconfigured_env["PATH"] = "/usr/bin:/bin"
        second = self._run_setup(unconfigured_env)
        second_output = second.stdout + second.stderr

        self.assertEqual(second.returncode, 0, second_output)
        self.assertIn(
            f"Codex CLI OK: {self.fake_bin / 'codex-custom'} "
            "(codex-cli 9.8.7)",
            second_output,
        )
        self.assertIn(f"Codex home: {codex_home}", second_output)

        replacement_home = self.root / "replacement-codex-home"
        replacement_home.mkdir()
        self._write_executable(
            "codex-replacement",
            "#!/bin/sh\nprintf 'codex-cli 10.0.0\\n'\n",
        )
        replacement_env = self._environment(state_root)
        replacement_env.update(
            {
                "CODEX_HOME": str(replacement_home),
                "MULTIVERSE_CODEX_BIN": "codex-replacement",
            }
        )
        replacement = self._run_setup(replacement_env)
        replacement_output = replacement.stdout + replacement.stderr

        self.assertEqual(replacement.returncode, 0, replacement_output)
        self.assertIn(f"Codex home: {replacement_home}", replacement_output)
        refreshed_snapshot = snapshot_path.read_text()
        self.assertIn(
            f"export CODEX_HOME={replacement_home}", refreshed_snapshot
        )
        self.assertIn(
            "export MULTIVERSE_CODEX_BIN=codex-replacement",
            refreshed_snapshot,
        )
        self.assertNotIn(str(codex_home), refreshed_snapshot)

    def test_setup_reports_and_snapshots_external_pi_cli(self) -> None:
        state_root = self.root / "pi-state"
        pi_sessions = self.root / "pi-sessions"
        pi_sessions.mkdir()
        self._write_executable(
            "pi-custom",
            "#!/bin/sh\nprintf 'pi 1.2.3\\n'\n",
        )
        env = self._environment(state_root)
        env.update(
            {
                "MULTIVERSE_PI_BIN": "pi-custom",
                "PI_CODING_AGENT_SESSION_DIR": str(pi_sessions),
            }
        )

        completed = self._run_setup(env)
        output = completed.stdout + completed.stderr

        self.assertEqual(completed.returncode, 0, output)
        self.assertIn(
            f"pi CLI OK: {self.fake_bin / 'pi-custom'} (pi 1.2.3)", output
        )
        self.assertIn("pi extension OK", output)
        snapshot = (state_root / "pi-env.sh").read_text()
        self.assertIn("export MULTIVERSE_PI_BIN=pi-custom", snapshot)
        self.assertIn(
            f"export PI_CODING_AGENT_SESSION_DIR={pi_sessions}", snapshot
        )

    def test_setup_reports_optional_codex_failures_without_failing(self) -> None:
        state_root = self.root / "missing-state"
        env = self._environment(state_root)
        env.update(
            {
                "MULTIVERSE_CODEX_BIN": "missing-codex",
                "MULTIVERSE_CODEX_PLUGIN_ROOT": str(
                    self.root / "missing-plugin"
                ),
            }
        )

        completed = self._run_setup(env)
        output = completed.stdout + completed.stderr

        self.assertEqual(completed.returncode, 0, output)
        self.assertIn("Codex CLI not found: 'missing-codex'", output)
        self.assertIn("Codex plugin bundle incomplete", output)
        self.assertIn("Other installed runtimes remain available", output)


if __name__ == "__main__":
    unittest.main()
