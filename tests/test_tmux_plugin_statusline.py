"""Plugin status-line items on the tmux bar: declared per launch, written as files, read by a stdlib-only script."""
import os
from pathlib import Path
import subprocess
import sys
from uuid import uuid4

from multiverse.store import read, write
from multiverse.terminal import statusline
from test_multiverse import BaseTest

PROJECT = Path(__file__).resolve().parents[1]


class PluginItemsTest(BaseTest):
    def setUp(self):
        super().setUp()
        self.agent, self.launch = str(uuid4()), str(uuid4())
        self.directory = self.root / "state/agents" / self.agent
        self.properties = {"id": self.agent, "universe_id": read(self.root / "universe.json")["id"],
                           "name": "worker", "harness": "codex", "phase": "working", "summary": "Read the patch"}
        self.runtime = {"launch_id": self.launch, "state": "running", "status": "idle"}
        self.save()

    def save(self, items=None):
        runtime = dict(self.runtime)
        if items is not None:
            runtime["plugins"] = {"plugins": {}, "status_items": items}
        write(self.directory / "properties.json", self.properties)
        write(self.directory / "runtime.json", runtime)

    def item(self, name, text):
        path = self.directory / "status-items" / f"{name}.txt"
        path.parent.mkdir(parents=True, exist_ok=True)
        if isinstance(text, bytes):
            path.write_bytes(text)
        else:
            path.write_text(text)

    def configure(self, **tmux):
        write(self.root / "statusline.json", {"schema": 1, "tmux": tmux})

    def render(self, family="codex"):
        return statusline.render(self.root, self.agent, self.launch, family)

    def test_untrusted_plugin_items_are_sanitized_for_tmux(self):
        """Plugin status items named ../x, badly named, undeclared, switched off or from another launch are never
        read, their text has controls stripped, # doubled, length bounded and only its first line kept, and the
        script tmux runs needs only the standard library; fragile because tmux runs #(command) in status strings, and
        a garbled or command-running bar raises no error."""
        baseline = self.render()  # the core items alone
        with self.subTest("undeclared, switched off and malformed items are never read"):
            self.item("ghost", "GHOST")
            self.configure(items={"ghost": True})
            (self.directory / "x.txt").write_text("ESCAPED")  # where an item named ../x would be read from
            self.save([{"name": "Bad_Name", "tmux": True}, {"name": "phase", "tmux": True},
                       {"name": "../x", "tmux": True}, {"name": "a" * 41, "tmux": True}, {"name": "off", "tmux": False},
                       "junk", {"tmux": True}, {"name": "dup", "tmux": True}, {"name": "dup", "tmux": False}])
            self.item("off", "OFF")
            self.item("dup", "DUP")
            self.assertEqual(self.render(), baseline + " · DUP")
            opened = []
            real = Path.open

            def spy(path, *args, **kwargs):
                opened.append(path.name)
                return real(path, *args, **kwargs)
            from unittest.mock import patch
            with patch.object(Path, "open", spy):
                self.render()
            self.assertNotIn("off.txt", opened)
            self.assertNotIn("ghost.txt", opened)
            self.assertNotIn("x.txt", opened)
            (self.root / "statusline.json").unlink()
        with self.subTest("text is bounded, cleaned and literal"):
            self.save([{"name": "cost", "tmux": True}])
            for written, shown in (("", None), ("\n", None), ("first\nsecond", "first"), ("a\x1b[31mred\x07", "ared"),
                                   ("#[fg=red]x", "##[fg=red]x"), (b"\xff\xfeok", "��ok"), ("x" * 5000, None)):
                with self.subTest(written=written[:20]):
                    self.item("cost", written)
                    rendered = self.render()
                    if shown is None and written and len(written) > 1000:
                        self.assertTrue(rendered.endswith("…"))
                        self.assertLess(len(rendered), 300)
                    elif shown is None:
                        self.assertEqual(rendered, baseline)
                    else:
                        self.assertEqual(rendered, baseline + " · " + shown)
        with self.subTest("another launch shows nothing, and the script tmux runs stays stdlib-only"):
            self.save([{"name": "cost", "tmux": True}])
            self.item("cost", "$1")
            self.assertEqual(statusline.render(self.root, self.agent, "other-launch", "codex"), "")
            script = PROJECT / "src/multiverse/terminal/statusline.py"
            result = subprocess.run([sys.executable, "-S", "-I", str(script), str(self.root), self.agent, self.launch,
                                     "codex"], text=True, capture_output=True, timeout=30,
                                    env={"PATH": os.environ.get("PATH", "")})
            self.assertEqual(result.stdout.strip(), baseline + " · $1", result.stderr)
            self.item("cost", "$2")  # replaced live: the next refresh reads it again
            self.assertTrue(self.render().endswith("· $2"))
