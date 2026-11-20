"""The plugin core: manifests, discovery, trust, plugins.json, settings, loading and the API."""
import contextlib
import errno
import io
import json
import threading
import time
import os
from pathlib import Path
import shutil
import subprocess
import sys
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from multiverse import cli, plugins
from multiverse.plugins import __main__ as helper, api, config, loader, manifest, settings, trust
from multiverse.plugins.testing import PluginTestCase
from multiverse.tools import doctor, plugins as tool
from multiverse.universe import catalog

SCHEMA = {"type": "object", "additionalProperties": False, "properties": {
    "greeting": {"type": "string", "minLength": 1},
    "limits": {"type": "object", "properties": {"count": {"type": "integer", "minimum": 0, "maximum": 10}}}}}
HARMFUL = ("import os\nopen(os.environ['MARKER'], 'w').write('ran')\n"
           "def register(mv):\n    pass\n")


def run_quietly(call, stdin=""):
    """(exit code, stdout, stderr) of `call` run as a command's main: SystemExit is its exit code."""
    out, err = io.StringIO(), io.StringIO()
    with mock.patch.object(sys, "stdin", io.StringIO(stdin)), contextlib.redirect_stdout(out), \
            contextlib.redirect_stderr(err):
        try:
            code = call()
        except SystemExit as exit:
            code = exit.code
    code = 0 if code is None else code if isinstance(code, int) else 1
    return code, out.getvalue(), err.getvalue()


class ManifestTest(PluginTestCase):
    def test_manifest_and_discovery_refusals(self):
        """Bad or reserved names, unknown extension points, permissions or harness entries, a schema or extension
        path outside the plugin, unknown fields and bad JSON make a manifest invalid, a newer API reads as update
        mv2 and an enabled absent plugin as missing; fragile because the newer-API check must come before the
        unknown-name checks and the path checks keep a plugin's files inside it."""
        cases = {
            "name": ({"name": "Hello_World"}, "name must be"),
            "reserved": ({"name": "launches"}, "reserved"),
            "api": ({"api": 1}, "api must be"),
            "point": ({"extends": ["teleport"]}, "does not have: teleport"),
            "permission": ({"permissions": ["root"]}, "unknown permissions: root"),
            "commands without cli": ({"commands": {"hello": "x"}}, 'needs "cli"'),
            "field": ({"colour": "red"}, "unknown fields: colour"),
            "escape": ({"settings": {"version": 1, "schema": "../x.json"}}, "inside the plugin"),
            "default": ({"default": "maybe"}, 'default must be'),
            "harness permission": ({"harnesses": {"claude": [{"dir": "c"}]}}, 'needs the permission "harness.claude"'),
            "harness family": ({"permissions": ["harness.claude"], "harnesses": {"emacs": []}},
                               "unknown harness family"),
            "harness version": ({"permissions": ["harness.claude"],
                                 "harnesses": {"claude": [{"dir": "c", "min_version": "2.1"}]}}, "MAJOR.MINOR.PATCH"),
            "harness escape": ({"permissions": ["harness.pi"], "harnesses": {"pi": [{"extension": "/etc/x.ts"}]}},
                               "inside the plugin"),
            "core status item": ({"extends": ["statusline"], "statusline": {"inbox": {}}}, "one mv2 draws itself"),
            "undeclared status item": ({"statusline": {"trees": {}}}, 'needs "statusline" in extends'),
            "a name unknown at its own api": ({"permissions": ["transcript.read"]}, "unknown permissions: transcript"),
        }
        for label, (fields, expected) in cases.items():
            with self.subTest(label):
                directory = self.write_plugin("case", fields)
                with self.assertRaisesRegex(manifest.ManifestError, expected):
                    manifest.load(directory)
        (self.plugins_dir / "broken").mkdir()
        (self.plugins_dir / "broken" / "plugin.json").write_text("{")
        with self.assertRaisesRegex(manifest.ManifestError, "not valid JSON"):
            manifest.load(self.plugins_dir / "broken")
        with self.subTest("harness versions it does not fit"):
            found = manifest.load(self.write_plugin("guard", {
                "extends": ["launch", "statusline"], "permissions": ["harness.claude", "harness.pi", "harness.codex"],
                "harnesses": {"claude": [{"dir": "claude", "min_version": "2.1.290", "needs_bridge": True}],
                              "pi": [{"extension": "pi/extension.ts"}],
                              "codex": [{"min_version": "0.162.1", "max_version": "0.162.1",
                                         "config": {"model_verbosity": "low"}}]},
                "statusline": {"trees": {"claude": True, "tmux": True}}}))
            [claude] = found.harnesses["claude"]
            self.assertEqual((claude.path, claude.min_version, claude.needs_bridge), ("claude", (2, 1, 290), True))
            self.assertIsNone(claude.fits((2, 1, 296)))
            self.assertIn("2.1.290", claude.fits((2, 1, 289)))
            self.assertIsNotNone(claude.fits(None))
            [codex] = found.harnesses["codex"]
            self.assertEqual(codex.fields, {"config": {"model_verbosity": "low"}})
            self.assertIn("0.162.1 or older", codex.fits((0, 162, 2)))
            self.assertEqual(found.statusline, (manifest.Item("trees", True, True),))
        with self.subTest("states in a universe"):
            self.enable("ghost")
            self.write_plugin("future", {"api": "1.999"})
            self.enable("future")
            # Written for a newer API, with names this mv2 does not know yet: incompatible, never invalid.
            self.write_plugin("newer", {"api": "1.999", "permissions": ["calendar.read", "models.claude"],
                                        "extends": ["transcripts"], "transcripts": {"rows": 10}})
            self.enable("newer")
            (self.plugins_dir / "bad").mkdir()
            (self.plugins_dir / "bad" / "plugin.json").write_text("[]")
            self.write_plugin("typo", {"permissions": ["transcript.read"]})
            states = {found.name: found for found in plugins.entries(self.root)}
            self.assertEqual(states["ghost"].state, "missing")
            have = ".".join(map(str, loader.API_VERSION))
            for name in ("future", "newer"):
                self.assertEqual(states[name].state, "incompatible")
                self.assertIn(f"needs plugin API 1.999; this mv2 has {have}; update mv2", states[name].problem)
            self.assertIn("calendar.read", states["newer"].manifest.permission_texts())
            self.assertEqual(states["bad"].state, "invalid")
            self.assertEqual(states["typo"].state, "invalid")
        with self.subTest("mv2 plugins new"):
            parent = self.work / "new-plugins"
            parent.mkdir()
            for name in ("core", "plugins", "list"):
                with self.subTest(name), self.assertRaises(ValueError):
                    tool.run(self.root, plugin_action="new", name=name, directory=str(parent))
            self.assertEqual(list(parent.iterdir()), [])
            tool.run(self.root, plugin_action="new", name="fresh", directory=str(parent))
            self.assertTrue((parent / "fresh" / "plugin.json").exists())


class DiscoveryTest(PluginTestCase):
    bundled = True  # typeahead is the bundled plugin a cloned repository's copy would replace

    def test_which_copy_runs_and_whether_it_is_on(self):
        """Path, universe, personal and bundled copies take precedence in that order, an untrusted copy never hides
        another, and only bundled plugins default on; a security boundary, since a cloned repository's
        plugins/NAME or a plugin's own default must never run code the user did not choose."""
        with self.subTest("only bundled plugins default on"):
            self.write_plugin("quiet", {"default": "on"})
            found = self.entry("quiet")
            self.assertEqual((found.state, found.on, found.source), ("off", False, "path"))
            self.enable("quiet")
            self.assertEqual(self.entry("quiet").state, "on")
            self.assertEqual((self.entry("typeahead").source, self.entry("typeahead").on), ("bundled", True))
        with self.subTest("precedence"):
            self.write_plugin("twin", {"version": "1.0.0"})
            universe_copy = self.write_plugin("twin", {"version": "2.0.0"}, where=self.root / "plugins")
            personal = self.write_plugin("twin", {"version": "3.0.0"}, where=self.home / ".multiverse" / "plugins")
            found = self.entry("twin")
            self.assertEqual((found.source, found.manifest.version), ("path", "1.0.0"))
            self.assertEqual(found.shadowed, [("universe", universe_copy), ("personal", personal)])
            with mock.patch.dict(os.environ, {"MV2_PLUGIN_PATH": ""}):
                # An untrusted universe copy never hides another source's plugin; trusted, it comes first.
                found = self.entry("twin")
                self.assertEqual((found.source, found.shadowed), ("personal", [("universe", universe_copy)]))
                trust.trust(universe_copy)
                self.assertEqual(self.entry("twin").source, "universe")
        with self.subTest("a universe copy of a bundled plugin"):
            marker = self.work / "marker"
            os.environ["MARKER"] = str(marker)
            copy = self.write_plugin("typeahead", {"version": "9.9.9", "module": "plugin"}, {"plugin.py": HARMFUL},
                                     where=self.root / "plugins")
            found = self.entry("typeahead")
            self.assertEqual(found.source, "bundled")
            self.assertIn("universe", [source for source, _ in found.shadowed])
            item = next(entry for entry in plugins.listing(self.root) if entry["name"] == "typeahead")
            self.assertEqual((item["source"], item["overrides"]), ("bundled", []))  # untrusted: shadowed, no more
            self.assertFalse(marker.exists())
            trust.trust(copy)
            item = next(entry for entry in plugins.listing(self.root) if entry["name"] == "typeahead")
            self.assertEqual(item["source"], "universe")
            self.assertEqual([entry["source"] for entry in item["overrides"]], ["bundled"])

    def test_safe_mode_loads_nothing(self):
        """MV2_PLUGINS=off loads no plugin; a break goes unnoticed because safe mode is used only after a plugin
        has broken mv2, the worst time to find it broken."""
        self.write_plugin("hello", {"module": "plugin", "extends": ["bootstrap"]},
                          {"plugin.py": "def register(mv): mv.bootstrap.line('hi')\n"})
        self.enable("hello")
        with mock.patch.dict(os.environ, {"MV2_PLUGINS": "off"}):
            self.assertEqual(self.entry("hello").state, "safe-mode")
            self.assertEqual(loader.active(self.root), [])
            self.assertEqual(plugins.bootstrap_lines(self.root), [])
        self.assertEqual(plugins.bootstrap_lines(self.root), ["hi"])


class RegistryTest(PluginTestCase):
    def test_a_recorded_folder_is_one_identity_pinned_by_the_persons_enable_alone(self):
        """A recorded folder runs pinned everywhere, pinned as admitted, never by a listing or agent; else edits run."""
        from multiverse.plugins import events, registry
        home = self.work / "dev"
        person = lambda: mock.patch.dict(os.environ, {"MV2_PLUGIN_PATH": str(home)})  # noqa: E731
        agent = {"MV2_CONTROL_SOCKET": str(self.work / "s.sock")}

        def plugin(event="agent.status", label="one", where=home):
            return self.write_plugin("relay", {"extends": ["events"], "events": [event]},
                                     {"plugin.py": f"LABEL = {label!r}\ndef register(mv):\n    pass\n"}, where=where)
        with self.subTest("on by hand, recorded by the person's listing"):
            by_hand = self.write_plugin("byhand", {"extends": ["events"], "events": ["agent.status"]}, where=home)
            self.enable("byhand")  # turned on by hand: plugins.json only
            with mock.patch.dict(os.environ, agent):  # an agent's listing records no folder
                plugins.listing(self.root, wait=None)
            self.assertEqual(registry.read(self.root).get("byhand", {}).get("directory"), None)
            with person():
                plugins.listing(self.root, wait=None)  # the person's: at what it already runs
            self.assertEqual((registry.read(self.root)["byhand"]["directory"], trust.state(by_hand)),
                             (str(by_hand.resolve()), "trusted"))
            with mock.patch.dict(os.environ, agent):
                self.assertEqual(self.entry("byhand").state, "on")  # available where it isn't found
        folder = plugin()
        with self.subTest("the enable pins what it admitted"):
            real = config.set_enabled

            def edited_meanwhile(*args, **keywords):
                done = real(*args, **keywords)
                plugin(label="edited during the enable")
                return done
            with person(), mock.patch.object(config, "set_enabled", side_effect=edited_meanwhile):
                plugins.set_enabled(self.root, "relay", True)
            self.assertEqual(trust.state(folder), "changed")  # pinned at the admitted bytes, not these
            with person():
                tool.run(self.root, plugin_action="trust", name="relay")
        with self.subTest("edits while being turned on pin and publish nothing of theirs"):
            late = self.write_plugin("late", {"extends": ["events"], "events": ["agent.status"]}, where=home)
            for patched, target in ((plugins, "_grant_for"), (config, "set_enabled")):
                real = getattr(patched, target)

                def then_edit(*args, real=real, **keywords):
                    done = real(*args, **keywords)
                    self.write_plugin("late", {"extends": ["events"], "events": ["agent.ready"]},
                                      {"plugin.py": "EDITED = 1\n"}, where=home)
                    return done
                with person(), mock.patch.object(patched, target, side_effect=then_edit):
                    plugins.set_enabled(self.root, "late", True)
                self.assertNotEqual(trust.state(late), "trusted", target)  # pinned at what was checked, not these
                self.assertEqual(registry.read(self.root)["late"]["events"], ["agent.status"], target)
                trust.forget(late)
                self.write_plugin("late", {"extends": ["events"], "events": ["agent.status"]}, {"plugin.py": ""},
                                  where=home)
                plugins.set_enabled(self.root, "late", False)
        with self.subTest("the choice is published as admitted, whatever discovery finds after"):
            other = self.write_plugin("pick", {"extends": ["events"], "events": ["agent.ready"]},
                                      where=trust.user_directory() / "plugins")
            chosen = self.write_plugin("pick", {"extends": ["events"], "events": ["agent.status"]}, where=home)
            real = config.set_enabled

            def personal_wins_after(*args, **keywords):
                done = real(*args, **keywords)
                os.environ["MV2_PLUGIN_PATH"] = ""  # now discovery finds the personal copy instead
                return done
            with person(), mock.patch.object(config, "set_enabled", side_effect=personal_wins_after):
                plugins.set_enabled(self.root, "pick", True)
            self.assertEqual((registry.read(self.root)["pick"], trust.state(chosen)),
                             ({"events": ["agent.status"], "source": "path", "directory": str(chosen.resolve())},
                              "trusted"))
            shutil.rmtree(other)
            plugins.set_enabled(self.root, "pick", False)
        with self.subTest("a first listing of a folder pinned at other content records no patterns"):
            stale = self.write_plugin("stale", {"extends": ["events"], "events": ["agent.status"]}, where=home)
            trust.pin(stale, "another fingerprint")
            self.enable("stale")
            with person():
                plugins.listing(self.root, wait=None)
            self.assertEqual(registry.read(self.root)["stale"]["events"], [])
            self.enable("stale", on=False)
        with self.subTest("an incompatible manifest admitted is refused"):
            odd = self.write_plugin("odd", {"extends": ["events"], "events": ["agent.status"]}, where=home)
            real_snapshot = registry.snapshot

            def swapped(directory):
                self.write_plugin("odd", {"api": "999.0", "extends": ["events"], "events": ["agent.status"]},
                                  where=home)
                return real_snapshot(directory)
            with person(), mock.patch.object(registry, "snapshot", side_effect=swapped), \
                    self.assertRaisesRegex(plugins.PluginError, "needs plugin API 999.0"):
                plugins.set_enabled(self.root, "odd", True)
            self.assertFalse(config.load(self.root).is_on("odd"))
            self.assertNotEqual(trust.state(odd), "trusted")
        with self.subTest("pinned in the person's own process too"):
            plugin(label="edited")
            with person():
                self.assertEqual(self.entry("relay").state, "changed")
        with self.subTest("an edited or shadowed folder changes no recorded subscription"):
            plugin(event="agent.ready", label="edited again")
            with person():
                plugins.listing(self.root, wait=None)  # the folder, edited
            shadow = plugin(event="agent.ready", label="shadow", where=self.work / "shadow")
            with mock.patch.dict(os.environ, {"MV2_PLUGIN_PATH": str(shadow.parent)}):
                plugins.listing(self.root, wait=None)  # another copy, found first
            self.assertEqual(registry.read(self.root)["relay"]["events"], ["agent.status"])
            plugin()  # back as it was
        with self.subTest("trust reaches the recorded folder past another copy"):
            other = plugin(label="other", where=self.work / "other")
            with mock.patch.dict(os.environ, {"MV2_PLUGIN_PATH": str(other.parent)}):
                done = tool.run(self.root, plugin_action="trust", name="relay")
            self.assertEqual((Path(done["directory"]), trust.state(folder), trust.state(other)),
                             (folder.resolve(), "trusted", "untrusted"))
        with self.subTest("an agent's own copy changes nothing recorded"):
            plugin(event="agent.ready", label="agent's", where=self.work / "agents")
            with mock.patch.dict(os.environ, {**agent, "MV2_PLUGIN_PATH": str(self.work / "agents")}):
                plugins.listing(self.root, wait=None)
            recorded = registry.read(self.root)["relay"]["directory"]
            self.assertEqual((recorded, events.subscribers(self.root, "agent.ready")), (str(folder.resolve()), []))
        with self.subTest("an older listing never replaces the subscriptions approved since"):
            with person():
                earlier = plugins.entries(self.root)  # saw agent.status
                plugin(event="agent.ready", label="approved later")
                tool.run(self.root, plugin_action="trust", name="relay")
                plugins.listing(self.root, wait=None)
                registry.record(self.root, earlier, person=True)
            self.assertEqual(events.subscribers(self.root, "agent.ready"), ["relay"])
            self.assertNotIn("relay", events.subscribers(self.root, "agent.status"))
        with self.subTest("a pin that moves as the record is written is what's published"):
            from multiverse.store import write
            stale = registry.read(self.root)
            write(registry.path(self.root), {"schema": 1, "plugins": {}})  # so this listing writes again
            plugin(label="approved as it's written")
            real_load = config.load

            def approving(root):  # under the writer lock, just before publishing
                trust.trust(folder)
                return real_load(root)
            with person():
                current = plugins.entries(self.root)
                with mock.patch.object(config, "load", side_effect=approving):
                    registry.record(self.root, current, person=True)
            self.assertEqual(registry.read(self.root)["relay"]["events"], ["agent.status"])
            self.assertEqual(stale["relay"]["directory"], registry.read(self.root)["relay"]["directory"])
        with self.subTest("a first listing records nothing that changed its name or API as it was read"):
            for name, change in (("renamed", {"name": "another"}), ("future", {"api": "999.0"})):
                made = self.write_plugin(name, {"extends": ["events"], "events": ["agent.status"]}, where=home)
                self.enable(name)
                snapshot = registry.snapshot

                def changing(directory, name=name, change=change):
                    self.write_plugin(name, {"extends": ["events"], "events": ["agent.ready"], **change}, where=home)
                    return snapshot(directory)
                with person(), mock.patch.object(registry, "snapshot", side_effect=changing):
                    plugins.listing(self.root, wait=None)
                self.assertEqual((trust.state(made), name in registry.read(self.root)), ("untrusted", False), name)
                config.set_enabled(self.root, name, False)
        with self.subTest("a first pin never replaces one published meanwhile"):
            fresh = plugin(label="unpinned", where=self.work / "fresh")
            value = trust.current(fresh)
            trust.pin(fresh, "published meanwhile")
            self.assertFalse(trust.pin_first(fresh, value))
            self.assertEqual(trust.state(fresh), "changed")
        with self.subTest("a record that isn't a file doesn't stall a read"):
            path = registry.path(self.root)
            path.unlink()
            os.mkfifo(path)
            started = time.monotonic()
            self.assertIsNone(registry.known(self.root))
            self.assertLess(time.monotonic() - started, 2)
            path.unlink()

    def test_a_plugin_loaded_from_its_folder_runs_its_pinned_copy_once_pinned(self):
        """Once a listing pins a folder, what this process loaded from it isn't used: the copy loads; else edits run."""
        import builtins
        import threading
        self.addCleanup(lambda: [builtins.__dict__.pop(name, None) for name in ("MV2_GATE", "MV2_IN")])
        later = "def later():\n    from .extra import value\n    return value\n"

        def version(name, value, gated=False):
            wait = ("    import builtins\n    builtins.MV2_IN.set()\n    builtins.MV2_GATE.wait(5)\n"
                    if gated else "")
            manifest = {"module": "plugin", **({"settings": {"version": 1}} if gated else {})}  # gated: checked
            return self.write_plugin(name, manifest,
                                     {"plugin.py": f"value = {value!r}\n{later}def register(mv):\n{wait}    pass\n",
                                      "extra.py": "value = 'approved'\n"})
        with self.subTest("loaded before the pin"):
            folder = version("done", "approved")
            self.enable("done")
            first = loader.load(self.entry("done"))  # from its own folder: nothing listed it yet
            states = {item["name"]: item["state"] for item in plugins.listing(self.root, wait=None)}  # pins it
            self.assertEqual(states["done"], "loaded")  # its copy, loaded again here at once
            pinned = loader.load(self.entry("done"))
            self.assertIsNot(pinned, first)
            self.assertNotIn(folder.resolve(), Path(pinned.module.__file__).resolve().parents)  # the copy's
            (folder / "extra.py").write_text("value = 'edited after the pin'\n")
            self.assertEqual(pinned.module.later(), "approved")
        with self.subTest("still loading when pinned"):
            builtins.MV2_GATE, builtins.MV2_IN = threading.Event(), threading.Event()
            folder = version("busy", "approved", gated=True)
            self.enable("busy")
            with self.assertRaises(loader.StillLoading):
                loader.load(self.entry("busy"), wait=0)
            self.assertTrue(builtins.MV2_IN.wait(5))  # imported from its folder, held in register()
            started = time.monotonic()
            states = {item["name"]: item["state"] for item in plugins.listing(self.root, wait=None)}
            self.assertLess(time.monotonic() - started, 2)  # its settings check never waits out that load
            self.assertEqual(states["busy"], "loading")  # its copy's load started, never waited out
            builtins.MV2_GATE.set()
            pinned = loader.load(self.entry("busy"))
            self.assertNotIn(folder.resolve(), Path(pinned.module.__file__).resolve().parents)

    def test_a_recorded_plugin_never_runs_unverified_whatever_its_record_says(self):
        """A recorded path plugin is verified if its record is damaged, gone, relinked or unpublished; else edits run."""
        from multiverse.plugins import registry
        home = self.work / "dev"
        person = mock.patch.dict(os.environ, {"MV2_PLUGIN_PATH": str(home)})

        def plugin(label, where=home):
            return self.write_plugin("relay", {"module": "plugin"},
                                     {"plugin.py": f"LABEL = {label!r}\ndef register(mv):\n    pass\n"}, where=where)
        folder = plugin("approved")
        with person:
            seen = {}
            real = config.set_enabled

            def publishing(*args, **kwargs):  # what a reader sees as the switch is published
                seen.update(recorded=(registry.read(self.root).get("relay") or {}).get("directory"),
                            pinned=trust.state(folder))
                plugins.listing(self.root, wait=0)  # a listing while it is still off
                seen.update(kept=(registry.read(self.root).get("relay") or {}).get("directory"))
                return real(*args, **kwargs)
            with mock.patch.object(config, "set_enabled", side_effect=publishing):
                plugins.set_enabled(self.root, "relay", True)
            with self.subTest("recorded and pinned before its switch is published"):
                self.assertEqual(seen, {"recorded": str(folder.resolve()), "pinned": "trusted",
                                        "kept": str(folder.resolve())})
            plugin("edited")
            self.assertEqual(self.entry("relay").state, "changed")
            path = registry.path(self.root)
            kept = path.read_bytes()
            with self.subTest("its record can't be read"):
                path.write_text("{ damaged")
                self.assertEqual(self.entry("relay").state, "changed")
            with self.subTest("its record is gone, its pin isn't"):
                path.unlink()
                self.assertEqual(self.entry("relay").state, "changed")
            path.write_bytes(kept)
            with self.subTest("its recorded folder relinked to another"):
                aside = self.work / "aside"
                folder.rename(aside)
                replacement = plugin("replacement", where=self.work / "elsewhere")
                folder.symlink_to(replacement, target_is_directory=True)
                self.assertNotIn(self.entry("relay").state, ("on", "loaded"))
                with self.subTest("relinked to a plugin of another name"):
                    trusted_other = self.write_plugin("namesake", {"module": "plugin"},
                                                      {"plugin.py": "def register(mv):\n    pass\n"},
                                                      where=self.root / "plugins")
                    trust.pin(trusted_other, trust.current(trusted_other))
                    config.set_enabled(self.root, "namesake", True)
                    folder.unlink()
                    renamed = self.write_plugin("namesake", {"module": "plugin"},
                                                {"plugin.py": "X = 1\ndef register(mv):\n    pass\n"},
                                                where=self.work / "renamed")
                    folder.symlink_to(renamed, target_is_directory=True)
                    self.assertEqual(self.entry("namesake").source, "universe")  # the relinked entry never masks it
                    alias = self.work / "dev-alias"
                    alias.symlink_to(home, target_is_directory=True)
                    with mock.patch.dict(os.environ, {"MV2_PLUGIN_PATH": str(alias)}):  # through an alias of its parent
                        self.assertEqual(self.entry("namesake").source, "universe")
                    alias.unlink()
                    folder.unlink()
                    folder.symlink_to(replacement, target_is_directory=True)
                with self.subTest("and its record a link to nothing"):
                    path.unlink()
                    path.symlink_to(self.work / "nowhere")
                    self.assertNotIn(self.entry("relay").state, ("on", "loaded"))
                    path.unlink()
                    path.write_bytes(kept)
                folder.unlink()
                aside.rename(folder)
            with self.subTest("a pin replaced before the switch is published leaves it off"):
                other = self.write_plugin("other", {"module": "plugin"}, {"plugin.py": "def register(mv):\n    pass\n"},
                                          where=home)
                real_record = registry.record

                def pin_replaced(*args, **kwargs):
                    done = real_record(*args, **kwargs)
                    trust.pin(other, "sha256:" + "0" * 64)
                    return done
                with mock.patch.object(registry, "record", side_effect=pin_replaced), \
                        self.assertRaisesRegex(plugins.PluginError, "left off"):
                    plugins.set_enabled(self.root, "other", True)
                self.assertNotEqual(config.load(self.root).enabled.get("other"), True)
        with self.subTest("an enable finishing late never undoes a later one"):
            def swap(where, event):
                return self.write_plugin("swap", {"module": "plugin", "extends": ["events"], "events": [event]},
                                         {"plugin.py": "def register(mv):\n    pass\n"}, where=where)
            first, second = swap(self.work / "a", "agent.status"), swap(self.work / "b", "agent.ready")
            real_anchor, turned = plugins.anchor_events, []

            def again(root, name, again=False):  # the first enable has published; a second one, elsewhere, now
                if not turned:
                    turned.append(True)
                    with mock.patch.dict(os.environ, {"MV2_PLUGIN_PATH": str(second.parent)}):
                        plugins.set_enabled(self.root, "swap", False)
                        plugins.set_enabled(self.root, "swap", True)
                return real_anchor(root, name, again=again)
            with mock.patch.dict(os.environ, {"MV2_PLUGIN_PATH": str(first.parent)}), \
                    mock.patch.object(plugins, "anchor_events", side_effect=again):
                plugins.set_enabled(self.root, "swap", True)
            self.assertEqual((registry.read(self.root)["swap"]["directory"], registry.read(self.root)["swap"]["events"]),
                             (str(second.resolve()), ["agent.ready"]))
        with self.subTest("an enable that looked before another turned it on elsewhere is refused"):
            def race(where):
                return self.write_plugin("race", {"module": "plugin"}, {"plugin.py": "def register(mv):\n    pass\n"},
                                         where=where)
            near, far = race(self.work / "near"), race(self.work / "far")
            real_writing, raced = registry.writing, []

            def first_elsewhere(root, name):  # as this enable reaches the writer lock, another publishes `far`
                if not raced:
                    raced.append(True)
                    with mock.patch.dict(os.environ, {"MV2_PLUGIN_PATH": str(far.parent)}):
                        plugins.set_enabled(self.root, "race", True)
                return real_writing(root, name)
            with mock.patch.dict(os.environ, {"MV2_PLUGIN_PATH": str(near.parent)}), \
                    mock.patch.object(registry, "writing", side_effect=first_elsewhere), \
                    self.assertRaisesRegex(plugins.PluginError, "turn it off first"):
                plugins.set_enabled(self.root, "race", True)
            self.assertEqual(registry.read(self.root)["race"]["directory"], str(far.resolve()))
        with self.subTest("a listing while its folder is briefly gone keeps what was prepared"):
            brief = self.write_plugin("brief", {"module": "plugin"}, {"plugin.py": "def register(mv):\n    pass\n"},
                                      where=home)
            real, kept_then = config.set_enabled, []

            def vanishing(*args, **kwargs):
                brief.rename(self.work / "brief-away")
                plugins.listing(self.root, wait=0)
                kept_then.append((registry.read(self.root).get("brief") or {}).get("directory"))
                (self.work / "brief-away").rename(brief)
                return real(*args, **kwargs)
            with person, mock.patch.object(config, "set_enabled", side_effect=vanishing):
                plugins.set_enabled(self.root, "brief", True)
            self.assertEqual(kept_then, [str(brief.resolve())])
        with self.subTest("a trusted universe copy keeps its place over a lower one, a package"):
            winner = self.write_plugin("twin", {"module": "plugin"}, {"plugin.py": "def register(mv):\n    pass\n"},
                                       where=self.root / "plugins")
            trust.pin(winner, trust.current(winner))
            shadow = self.write_plugin("twin", {"module": "plugin"}, {"plugin.py": "X = 1\ndef register(mv):\n    pass\n"},
                                       where=self.work / "site")
            with mock.patch.dict(os.environ, {"MV2_PLUGIN_PATH": ""}), \
                    mock.patch.object(loader, "installed_directories", return_value=[shadow]):
                plugins.set_enabled(self.root, "twin", True)
                self.assertEqual((self.entry("twin").source, registry.read(self.root)["twin"]["directory"]),
                                 ("universe", None))

    def test_a_plugin_on_here_acts_the_same_in_every_process_however_it_was_found(self):
        """A plugin only the person's MV2_PLUGIN_PATH finds is journaled and run, pinned, by agents too; else events go missing."""
        from multiverse.plugins import events, registry
        dev = self.work / "dev"
        directory = self.write_plugin("relay", {"module": "plugin", "extends": ["events"], "events": ["agent.*"]},
                                      {"plugin.py": "def register(mv):\n    mv.events.on('agent.*', lambda e: None)\n"},
                                      where=dev)
        person = {"MV2_PLUGIN_PATH": os.pathsep.join([os.environ["MV2_PLUGIN_PATH"], str(dev)])}
        agent = {"MV2_PLUGIN_PATH": os.environ["MV2_PLUGIN_PATH"], "MV2_CONTROL_SOCKET": str(self.work / "s.sock")}
        with mock.patch.dict(os.environ, person):
            plugins.set_enabled(self.root, "relay", True)
        recorded = registry.read(self.root)["relay"]
        self.assertEqual((recorded["events"], recorded["source"], recorded["directory"]),
                         (["agent.*"], "path", str(directory.resolve())))
        with self.subTest("an agent's process, without that path"), mock.patch.dict(os.environ, agent):
            self.assertEqual(events.subscribers(self.root, "agent.status"), ["relay"])
            found = self.entry("relay")
            self.assertEqual((found.state, found.source), ("on", "recorded"))
            loaded = loader.load(found)
            self.assertNotIn(directory.resolve(), Path(loaded.module.__file__).resolve().parents)  # its pinned copy runs
        with mock.patch.dict(os.environ, agent):
            self.assertEqual(self.entry("relay").state, "loaded")  # listed, and kept, just before the edit
        (directory / "plugin.py").write_text("def register(mv):\n    raise SystemExit('edited')\n")
        with self.subTest("edited after it was recorded"), mock.patch.dict(os.environ, agent):
            self.assertEqual(self.entry("relay").state, "changed")  # never runs what the person's process didn't see
            self.assertEqual(events.subscribers(self.root, "agent.status"), ["relay"])  # still recorded for later
        with mock.patch.dict(os.environ, person):
            plugins.listing(self.root, wait=None)  # a listing approves nothing
        with mock.patch.dict(os.environ, agent):
            self.assertEqual(self.entry("relay").state, "changed")
        with mock.patch.dict(os.environ, person):
            tool.run(self.root, plugin_action="trust", name="relay")  # the person approves the edit
        with self.subTest("trusted again by the person"), mock.patch.dict(os.environ, agent):
            self.assertEqual(self.entry("relay").state, "on")
        with self.subTest("an agent's process records patterns, never a folder"), mock.patch.dict(os.environ, agent):
            own = self.write_plugin("mine", {"extends": ["events"], "events": ["agent.status"]}, where=self.work / "own")
            here = os.pathsep.join([agent["MV2_PLUGIN_PATH"], str(own.parent)])
            with mock.patch.dict(os.environ, {"MV2_PLUGIN_PATH": here}):
                plugins.set_enabled(self.root, "mine", True)
            self.assertEqual(registry.read(self.root)["mine"], {"events": ["agent.status"], "source": "path",
                                                                "directory": None})
            self.assertEqual(registry.read(self.root)["relay"]["directory"], str(directory.resolve()))  # kept
            # Another process, without that agent's path, still records its events: from the patterns alone.
            self.assertIn("mine", events.subscribers(self.root, "agent.status"))
        with self.subTest("decided without finding plugins"), mock.patch.dict(os.environ, agent), \
                mock.patch.object(loader, "manifest_records", side_effect=OSError("no discovery")):
            self.assertEqual(events.subscribers(self.root, "agent.status"), ["mine", "relay"])
        with self.subTest("a plugin turned on by hand doesn't cost a recorded one"), mock.patch.dict(os.environ, agent):
            config.set_enabled(self.root, "legacy", True)  # on, never recorded: found by discovery, if at all
            with mock.patch.object(loader, "declared", side_effect=OSError("no discovery")):
                self.assertEqual(events.subscribers(self.root, "agent.status"), ["mine", "relay"])
            config.set_enabled(self.root, "legacy", False)
        with self.subTest("a listing older than a plugin turned on"), mock.patch.dict(os.environ, person):
            stale = loader.entries(self.root)
            late = self.write_plugin("late", {"extends": ["events"], "events": ["agent.*"]}, where=self.work / "late")
            with mock.patch.dict(os.environ, {"MV2_PLUGIN_PATH": os.pathsep.join([person["MV2_PLUGIN_PATH"],
                                                                                   str(late.parent)])}):
                plugins.set_enabled(self.root, "late", True)
            registry.record(self.root, stale, person=True)  # written after, from what it saw before
            self.assertEqual(registry.read(self.root)["late"]["directory"], str(late.resolve()))
        with self.subTest("a listing older than a move"), mock.patch.dict(os.environ, person):
            stale = loader.entries(self.root)  # sees relay where it was
            moved = self.write_plugin("relay", {"module": "plugin", "extends": ["events"], "events": ["agent.*"]},
                                      {"plugin.py": "def register(mv):\n    pass\n"}, where=self.work / "relay-new")
            with mock.patch.dict(os.environ, {"MV2_PLUGIN_PATH": os.pathsep.join([agent["MV2_PLUGIN_PATH"],
                                                                                   str(moved.parent)])}):
                with self.assertRaisesRegex(plugins.PluginError, "turn it off first"):
                    plugins.set_enabled(self.root, "relay", True)  # on from one folder: never moved while on
                self.assertEqual(registry.read(self.root)["relay"]["directory"], str(directory.resolve()))
                plugins.set_enabled(self.root, "relay", False)
                plugins.set_enabled(self.root, "relay", True)  # off, then on again where it is now
            registry.record(self.root, stale, person=True)
            self.assertEqual(registry.read(self.root)["relay"]["directory"], str(moved.resolve()))
            with mock.patch.dict(os.environ, {"MV2_PLUGIN_PATH": os.pathsep.join([agent["MV2_PLUGIN_PATH"],
                                                                                   str(directory.parent)])}):
                plugins.set_enabled(self.root, "relay", False)
                plugins.set_enabled(self.root, "relay", True)  # and back, for the cases below
        with self.subTest("an agent turning it on can't move the person's record"), mock.patch.dict(os.environ, agent):
            mine = self.write_plugin("relay", {"module": "plugin", "extends": ["events"], "events": ["agent.*"]},
                                     {"plugin.py": "def register(mv):\n    pass\n"},
                                     where=trust.user_directory() / "plugins")
            directory.rename(directory.with_name("relay-away"))  # the person's folder, gone for a moment
            plugins.set_enabled(self.root, "relay", True)  # an agent's enable, finding the personal one
            self.assertEqual(registry.read(self.root)["relay"]["directory"], str(directory.resolve()))
            directory.with_name("relay-away").rename(directory)
            shutil.rmtree(mine)
        with self.subTest("a listing that can't find it keeps its record"), mock.patch.dict(os.environ, agent):
            moved = directory.with_name("relay-away")
            directory.rename(moved)
            self.addCleanup(lambda: moved.exists() and moved.rename(directory))
            plugins.listing(self.root, wait=None)
            self.assertEqual(registry.read(self.root)["relay"]["directory"], str(directory.resolve()))
            moved.rename(directory)
        with self.subTest("turned off from a shell that finds it only by its record"):
            with mock.patch.dict(os.environ, {"MV2_PLUGIN_PATH": ""}):
                done = tool.run(self.root, plugin_action="disable", name="relay")  # its record is dropped as it goes
            self.assertEqual((done["on"], done["state"]), (False, "off"))
            self.assertNotIn("relay", registry.read(self.root))
            self.assertIs(config.load(self.root).enabled.get("relay"), False)
            with mock.patch.dict(os.environ, person):
                plugins.set_enabled(self.root, "relay", True)  # back on, for the case below
        with self.subTest("turned off"), mock.patch.dict(os.environ, person):
            plugins.set_enabled(self.root, "relay", False)
            self.assertNotIn("relay", registry.read(self.root))
            with mock.patch.dict(os.environ, agent):
                self.assertNotIn("relay", events.subscribers(self.root, "agent.status"))


class ConfigTest(PluginTestCase):
    def test_plugins_json_keeps_unknown_keys_and_survives_damage(self):
        """Writing plugins.json keeps keys from newer and older mv2, and an unusable file is never rewritten and turns
        every plugin off (a missing one means the defaults); a break goes unnoticed because older runners sharing the
        universe silently turn every plugin off, the next write drops the newer fields, or a torn file turns a
        plugin a person turned off back on, or gives back the access a person took away."""
        path = config.path(self.root)
        with self.subTest("an older mv2's keys"):
            # "keys" is from before plugins lost their dashboard keys: still read, and kept.
            path.write_text(json.dumps({"schema": 1, "theme": "dusk", "keys": {"u": None}, "enabled": {"a": True}}))
            config.set_enabled(self.root, "b", True)
            self.assertEqual(json.loads(path.read_text()),
                             {"schema": 1, "theme": "dusk", "keys": {"u": None}, "enabled": {"a": True, "b": True}})
            self.assertIsNone(config.load(self.root).problem)
        with self.subTest("a newer mv2's keys"):
            self.write_plugin("tool")
            config.set_enabled(self.root, "tool", True)
            path.write_text(json.dumps({**json.loads(path.read_text()), "layout": {"tool": "left"},
                                        "sandbox": "strict"}))
            loaded = config.load(self.root)
            self.assertIsNone(loaded.problem)
            self.assertTrue(loaded.is_on("tool"))
            self.assertEqual(plugins.entry(self.root, "tool").state, "on")
            self.assertTrue(any("layout, sandbox" in note for note in plugins.notes(self.root)))
            config.set_enabled(self.root, "tool", False)  # writing it keeps what a newer mv2 put there
            self.assertEqual(json.loads(path.read_text())["layout"], {"tool": "left"})
        with self.subTest("unusable, after a person turned a default-on plugin off and lowered a grant"):
            bundled = self.work / "bundled"
            self.write_plugin("deft", {"default": "on"}, where=bundled)
            with mock.patch.object(loader, "BUNDLED", bundled):
                path.write_text(json.dumps({"schema": 1, "enabled": {"deft": False, "tool": True},
                                            "access": {"tool": "observe"}}))
                loader.reset()
                self.assertEqual((plugins.entry(self.root, "deft").on, loader.access_level(
                    self.root, plugins.entry(self.root, "tool"))), (False, "observe"))
                path.write_text('{"schema": 1, "enabled": {"deft": false, "tool": tr')  # torn mid-write
                loader.reset()
                for name in ("deft", "tool"):
                    found = plugins.entry(self.root, name)
                    self.assertEqual((found.on, loader.access_level(self.root, found)), (False, None), name)
                self.assertIn("every plugin is off here until it is repaired", plugins.problems(self.root)[0])
                path.unlink()  # missing, not damaged: the defaults
                loader.reset()
                self.assertTrue(plugins.entry(self.root, "deft").on)
        with self.subTest("torn between reading the plugin and deciding its access"):
            path.write_text(json.dumps({"schema": 1, "enabled": {"tool": True}, "access": {"tool": "observe"}}))
            loader.reset()
            found = plugins.entry(self.root, "tool")  # read while the file was whole: on, at observe
            path.write_text('{"schema": 1, "enabled": {"tool": tr')  # then torn before the decision reads it
            self.assertIsNone(loader.access_level(self.root, found))  # never the request (act) from an empty read
            path.write_text(json.dumps({"schema": 1, "enabled": {"tool": False}}))  # or turned off meanwhile
            self.assertIsNone(loader.access_level(self.root, found))
        with self.subTest("unusable"):
            for text in ('{"enabled": {"a": "yes"}}', '{"schema": 1, "access": {}, "enabled": {"a": "yes"}}'):
                path.write_text(text)
                found = config.load(self.root)
                self.assertEqual(found.enabled, {})
                self.assertIn("enabled.a must be true or false", found.problem)
                with self.assertRaisesRegex(ValueError, "enabled.a"):
                    config.set_enabled(self.root, "b", True)
                self.assertEqual(path.read_text(), text)
                self.assertIn("plugins.json cannot be used", plugins.problems(self.root)[0])


CHECKED = """
def register(mv):
    def check(values):
        if values.get("level", 1) not in (1, 2):
            raise ValueError("level must be 1 or 2")
    mv.settings.check(check)
"""
SCREENED = """
def register(mv):
    def check(values):
        if values.get("level", 1) not in (1, 2):
            raise ValueError("level must be 1 or 2")
    mv.settings.check(check)
    mv.dashboard.settings(screen="screens:Settings")
    def run(root, **arguments):
        return {"ran": True}
    mv.cli.command("screened", lambda parser: None, run)
"""
LEVELS = {"type": "object", "properties": {"schema": {"type": "integer"}, "level": {"type": "integer"}}}
LEVEL_SETTINGS = {"version": 1, "schema": "settings.schema.json", "defaults": "defaults/settings.json"}


class SettingsTest(PluginTestCase):
    def test_long_settings_notes_keep_the_path_in_every_diagnostic(self):
        """Truncating a fallback note can lose the settings path needed to fix it across every diagnostic view."""
        note = "fallback_model is not used: " + str(self.root / ("long-workspace/" * 24) / 'harnesses/claude/harness.json')
        self.greeter('long-note', "def register(mv):\n"
                                  "    @mv.settings.check\n"
                                  "    def check(values):\n"
                                  f"        return [{(note + chr(10) + 'second line hidden')!r}, 3] + "
                                  "['extra ' + str(i) for i in range(25)]\n")
        self.enable('long-note')
        expected = [note, *[f'extra {i}' for i in range(19)]]
        for action in ('info', 'settings', 'list'):
            with self.subTest(action=action):
                result = tool.run(self.root, plugin_action=action, name='long-note' if action != 'list' else None)
                shown = tool.text(action, result)
                self.assertIn(note, shown)
                self.assertNotIn('second line hidden', shown)
                notes = (result['notes'] if action == 'settings' else result['settings']['notes']
                         if action == 'info' else next(row for row in result['plugins']
                                                      if row['name'] == 'long-note')['settings']['notes'])
                self.assertEqual(notes, expected)
        self.assertTrue(any(note in problem for problem in doctor.run(self.root)['problems']))

    def greeter(self, name, check=None):
        self.write_plugin(name, {"module": "plugin", "settings": {
            "version": 2, "schema": "schema.json", "defaults": "defaults.json", "legacy": f"{name}.json"}},
            {"plugin.py": "def register(mv):\n    pass\n" if check is None else check,
             "schema.json": json.dumps(SCHEMA), "defaults.json": json.dumps({"schema": 2, "greeting": "hello"})})
        return self.entry(name).manifest

    def leveled(self, name, code=CHECKED):
        self.write_plugin(name, {"module": "plugin", "settings": LEVEL_SETTINGS}, {
            "plugin.py": code, "settings.schema.json": json.dumps(LEVELS),
            "defaults/settings.json": json.dumps({"schema": 1, "level": 1})})
        self.enable(name)
        return settings.path(self.root, name)

    def mv2_settings(self, name, *assignments):
        """`mv2 plugins settings NAME --set ...` as a new process runs it: nothing loaded yet."""
        loader.reset()
        return tool.run(self.root, plugin_action="settings", name=name, assignments=assignments)

    def state(self, name):
        loader.reset()
        return next(item for item in plugins.listing(self.root) if item["name"] == name)["state"]

    def test_settings_edit_writes_only_valid_named_changes(self):
        """A settings edit changes only named keys, DELETE removes one, a schema refusal or the plugin's own check
        (plugin on or off) writes nothing, a refused file reads unusable while the plugin keeps its command, a plugin's
        own settings screen is had where it is off too (or why not), on request and never by listing it, and
        enabling twice never reinstalls defaults, and notes a check returns keep the settings usable and show in info,
        settings and as doctor problems; fragile because the check and the screen need the plugin's code even where it is off
        (COMPACTION-2), and a break quietly writes a bad file, says "no schema" on the Plugins screen, imports off
        plugins on every mv2 plugins, or drops a note such as an ignored key nobody then sees."""
        with self.subTest("only the named keys"):
            manifest_ = self.greeter("greeter")
            path = settings.path(self.root, "greeter")
            path.parent.mkdir(exist_ok=True)
            path.write_text('{"schema": 2, "limits": {"count": 3}, "greeting": "hey"}')
            settings.edit(self.root, manifest_, {"limits.count": 4})
            self.assertEqual(json.loads(path.read_text()), {"schema": 2, "limits": {"count": 4}, "greeting": "hey"})
            settings.edit(self.root, manifest_, {("limits", "count"): settings.DELETE})
            self.assertEqual(json.loads(path.read_text()), {"schema": 2, "greeting": "hey"})
            with self.assertRaisesRegex(ValueError, "at most 10"):
                settings.edit(self.root, manifest_, {"limits.count": 11})
            with self.assertRaisesRegex(ValueError, "unknown setting 'colour'"):
                settings.edit(self.root, manifest_, {"colour": "red"})
            self.assertEqual(json.loads(path.read_text()), {"schema": 2, "greeting": "hey"})
        with self.subTest("defaults are installed once"):
            self.greeter("fresh")
            result = plugins.set_enabled(self.root, "fresh", True)
            self.assertEqual(result["installed"], [str(settings.path(self.root, "fresh"))])
            self.assertEqual(plugins.set_enabled(self.root, "fresh", True)["installed"], [])
            self.assertEqual(plugins.settings_of(self.root, "fresh")[0], {"schema": 2, "greeting": "hello"})
        with self.subTest("its own check's notes: the settings stay usable, and info and settings show them"):
            self.greeter("noted", "def register(mv):\n"
                                  "    @mv.settings.check\n"
                                  "    def check(values):\n"
                                  "        if values.get('greeting') == 'yo':\n"
                                  "            return ['greeting: yo is informal, kept\\nmore', 3]\n")
            self.enable("noted")
            path = settings.path(self.root, "noted")
            path.parent.mkdir(exist_ok=True)
            path.write_text('{"schema": 2, "greeting": "yo"}')
            loader.load(self.entry("noted"))
            note = "greeting: yo is informal, kept"  # its first line only; the 3 is no note
            self.assertTrue(self.entry("noted").usable)
            listed = next(item for item in plugins.listing(self.root, wait=None) if item["name"] == "noted")
            self.assertEqual((listed["settings"]["problem"], listed["settings"]["notes"]), (None, [note]))
            for action in ("info", "settings"):
                result = tool.run(self.root, plugin_action=action, name="noted")
                self.assertEqual(result["notes"] if action == "settings" else result["settings"]["notes"], [note])
                self.assertIn(note, tool.text(action, result))
            report = doctor.run(self.root)  # and doctor counts it, so a typo can't go unseen
            self.assertIn(f"Plugin noted: {path}: {note}", report["problems"])
            self.assertFalse(report["ok"])
        with self.subTest("its own check, once loaded"):
            self.greeter("scary", "def register(mv):\n"
                                  "    @mv.settings.check\n"
                                  "    def check(values):\n"
                                  "        if values.get('greeting') == 'boo':\n"
                                  "            raise ValueError('greeting: too scary')\n")
            self.enable("scary")
            loader.load(self.entry("scary"))
            with self.assertRaisesRegex(ValueError, "too scary"):
                plugins.edit_settings(self.root, "scary", {"greeting": "boo"})
            settings.path(self.root, "scary").write_text('{"schema": 2, "greeting": "boo"}')
            found = self.entry("scary")
            self.assertEqual(found.state, "unusable")
            self.assertFalse(found.usable)
            self.assertNotIn("scary", [loaded.name for loaded in loader.active(self.root)])
        with self.subTest("its own check refuses an edit"):
            path = self.leveled("picky")
            self.mv2_settings("picky", "level=2")
            before = path.read_text()
            with self.assertRaisesRegex(ValueError, "level must be 1 or 2"):
                self.mv2_settings("picky", "level=3")
            self.assertEqual(path.read_text(), before)
            self.assertIn(self.state("picky"), ("on", "loaded"))
        with self.subTest("its own check runs where the plugin is off"):
            path = self.leveled("dormant")
            self.enable("dormant", on=False)
            with self.assertRaisesRegex(ValueError, "level must be 1 or 2"):
                self.mv2_settings("dormant", "level=3")
            self.assertFalse(path.exists())
        with self.subTest("a file its check refuses"):
            path = self.leveled("refused")
            path.parent.mkdir(exist_ok=True)
            path.write_text(json.dumps({"schema": 1, "level": 9}))
            self.assertEqual(self.state("refused"), "unusable")
            loader.reset()
            self.assertEqual(tool.run(self.root, plugin_action="info", name="refused")["state"], "unusable")
            self.assertIn("level must be 1 or 2", self.mv2_settings("refused")["problem"])
            loader.reset()
            self.assertTrue(any("refused" in text and "its settings cannot be used" in text
                                for _, text in doctor.check(self.root)))
            self.mv2_settings("refused", "level=1")  # and it can be fixed from the command line
            self.assertIn(self.state("refused"), ("on", "loaded"))
        with self.subTest("code that cannot run: its schema alone"):
            path = self.leveled("broken", "raise RuntimeError('cannot load')\n")
            written = self.mv2_settings("broken", "level=3")
            self.assertIn("failed to load: RuntimeError: cannot load", written["unchecked"])
            self.assertEqual(json.loads(path.read_text())["level"], 3)
            with self.assertRaises(ValueError):
                self.mv2_settings("broken", "level=x")
        with self.subTest("a refused file keeps its command and screen"):
            self.write_plugin("screened", {"module": "plugin", "extends": ["dashboard.settings", "cli"],
                                           "commands": {"screened": "Show its state"}, "settings": {"version": 1}},
                              {"plugin.py": SCREENED})
            self.enable("screened")
            settings.path(self.root, "screened").write_text('{"schema": 1, "level": 9}')
            loader.reset()  # a fresh process: nothing loaded yet
            item = next(entry for entry in plugins.listing(self.root) if entry["name"] == "screened")
            self.assertEqual(item["settings"]["screen"], "screens:Settings")
            self.assertIn("screened", plugins.commands(self.root))
            loader.reset()
            with mock.patch.object(sys, "argv", ["mv2", "-u", str(self.root), "screened"]), \
                    contextlib.chdir(self.work):
                code, out, err = run_quietly(cli.main)
            self.assertEqual(code, 0, err)
            self.assertEqual(json.loads(out), {"ran": True})
            self.assertIn("plugin screened: its settings cannot be used: level must be 1 or 2", err)
        with self.subTest("off here: listed without importing it; its own screen comes on request, or why not"):
            settings.path(self.root, "screened").write_text('{"schema": 1, "level": 1}')
            self.enable("screened", on=False)
            loader.reset()  # a fresh process: nothing loaded yet
            item = next(entry for entry in plugins.listing(self.root) if entry["name"] == "screened")
            self.assertNotIn("screened", {Path(key).name for key in loader._loaded})  # off: never imported here
            self.assertEqual((item["state"], item["settings"]["own_view"], item["settings"]["problem"]),
                             ("off", True, None))  # the file is fine: no load problem in its place
            self.assertEqual(plugins.settings_view(self.root, "screened"),
                             {"screen": "screens:Settings", "fields": None, "problem": None, "loading": False})
            loader.reset()  # a fresh process whose load is still running: not failed, so no schema fallback
            with mock.patch.object(loader, "load", side_effect=loader.StillLoading("slow")):
                view = plugins.settings_view(self.root, "screened")
            self.assertEqual((view["loading"], view["problem"]), (True, "screened is still loading"))
            loader.reset()
            with mock.patch.dict(os.environ, {"MV2_PLUGINS": "off"}):
                item = next(entry for entry in plugins.listing(self.root) if entry["name"] == "screened")
                view = plugins.settings_view(self.root, "screened")
            self.assertIsNone(item["settings"]["problem"])
            self.assertEqual((view["screen"], view["fields"]), (None, None))
            self.assertIn("MV2_PLUGINS=off", view["problem"])


class LoadingTest(PluginTestCase):
    def test_bootstrap_lines_are_bounded(self):
        """A plugin's bootstrap lines are bounded in count and length and refused if they hold any line separator;
        a break goes unnoticed because the extra text silently enters every agent's bootstrap, costing context or
        injecting lines into the prompt."""
        self.write_plugin("chatty", {"module": "plugin", "extends": ["bootstrap"]},
                          {"plugin.py": "def register(mv):\n    for n in range(3): mv.bootstrap.line(f'line {n}')\n"})
        self.enable("chatty")
        self.assertEqual(self.entry("chatty").state, "on")
        self.assertEqual(plugins.bootstrap_lines(self.root), [])
        self.assertIn("at most 2 bootstrap lines", self.entry("chatty").problem)
        self.enable("chatty", on=False)
        for text in ("one\rtwo", "one two", "one\ntwo", "one\x85two", " "):
            with self.subTest(text=text):
                self.write_plugin("split", {"module": "plugin", "extends": ["bootstrap"]},
                                  {"plugin.py": f"def register(mv): mv.bootstrap.line({text!r})\n"})
                self.enable("split")
                loader.reset()
                self.assertEqual(catalog._plugin_lines(self.root), [])
                self.assertIn("one line of text", self.entry("split").problem)
        self.enable("split", on=False)
        self.write_plugin("long", {"module": "plugin", "extends": ["bootstrap"]},
                          {"plugin.py": "def register(mv):\n    mv.bootstrap.line('a' * 200)\n"
                                        "    mv.bootstrap.line('b' * 101)\n"})
        self.enable("long")
        self.assertEqual(catalog._plugin_lines(self.root), [])
        self.assertIn(f"{api.BOOTSTRAP_CHARACTERS} characters", self.entry("long").problem)


    def test_a_column_stands_in_only_for_a_data_column_and_only_alone(self):
        """replaces= naming Name, Live, Status or Focus (or an alias for one) is said as a problem and the column
        loads without it, and two plugins replacing one column cancel, said too: a break would silently hide
        core's focus, waiting and damaged markers, or let the dashboard flip between two plugins' columns."""
        def column(key, replaces, fallback=None):
            extra = f", fallback={fallback!r}" if fallback else ""
            return (f"    mv.dashboard.column({key!r}, {key.title()!r}, 6, lambda row: None, "
                    f"replaces={replaces!r}{extra})\n")
        for name, lines in (("estimate", column("estimate", "tokens")),
                            ("rival", column("rival", "context") + column("marker", "activity")),
                            ("own", column("own", "phase", fallback="summary"))):
            self.write_plugin(name, {"api": "1.13", "module": "plugin", "extends": ["dashboard.column"]},
                              {"plugin.py": "def register(mv):\n" + lines})
            self.enable(name)
        plugins.listing(self.root)  # recorded and pinned first: from then on they run from their pinned copies
        problems = plugins.problems(self.root)  # loads them; listing() itself never imports a plugin
        found = {item["name"]: item for item in plugins.listing(self.root)}
        self.assertEqual((found["estimate"]["provides"]["replaces"], found["rival"]["provides"]["replaces"],
                          found["own"]["provides"]["replaces"]), (["context"], ["context"], ["phase"]))
        self.assertIn("estimate and rival both replace the context column, so neither does", problems)
        self.assertIn("rival's column marker can't replace Status: Name, Live, Status and Focus always show core's "
                      "own", problems)
        registered = {item.name: item.loaded.registrations.columns for item in loader.entries(self.root)
                      if item.loaded is not None and item.name in found}
        marker, own, estimate = registered["rival"]["marker"], registered["own"]["own"], registered["estimate"]["estimate"]
        self.assertEqual((marker.replaces, marker.fallback), (None, None))  # it still loads, under its own key
        self.assertEqual((own.replaces, own.fallback), ("phase", "summary"))  # an explicit fallback wins
        self.assertEqual((estimate.replaces, estimate.fallback), ("context", "context"))  # replaces implies it
        self.enable("rival", on=False)
        self.assertFalse([line for line in plugins.problems(self.root) if "replace" in line])


class ApiTest(PluginTestCase):
    def test_api_calls_need_their_declared_permission(self):
        """An API call without its declared permission raises (including agents in a panel's context without
        agents.read), a registration outside extends leaves the plugin out, and an import error leaves only it
        out; a break goes unnoticed because the plugin simply gets access the user never approved."""
        with self.subTest("registrations and imports"):
            self.write_plugin("crash", {"module": "plugin"}, {"plugin.py": "raise RuntimeError('boom')\n"})
            self.write_plugin("sneaky", {"module": "plugin"},
                              {"plugin.py": "def register(mv): mv.bootstrap.line('x')\n"})
            self.write_plugin("good", {"module": "plugin", "extends": ["bootstrap"]},
                              {"plugin.py": "def register(mv): mv.bootstrap.line('fine')\n"})
            for name in ("crash", "sneaky", "good"):
                self.enable(name)
            self.assertEqual([loaded.name for loaded in loader.active(self.root)], ["good"])
            states = {found.name: (found.state, found.problem) for found in plugins.entries(self.root)}
            self.assertEqual(states["crash"], ("failed", "crash failed to load: RuntimeError: boom"))
            self.assertIn('does not list it in "extends"', states["sneaky"][1])
            self.assertTrue(any("crash failed to load" in problem for problem in plugins.problems(self.root)))
            self.assertEqual(plugins.bootstrap_lines(self.root), ["fine"])
        with self.subTest("calls"):
            self.write_plugin("probe", {"module": "plugin"}, {"plugin.py": "def register(mv): pass\n"})
            self.enable("probe")
            universe = api.Plugin(loader.load(self.entry("probe"))).universe(self.root)
            with self.assertRaisesRegex(api.Error, "did not ask for permission 'agents.read'"):
                universe.agents()
            with self.assertRaisesRegex(api.Error, "agents.control"):
                universe.spawn("worker")
            self.assertEqual(universe.name, "test")
            self.assertTrue(any(item["name"] == "coordination" for item in universe.catalog()))
        with self.subTest("a panel's agents"):
            panel = ("def register(mv):\n"
                     "    @mv.dashboard.panel('board', title='Board')\n"
                     "    def board(context):\n"
                     "        return []\n")
            self.write_plugin("boards", {"module": "plugin", "extends": ["dashboard.panel"],
                                         "permissions": ["agents.read"]}, {"plugin.py": panel})
            self.write_plugin("blind", {"module": "plugin", "extends": ["dashboard.panel"]}, {"plugin.py": panel})
            self.enable("boards")
            self.enable("blind")
            rows = [{"id": "a1", "name": "worker", "universe": str(self.root), "live": "alive"}]
            found = {loaded.name: loaded for loaded, _, _ in plugins.dashboard_panels(self.root)}
            self.assertEqual([agent.name for agent in plugins.panel_context(found["boards"], self.root, {}, rows,
                                                                            set()).agents], ["worker"])
            self.assertEqual(plugins.panel_context(found["blind"], self.root, {}, rows, set()).agents, ())

    def test_history_gives_the_last_messages_and_which_are_unread_without_reading_any(self):
        """history() gives the last K messages flagged unread as the inbox shows them, marking none read; it once always raised, and a consuming read or a wrong flag hides an unseen message."""
        import shutil
        from multiverse.universe import mailbox
        self.write_plugin("reader", {"module": "plugin", "permissions": ["agents.read", "messages.read"]},
                          {"plugin.py": "def register(mv): pass\n"})
        self.enable("reader")
        directory, person = self.agent("worker"), {"name": "user", "agent_id": None}
        for text in ("one", "two"):
            mailbox.append_message(directory, text, person)
        mailbox.inbox(directory)  # the agent reads both
        mailbox.append_message(directory, "go", person, delivery="steer")  # delivered natively, never in its inbox
        mailbox.append_message(directory, "up", person, delivery="follow-up", inbox_only_text="up")  # its inbox copy
        mailbox.append_message(directory, "held", person, intended="steer", focus="f1")
        for text in ("three", "four"):
            mailbox.append_message(directory, text, person)
        mailbox.held(directory, "f1")  # mv2 held reads one ahead of the read position
        records = ("inbox.cursor", "inbox-read.json", "inbox-state.json")
        before = {name: (directory / name).read_bytes() if (directory / name).exists() else None for name in records}
        worker = api.Plugin(loader.load(self.entry("reader"))).universe(self.root).agent("worker")
        self.assertEqual([(message["text"], message["unread"]) for message in worker.history(last=6)],
                         [("two", False), ("go", False), ("up", True), ("held", False), ("three", True),
                          ("four", True)])
        self.assertEqual([message["text"] for message in worker.history(last=2)], ["three", "four"])
        self.assertEqual(set(worker.history(last=1)[0]), {"at", "from", "text", "delivery", "unread"})
        self.assertEqual({name: (directory / name).read_bytes() if (directory / name).exists() else None
                          for name in records}, before)  # nothing marked read
        self.assertEqual([message["text"] for message in worker.inbox()][-2:], ["three", "four"])
        self.assertEqual(len(worker.inbox()), 3)  # still unread: the copy, three and four
        with self.assertRaisesRegex(api.Error, "positive number"):
            worker.history(last=0)
        shutil.rmtree(directory)
        with self.assertRaises(api.Error):
            worker.history()
        self.assertFalse(directory.exists())  # a removed agent's folder isn't made again

    def test_a_plugin_sends_as_the_user_only_when_it_asks(self):
        """agent.send(as_user=True) marks the message as the person's while mv2 records the plugin (1.21); a lost mark sends the person's relayed words as a plugin's, and a lost sender hides who sent it."""
        from multiverse.universe import mailbox
        self.write_plugin("relay", {"module": "plugin", "permissions": ["agents.read", "messages.send"],
                                    "access": {"request": "act", "minimum": "act"}},
                          {"plugin.py": "def register(mv): pass\n"})
        self.enable("relay")
        directory = self.agent("worker")
        worker = api.Plugin(loader.load(self.entry("relay"))).universe(self.root).agent("worker")
        worker.send("plain", delivery="inbox-only")
        worker.send("typed by the person", delivery="inbox-only", as_user=True)
        sent = mailbox.inbox(directory, all_messages=True, limit=None)["messages"][-2:]
        self.assertEqual([(message["from"]["plugin"], message.get("as_user")) for message in sent],
                         [("relay", None), ("relay", True)])
        with self.assertRaisesRegex(api.Error, "as_user"):
            worker.send("x", delivery="inbox-only", as_user="yes")
        relay = api.Plugin(loader.load(self.entry("relay")))
        self.assertFalse(relay.in_agent_session(self.root))  # the person's shell (1.21)
        with mock.patch.dict(os.environ, {"MV2_CONTROL_SOCKET": str(self.work / "s.sock")}):  # an agent's mark
            self.assertTrue(relay.in_agent_session(self.root))
        from multiverse.runtime import agent_identity
        with mock.patch.dict(os.environ, {"MV2_UNIVERSE": str(self.root)}), \
                mock.patch.object(agent_identity, "runs_in_agent", return_value=False) as checked:
            relay.in_agent_session()  # no universe given: the pane check still runs, in the current one
        self.assertEqual(checked.call_args_list[-1], mock.call(root=self.root))

    def test_a_plugin_reads_one_messages_sender_by_its_id_never_its_text(self):
        """agent.message(id) gives a turn event's message's sender as mv2 records it, without its text (1.21); else a plugin matches 'Message from' text, or reads what it may not."""
        from multiverse.universe import mailbox
        self.write_plugin("reader", {"module": "plugin", "permissions": ["agents.read", "messages.read"]},
                          {"plugin.py": "def register(mv): pass\n"})
        self.write_plugin("blind", {"module": "plugin", "permissions": ["agents.read"]},
                          {"plugin.py": "def register(mv): pass\n"})
        for name in ("reader", "blind"):
            self.enable(name)
        directory = self.agent("worker")
        sent = mailbox.append_message(directory, "the secret plan", {"name": "plugin relay", "plugin": "relay",
                                                                     "agent_id": None}, "steer")
        worker = api.Plugin(loader.load(self.entry("reader"))).universe(self.root).agent("worker")
        found = worker.message(sent["id"])
        self.assertEqual(found, {"at": sent["at"], "delivery": "steer", "as_user": False,
                                 "from": {"name": "plugin relay", "plugin": "relay", "agent_id": None}})
        self.assertNotIn("the secret plan", repr(found))
        with self.assertRaises(api.Error):
            worker.message("not-a-message")
        with self.assertRaisesRegex(api.Error, "messages.read"):
            api.Plugin(loader.load(self.entry("blind"))).universe(self.root).agent("worker").message(sent["id"])
        real, moved = mailbox.lock, self.work / "worker-moved"

        def removed_meanwhile(path, **keywords):  # the agent's folder goes after the check, before the read's lock
            if not moved.exists():
                directory.rename(moved)
            return real(path, **keywords)
        with mock.patch.object(mailbox, "lock", side_effect=removed_meanwhile):
            with self.assertRaises(api.AgentGone):
                worker.message(sent["id"])
        self.assertFalse(directory.exists())  # and the read never made it again

    def test_an_agents_harness_home_is_its_recorded_storage(self):
        """mv.harness_info(..., agent=) gives the home the agent's launches use, from its records (session home,
        storage, or the legacy evidence resume adopts; an unset default only under a recorded launch HOME), unknown
        (None) for a launched agent with no proof, and this process's environment only for one never launched; and
        its recorded storage values, an explicit value kept apart from a recorded absence even where both name one
        directory, completed with what resolves it (the launch HOME for an unset or empty home variable, the
        workspace for a relative value) or None without it; a break goes unnoticed because tests run every process
        with one environment and directory, while the dashboard or the host may run with another and then read
        another store, or run Claude under another config."""
        self.write_plugin("reader", {"module": "plugin", "permissions": ["agents.read"]},
                          {"plugin.py": "def register(mv): pass\n"})
        self.enable("reader")
        mv = api.Plugin(loader.load(self.entry("reader")))
        mine = Path(os.environ["CLAUDE_CONFIG_DIR"])  # this process's Claude home
        recorded = self.work / "launch-claude"
        launch_home = self.work / "launch-home"  # the HOME a launch's request recorded
        rows = {  # name: (properties, the last launch's request env and unset_env or None, expected home)
            "session-home": ({"native_session_home": str(recorded)}, None, recorded),
            "storage": ({"native_storage_environment": {"CLAUDE_CONFIG_DIR": str(recorded)}}, None, recorded),
            "storage-unset-without-launch-home": ({"native_storage_environment": {"CLAUDE_CONFIG_DIR": None}},
                                                  None, None),
            "storage-unset-with-launch-home": ({"native_storage_environment": {"CLAUDE_CONFIG_DIR": None}},
                                               ({"HOME": str(launch_home)}, []), launch_home / ".claude"),
            "launched-without-proof": ({}, None, None),
            "never-launched": ({}, "no launch at all", mine),
            "damaged-runtime-is-a-launch": ({}, "runtime without its launch id", None),
            "a-session-journal-is-a-launch": ({}, "a session journal only", None),
            "native-session-without-proof": ({"native_session_id": "c1"}, None, None),
            "damaged": ({"native_storage_environment": ["not", "a", "record"]}, None, None),
            "explicit-default-directory": ({"native_storage_environment": {"CLAUDE_CONFIG_DIR": str(
                Path(os.environ["HOME"]) / ".claude")}}, None, Path(os.environ["HOME"]) / ".claude"),
            "empty-with-launch-home": ({"native_storage_environment": {"CLAUDE_CONFIG_DIR": ""}},
                                       ({"HOME": str(launch_home)}, []), launch_home / ".claude"),
            "relative-to-the-workspace": ({"native_storage_environment": {"CLAUDE_CONFIG_DIR": "relative-config"}},
                                          None, self.work / "project" / "relative-config"),
            "relative-without-an-absolute-workspace": ({"workspace": "somewhere", "native_storage_environment": {
                "CLAUDE_CONFIG_DIR": "relative-config"}}, None, None)}
        old_codex = str(self.work / "proven-old-home")
        codex_rows = {  # agents from before storage was recorded: what a resume adopts, never the reader's
            "legacy-codex": ({"native_session_id": "x1", "native_service_identity": {
                "provider": "codex", "scope": {"CODEX_HOME": old_codex, "CODEX_SQLITE_HOME": None}},
                "native_environment": {"CODEX_HOME": old_codex}}, None, Path(old_codex)),
            "unbound-but-launched-codex": ({}, ({"CODEX_HOME": old_codex}, ["CODEX_SQLITE_HOME"]), Path(old_codex)),
            "legacy-scope-without-a-runtime": ({"native_service_identity": {
                "provider": "codex", "scope": {"CODEX_HOME": old_codex, "CODEX_SQLITE_HOME": None}}},
                "no launch at all", Path(old_codex)),
            "codex-without-proof": ({"native_session_id": "x2"}, None, None)}
        old_codex_storage = {"CODEX_HOME": old_codex, "CODEX_SQLITE_HOME": None}
        storage_rows = {  # the environment that selects the same store, where proven (every other row: None)
            "storage": {"CLAUDE_CONFIG_DIR": str(recorded)},
            "storage-unset-with-launch-home": {"CLAUDE_CONFIG_DIR": None, "HOME": str(launch_home)},
            "empty-with-launch-home": {"CLAUDE_CONFIG_DIR": "", "HOME": str(launch_home)},
            "relative-to-the-workspace": {"CLAUDE_CONFIG_DIR": str(self.work / "project" / "relative-config")},
            "explicit-default-directory": {"CLAUDE_CONFIG_DIR": str(Path(os.environ["HOME"]) / ".claude")},
            "legacy-codex": old_codex_storage, "unbound-but-launched-codex": old_codex_storage,
            "legacy-scope-without-a-runtime": old_codex_storage}
        with mock.patch.dict(os.environ, {"CODEX_HOME": str(self.work / "reader-home")}):
            for harness_name, table in (("claude", rows), ("codex", codex_rows)):
                for name, (properties, request, expected) in table.items():
                    with self.subTest(name):
                        directory = self.agent(name, harness=harness_name, **properties)
                        if request == "no launch at all":
                            (directory / "runtime.json").unlink()
                        elif request == "runtime without its launch id":
                            (directory / "runtime.json").write_text('{"state": "running"}')
                        elif request == "a session journal only":
                            (directory / "runtime.json").unlink()
                            (directory / "sessions.jsonl").write_text("{}\n")
                        elif request is not None:
                            launch = json.loads((directory / "runtime.json").read_text())["launch_id"]
                            (directory / "launches" / launch).mkdir(parents=True)
                            (directory / "launches" / launch / "request.json").write_text(json.dumps(
                                {"argv": ["x"], "env": request[0], "unset_env": request[1]}))
                        agent = mv.universe(self.root).agent(name)
                        info = mv.harness_info(self.root, harness_name, agent=agent)
                        self.assertEqual((info.home, info.storage), (expected, storage_rows.get(name)))
        with self.subTest("without an agent: this process's environment, as before"):
            self.assertEqual((mv.harness_info(self.root, "claude").home, mv.harness_info(self.root, "claude").storage),
                             (mine, None))
        with self.subTest("an agent of another harness is refused"):
            self.agent("coder", harness="codex")
            with self.assertRaisesRegex(api.Error, "coder runs harness 'codex', not 'claude'"):
                mv.harness_info(self.root, "claude", agent=mv.universe(self.root).agent("coder"))
            with self.assertRaisesRegex(api.Error, r"pass mv\.universe\(universe\)\.agent\('storage'\)"):
                mv.harness_info(self.root, "claude", agent="storage")  # a name is refused, saying what to pass

    def test_a_filesystem_failure_is_an_oserror_too(self):
        """A filesystem failure in an API call (EACCES, ENOSPC) is an api.Error and also the OSError it was, errno
        kept, as state_dir and agent_state_dir raised in 1.9; a break goes unnoticed because tests rarely fill a
        disk, and a 1.9 plugin's except OSError then crashes on the first full one."""
        self.write_plugin("keeper", {"module": "plugin", "permissions": ["agents.read"]},
                          {"plugin.py": "def register(mv): pass\n"})
        self.enable("keeper")
        mv = api.Plugin(loader.load(self.entry("keeper")))
        self.agent("worker")
        worker = mv.universe(self.root).agent("worker")
        for failure, kind in ((PermissionError(errno.EACCES, "Permission denied"), PermissionError),
                              (OSError(errno.ENOSPC, "No space left on device"), OSError)):
            for name, call in (("state_dir", lambda: mv.state_dir(self.root)),
                               ("agent_state_dir", lambda: mv.agent_state_dir(worker)),
                               ("files.ensure_dir", lambda: api.files.ensure_dir(self.work / "elsewhere" / "a"))):
                with self.subTest(name, errno=errno.errorcode[failure.errno]), \
                        mock.patch.object(Path, "mkdir", side_effect=failure):
                    with self.assertRaises(api.Error) as caught:
                        call()
                    self.assertIsInstance(caught.exception, kind)
                    self.assertEqual(caught.exception.errno, failure.errno)
                    self.assertIn(failure.strerror, str(caught.exception))


HELPERS = """
import json, sys

def register(mv):
    mv.helper.action("refuse", lambda arguments: 2)
    mv.helper.action("crash", lambda arguments: 1 / 0)

    @mv.helper.action("status")
    def status(arguments):
        print("status", *arguments)
        return 3

    @mv.helper.action("guard")
    def guard(arguments):
        request = json.load(sys.stdin)
        for n in range(3):
            print(json.dumps({"event": "progress", "n": n}), flush=True)
        return 2 if request.get("refuse") else 0
"""


class HelperExitTest(PluginTestCase):
    def test_helper_exit_codes_and_streams(self):
        """python -m multiverse.plugins run exits 1 with empty stdout when mv2 refuses and 2 only for the action's
        own refusal, with the action's own stdin and stdout; a break goes unnoticed in mv2 because GOAT's Claude
        hook reads these codes, so a stray print or a crash exiting 2 changes what Claude does."""
        self.write_plugin("guardian", {"module": "plugin", "extends": ["helper"]}, {"plugin.py": HELPERS})
        self.write_plugin("broken", {"module": "plugin", "extends": ["helper"]}, {"plugin.py": "raise SystemExit(2)\n"})
        self.enable("guardian")
        self.enable("broken")
        root = str(self.root)
        # One real process: the exit code, stdin and stdout of `python -m` itself.
        environment = {**os.environ, "PYTHONPATH": str(Path(__file__).resolve().parents[1] / "src")}
        result = subprocess.run([sys.executable, "-m", "multiverse.plugins", "-u", root, "run", "guardian", "guard"],
                                input='{"refuse": true}', env=environment, capture_output=True, text=True, timeout=60)
        self.assertEqual(result.returncode, 2, result.stderr)
        self.assertEqual([json.loads(line)["n"] for line in result.stdout.splitlines()], [0, 1, 2])
        cases = (
            (("-u", root, "run", "guardian", "refuse"), 2, ""),  # deliberate
            (("-u", root, "run", "guardian", "status", "a", "--b"), 3, "status a --b\n"),
            (("-u", root, "run", "guardian", "guard"), 0, "".join(f'{{"event": "progress", "n": {n}}}\n'
                                                                    for n in range(3))),
            ((), 1, ""), (("run",), 1, ""), (("--nope",), 1, ""),
            (("-u", root, "run", "ghost", "refuse"), 1, ""), (("-u", root, "run", "broken", "refuse"), 1, ""),
            (("-u", str(self.work / "none"), "run", "a", "b"), 1, ""),
            (("-u", root, "run", "guardian", "missing"), 1, ""), (("-u", root, "run", "guardian", "crash"), 1, ""),
        )
        for arguments, code, stdout in cases:
            with self.subTest(arguments):
                loader.reset()  # each a new process
                self.assertEqual(run_quietly(lambda: helper.main(list(arguments)), stdin="{}")[:2], (code, stdout))
        self.assertIn("ZeroDivisionError", (self.root / "state" / "plugins" / "guardian" / "errors.log").read_text())


class GuardedTest(PluginTestCase):
    def test_guarded_calls_are_bounded_contained_and_lock_free(self):
        """loader.guarded(), the wrapper around every call into plugin code, times out, never starts a second
        thread for a busy site, contains SystemExit, records errors without raising and holds no global lock
        during the call; fragile because holding loader._lock deadlocks a plugin call that loads another plugin,
        and a break shows only with a slow plugin, as a frozen dashboard or killed host."""
        self.write_plugin("calls", {"module": "plugin"}, {"plugin.py": "def register(mv): pass\n"})
        self.enable("calls")
        loaded = loader.load(self.entry("calls"))
        with self.subTest("an error is recorded, never raised"):
            self.assertEqual(loader.guard(loaded, "column z", lambda: 1 / 0, root=self.root, default="—"), "—")
            self.assertEqual(loader.errors()["calls"], ["column z: ZeroDivisionError: division by zero"])
            log = (self.root / "state" / "plugins" / "calls" / "errors.log").read_text()
            self.assertIn("ZeroDivisionError", log)
            self.assertEqual(plugins.listing(self.root)[0]["errors"], ["column z: ZeroDivisionError: division by zero"])
        with self.subTest("time, busy sites and keys"):
            release = threading.Event()
            self.addCleanup(release.set)

            def hang():
                release.wait(10)
                return "late"
            before = len(loaded.errors)
            first = loader.guarded(loaded, "column x", hang, root=self.root, timeout=0.1)
            self.assertEqual(first.state, "timeout")
            busy = loader.guarded(loaded, "column x", hang, root=self.root, timeout=0.1)
            self.assertEqual((busy.state, busy.value), ("busy", None))
            other = loader.guarded(loaded, "column y", lambda: 3, root=self.root, timeout=0.1)
            self.assertEqual((other.state, other.value), ("ok", 3))
            keyed = loader.guarded(loaded, "column x", lambda: 4, root=self.root, timeout=0.1, key="row 2")
            self.assertEqual((keyed.state, keyed.value), ("ok", 4))  # a key of its own is not busy
            self.assertEqual(loaded.errors[before:], ["column x: TimeoutError: took longer than 0.1 s"])  # once
            release.set()

        def leave():
            raise SystemExit(3)
        for timeout in (None, 1):
            with self.subTest("SystemExit", timeout=timeout):
                outcome = loader.guarded(loaded, "action", leave, root=self.root, timeout=timeout)
                self.assertEqual(outcome.state, "error")
                self.assertIn("SystemExit", outcome.error)
                self.assertIsNone(loader.guard(loaded, "action", leave, root=self.root, timeout=timeout, default=None))
        with self.subTest("no global lock"):
            held = threading.Event()
            holder = threading.Thread(target=lambda: (loader._lock.acquire(), held.wait(10), loader._lock.release()))
            holder.start()
            self.addCleanup(holder.join, 10)
            self.addCleanup(held.set)
            started = time.monotonic()
            outcome = loader.guarded(loaded, "fact", lambda: 5, root=self.root, timeout=1)
            self.assertEqual((outcome.state, outcome.value), ("ok", 5))
            self.assertLess(time.monotonic() - started, 1)
            held.set()
