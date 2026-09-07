from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


RUNNER = Path(__file__).resolve().parents[1] / "bin" / "multiverse-session"


class ClaudeStatuslineLauncherTests(unittest.TestCase):
    def test_statusline_launcher_preserves_runtime_arguments_and_prompt(self) -> None:
        self._run(instrumented=True)

    def test_missing_statusline_launcher_keeps_original_runtime_launch(self) -> None:
        self._run(instrumented=False)

    def test_broken_statusline_import_keeps_original_runtime_launch(self) -> None:
        self._run(instrumented=True, broken_import=True)

    def _run(self, *, instrumented: bool, broken_import: bool = False) -> None:
        with tempfile.TemporaryDirectory(prefix="mv statusline ") as tmp:
            root = Path(tmp)
            plugin = root / "plugin"
            scripts = plugin / "scripts"
            scripts.mkdir(parents=True)
            python = plugin / ".venv" / "bin" / "python"
            python.parent.mkdir(parents=True)
            python.symlink_to(sys.executable)
            status = root / "sessions" / "wrapper" / "status.json"
            status.parent.mkdir(parents=True)
            status.write_text('{"status":"idle"}')
            output = root / "argv.json"
            fake_claude = root / "fake-claude"
            fake_claude.write_text(
                "#!/usr/bin/env python3\n"
                "import json, os, sys\n"
                "from pathlib import Path\n"
                "Path(os.environ['ARGV_OUTPUT']).write_text(json.dumps({"
                "'args': sys.argv[1:], 'captured': os.environ.get('CAPTURE_ACTIVE')}))\n"
            )
            fake_claude.chmod(0o755)
            if instrumented:
                if broken_import:
                    # Shadow any editable install or inherited PYTHONPATH so
                    # this always models an unavailable recorder dependency.
                    source = RUNNER.parent.parent / "scripts" / "claude-statusline.py"
                    (scripts / source.name).write_text(source.read_text())
                    broken_src = plugin / "src"
                    broken_src.mkdir()
                    (broken_src / "__init__.py").write_text("")
                    (broken_src / "claude_statusline.py").write_text(
                        "raise ImportError('simulated unavailable dependency')\n"
                    )
                else:
                    (scripts / "claude-statusline.py").write_text(
                        "import os, sys\n"
                        "assert sys.argv[1] == 'launch'\n"
                        "os.environ['CAPTURE_ACTIVE'] = 'yes'\n"
                        "os.execvpe(sys.argv[2], sys.argv[2:], os.environ)\n"
                    )
            prompt = "Task with 'quotes', $variables, and `literal backticks`."
            initial = root / "initial.txt"
            initial.write_text(prompt)
            env = {k: v for k, v in os.environ.items() if not k.startswith("MULTIVERSE_")}
            env.pop("CAPTURE_ACTIVE", None)
            env.update({
                "MULTIVERSE_CLAUDE_BIN": str(fake_claude),
                "MULTIVERSE_PLUGIN_ROOT": str(plugin),
                "MULTIVERSE_STATUS": str(status),
                "MULTIVERSE_ROOT": str(root),
                "MULTIVERSE_SESSION_CWD": str(root),
                "MULTIVERSE_SESSION_NAME": "test-agent",
                "MULTIVERSE_INITIAL_PROMPT_FILE": str(initial),
                "ARGV_OUTPUT": str(output),
            })
            result = subprocess.run(
                ["bash", str(RUNNER), "--resume", "native-uuid", "--model", "opus[1m]"],
                cwd=root, env=env, capture_output=True, text=True, timeout=10,
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            captured = json.loads(output.read_text())
            self.assertEqual(captured["args"], [
                "--plugin-dir", str(plugin), "--name", "test-agent",
                "--resume", "native-uuid", "--model", "opus[1m]", prompt,
            ])
            self.assertEqual(captured["captured"], "yes" if instrumented and not broken_import else None)
            self.assertEqual(json.loads(status.read_text())["status"], "ended")


if __name__ == "__main__":
    unittest.main()
