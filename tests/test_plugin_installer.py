"""mv2 plugins install, update and remove: plugins fetched from local git repositories and folders (never the
network), shown before they're placed, pinned at their content, and removed only where the installer put them."""
import json
import os
import shutil
from pathlib import Path
import subprocess
import sys
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from multiverse import plugins
from multiverse.plugins import installer, loader, trust
from multiverse.plugins.testing import PluginTestCase
from multiverse.tools import plugins as tool

CODE = "def register(mv):\n    pass\n"


class InstallerTest(PluginTestCase):
    def setUp(self):
        super().setUp()
        self.repository = self.work / "notes-repo"
        self.git("init", "--quiet", "--initial-branch=main", str(self.repository), cwd=self.work)
        self.commit({"plugins/notes/plugin.json": self.manifest(), "plugins/notes/plugin.py": CODE,
                     "README.md": "tools\n"}, "first")
        self.git("tag", "v1.0")
        self.url = self.repository.resolve().as_uri()

    def git(self, *args, cwd=None):
        return subprocess.run(["git", "-c", "user.name=t", "-c", "user.email=t@example.com", *args],
                              cwd=cwd or self.repository, check=True, capture_output=True, text=True,
                              stdin=subprocess.DEVNULL).stdout.strip()

    def manifest(self, version="1.0.0", **fields):
        return json.dumps({"schema": 1, "name": "notes", "version": version, "api": "1.0", "module": "plugin",
                           **fields})

    def commit(self, files, message):
        for relative, text in files.items():
            path = self.repository / relative
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(text)
        self.git("add", "-A")
        self.git("commit", "--quiet", "-m", message)
        return self.git("rev-parse", "HEAD")

    def staging(self):
        base = trust.user_directory() / installer.STAGING
        return sorted(child.name for child in base.iterdir()) if base.is_dir() else []

    def state(self, name="notes"):
        loader.reset()
        return plugins.entry(self.root, name).state

    def test_an_install_runs_only_what_the_person_saw_and_pins_it(self):
        """A plugin is placed only after a preview, from exactly its files, and changed files never run; else unseen code runs."""
        source = f"{self.url}@v1.0#plugins/notes"
        with mock.patch.dict(os.environ, {"MV2_AGENT_ID": "x"}), self.assertRaisesRegex(plugins.PluginError,
                                                                                       "Only the person"):
            plugins.install_preview(source)
        shown = plugins.install_preview(source)
        self.assertEqual((shown["name"], shown["follows"], shown["ref"], shown["path"], shown["placement"]),
                         ("notes", "tag", "v1.0", "plugins/notes", "personal"))
        self.assertEqual(shown["runs"]["python"], ["plugin.py"])
        target = trust.user_directory() / "plugins" / "notes"
        self.assertFalse(target.exists())  # nothing placed before the yes
        with self.subTest("the staged files changed after they were shown"):
            (trust.user_directory() / installer.STAGING / shown["key"] / "plugin" / "plugin.py").write_text("x = 1\n")
            with self.assertRaisesRegex(plugins.PluginError, "changed after they were shown"):
                plugins.install_confirm(shown["key"])
            self.assertFalse(target.exists())
        result = tool.run(self.root, plugin_action="install", name=source)
        self.assertEqual((result["result"], result.get("needs_yes")), (None, True))  # no terminal: no yes
        result = tool.run(self.root, plugin_action="install", name=source, yes=True)["result"]
        self.assertEqual((result["name"], Path(result["target"])), ("notes", target))
        self.assertEqual(self.staging(), [])
        self.assertEqual(trust._records()[str(target.resolve())].get("events"),
                         sorted(json.loads((target / "plugin.json").read_text()).get("events", [])))  # for the journal
        self.enable("notes")
        self.assertEqual(self.state(), "on")
        listed = {item["name"]: item for item in plugins.listing(self.root, wait=0)}["notes"]
        self.assertEqual((listed["install"]["source"], listed["install"]["follows"]), (source, "tag"))
        self.assertNotEqual(Path(loader.load(plugins.entry(self.root, "notes")).manifest.directory).resolve(),
                            target.resolve())  # it runs from a copy of exactly the pinned bytes
        (target / "plugin.py").write_text(CODE + "# edited\n")
        self.assertEqual(self.state(), "changed")
        alias = {"MV2_PLUGIN_PATH": os.pathsep.join([str(target), os.environ["MV2_PLUGIN_PATH"]])}
        with mock.patch.dict(os.environ, alias):  # found through a path naming it, and listed there
            plugins.listing(self.root, wait=0)
            self.assertEqual(trust.state(target), "changed")  # approves no edit: only the person's trust does
        with self.assertRaisesRegex(ValueError, "mv2 plugins remove"):
            tool.run(self.root, plugin_action="untrust", name="notes")  # would unpin it, not stop it
        tool.run(self.root, plugin_action="trust", name="notes")  # the person approves the edit
        self.assertEqual(self.state(), "on")
        with self.subTest("a folder"):
            folder = self.write_plugin("jot", {"module": "plugin"}, {"plugin.py": CODE}, where=self.work / "src")
            done = tool.run(self.root, plugin_action="install", name=str(folder), yes=True)["result"]
            self.assertEqual((done["name"], done["commit"]), ("jot", None))

    def test_an_update_asks_only_when_what_it_can_do_changes(self):
        """An update asks only when what the plugin can do changes; a wrong rule slips new powers past the person."""
        tool.run(self.root, plugin_action="install", name=f"{self.url}#plugins/notes", yes=True)
        two = self.commit({"plugins/notes/plugin.py": CODE + "# two\n"}, "code only")
        shown = plugins.update_preview("notes")
        self.assertEqual((shown["ask"], shown["commit"], len(shown["update"]["changes"])), (False, two, 1))
        plugins.install_cancel(shown["key"])
        updated = tool.run(self.root, plugin_action="update", name="notes")
        self.assertEqual(updated["result"]["commit"], two)
        self.assertIn("code only", tool.text("update", updated))  # the commits it brought, though nothing asked
        self.assertTrue(tool.run(self.root, plugin_action="update", name="notes")["preview"]["update"]["same"])
        self.commit({"plugins/notes/plugin.json": self.manifest("2.0.0", permissions=["agents.read"])}, "reads agents")
        asked = tool.run(self.root, plugin_action="update", name="notes")
        self.assertEqual((asked["result"], asked["preview"]["update"]["powers_changed"]), (None, ["permissions"]))
        self.assertEqual(installer.Place().records()["notes"]["commit"], two)  # nothing changed without the yes
        self.assertEqual(tool.run(self.root, plugin_action="update", name="notes", yes=True)["result"]["version"],
                         "2.0.0")
        with self.subTest("a tag install follows the newest tag"):
            tool.run(self.root, plugin_action="remove", name="notes")
            tool.run(self.root, plugin_action="install", name=f"{self.url}@v1.0#plugins/notes", yes=True)
            self.git("tag", "v1.10")
            newest = plugins.update_preview("notes")
            plugins.install_cancel(newest["key"])
            self.assertEqual(newest["ref"], "v1.10")
        with self.subTest("a commit install names where to go"):
            tool.run(self.root, plugin_action="remove", name="notes")
            tool.run(self.root, plugin_action="install", name=f"{self.url}@{two[:10]}#plugins/notes", yes=True)
            with self.assertRaisesRegex(plugins.PluginError, "pinned to commit"):
                plugins.update_preview("notes")
        self.assertEqual(self.staging(), [])

    def test_a_failed_install_leaves_nothing_behind(self):
        """Each failed fetch installs nothing and leaves nothing staged; a half-placed plugin could load unseen code."""
        self.commit({"links/notes/plugin.json": self.manifest(), "links/notes/plugin.py": CODE}, "links")
        os.symlink("/etc/hosts", self.repository / "links/notes/hosts")
        self.commit({}, "a link")
        hand = self.write_plugin("jot", {}, {}, where=trust.user_directory() / "plugins")
        self.write_plugin("pathy", {}, {})  # on MV2_PLUGIN_PATH
        for case, source, message in (
                ("no repository", (self.work / "nothing").as_uri(), "git could not fetch"),
                ("no such ref", f"{self.url}@v9#plugins/notes", "no tag, branch or commit named v9"),
                ("no such folder", f"{self.url}#plugins/gone", "no folder plugins/gone"),
                ("not a plugin", self.url, "isn't a plugin"),
                ("a link inside", f"{self.url}#links/notes", "symbolic link"),
                ("outside the repository", f"{self.url}#../x", "inside the repository"),
                ("a command URL", "ext::sh -c touch% /tmp/pwned", "neither a folder here nor a git URL"),
                ("an option", "--upload-pack=touch /tmp/pwned", "not a git URL or a folder"),
                ("a folder there already", str(self.write_plugin("jot", {}, {}, where=self.work / "other")),
                 "already there and wasn't installed"),
                ("a name another source has", str(self.write_plugin("pathy", {}, {}, where=self.work / "other")),
                 "a path plugin named pathy is already at")):
            with self.subTest(case):
                with self.assertRaisesRegex(plugins.PluginError, message):
                    plugins.install_preview(source)
                self.assertEqual(self.staging(), [])
                self.assertEqual(installer.Place().records(), {})
        self.assertTrue((hand / "plugin.json").is_file())  # a plugin placed by hand is left alone

    def test_remove_takes_only_what_the_installer_placed_and_says_what_is_left(self):
        """Remove takes only the installer's folder, record and pin and lists what stays; a wrong one deletes a hand-placed plugin."""
        self.write_plugin("jot", {}, {}, where=trust.user_directory() / "plugins")
        with self.assertRaisesRegex(plugins.PluginError, "wasn't installed by mv2 plugins install"):
            plugins.remove_installed("jot", self.root)
        self.commit({"plugins/notes/plugin.json": self.manifest(
            secrets={"token": {"label": "Token"}}, python={"requirements": "requirements.txt"}),
            "plugins/notes/requirements.txt": f"tinypkg==1.0 --hash=sha256:{'a' * 64}\n"}, "a secret, a package")
        installed = tool.run(self.root, plugin_action="install", name=f"{self.url}#plugins/notes", here=True,
                             yes=True)
        self.assertIn("  from ", tool.text("install", installed))  # --yes still shows what it installed
        done = installed["result"]
        target = self.root / "plugins" / "notes"
        self.assertEqual((Path(done["target"]), done["placement"]), (target, "universe"))
        self.enable("notes")
        self.assertEqual(self.state(), "on")  # trusted at what was installed
        (self.root / "plugin-settings").mkdir(exist_ok=True)
        (self.root / "plugin-settings" / "notes.json").write_text("{}\n")
        tool.run(self.root, plugin_action="secret", name="notes", level="token", secret_value="s3cret")
        from multiverse.plugins import manifest as manifests, packages
        folder = packages.folder(manifests.load(target))
        folder.mkdir(parents=True)  # as if its packages were installed
        with mock.patch.dict(os.environ, {"MV2_AGENT_ID": "x"}), self.assertRaisesRegex(plugins.PluginError,
                                                                                       "Only the person"):
            plugins.remove_installed("notes", self.root)
        removed = tool.run(self.root, plugin_action="remove", name="notes")
        self.assertFalse(target.exists())
        self.assertNotIn(str(target.resolve()), trust._records())
        self.assertEqual(installer.Place(self.root).records(), {})
        left = " ".join(item["what"] for item in removed["left"])
        for kept in ("plugins.json still turns it on", "plugin-settings/notes.json", "secret token", str(folder)):
            self.assertIn(kept, left)
        self.assertTrue((self.root / "plugin-settings" / "notes.json").is_file())
        for item in removed["left"]:  # each command it gives works
            words = item["clear"].split()
            if words[:3] == ["mv2", "plugins", "disable"]:
                self.assertIs(tool.run(self.root, plugin_action="disable", name=words[3])["forgotten"], True)
            elif words[:3] == ["mv2", "plugins", "secret"]:
                self.assertIs(tool.run(self.root, plugin_action="secret", name=words[3], level=words[4],
                                       clear=True)["cleared"], True)
        with self.subTest("installing where plugins.json still turns it on"):
            from multiverse.plugins import config
            config.set_enabled(self.root, "notes", True)  # left from before, as if never disabled
            shown = plugins.install_preview(f"{self.url}#plugins/notes", self.root, here=True)
            self.assertTrue(shown["on_here"])
            self.assertIn("still turns notes on", tool.preview_text("install", shown))
            plugins.install_confirm(shown["key"], self.root)
        with self.subTest("the screen's selection, exactly"):
            # A universe install and a personal one of one name: the screen acts on the one it showed, never the
            # other when the shown one is gone or changed.
            tool.run(self.root, plugin_action="install", name=f"{self.url}#plugins/notes", yes=True)
            row = installer.pinned(self.root)[str((trust.user_directory() / "plugins" / "notes").resolve())]
            with self.assertRaisesRegex(plugins.PluginError, "changed since it was shown"):
                plugins.remove_installed("notes", self.root, placement="personal",
                                         expected={**row, "commit": "0" * 40})
            tool.run(self.root, plugin_action="remove", name="notes")  # the personal one
            for call in (lambda: plugins.remove_installed("notes", self.root, placement="personal"),
                         lambda: plugins.update_preview("notes", root=self.root, placement="personal")):
                with self.assertRaisesRegex(plugins.PluginError, "no personal install of notes"):
                    call()
            self.assertTrue((target / "plugin.json").is_file())  # the universe's is untouched

    def test_an_installed_plugin_never_runs_unpinned_or_loses_what_it_replaced(self):
        """Pinning survives a lost record, a failed save puts everything back, and update/remove act only on the folder they placed; else edits run unseen or a hand-placed plugin is deleted."""
        target = trust.user_directory() / "plugins" / "notes"
        tool.run(self.root, plugin_action="install", name=f"{self.url}#plugins/notes", yes=True)
        self.enable("notes")
        with self.subTest("the install record lost"):
            installer.Place().record.unlink()
            (target / "plugin.py").write_text(CODE + "# edited\n")
            self.assertEqual(self.state(), "changed")
            trust.forget(target)  # and the pin: the marker inside still says it was installed
            self.assertEqual(self.state(), "untrusted")
            shutil.rmtree(target)
        with self.subTest("the record can't be saved"):
            with mock.patch.object(installer.Place, "save", side_effect=OSError(28, "No space left on device")):
                with self.assertRaises(OSError):
                    tool.run(self.root, plugin_action="install", name=f"{self.url}#plugins/notes", yes=True)
            self.assertEqual((target.exists(), str(target.resolve()) in trust._records(), self.staging()),
                             (False, False, []))
        with self.subTest("a first install whose leftover pin can't be put back"):
            trust.pin(target, "sha256:" + "0" * 64)  # left by an earlier failed first install
            with mock.patch.object(installer.Place, "save", side_effect=OSError(28, "No space left on device")), \
                    mock.patch.object(installer.trust, "pin", side_effect=[None, OSError(28, "No space left")]):
                with self.assertRaisesRegex(plugins.PluginError, "nothing was installed"):
                    tool.run(self.root, plugin_action="install", name=f"{self.url}#plugins/notes", yes=True)
            self.assertFalse(target.exists())
            trust.forget(target)
        first = tool.run(self.root, plugin_action="install", name=f"{self.url}#plugins/notes", yes=True)["result"]
        with self.subTest("an update whose record can't be saved"):
            self.commit({"plugins/notes/plugin.py": CODE + "# two\n"}, "two")
            with mock.patch.object(installer.Place, "save", side_effect=OSError(28, "No space left on device")):
                with self.assertRaises(OSError):
                    tool.run(self.root, plugin_action="update", name="notes")
            self.assertEqual((installer.Place().records()["notes"]["commit"], self.state()), (first["commit"], "on"))
            self.assertEqual((target / "plugin.py").read_text(), CODE)
        with self.subTest("an update shown before another one"):
            # A folder install has no commit to tell two updates apart: approving a code-only update must not
            # install over one that, meanwhile, changed what it can do.
            jot = self.write_plugin("jot", {"module": "plugin"}, {"plugin.py": CODE}, where=self.work / "src")
            tool.run(self.root, plugin_action="install", name=str(jot), yes=True)
            (jot / "plugin.py").write_text(CODE + "# code only\n")
            stale = plugins.update_preview("jot")
            self.assertFalse(stale["ask"])
            self.write_plugin("jot", {"module": "plugin", "permissions": ["agents.read"]}, {}, where=self.work / "src")
            tool.run(self.root, plugin_action="update", name="jot", yes=True)
            with self.assertRaisesRegex(plugins.PluginError, "changed since this update was shown"):
                plugins.install_confirm(stale["key"])
            self.assertEqual(installer.Place().records()["jot"]["powers"]["permissions"], ["agents.read"])
        with self.subTest("a folder there that the installer didn't place"):
            moved = self.work / "moved-notes"
            os.replace(target, moved)
            self.write_plugin("notes", {}, {}, where=trust.user_directory() / "plugins")
            for call in (lambda: plugins.update_preview("notes"), lambda: plugins.remove_installed("notes")):
                with self.assertRaisesRegex(plugins.PluginError, "isn't the folder mv2 plugins install placed"):
                    call()
            self.assertTrue((target / "plugin.json").is_file())
        self.assertEqual(self.staging(), [])

    def test_a_fetch_runs_nothing_from_the_repository_and_shows_its_text_escaped(self):
        """No link, filter or control character from a repository acts before the yes; else a fetch reads outside it, runs a command, or redraws the question."""
        outside = self.work / "outside"
        self.write_plugin("notes", {"module": "plugin"}, {"plugin.py": CODE}, where=outside)
        linked = self.work / "linked-repo"
        self.git("init", "--quiet", "--initial-branch=main", str(linked), cwd=self.work)
        os.symlink(outside, linked / "plugins")
        self.git("add", "-A", cwd=linked)
        self.git("commit", "--quiet", "-m", "linked", cwd=linked)
        with self.assertRaisesRegex(plugins.PluginError, "no folder plugins .*it is a link"):
            plugins.install_preview(f"{linked.resolve().as_uri()}#plugins/notes")
        ran = self.work / "filter-ran"
        config = self.work / "gitconfig"
        # Quoted: unquoted, git's config would end the command at the ";", and a checkout would fail, not run it.
        config.write_text(f"[filter \"evil\"]\n\tsmudge = \"sh -c 'touch {ran}; cat'\"\n\trequired = true\n")
        self.commit({".gitattributes": "* filter=evil\n", "plugins/notes/plugin.json": self.manifest(
            description="Notes\x1b[2J\x1b[Hall clear\u202e")}, "with a filter\x1b[31m")
        with mock.patch.dict(os.environ, {"GIT_CONFIG_GLOBAL": str(config)}):
            tool.run(self.root, plugin_action="install", name=f"{self.url}#plugins/notes", yes=True)
            self.assertFalse(ran.exists())
        self.commit({"plugins/notes/plugin.py": CODE + "# more\n"}, "subject\x1b[2Jhidden")
        shown = plugins.update_preview("notes")
        plugins.install_cancel(shown["key"])
        text = tool.preview_text("update", shown) + tool.preview_text("install", {**shown, "update": None})
        self.assertNotIn("\x1b", text)
        self.assertIn("\\x1b[2J", text)
        self.assertIn("\\u202e", text)
        with self.subTest("staging keeps what isn't the installer's"):
            staging = trust.user_directory() / installer.STAGING
            mine, other = staging / ("0" * 16), staging / "keep-this"
            for folder in (mine, other):
                folder.mkdir()
                os.utime(folder, (0, 0))
            jotter = self.write_plugin("jotter", {"module": "plugin"}, {"plugin.py": CODE}, where=outside)
            plugins.install_cancel(plugins.install_preview(str(jotter), self.root, here=True)["key"], self.root)
            plugins.install_cancel(plugins.update_preview("notes")["key"])
            self.assertEqual((mine.exists(), other.exists()), (False, True))

    def test_an_install_is_known_and_kept_whatever_runs_alongside_it(self):
        """The loader knows an install by the folder it finds, a confirm owns its stage, launches count in full, and markers are read safely; else unseen code runs or an old version is lost."""
        target = trust.user_directory() / "plugins" / "notes"
        launches = {"permissions": ["harness.claude"], "harnesses": {"claude": [{"dir": "claude"}]}}
        self.commit({"plugins/notes/plugin.json": self.manifest(**launches),
                     "plugins/notes/claude/.claude-plugin/plugin.json": '{"name": "notes"}\n'}, "launches")
        tool.run(self.root, plugin_action="install", name=f"{self.url}#plugins/notes", yes=True)
        self.enable("notes")
        with self.subTest("placed after the loader read the records"):
            (target / "plugin.py").write_text(CODE + "# edited\n")
            with mock.patch.object(installer, "managed_personal", return_value=(set(), True)):
                self.assertEqual(self.state(), "changed")  # its own marker says so
            with mock.patch.dict(os.environ, {"MV2_PLUGIN_PATH": str(target)}):
                self.assertEqual(self.state(), "changed")  # found first as a path plugin: still the installed one
            tool.run(self.root, plugin_action="trust", name="notes")
        with self.subTest("an edited install has no baseline: its update asks"):
            self.commit({"plugins/notes/plugin.py": CODE + "# upstream\n"}, "code")
            shown = plugins.update_preview("notes")
            plugins.install_cancel(shown["key"])
            self.assertEqual((shown["update"]["powers_changed"], shown["ask"]), ([], True))
            tool.run(self.root, plugin_action="update", name="notes", yes=True)
        with self.subTest("a launch changed within its harness"):
            self.commit({"plugins/notes/plugin.json": self.manifest(
                **{**launches, "harnesses": {"claude": [{"dir": "claude", "needs_bridge": False}]}})}, "no bridge")
            shown = plugins.update_preview("notes")
            plugins.install_cancel(shown["key"])
            self.assertEqual((shown["update"]["powers_changed"], shown["ask"]), (["harnesses"], True))
            self.assertEqual(shown["powers"]["harnesses"]["claude"][0]["path"], "claude")  # as declared
            self.assertIn("without the bridge", tool.preview_text("update", shown))
        with self.subTest("a cancel or a second confirm during a confirm"):
            shown = plugins.update_preview("notes")
            during = []
            save = installer.Place.save

            def busy(place, records):  # another process acts on the same preview while this one places it
                within_reach = installer._where(shown["key"], None) is not None
                during.append(within_reach)
                if not within_reach:  # else a cancel would wait on this confirm's lock: the claim failed
                    during.append(plugins.install_cancel(shown["key"]))
                    with self.assertRaisesRegex(plugins.PluginError, "gone"):
                        plugins.install_confirm(shown["key"])
                save(place, records)
            with mock.patch.object(installer.Place, "save", busy):
                plugins.install_confirm(shown["key"])
            self.assertEqual((during, self.state()), ([False, False], "on"))
        with self.subTest("a sweep between the claim and the placing"):
            shown = plugins.update_preview("notes")
            where = installer.Place()
            os.utime(where.staging / shown["key"], (0, 0))  # fetched long ago
            real_hash = installer.trust.fingerprint

            def swept_meanwhile(directory):
                installer._sweep(where)
                return real_hash(directory)
            with mock.patch.object(installer.trust, "fingerprint", swept_meanwhile):
                plugins.install_confirm(shown["key"])
        with self.subTest("an old version a failed rollback couldn't put back"):
            self.commit({"plugins/notes/plugin.py": CODE + "# three\n"}, "three")
            real = os.replace

            def stuck(source, destination):
                if Path(source).name == "replaced" and Path(destination) == target:
                    raise OSError(5, "Input/output error")
                return real(source, destination)
            with mock.patch.object(installer.Place, "save", side_effect=OSError(28, "No space left")), \
                    mock.patch.object(installer.os, "replace", stuck):
                with self.assertRaisesRegex(plugins.PluginError, "previous version is in"):
                    tool.run(self.root, plugin_action="update", name="notes", yes=True)
            kept = list((trust.user_directory() / installer.RECOVERY).iterdir())
            self.assertEqual(len(kept), 1)
            self.assertTrue((kept[0] / "plugin.json").is_file())
            os.replace(kept[0], target)  # put back by hand, as the error says
            self.commit({"plugins/notes/plugin.py": CODE + "# four\n"}, "four")

            def stuck_twice(source, destination):  # recovery can't take it either
                if Path(source).name == "replaced":
                    raise OSError(5, "Input/output error")
                return real(source, destination)
            with mock.patch.object(installer.Place, "save", side_effect=OSError(28, "No space left")), \
                    mock.patch.object(installer.os, "replace", stuck_twice):
                with self.assertRaises(plugins.PluginError) as caught:
                    tool.run(self.root, plugin_action="update", name="notes", yes=True)
            named = Path(str(caught.exception).rsplit("previous version is in ", 1)[1])
            self.assertTrue((named / "plugin.json").is_file())  # where it really is
            os.replace(named, target)
            shutil.rmtree(named.parent)
            real_pin = installer.trust.pin
            pins = []

            def pin_once(directory, value, events=None):  # the pin before placing works; putting back doesn't
                pins.append(value)
                if len(pins) > 1:
                    raise OSError(28, "No space left on device")
                return real_pin(directory, value, events)
            before = (target / "plugin.py").read_text()
            with mock.patch.object(installer.Place, "save", side_effect=OSError(28, "No space left")), \
                    mock.patch.object(installer.trust, "pin", pin_once):
                with self.assertRaises(plugins.PluginError) as caught:
                    tool.run(self.root, plugin_action="update", name="notes", yes=True)
            self.assertTrue(str(caught.exception).endswith(f"the previous version is in {target}"))  # where it is
            self.assertEqual((target / "plugin.py").read_text(), before)
            self.assertEqual(self.state(), "changed")  # the new version's pin on the old files: nothing runs
            tool.run(self.root, plugin_action="trust", name="notes")
        with self.subTest("a marker that is a link, or not a file"):
            mark = target / installer.MARKER
            saved = mark.read_text()
            aside = self.work / "marker"
            aside.write_text(saved)
            mark.unlink()
            os.symlink(aside, mark)
            with self.assertRaisesRegex(plugins.PluginError, "isn't the folder mv2 plugins install placed"):
                plugins.update_preview("notes")
            mark.unlink()
            os.mkfifo(mark)  # read naively, this would wait forever
            with self.assertRaisesRegex(plugins.PluginError, "isn't the folder mv2 plugins install placed"):
                plugins.remove_installed("notes")
            mark.unlink()
            mark.write_text(saved)
        with self.subTest("a grant that appears after an agent's disable looked"):
            from multiverse.plugins import config
            config.set_grant(self.root, "ghost", "observe")
            with mock.patch.dict(os.environ, {"MV2_AGENT_ID": "x"}), \
                    mock.patch.object(config, "load", return_value=config.Config()):  # looked before it was there
                with self.assertRaisesRegex(plugins.PluginError, "Only the person forgets"):
                    plugins.set_enabled(self.root, "ghost", False)
            self.assertEqual(config.load(self.root).access.get("ghost"), "observe")
        row = {item["name"]: item for item in plugins.listing(self.root, wait=0)}["notes"]["install"]
        with self.assertRaisesRegex(plugins.PluginError, "changed since it was shown"):
            plugins.remove_installed("notes", placement="personal", expected={**row, "install": "0" * 16})
        plugins.remove_installed("notes", placement="personal", expected=row)
        self.assertEqual(self.staging(), [])

    def test_listing_reads_the_install_maps_only_when_it_must(self):
        """A listing reads the installer's maps only for personal plugins, once, retrying only a map written before; else every command waits on an absent file, or a pinned plugin runs unpinned."""
        with mock.patch.object(installer, "read_map", side_effect=AssertionError("read with no personal plugin")):
            loader.reset()
            plugins.entries(self.root)  # no personal plugins: nothing read at all
        self.write_plugin("jot", {}, {}, where=trust.user_directory() / "plugins")  # placed by hand
        from multiverse import store
        import traceback

        def sleep(_):  # the installer's own reads only: other optional records keep their own rules
            if any(frame.filename.endswith(os.path.join("plugins", "installer.py"))
                   for frame in traceback.extract_stack()):
                raise AssertionError("waited for an absent install map")
        with mock.patch.object(store.time, "sleep", side_effect=sleep):
            loader.reset()
            plugins.entries(self.root)  # neither map ever written: read once, never waited for
        target = trust.user_directory() / "plugins" / "notes"
        tool.run(self.root, plugin_action="install", name=f"{self.url}#plugins/notes", yes=True)
        self.enable("notes")
        with mock.patch.object(installer, "managed_personal", wraps=installer.managed_personal) as looked:
            loader.reset()
            plugins.entries(self.root)
        self.assertEqual(looked.call_count, 1)  # the cache signature and the entries share one lookup
        with mock.patch.object(loader, "_source_directories", wraps=loader._source_directories) as scanned:
            loader.reset()
            plugins.entries(self.root)
        self.assertEqual(scanned.call_count, 1)  # one look at the folders: none can arrive between the stages
        # Known by its pin alone: the record no longer names it and its marker is gone.
        installer.Place().save({})
        (target / installer.MARKER).unlink()
        real, missed = installer.read, []

        def missing(times):
            def read(path):
                if Path(path).name == trust.FILE and len(missed) < times:
                    missed.append(path)
                    raise FileNotFoundError(2, "No such file or directory", str(path))
                return real(path)
            return read
        with self.subTest("trusted-plugins.json caught mid-replacement once"):
            with mock.patch.object(installer, "read", missing(1)):
                self.assertEqual(self.state(), "changed")  # verified: its marker is gone, so it isn't what was pinned
            self.assertTrue(missed)
        with self.subTest("missing through every retry, then back unchanged"):
            missed.clear()
            with mock.patch.object(installer, "read", missing(10 ** 6)):
                self.assertEqual(self.state(), "changed")  # uncertain: every personal plugin is verified
            self.assertEqual(self.state(), "changed")  # and that listing wasn't kept as ordinary
        with self.subTest("known by its pin alone, both readers missing it, then back unchanged"):
            jot = trust.user_directory() / "plugins" / "jot"
            os.replace(jot, self.work / "jot-aside")  # notes alone: the doubtful and the clean read class it alike
            self.addCleanup(lambda: (self.work / "jot-aside").exists() and os.replace(self.work / "jot-aside", jot))
            trust.trust(target)  # pinned at what is there now: it runs
            self.assertEqual(self.state(), "on")
            real_trust, gone = trust.read, []

            def trust_missing(path, *args, **kwargs):
                if Path(path).name == trust.FILE:
                    gone.append(path)
                    raise FileNotFoundError(2, "No such file or directory", str(path))
                return real_trust(path, *args, **kwargs)
            missed.clear()
            with mock.patch.object(installer, "read", missing(10 ** 6)), \
                    mock.patch.object(trust, "read", trust_missing):
                self.assertNotEqual(plugins.entry(self.root, "notes").state, "on")
            self.assertTrue(gone)
            self.assertEqual(plugins.entry(self.root, "notes").state, "on")  # read cleanly again: not the doubtful one
            os.replace(self.work / "jot-aside", jot)
        with self.subTest("a record written before the write lock, missed once"):
            installer.Place().save({"notes": {"install": "0" * 16, "kind": "folder"}})  # names it again
            installer.Place().writes.unlink()  # as an install from before that lock existed
            trust.forget(target)  # known by its record alone now
            missed.clear()
            record = installer.Place().record

            def record_missing_once(path):
                if Path(path) == record and not missed:
                    missed.append(path)
                    raise FileNotFoundError(2, "No such file or directory", str(path))
                return real(path)
            with mock.patch.object(installer, "read", record_missing_once):
                self.assertEqual(self.state(), "untrusted")  # the record is there: retried, so still verified
            self.assertTrue(missed)
            self.assertTrue(installer.Place().writes.exists())  # seen: from now on its lock says it was written
        with self.subTest("a damaged entry, the only sign left of the install"):
            installer.Place().record.write_text('{"schema": 1, "plugins": {"notes": "damaged"}}\n')
            self.assertEqual(self.state(), "untrusted")  # can't tell: verified, so it doesn't run
            installer.Place().save({})
            trust.trust(target)
        with self.subTest("an install moved elsewhere and linked back"):
            moved = self.work / "notes-moved"
            os.replace(target, moved)
            os.symlink(moved, target)
            jot = trust.user_directory() / "plugins" / "jot"
            os.replace(jot, self.work / "jot-aside")  # the link is the only personal folder
            (moved / "plugin.py").write_text(CODE + "# edited\n")
            self.assertNotEqual(self.state(), "on")  # known by its place in the personal plugins: verified
            with mock.patch.dict(os.environ, {"MV2_PLUGIN_PATH": str(moved)}):  # its target, by another spelling
                self.assertNotEqual(self.state(), "on")
            target.unlink()
            os.replace(moved, target)
            os.replace(self.work / "jot-aside", jot)
            (target / "plugin.py").write_text(CODE)
            trust.trust(target)
        hand = self.write_plugin("jot", {}, {}, where=trust.user_directory() / "plugins")
        for case, path, text in (("a damaged install record", installer.Place().record, "{not json"),
                                 ("a pin map whose plugins aren't a map", trust.user_directory() / trust.FILE,
                                  '{"schema": 1, "plugins": []}')):
            with self.subTest(case):
                saved = path.read_text()
                path.write_text(text)
                self.assertEqual(self.state("jot"), "untrusted")  # can't tell installs apart: none runs unverified
                path.write_text(saved)
        self.assertEqual(self.state("jot"), "off")  # placed by hand, and not on: an ordinary personal plugin
        missed.clear()
        with mock.patch.object(installer, "read", missing(10 ** 6)):
            loader.reset()
            plugins.entries(self.root)
            self.assertEqual(plugins.entry(self.root, "jot").state, "untrusted")  # uncertain: verified
        self.assertEqual(plugins.entry(self.root, "jot").state, "off")  # same files, read cleanly: not kept
        shutil.rmtree(hand)
