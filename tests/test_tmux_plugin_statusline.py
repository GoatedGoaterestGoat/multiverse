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

    def test_no_plugins_is_byte_identical(self):
        baseline = self.render()
        for items in (None, [], "junk", [{"name": "cost"}]):
            with self.subTest(items=items):
                self.save(items)
                self.assertEqual(self.render(), baseline)
        self.save([{"name": "cost", "plugin": "p", "claude": True, "tmux": True}])
        self.assertEqual(self.render(), baseline)  # declared, but no file written yet
        self.assertEqual(baseline, "worker · codex · status idle · phase working")

    def test_defaults_then_tmux_items_then_family_override(self):
        self.save([{"name": "cost", "plugin": "p", "claude": True, "tmux": True},
                   {"name": "trees", "plugin": "q", "claude": True, "tmux": False}])
        self.item("cost", "$1.20")
        self.item("trees", "trees 3/4")
        self.assertEqual(self.render(), "worker · codex · status idle · phase working · $1.20")
        # Named in tmux.items, they also take that place: before the core items the file leaves unnamed.
        self.configure(items={"cost": False, "trees": True})
        self.assertEqual(self.render(), "trees 3/4 · worker · codex · status idle · phase working")
        self.configure(items={"cost": False, "trees": True}, harnesses={"codex": {"items": {"cost": True, "trees": False}}})
        self.assertEqual(self.render(), "$1.20 · worker · codex · status idle · phase working")
        self.assertEqual(self.render("pi"), "trees 3/4 · worker · codex · phase working")

    def test_a_default_off_item_named_compaction_is_a_plugin_item_here(self):
        self.save([{"name": "compaction", "plugin": "compaction", "claude": True, "tmux": False}])
        self.item("compaction", "trees 3/4")
        self.assertEqual(statusline.declared_items(read(self.directory / "runtime.json")), [("compaction", False)])
        self.assertNotIn("trees", self.render())
        self.configure(items={"compaction": True})
        self.assertEqual(self.render(), "trees 3/4 · worker · codex · status idle · phase working")
        self.configure(harnesses={"codex": {"items": {"compaction": True}}})
        self.assertEqual(self.render(), "worker · codex · status idle · phase working · trees 3/4")
        self.assertNotIn("trees", self.render("pi"))

    def test_named_items_take_their_place_among_core_items(self):
        self.save([{"name": "cost", "plugin": "p", "tmux": True}, {"name": "trees", "plugin": "q", "tmux": True},
                   {"name": "late", "plugin": "r", "tmux": True}])
        for name in ("cost", "trees", "late"):
            self.item(name, name.upper())
        self.configure(items={"trees": True, "agent": True, "cost": True, "phase": True})
        self.assertEqual(self.render(), "TREES · worker · COST · phase working · codex · status idle · LATE")
        # A family-only key changes an item but not its place.
        self.configure(items={"trees": True, "agent": True}, harnesses={"codex": {"items": {"late": False}}})
        self.assertEqual(self.render(), "TREES · worker · codex · status idle · phase working · COST")

    def test_undeclared_disabled_and_malformed_items_are_never_read(self):
        self.item("ghost", "GHOST")
        self.configure(items={"ghost": True})
        self.save([{"name": "Bad_Name", "tmux": True}, {"name": "phase", "tmux": True}, {"name": "../x", "tmux": True},
                   {"name": "a" * 41, "tmux": True}, {"name": "off", "tmux": False}, "junk", {"tmux": True},
                   {"name": "dup", "tmux": True}, {"name": "dup", "tmux": False}])
        self.item("off", "OFF")
        self.item("dup", "DUP")
        self.assertEqual(self.render(), "worker · codex · status idle · phase working · DUP")
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

    def test_text_is_bounded_cleaned_and_literal(self):
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
                    self.assertEqual(rendered, "worker · codex · status idle · phase working")
                else:
                    self.assertEqual(rendered, "worker · codex · status idle · phase working · " + shown)

    def test_a_stale_launch_shows_nothing_and_the_script_stays_stdlib_only(self):
        self.save([{"name": "cost", "tmux": True}])
        self.item("cost", "$1")
        self.assertEqual(statusline.render(self.root, self.agent, "other-launch", "codex"), "")
        script = PROJECT / "src/multiverse/terminal/statusline.py"
        result = subprocess.run([sys.executable, "-S", "-I", str(script), str(self.root), self.agent, self.launch, "codex"],
                                text=True, capture_output=True, timeout=30, env={"PATH": os.environ.get("PATH", "")})
        self.assertEqual(result.stdout.strip(), "worker · codex · status idle · phase working · $1", result.stderr)
        self.item("cost", "$2")  # replaced live: the next refresh reads it again
        self.assertTrue(self.render().endswith("· $2"))
