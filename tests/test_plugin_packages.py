"""A plugin's Python packages (1.20): installed only with the person's ok, into a folder of its own."""
import base64
import hashlib
import importlib.metadata
import json
import os
from pathlib import Path
import sys
import unittest
from unittest import mock
import zipfile

from multiverse import plugins
from multiverse.plugins import config, loader, packages
from multiverse.plugins.testing import PluginTestCase
from multiverse.tools import plugins as tool


def wheel(directory, name, version, body=None):
    """A pure-Python wheel of package `name` at `version` in `directory`, and its sha256."""
    files = {f"{name}/__init__.py": body or f"VERSION = {version!r}\n",
             f"{name}-{version}.dist-info/METADATA": f"Metadata-Version: 2.1\nName: {name}\nVersion: {version}\n",
             f"{name}-{version}.dist-info/WHEEL": "Wheel-Version: 1.0\nGenerator: mv2-test\nRoot-Is-Purelib: true\n"
                                                  "Tag: py3-none-any\n"}
    record = [f"{path},sha256={base64.urlsafe_b64encode(hashlib.sha256(text.encode()).digest()).rstrip(b'=').decode()}"
              f",{len(text.encode())}" for path, text in files.items()]
    files[f"{name}-{version}.dist-info/RECORD"] = "\n".join([*record, f"{name}-{version}.dist-info/RECORD,,"]) + "\n"
    path = Path(directory) / f"{name}-{version}-py3-none-any.whl"
    with zipfile.ZipFile(path, "w") as archive:
        for member, text in files.items():
            archive.writestr(member, text)
    return hashlib.sha256(path.read_bytes()).hexdigest()


@unittest.skipUnless(packages.installer(), "needs uv or pip to install the test's local wheels")
class PackagesTest(PluginTestCase):
    def setUp(self):
        super().setUp()
        self.wheels = self.work / "wheels"
        self.wheels.mkdir()
        # The test's own wheels only: no index, no network, and the installer's caches inside the test.
        self.enterContext(mock.patch.object(packages, "INDEX", ("--no-index", "--find-links", str(self.wheels))))
        self.enterContext(mock.patch.dict(os.environ, {"UV_CACHE_DIR": str(self.work / "uv-cache"),
                                                       "PIP_NO_CACHE_DIR": "1"}))
        self.enterContext(mock.patch.object(sys, "path", list(sys.path)))
        self.enterContext(mock.patch.object(packages, "_active", {}))
        self.addCleanup(lambda: [sys.modules.pop(name) for name in list(sys.modules)
                                 if name.split(".")[0] in ("mv2fixture", "mv2other")])
        self.hashes = {version: wheel(self.wheels, "mv2fixture", version) for version in ("1.0", "2.0")}

    def needing(self, name, lines):
        return self.write_plugin(name, {"api": "1.20", "module": "plugin", "python": {"requirements": "req.txt"}},
                                 {"plugin.py": "import mv2fixture\n\ndef register(mv):\n    pass\n",
                                  "req.txt": "".join(line + "\n" for line in lines)})

    def pinned(self, version):
        return f"mv2fixture=={version} \\\n    --hash=sha256:{self.hashes[version]}"

    def run_tool(self, action, name, **options):
        return tool.run(self.root, plugin_action=action, name=name, **options)

    def installed(self):
        base = packages.root()
        return sorted(child.name for child in base.iterdir()) if base.is_dir() else []

    def test_packages_install_only_with_the_persons_ok_and_load_after_mv2s_own(self):
        """A plugin's packages install only when the person says yes, never on a refresh, from an agent or for a
        changed list it wasn't asked about, and load after mv2's own; a break runs unreviewed code or shadows mv2."""
        self.needing("relay", [self.pinned("1.0")])
        with self.subTest("enable without the person's ok"):
            with self.assertRaisesRegex(ValueError, "run mv2 plugins enable relay --install-packages"):
                self.run_tool("enable", "relay")
            self.assertFalse(self.entry("relay").on)
        with self.subTest("an agent can't approve them"):
            with mock.patch.dict(os.environ, {"MV2_AGENT_ID": "someone"}), \
                    self.assertRaisesRegex(plugins.PluginError, "Only the person can approve"):
                self.run_tool("enable", "relay", install_packages=True)
            self.assertFalse(self.entry("relay").on)
        with self.subTest("on without them: it fails to load, naming the command, and nothing installs"):
            config.set_enabled(self.root, "relay", True)
            with self.assertRaisesRegex(loader.PluginError, "run mv2 plugins packages relay"):
                loader.load(self.entry("relay"))
            self.assertEqual(self.installed(), [])
            config.set_enabled(self.root, "relay", False)
        with self.subTest("the person's ok"):
            result = self.run_tool("enable", "relay", install_packages=True)
            self.assertEqual(result["packages_installed"]["installed"], ["mv2fixture==1.0"])
            loader.reset()
            loader.load(self.entry("relay"))
            self.assertEqual(sys.modules["mv2fixture"].VERSION, "1.0")
            site = packages.folder(self.entry("relay").manifest) / "site"
            self.assertEqual(sys.path[-1], str(site))  # after every entry of mv2's own
            self.assertIn("Installed its Python packages", tool.text("enable", result))
        with self.subTest("a changed list needs the person's ok again"):
            (self.plugins_dir / "relay" / "req.txt").write_text(self.pinned("2.0") + "\n")
            loader.reset()
            packages._active.clear()
            with self.assertRaisesRegex(loader.PluginError, "run mv2 plugins packages relay"):
                loader.load(self.entry("relay"))
            shown = self.run_tool("packages", "relay")
            self.assertEqual((shown["packages"]["packages"], shown["packages"]["installed"]),
                             (["mv2fixture==2.0"], False))
            self.run_tool("packages", "relay", install_packages=True)
            self.assertTrue(self.run_tool("packages", "relay")["packages"]["installed"])

    def test_a_yes_is_for_the_list_shown(self):
        """A yes on the terminal installs only the list that was shown: one changed meanwhile installs nothing and
        says so; a break turns a yes for one package into one for another (reviewer, 72790824)."""
        directory = self.needing("relay", [self.pinned("1.0")])

        def changed(prompt):
            (directory / "req.txt").write_text(self.pinned("2.0") + "\n")
            return "y"
        with mock.patch.object(sys.stdin, "isatty", return_value=True), \
                mock.patch.object(sys.stdout, "isatty", return_value=True), \
                mock.patch("builtins.input", side_effect=changed), mock.patch("builtins.print"), \
                self.assertRaisesRegex(plugins.PluginError, "changed after you were shown them"):
            tool.cli_run(self.root, plugin_action="enable", name="relay", grant="act")
        self.assertEqual(self.installed(), [])
        self.assertFalse(self.entry("relay").on)

    def test_a_load_rechecks_what_was_installed(self):
        """Loading rechecks an installed folder against mv2's packages as they are now, and refuses a new version
        of a package this process already loaded for the plugin; a break gives it a package mv2 replaced, or the
        old one while saying the new one (reviewer, 72790824)."""
        directory = self.needing("relay", [self.pinned("1.0")])
        self.run_tool("enable", "relay", install_packages=True)
        loader.reset()
        loader.load(self.entry("relay"))
        with self.subTest("a newer version of the same plugin's package in a running process"):
            (directory / "req.txt").write_text(self.pinned("2.0") + "\n")
            self.run_tool("packages", "relay", install_packages=True)
            loader.reset()
            with self.assertRaisesRegex(loader.PluginError, "already loaded mv2fixture 1.0 for it: restart"):
                loader.load(self.entry("relay"))
        with self.subTest("mv2 has the package now, at another version"):
            packages._active.clear()
            own = self.work / "mv2-now"
            (own / "mv2fixture-3.0.dist-info").mkdir(parents=True)
            (own / "mv2fixture-3.0.dist-info" / "METADATA").write_text("Metadata-Version: 2.1\nName: mv2fixture\n"
                                                                        "Version: 3.0\n")
            sys.path.insert(0, str(own))
            loader.reset()
            with self.assertRaisesRegex(loader.PluginError, "mv2 itself now uses mv2fixture 3.0"):
                loader.load(self.entry("relay"))

    def test_every_listing_says_why_installed_packages_cant_be_used_now(self):
        """mv2 plugins, info and a running dashboard say, without loading the plugin, when mv2's packages moved
        under what was installed or left out, or this process loaded an older version; a break shows the plugin
        fine until something imports the wrong package (reviewer and qa-plugins, aae6e365 and 6d2eaa1d)."""
        textual = importlib.metadata.version("textual")
        directory = self.needing("relay", [self.pinned("1.0"), f"textual=={textual} --hash=sha256:{'a' * 64}"])
        self.run_tool("enable", "relay", install_packages=True)
        loader.reset()
        loader.load(self.entry("relay"))
        problem = lambda: self.run_tool("info", "relay")["problem"] or ""
        with self.subTest("a running process loaded an older version"):
            (directory / "req.txt").write_text(f"{self.pinned('2.0')}\ntextual=={textual} --hash=sha256:{'a' * 64}\n")
            self.run_tool("packages", "relay", install_packages=True)
            self.assertIn("already loaded mv2fixture 1.0 for it: restart", problem())
            packages._active.clear()
            self.assertEqual(problem(), "")
        with self.subTest("a package left out as mv2's: mv2 has another version now"):
            moved = {**packages.mv2_packages(), "textual": "0.0.1"}
            with mock.patch.object(packages, "mv2_packages", return_value=moved):
                self.assertIn("left out as mv2's own, but mv2 now has textual 0.0.1", problem())
                self.assertFalse(self.run_tool("packages", "relay")["packages"]["installed"])
                # The record's own list of what it left out is never trusted (reviewer, 58608ed9).
                marker = packages.folder(self.entry("relay").manifest) / packages.COMPLETE
                record = json.loads(marker.read_text())
                marker.write_text(json.dumps({key: value for key, value in record.items() if key != "skipped"}))
                self.assertIn("left out as mv2's own, but mv2 now has textual 0.0.1", problem())
                marker.write_text("{}")
                self.assertIn("install its packages again", problem())
                marker.write_text(json.dumps(record))
        with self.subTest("an installed package: mv2 has it now, at another version"):
            own = self.work / "mv2-now"
            (own / "mv2fixture-3.0.dist-info").mkdir(parents=True)
            (own / "mv2fixture-3.0.dist-info" / "METADATA").write_text("Metadata-Version: 2.1\nName: mv2fixture\n"
                                                                        "Version: 3.0\n")
            sys.path.insert(0, str(own))
            self.assertIn("mv2 itself now uses mv2fixture 3.0", problem())

    def test_a_plugin_failing_on_its_packages_reads_failed_until_they_change(self):
        """A plugin that failed to load for its packages reads as failed on every listing, not as loading again,
        and loads once they're installed; a break leaves the Plugins screen saying loading forever (qa-plugins)."""
        self.needing("relay", [self.pinned("1.0")])
        config.set_enabled(self.root, "relay", True)
        state = lambda: {item["name"]: item["state"] for item in plugins.listing(self.root, wait=0)}["relay"]
        with self.assertRaises(loader.PluginError):
            loader.load(self.entry("relay"))
        self.assertEqual([state() for _ in range(3)], ["failed"] * 3)
        self.run_tool("packages", "relay", install_packages=True)
        loader.load(self.entry("relay"))
        self.assertEqual(state(), "loaded")
        self.needing("second", [self.pinned("2.0")])
        self.run_tool("enable", "second", install_packages=True)
        with self.assertRaisesRegex(loader.PluginError, "loaded first"):
            loader.load(self.entry("second"))
        self.assertEqual({item["name"]: item["state"] for item in plugins.listing(self.root, wait=0)}["second"],
                         "failed")

    def test_a_trusted_universe_plugin_failing_on_its_packages_reads_failed_until_they_change(self):
        """A trusted copy's cache marker, touched on each use, doesn't make an unchanged failure look edited, and its
        packages being installed loads it again; a break shows it on, or keeps it failed (reviewer, 604a18f6)."""
        from multiverse.plugins import trust
        self.write_plugin("relay", {"api": "1.20", "module": "plugin", "python": {"requirements": "req.txt"}},
                          {"plugin.py": "def register(mv):\n    pass\n", "req.txt": self.pinned("1.0") + "\n"},
                          where=self.root / "plugins")
        self.run_tool("trust", "relay")
        config.set_enabled(self.root, "relay", True)
        with self.assertRaises(loader.PluginError):
            loader.load(self.entry("relay"))
        state = lambda: {item["name"]: item["state"] for item in plugins.listing(self.root, wait=0)}["relay"]
        self.assertEqual([state() for _ in range(3)], ["failed"] * 3)  # each use touches the copy's marker
        with mock.patch.object(trust.os, "utime", side_effect=OSError("no touch")):  # recovery never rides on it
            self.run_tool("packages", "relay", install_packages=True)
            loader.load(self.entry("relay"))
        self.assertEqual(state(), "loaded")

    def test_every_package_is_pinned_and_hashed(self):
        """A requirements file must pin each package with == and hash it, with no installer options; a break lets
        the index choose what runs inside mv2."""
        good = f"--hash=sha256:{'a' * 64}"
        for lines, named in (([f"mv2fixture>=1.0 {good}"], "must pin one exact version"),
                             (["mv2fixture==1.0"], "has no --hash"),
                             ([f"-r other.txt"], "isn't allowed"),
                             ([f"--index-url https://example.invalid/simple"], "isn't allowed"),
                             ([f"mv2fixture==1.0 {good}", f"MV2Fixture==2.0 {good}"], "listed twice"),
                             ([f"mv2fixture==1.0 {good} --no-binary"], "only --hash"),
                             ([f"mv2fixture==1.* {good}"], "must pin one exact version"),
                             ([f"mv2fixture==1.0,!=1.0.post1 {good}"], "must pin one exact version"),
                             ([f"mv2fixture[extra]==1.0 {good}"], "must pin one exact version"),
                             # An installer option can't pass for a marker (reviewer, 72790824).
                             ([f'mv2fixture==1.0 ; python_version >= "3" --no-binary=:all: {good}'],
                              "isn't an environment marker"),
                             ([f'mv2fixture==1.0 ; anything == "1" {good}'], "isn't an environment marker"),
                             ([], "lists no packages")):
            with self.subTest(lines=lines):
                self.needing("pins", lines)
                loader.reset()
                self.assertIn(named, self.entry("pins").problem or "")

    def test_a_package_mv2_has_is_used_from_mv2_or_refused(self):
        """A package mv2 already has at the same version is left out and mv2's is used, and at another version the
        install is refused; a break lets a plugin replace a package mv2 itself imports."""
        textual = importlib.metadata.version("textual")
        fake = f"--hash=sha256:{'a' * 64}"  # never fetched: it is left out, or refused, before the installer runs
        self.needing("same", [f"textual=={textual} {fake}", self.pinned("1.0")])
        result = self.run_tool("packages", "same", install_packages=True)
        self.assertEqual((result["install"]["installed"], result["install"]["skipped"]),
                         (["mv2fixture==1.0"], [f"textual=={textual}"]))
        self.needing("mine", [f"textual=={textual} {fake}"])  # every one is mv2's own: nothing to agree to
        self.assertTrue(self.run_tool("packages", "mine")["packages"]["installed"])
        self.run_tool("enable", "mine")
        loader.reset()
        with self.assertRaisesRegex(loader.PluginError, "No module named 'mv2fixture'"):  # it loads; its import fails
            loader.load(self.entry("mine"))
        self.needing("other", [f"textual==0.0.1 {fake}"])
        with self.assertRaisesRegex(plugins.PluginError, f"mv2 itself uses textual {textual}"):
            self.run_tool("packages", "other", install_packages=True)
        self.assertEqual(len(self.installed()), 1)

    def test_two_plugins_with_one_package_at_two_versions_refuse_the_second(self):
        """Plugins share one Python, so a second plugin needing another version of a package a loaded plugin has is
        refused, naming the first; a break gives one of them the wrong version without a word."""
        self.needing("first", [self.pinned("1.0")])
        self.needing("second", [self.pinned("2.0")])
        for name in ("first", "second"):
            self.run_tool("enable", name, install_packages=True)
        loader.reset()
        # Every listing names it for both, whichever this process would load first (qa-plugins API120-PACKAGES-2).
        listed = {item["name"]: item["problem"] for item in plugins.listing(self.root, wait=0)}
        self.assertIn("first needs mv2fixture 1.0, but second needs mv2fixture 2.0", listed["first"] or "")
        self.assertIn("second needs mv2fixture 2.0, but first needs mv2fixture 1.0", listed["second"] or "")
        info = self.run_tool("info", "second")
        self.assertIn("they can't both load", info["problem"] or "")
        self.assertTrue(any("they can't both load" in line for line in plugins.problems(self.root)))
        loader.reset()
        loader.load(self.entry("first"))
        with self.assertRaisesRegex(loader.PluginError, "first, loaded first, has mv2fixture 1.0"):
            loader.load(self.entry("second"))

    def test_loads_at_once_and_a_damaged_record_never_pass_a_conflict(self):
        """Two plugins loading at once with two versions of one package can't both pass, and an install record that
        can't be read is installed again, never trusted; a break hands both the first version, or skips the check
        of what was left out as mv2's (reviewer, 003c1448)."""
        import threading
        self.needing("first", [self.pinned("1.0")])
        self.needing("second", [self.pinned("2.0")])
        for name in ("first", "second"):
            self.run_tool("enable", name, install_packages=True)
        manifests = [self.entry(name).manifest for name in ("first", "second")]
        meet, real, outcomes = threading.Barrier(2, timeout=1), packages._check, []

        def both_checking(*arguments, **options):
            # Each load, its check done, waits here for the other before it publishes: with the check and the
            # publishing one step, the other can't have checked yet, so it is refused once this one publishes.
            answer = real(*arguments, **options)
            try:
                meet.wait()
            except threading.BrokenBarrierError:
                pass
            return answer

        def load(manifest):
            try:
                outcomes.append(packages.activate(manifest) and "loaded")
            except packages.PackagesError:
                outcomes.append("refused")
        with mock.patch.object(packages, "_check", side_effect=both_checking):
            threads = [threading.Thread(target=load, args=(manifest,)) for manifest in manifests]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join(10)
        self.assertEqual(sorted(outcomes), ["loaded", "refused"])
        packages._active.clear()
        marker = packages.folder(manifests[0]) / packages.COMPLETE
        marker.write_text("{not json")
        self.assertIn("install record", packages.problem(manifests[0]) or "")
        with self.assertRaisesRegex(packages.PackagesError, "install its packages again"):
            packages.activate(manifests[0])
        self.assertFalse(self.run_tool("packages", "first")["packages"]["installed"])

    def test_a_failed_install_leaves_the_plugin_as_it_was_and_says_so(self):
        """An install that fails (here a hash that doesn't match) leaves no folder behind and the plugin off, and
        says why; a break leaves half a folder that later loads as installed."""
        self.needing("broken", [f"mv2fixture==1.0 --hash=sha256:{'0' * 64}"])
        with self.assertRaisesRegex(plugins.PluginError, "could not be installed"):
            self.run_tool("enable", "broken", install_packages=True)
        self.assertFalse(self.entry("broken").on)
        self.assertEqual(self.installed(), [])
        # Already on, with a list that changed: it stays on, unable to load, and says so (API120-PACKAGES-3).
        directory = self.needing("relay", [self.pinned("1.0")])
        self.run_tool("enable", "relay", install_packages=True)
        (directory / "req.txt").write_text(f"mv2fixture==2.0 --hash=sha256:{'0' * 64}\n")
        with self.assertRaisesRegex(plugins.PluginError, "relay stays on here, but can't load until they install"):
            self.run_tool("enable", "relay", install_packages=True)
        self.assertTrue(self.entry("relay").on)


if __name__ == "__main__":
    unittest.main()
