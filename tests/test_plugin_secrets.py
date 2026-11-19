"""A plugin's secrets (1.20): kept outside every universe, read only by their plugin, shown to no one."""
import io
import json
import os
from pathlib import Path
import stat
import subprocess
import sys
import unittest
from unittest import mock

from multiverse import cli
from multiverse.plugins import api, loader, secrets
from multiverse.plugins.testing import PluginTestCase
from multiverse.tools import plugins as tool

VALUE = "sk-live-Do-Not-Leak-0f3a9"  # what no file, output or argument may hold
KEEPER = {"api": "1.20", "module": "plugin", "access": {"request": "observe"},
          "secrets": {"api_key": {"label": "API key", "help": "From the provider's console.", "required": True},
                      "token": {"label": "Webhook token"}}}
PLUGIN = "MV = []\n\ndef register(mv):\n    MV.append(mv)\n"

# A stand-in for macOS `security` and Linux `secret-tool`: it keeps entries in a JSON file and logs each call's
# arguments and stdin, so the test sees exactly what reached the tool.
FAKE_TOOL = r'''#!{python}
import json, os, shlex, sys
store, log = os.environ["FAKE_KEYCHAIN"], os.environ["FAKE_KEYCHAIN"] + ".log"
data = json.load(open(store)) if os.path.exists(store) else {{}}
stdin = "" if sys.stdin.isatty() else sys.stdin.read()
with open(log, "a") as handle:
    handle.write(json.dumps({{"argv": sys.argv, "stdin": stdin}}) + "\n")
name, args = os.path.basename(sys.argv[0]), sys.argv[1:]
def keep():
    json.dump(data, open(store, "w"))
if name == "security":
    if args == ["-i"]:
        words = shlex.split(stdin)
        account, value = words[words.index("-a") + 1], words[words.index("-w") + 1]
        data[account] = value; keep()
    elif args[0] == "find-generic-password":
        account = args[args.index("-a") + 1]
        if account not in data:
            sys.exit(44)
        print(data[account])
    elif args[0] == "delete-generic-password":
        data.pop(args[args.index("-a") + 1], None); keep()
else:
    account = args[args.index("account") + 1]
    if args[0] == "store":
        data[account] = stdin; keep()
    elif args[0] == "lookup":
        if account not in data:
            sys.exit(1)
        print(data[account])
    elif args[0] == "clear":
        data.pop(account, None); keep()
'''


class SecretsTest(PluginTestCase):
    def setUp(self):
        super().setUp()
        self.write_plugin("keeper", KEEPER, {"plugin.py": PLUGIN})
        self.enable("keeper")

    def run_tool(self, action, name="keeper", **options):
        return tool.run(self.root, plugin_action=action, name=name, **options)

    def refused(self, action, **options):
        try:
            self.run_tool(action, **options)
        except Exception as error:  # the tool's refusal, whichever error carries it
            return str(error)
        return ""

    def mv(self, name="keeper"):
        loader.load(self.entry(name))
        import importlib
        module = importlib.import_module(f"{loader.load(self.entry(name)).package}.plugin")
        return module.MV[-1]

    def files_holding(self, value, where):
        return [str(path) for path in Path(where).rglob("*") if path.is_file() and value.encode() in path.read_bytes()]

    def test_a_secret_stays_out_of_every_file_and_only_its_plugin_reads_it(self):
        """A secret's value lands only in the 0600 store, never the universe, index, outputs or another plugin's
        reach, and a missing required one makes the plugin unusable naming the command; a break leaks an API key."""
        with self.subTest("missing: unusable here, naming the command"):
            self.assertEqual(self.entry("keeper").state, "unusable")
            self.assertIn("mv2 plugins secret keeper api_key", self.entry("keeper").problem)
        result = self.run_tool("secret", level="api_key", secret_value=VALUE)
        with self.subTest("set: nowhere but the store, which only the user can read"):
            self.assertEqual(result["store"], "file")
            store = self.home / ".multiverse" / secrets.DIRECTORY
            self.assertEqual(self.files_holding(VALUE, self.work), [str(store / secrets.FILE)])
            self.assertEqual(stat.S_IMODE((store / secrets.FILE).stat().st_mode), 0o600)
            self.assertEqual(stat.S_IMODE(store.stat().st_mode), 0o700)
            self.assertEqual(stat.S_IMODE((store / ".lock").stat().st_mode), 0o600)
            self.assertIn("(MV2_SECRET_STORE=file)", tool.text("secret", result))  # why the file, not "no keychain"
        with self.subTest("a lock that isn't a plain file refuses at once, never waiting or following it"):
            import threading
            lock, aside = store / ".lock", self.work / "aside"
            lock.chmod(0o644)  # one made before locks were the user's alone
            self.run_tool("secret", level="api_key", secret_value=VALUE)
            self.assertEqual(stat.S_IMODE(lock.stat().st_mode), 0o600)
            aside.write_text("")
            mode = stat.S_IMODE(aside.stat().st_mode)
            for make in (lambda: os.mkfifo(lock), lambda: lock.symlink_to(aside)):
                lock.unlink()
                make()
                outcome = []
                worker = threading.Thread(target=lambda: outcome.append(self.refused("secret", level="api_key",
                                                                                     secret_value=VALUE)),
                                          daemon=True)
                worker.start()
                worker.join(10)
                self.assertFalse(worker.is_alive())  # a FIFO opened blocking would wait here for a writer
                self.assertIn("remove it", outcome[0])
                self.assertEqual(stat.S_IMODE(aside.stat().st_mode), mode)  # a link's target is left alone
            lock.unlink()
            import errno
            out_of_descriptors = OSError(errno.EMFILE, os.strerror(errno.EMFILE))
            with mock.patch.object(secrets.os, "open", side_effect=out_of_descriptors):
                refusal = self.refused("secret", level="api_key", secret_value=VALUE)
            self.assertIn("can't be opened", refusal)
            self.assertNotIn("remove it", refusal)  # it may be a valid lock another process holds
            for shown in (tool.text("secret", result), json.dumps(result),
                          tool.text("secrets", self.run_tool("secrets"))):
                self.assertNotIn(VALUE, shown)
            self.assertIn("api_key (API key, required): set", tool.text("secrets", self.run_tool("secrets")))
            self.assertEqual(self.entry("keeper").state, "on")
        mv = self.mv()
        here = mv.universe(self.root)
        with self.subTest("its plugin reads it; nothing else does"):
            self.assertEqual(here.secret("api_key"), VALUE)
            with self.assertRaisesRegex(api.SecretMissing, "mv2 plugins secret keeper token"):
                here.secret("token")
            with self.assertRaisesRegex(api.Error, "declares no secret 'other'"):
                here.secret("other")
            self.write_plugin("snoop", {"api": "1.20", "module": "plugin", "access": {"request": "observe"},
                                        "secrets": {"api_key": {"label": "API key"}}}, {"plugin.py": PLUGIN})
            self.enable("snoop")
            loader._entries.clear()
            with self.assertRaises(api.SecretMissing):  # its own api_key, never keeper's
                self.mv("snoop").universe(self.root).secret("api_key")
        with self.subTest("off here: it can't read it"):
            self.enable("keeper", False)
            loader._entries.clear()
            with self.assertRaisesRegex(api.Error, "no access here|not on and trusted"):
                here.secret("api_key")

    def test_a_subverse_reads_its_parents_until_it_sets_its_own(self):
        """A subverse with no value of its own reads its nearest parent's, live, and its own overrides it until
        cleared; nothing is copied; a break makes subverses miss the key or keep a stale copy (user 2026-11-19)."""
        sub = self.subverse("ops")
        self.enable("keeper", root=sub)
        here = lambda root: self.mv().universe(root).secret("api_key")
        self.run_tool("secret", level="api_key", secret_value=VALUE)
        self.assertEqual(here(sub), VALUE)
        self.assertEqual(self.files_holding(VALUE, sub), [])
        self.assertEqual(tool.run(sub, plugin_action="secrets", name="keeper")["secrets"][0]["from"], str(self.root))
        tool.run(self.root, plugin_action="secret", name="keeper", level="api_key", secret_value="rotated")
        self.assertEqual(here(sub), "rotated")  # read live, never a copy
        tool.run(sub, plugin_action="secret", name="keeper", level="api_key", secret_value="its-own")
        self.assertEqual((here(sub), here(self.root)), ("its-own", "rotated"))
        cleared = tool.run(sub, plugin_action="secret", name="keeper", level="api_key", clear=True)
        self.assertIn(f"{self.root}'s is used now", tool.text("secret", cleared))
        self.assertEqual(here(sub), "rotated")
        tool.run(self.root, plugin_action="secret", name="keeper", level="api_key", clear=True)
        loader._entries.clear()
        with self.assertRaises(api.SecretMissing):
            here(sub)

    def test_only_the_person_sets_one_and_never_as_an_argument(self):
        """An agent can't set or clear a secret, and the command reads the value from stdin or a hidden prompt,
        never an argument (shell history); a break leaves the key in an agent's transcript or the history."""
        with mock.patch.dict(os.environ, {"MV2_AGENT_ID": "someone"}):
            for options in ({"secret_value": VALUE}, {"clear": True}, {}):  # {}: an agent piping it in
                with self.subTest(options=list(options)), self.assertRaisesRegex(Exception, "Only the person"):
                    self.run_tool("secret", level="api_key", **options)
        # A value given as an argument is refused, and never repeated (qa-plugins API120-SECRETS-1).
        output, errors = io.StringIO(), io.StringIO()
        with mock.patch.object(sys, "argv", ["mv2", "-u", str(self.root), "plugins", "secret", "keeper", "api_key",
                                             VALUE]), mock.patch("sys.stdout", output), \
                mock.patch("sys.stderr", errors), self.assertRaises(SystemExit) as exited:
            cli.main()
        self.assertEqual(exited.exception.code, 2)  # a usage error, as argparse gives one (mv2-lead-II)
        self.assertIn("never a value as an argument", errors.getvalue() + output.getvalue())
        self.assertNotIn(VALUE, errors.getvalue() + output.getvalue())
        # As an option too, unknown or known, its value next or joined (qa-plugins; lifecycle, secrets-fix4).
        for shaped in (f"--value={VALUE}", "--value", f"--clear={VALUE}", f"--grant={VALUE}", "--grant"):
            with self.subTest(option=shaped):
                output, errors = io.StringIO(), io.StringIO()
                words = [shaped] if "=" in shaped else [shaped, VALUE]
                # Options before the action too, with values of their own (lifecycle, secrets-fix4 review 2).
                ahead = ["--grant", "observe", "--dir", "somewhere"] if shaped.startswith("--value") else []
                with mock.patch.object(sys, "argv", ["mv2", "-u", str(self.root), "plugins", *ahead, "secret",
                                                     "keeper", "api_key", *words]), mock.patch("sys.stdout", output), \
                        mock.patch("sys.stderr", errors), self.assertRaises(SystemExit) as exited:
                    cli.main()
                self.assertEqual(exited.exception.code, 2)
                self.assertIn("nothing was saved", errors.getvalue())
                self.assertNotIn(VALUE, errors.getvalue() + output.getvalue())
        self.assertEqual(secrets.where(self.root, "keeper", "api_key"), None)
        stdin = io.StringIO(VALUE + "\n")
        stdin.isatty = lambda: False
        output = io.StringIO()
        with mock.patch.object(sys, "argv", ["mv2", "-u", str(self.root), "plugins", "secret", "keeper", "api_key"]), \
                mock.patch("sys.stdin", stdin), mock.patch("sys.stdout", output):
            cli.main()
        self.assertNotIn(VALUE, output.getvalue())
        self.assertEqual(secrets.read(self.root, "keeper", "api_key"), VALUE)  # the newline dropped

    def test_the_keychain_tools_get_the_value_on_stdin_never_in_their_arguments(self):
        """With a keychain, the value reaches security or secret-tool on stdin only, and reads back exactly; a break
        shows the key to every user's ps."""
        python = sys.executable
        for tool_name, platform in (("security", "darwin"), ("secret-tool", "linux")):
            with self.subTest(tool=tool_name):
                fake = self.bin / tool_name
                fake.write_text(FAKE_TOOL.format(python=python))
                fake.chmod(0o755)
                keychain = self.work / f"{tool_name}.json"
                tricky = VALUE + ' "quoted" and \\ backslash'
                with mock.patch.dict(os.environ, {secrets.STORE: "keychain", "FAKE_KEYCHAIN": str(keychain)}), \
                        mock.patch.object(secrets.sys, "platform", platform):
                    self.assertEqual(self.run_tool("secret", level="api_key", secret_value=tricky)["store"],
                                     "keychain")
                    self.assertEqual(secrets.read(self.root, "keeper", "api_key"), tricky)
                    self.assertTrue(self.run_tool("secret", level="api_key", clear=True)["cleared"])
                    self.assertEqual(secrets.read(self.root, "keeper", "api_key"), None)
                calls = [json.loads(line) for line in Path(f"{keychain}.log").read_text().splitlines()]
                self.assertEqual(len(calls), 3)
                self.assertFalse([call for call in calls if any(VALUE in part for part in call["argv"])])
                self.assertFalse([call for call in calls if VALUE in call["stdin"]])  # stored as hex, exactly
                self.assertEqual(self.files_holding(VALUE, self.work), [])
                fake.unlink()

    def test_a_keychain_tool_never_gets_a_command_or_shows_a_value_and_a_refused_clear_stays_set(self):
        """No universe id can add a command to security -i, a tool's own output never reaches an error, and a clear
        the keychain refused stays recorded as set; a break runs an injected command, shows the key, or hides one
        still kept (reviewer, 8ff694b8)."""
        from types import SimpleNamespace
        from multiverse import plugins
        from multiverse.store import read, write
        encoded = VALUE.encode().hex()
        with self.subTest("an id with command syntax reaches no tool"):
            meta, calls = read(self.root / "universe.json"), []
            original = dict(meta)
            meta["id"] += '"\ndelete-generic-password -s "canary"\n#'
            write(self.root / "universe.json", meta)
            with mock.patch.dict(os.environ, {secrets.STORE: "keychain"}), \
                    mock.patch.object(secrets.sys, "platform", "darwin"), \
                    mock.patch.object(secrets, "_tool", side_effect=lambda *a, **k: calls.append(a)), \
                    self.assertRaisesRegex(Exception, "isn't one mv2 wrote"):
                self.run_tool("secret", level="api_key", secret_value=VALUE)
            self.assertEqual(calls, [])
            write(self.root / "universe.json", original)
        for platform in ("darwin", "linux"):
            with self.subTest("a tool echoing its input", platform=platform), \
                    mock.patch.dict(os.environ, {secrets.STORE: "keychain"}), \
                    mock.patch.object(secrets.sys, "platform", platform), \
                    mock.patch.object(secrets, "_tool", return_value=SimpleNamespace(
                        returncode=1, stdout=encoded, stderr="error: rejected " + encoded)):
                with self.assertRaises(plugins.PluginError) as raised:
                    self.run_tool("secret", level="api_key", secret_value=VALUE)
                self.assertNotIn(encoded, str(raised.exception))
                self.assertNotIn(VALUE, str(raised.exception))
            with self.subTest("a clear the keychain refused", platform=platform):
                kept = {}

                def tool(arguments, stdin=None, what=None):
                    if arguments[1] in ("delete-generic-password", "clear"):
                        return SimpleNamespace(returncode=1, stdout="", stderr="refused")
                    if stdin is not None:
                        kept["entry"] = encoded
                    return SimpleNamespace(returncode=0, stdout=kept.get("entry", ""), stderr="")
                with mock.patch.dict(os.environ, {secrets.STORE: "keychain"}), \
                        mock.patch.object(secrets.sys, "platform", platform), \
                        mock.patch.object(secrets, "_tool", side_effect=tool):
                    self.run_tool("secret", level="api_key", secret_value=VALUE)
                    with self.assertRaisesRegex(plugins.PluginError, "still kept and still recorded as set"):
                        self.run_tool("secret", level="api_key", clear=True)
                    self.assertIsNotNone(secrets.where(self.root, "keeper", "api_key"))
                    self.assertEqual(secrets.read(self.root, "keeper", "api_key"), VALUE)

    def test_a_listing_sees_a_secret_set_or_cleared_by_another_process(self):
        """Every listing, in this universe and a subverse reading its parent's, sees a required secret set or
        cleared elsewhere, and the stamp a screen watches changes; a break shows a plugin usable after its key
        was cleared in another terminal (qa-dashboard, via claude-tui)."""
        from multiverse import plugins
        sub = self.subverse("ops")
        self.enable("keeper", root=sub)
        state = lambda root: {item["name"]: item["state"] for item in plugins.listing(root, wait=0)}["keeper"]
        self.assertEqual((state(self.root), state(sub), plugins.secrets_stamp()), ("unusable", "unusable", None))
        secrets.set_secret(self.root, "keeper", "api_key", VALUE)  # as another process would, past this one's cache
        stamp = plugins.secrets_stamp()
        self.assertIsNotNone(stamp)
        self.assertEqual((state(self.root), state(sub)), ("on", "on"))
        secrets.clear_secret(self.root, "keeper", "api_key")
        self.assertNotEqual(plugins.secrets_stamp(), stamp)
        self.assertEqual((state(self.root), state(sub)), ("unusable", "unusable"))

    def test_a_terminal_that_cant_hide_typing_reads_nothing(self):
        """Where getpass can't hide what is typed it warns and would read it shown; the command refuses before
        reading instead; a break shows the key on the screen (claude-tui, from the reviewer)."""
        import getpass
        import warnings
        read = []

        def shown(prompt):
            warnings.warn("Can not control echo on the terminal.", getpass.GetPassWarning)
            read.append(prompt)
            return VALUE
        stdin = io.StringIO("")
        stdin.isatty = lambda: True
        with mock.patch("sys.stdin", stdin), mock.patch.object(getpass, "getpass", side_effect=shown), \
                self.assertRaisesRegex(ValueError, "can't hide what you type, so nothing was read"):
            tool.cli_run(self.root, plugin_action="secret", name="keeper", level="api_key")
        self.assertEqual(read, [])
        self.assertIsNone(secrets.where(self.root, "keeper", "api_key"))

    def test_doctor_names_a_secret_its_plugin_no_longer_declares(self):
        """A secret left set after its plugin stopped declaring it is named with the command that clears it, and
        clearing works; a break leaves an old API key in the store with nothing to show."""
        from multiverse import plugins
        self.run_tool("secret", level="token", secret_value=VALUE)
        self.write_plugin("keeper", {**KEEPER, "secrets": {"api_key": KEEPER["secrets"]["api_key"]}},
                          {"plugin.py": PLUGIN})
        loader._entries.clear()
        problems = [line for line in plugins.problems(self.root) if "token" in line]
        self.assertEqual(len(problems), 1)
        self.assertIn("mv2 plugins secret keeper token --clear", problems[0])
        self.assertTrue(self.run_tool("secret", level="token", clear=True)["cleared"])
        self.assertEqual(self.files_holding(VALUE, self.work), [])

    def test_a_manifest_declares_its_secrets_plainly(self):
        """Each secret has a lowercase key and a short label, and required is true or false; a break shows the
        person a nameless prompt, or a key that no command can name."""
        for secret, named in (({"API": {"label": "x"}}, "lowercase"), ({"key": {}}, "label"),
                              ({"key": {"label": "x", "required": "yes"}}, "true or false"),
                              ({"key": {"label": "x", "default": "v"}}, "must be")):
            with self.subTest(secret=secret):
                self.write_plugin("bad", {"api": "1.20", "secrets": secret})
                loader._entries.clear()
                self.assertIn(named, self.entry("bad").problem or "")


    def test_a_keychain_tool_never_outlives_its_call(self):
        """A keychain tool that runs out of time or is interrupted is killed and reaped, its output never shown; a
        leftover tool would hold the value it was given, unseen."""
        real, started = subprocess.Popen, []
        def start(*args, **kwargs):
            started.append(real(*args, **kwargs))
            return started[-1]
        sleeper = [sys.executable, "-c",  # echoes what it was given, and keeps running once its pipes close
                   "import sys, time\ntry:\n    print(sys.stdin.read(), flush=True)\nexcept OSError:\n    pass\n"
                   "time.sleep(60)"]
        cases = (("out of time", secrets.SecretsError, mock.patch.object(secrets, "TOOL_SECONDS", .2)),
                 ("interrupted", KeyboardInterrupt,
                  mock.patch.object(real, "communicate", side_effect=KeyboardInterrupt)))
        for case, raised, extra in cases:
            with self.subTest(case=case):
                started.clear()
                try:
                    with mock.patch.object(secrets.subprocess, "Popen", side_effect=start), extra:
                        with self.assertRaises(raised) as caught:
                            secrets._tool(sleeper, "VALUE-NEVER-SHOWN")
                    self.assertNotIn("VALUE-NEVER-SHOWN", repr(caught.exception))
                    self.assertIsNotNone(started[0].poll(), "the tool is still running")
                finally:
                    for process in started:
                        if process.poll() is None:
                            process.kill()
                        process.wait(timeout=5)

if __name__ == "__main__":
    unittest.main()
