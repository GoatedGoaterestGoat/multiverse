"""Offline regressions for launch environment and first-inbox checkpoints."""
import contextlib
from functools import partial
import io
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import unittest
from unittest.mock import Mock, patch
from uuid import uuid4

from native_safety import install as install_native_safety
install_native_safety()

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from multiverse.universe import agents
from multiverse.runtime import lifecycle
from multiverse.runtime import runner
from multiverse import harnesses
from multiverse.harnesses.types import LaunchContext, SessionHandle
from multiverse.universe import templates
import json
from multiverse.cli import parser, run
from multiverse.universe.mailbox import append_message, inbox
from multiverse.store import read, write, lock
from multiverse.tools import _common, inbox as inbox_tool, report, runtime_update, send, session_bind
from isolation import isolate


class LaunchIsolationTest(unittest.TestCase):
    def setUp(self):
        isolate(self)
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.work = Path(temporary.name).resolve()
        self.root = templates.create(self.work / "universe", "test", workdir=self.work)
        path = self.root / 'harnesses/codex/harness.json'
        definition = read(path)
        definition['settings']['inbox_bridge'] = False
        write(path, definition)
        self.enterContext(patch('multiverse.runtime.native.start_shared_service',
            side_effect=AssertionError('Offline launch contracts must never prepare a native daemon')))

    def agent(self, name, **properties):
        """An agent record whose current launch is `current`."""
        agent = self.root / 'state/agents' / str(uuid4())
        write(agent / 'properties.json', {'id': agent.name, 'name': name,
              'universe_id': read(self.root / 'universe.json')['id'], **properties})
        write(agent / 'runtime.json', {'launch_id': 'current'})
        return agent

    def command(self, env, entry, *args):
        """Run a CLI entry point in this process, as its `python -m` runs it, with `env` added."""
        out, err = io.StringIO(), io.StringIO()
        code = 0
        with patch.dict(os.environ, env), patch.object(sys, 'argv', ['mv2', '-u', str(self.root), *args]), \
                contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            try:
                entry()
            except SystemExit as error:
                code = error.code or 0
        return code, out.getvalue(), err.getvalue()

    def test_launch_storage_presence_survives_tmux_and_resume(self):
        """Storage values, absence, legacy evidence and aliases must survive tmux and resume; inherited or guessed homes silently select another account, including for children using a different harness."""
        for family, selected in (
                ('claude', {'CLAUDE_CONFIG_DIR': None}),
                ('claude', {'CLAUDE_CONFIG_DIR': ''}),
                ('claude', {'CLAUDE_CONFIG_DIR': str(self.work / 'claude-storage')}),
                ('pi', {'CODEX_HOME': str(self.work / 'codex-from-pi')}),
                ('codex', {'CODEX_HOME': None, 'CODEX_SQLITE_HOME': None}),
                ('codex', {'CODEX_HOME': str(self.work / 'codex-storage'),
                           'CODEX_SQLITE_HOME': str(self.work / 'database')})):
            with self.subTest(family=family, selected=selected):
                executable = self.work / (family + '-version-only')
                version = '2.1.303 (Claude Code)' if family == 'claude' else 'codex-cli 0.163.0'
                executable.write_text(f'#!{sys.executable}\nprint({version!r})\n')
                executable.chmod(0o755)
                manifest = self.root / 'harnesses' / family / 'harness.json'
                definition = read(manifest)
                definition['settings'].update(inbox_bridge=False, executable=str(executable))
                write(manifest, definition)
                backend = Mock()
                backend.create.return_value = {'socket': str(self.work / 'absent.sock'), 'session': '$1', 'pane': '%1'}
                selected = {**{key: None for key in ('CLAUDE_CONFIG_DIR', 'CODEX_HOME', 'CODEX_SQLITE_HOME')}, **selected}
                foreign = 'CODEX_HOME' if family == 'claude' else 'CLAUDE_CONFIG_DIR'
                selected[foreign] = str(self.work / ('other-harness-' + family))
                caller = {key: value for key, value in os.environ.items() if key not in selected}
                caller.update({key: value for key, value in selected.items() if value is not None})
                expected = dict(selected)
                if family == 'codex':
                    expected['CODEX_HOME'] = str(Path(selected['CODEX_HOME'] or (Path(caller['HOME']) / '.codex')).resolve())
                with patch.object(lifecycle, 'Tmux', return_value=backend), \
                        patch.object(lifecycle, 'observe', return_value={'live': 'dormant'}), \
                        patch.dict(os.environ, caller, clear=True):
                    result = lifecycle.start(self.root, name='storage-' + str(uuid4()), harness=family)
                    directory = self.root / 'state/agents' / result['id']
                    if family == 'codex':
                        agents.bind_session(self.root, result['id'], str(uuid4()))
                    elif family == 'pi':
                        (directory / 'session.jsonl').write_text(json.dumps({'type': 'session', 'id': str(uuid4())}) + '\n')
                    for mode in ('spawn', 'resume'):
                        if mode == 'resume':
                            for key in selected:
                                os.environ[key] = str(self.work / ('caller-changed-' + key))
                            result = lifecycle.start(self.root, resume=result['id'])
                        request = read(directory / 'launches' / result['runtime']['launch_id'] / 'request.json')
                        with patch.dict(os.environ, {**{key: str(self.work / ('server-' + key)) for key in selected},
                                                     'HOME': str(self.work / 'server-home')}):
                            environment = runner.launch_environment(request)
                        self.assertEqual({key: environment.get(key) for key in selected}, expected, mode)
                        self.assertEqual(environment['HOME'], caller['HOME'], mode)
                        self.assertEqual(read(directory / 'properties.json')['native_storage_environment'], expected)
                    if family == 'codex' and selected['CODEX_HOME'] is not None:
                        for key in ('CODEX_HOME', 'CODEX_SQLITE_HOME'):
                            with self.subTest(retargeted=key):
                                path = Path(expected[key])
                                path.mkdir(parents=True, exist_ok=True)
                                original = path.with_name(path.name + '-original')
                                different = path.with_name(path.name + '-different')
                                different.mkdir()
                                paths = [directory / 'properties.json', directory / 'runtime.json',
                                         *directory.glob('launches/*/request.json')]
                                before = {path: path.read_bytes() for path in paths}
                                launches = list((directory / 'launches').iterdir())
                                created = backend.create.call_count
                                path.rename(original)
                                path.symlink_to(different, target_is_directory=True)
                                try:
                                    with self.assertRaisesRegex(ValueError, 'Native session storage differs') as caught:
                                        lifecycle.start(self.root, resume=result['id'])
                                    self.assertIn(str(path), str(caught.exception))
                                    self.assertIn(str(different), str(caught.exception))
                                    self.assertIn('Restore the original storage', str(caught.exception))
                                    self.assertEqual({path: path.read_bytes() for path in paths}, before)
                                    self.assertEqual(list((directory / 'launches').iterdir()), launches)
                                    self.assertEqual(backend.create.call_count, created)
                                finally:
                                    path.unlink()
                                    original.rename(path)
                    if family == 'codex' and selected['CODEX_SQLITE_HOME'] is None:
                        properties = read(directory / 'properties.json')
                        properties['native_service_identity'] = {'provider': 'codex', 'scope': {
                            'CODEX_HOME': expected['CODEX_HOME']}}
                        write(directory / 'properties.json', properties)
                        before = (directory / 'runtime.json').read_bytes()
                        with self.assertRaisesRegex(ValueError, 'Cannot add CODEX_SQLITE_HOME'):
                            lifecycle.start(self.root, resume=result['id'])
                        self.assertEqual((directory / 'runtime.json').read_bytes(), before)
        # A main-era plain launch inherited tmux's home, which may differ from
        # this caller. Only its saved service scope or explicit request is proof.
        with self.subTest('legacy storage must come from the launch, never the current caller'):
            properties = read(directory / 'properties.json')
            facts = read(directory / 'runtime.json')
            request_path = directory / 'launches' / facts['launch_id'] / 'request.json'
            request = read(request_path)
            legacy = {key: value for key, value in properties.items()
                      if key not in ('native_storage_environment', 'native_environment', 'native_service_identity')}
            storage = {'CODEX_HOME': str(self.work / 'actual-old-home'),
                       'CODEX_SQLITE_HOME': str(self.work / 'actual-old-database')}
            for proof in ('missing', 'request', 'service', 'partial-request', 'request-unset', 'request-alias'):
                with self.subTest(proof=proof):
                    old_request = {**request, 'env': {key: value for key, value in request['env'].items()
                                   if key not in (*storage, 'HOME')},
                                   'unset_env': [key for key in request['unset_env'] if key not in storage]}
                    old_properties = dict(legacy)
                    expected = dict(storage)
                    if proof in ('request', 'partial-request', 'request-alias'):
                        old_request['env'].update({'CODEX_HOME': storage['CODEX_HOME']} if proof == 'partial-request' else storage)
                    elif proof == 'service':
                        old_properties['native_service_identity'] = {'provider': 'codex', 'scope': dict(storage)}
                        old_properties['native_environment'] = dict(storage)
                    elif proof == 'request-unset':
                        old_request['env']['HOME'] = str(self.work / 'original-home')
                        old_request['unset_env'].extend(storage)
                        expected = {'CODEX_HOME': str(self.work / 'original-home/.codex'), 'CODEX_SQLITE_HOME': None}
                    if proof == 'request-alias':
                        path = Path(storage['CODEX_HOME'])
                        path.symlink_to(self.work / 'different-home', target_is_directory=True)
                        self.addCleanup(path.unlink, missing_ok=True)
                    write(directory / 'properties.json', old_properties)
                    write(directory / 'runtime.json', facts)
                    write(request_path, old_request)
                    paths = [directory / 'properties.json', directory / 'runtime.json', request_path]
                    before = {path: path.read_bytes() for path in paths}
                    backend.reset_mock()
                    with patch.object(lifecycle, 'Tmux', return_value=backend), \
                            patch.object(lifecycle, 'observe', return_value={'live': 'dormant'}), \
                            patch.dict(os.environ, {key: str(self.work / ('wrong-caller-' + key)) for key in storage}):
                        if proof in ('missing', 'partial-request', 'request-alias'):
                            reason = 'Native session storage differs' if proof == 'request-alias' else 'original Codex storage.*not recorded'
                            with self.assertRaisesRegex(ValueError, reason):
                                lifecycle.start(self.root, resume=directory.name)
                            self.assertEqual({path: path.read_bytes() for path in paths}, before)
                            backend.create.assert_not_called()
                        else:
                            result = lifecycle.start(self.root, resume=directory.name)
                            actual = read(directory / 'launches' / result['runtime']['launch_id'] / 'request.json')
                            self.assertEqual({key: actual['env'].get(key) for key in storage}, expected)
                            self.assertEqual({key: result['native_storage_environment'][key] for key in storage}, expected)

    def test_inbox_checkpoints_never_skip_or_repeat(self):
        """Inbox cursor checkpoints never skip or repeat a message, through an empty first read, a limited read,
        pages and a partial tail; fragile because of byte-offset arithmetic, and a break silently loses or
        duplicates messages."""
        agent = self.work / "empty-agent"
        for existing in (False, True):
            with self.subTest(read='empty', existing=existing):
                if existing:
                    (agent / "inbox.jsonl").touch()
                batch = inbox(agent)
                self.assertEqual(batch, {"messages": [], "cursor": 0})
                self.assertEqual(inbox(agent), batch)
                for invalid in (-1, 1):
                    with self.assertRaises(ValueError):
                        inbox(agent, after=invalid)
        message = append_message(agent, "after empty checkpoint", {})
        self.assertEqual(inbox(agent)["messages"], [message])
        self.assertEqual(inbox(agent)["messages"], [])

        with self.subTest(read='limited'):
            agent = self.work / 'limited-agent'
            first = append_message(agent, 'nonce one', {})
            second = append_message(agent, 'nonce two', {})
            self.assertNotEqual(first['id'], second['id'])
            batch = inbox(agent, limit=1)
            self.assertEqual(batch['messages'][0]['id'], first['id'])
            self.assertEqual(inbox(agent)['messages'][0]['id'], second['id'])
            with self.assertRaises(ValueError):
                inbox(agent, after=batch['cursor'] + 1)
            self.assertEqual(inbox(agent)['messages'], [])

        with self.subTest(read='pages'):
            agent = self.work / 'paged-agent'
            agent.mkdir()
            messages = [append_message(agent, f'内容 {index}', {}) for index in range(53)]
            history = inbox(agent, all_messages=True)
            self.assertFalse((agent / 'inbox.cursor').exists())
            with self.assertRaisesRegex(ValueError, 'browse ahead'):
                inbox(agent, after=history['cursor'])
            first = inbox(agent, after=0)
            second = inbox(agent, after=first['cursor'])
            self.assertEqual(first['messages'], messages[:50])
            self.assertTrue(first['has_more'])
            self.assertEqual(second['messages'], messages[50:])
            self.assertFalse(second['has_more'])
            self.assertEqual(second['read_cursor'], first['cursor'])
            self.assertEqual(inbox(agent, after=0)['messages'], [])
            with self.assertRaisesRegex(ValueError, 'Invalid inbox cursor'):
                inbox(agent, after=first['cursor'] - 1)
            with (agent / 'inbox.jsonl').open('ab') as out:
                out.write(b'{"incomplete":')
            self.assertFalse(inbox(agent, after=second['cursor'])['has_more'])

    def test_inbox_unknown_agent_does_not_create_state(self):
        """Reading the inbox of an unknown agent creates no state; a break goes unnoticed because the stray
        folder only shows up later as a record-less orphan row."""
        before = list((self.root / "state" / "agents").iterdir())
        args = parser().parse_args(["-u", str(self.root), "inbox", "--agent", "missing"])
        with self.assertRaisesRegex(ValueError, "No agent named missing here"):
            run(args)
        self.assertEqual(list((self.root / "state" / "agents").iterdir()), before)

    def adapter_values(self):
        values = dict(name='literal', agent_id=str(uuid4()), session_id=str(uuid4()), bootstrap='Hello "quoted"',
                      bootstrap_json='"Hello"', task='--literal $(false) `false` {literal}',
                      agent_dir=str(self.work / 'agent'), harness_dir='/local/harness',
                      session_file=str(self.work / 'agent/session.jsonl'), bootstrap_file='/launch/bootstrap.md',
                      task_file='/launch/task.md', workspace=str(self.work), model='chosen',
                      control_socket='/tmp/owned/rpc.sock')
        session_file = Path(values['session_file'])
        session_file.parent.mkdir(exist_ok=True)
        session_file.write_text(json.dumps({'type': 'session', 'id': values['session_id']}) + '\n')
        return values

    def test_launch_request_carries_the_right_socket_env_and_workspace(self):
        """On spawn and resume the launch request carries the actual tmux socket, the harness env, identity over
        inherited values, the agent's own workspace and the legacy no-workspace fallback; fragile because it
        merges several env sources and old records, and a break runs the agent in the wrong worktree or points its
        mv2 at the user's live tmux server."""
        def session(directory):
            (directory / "session.jsonl").write_text(json.dumps({"type": "session", "id": str(uuid4())}) + "\n")

        terminal = {"socket": "/actual/tmux.sock", "session": "$1", "pane": "%1"}
        backend = Mock()
        backend.create.return_value = terminal
        with self.subTest(part='socket'), patch.object(lifecycle, "Tmux", return_value=backend) as tmux, \
                patch.object(lifecycle.shutil, "which", return_value="/fake/executable"), \
                patch.object(lifecycle, "observe", return_value={"live": "dormant"}), \
                patch.dict(os.environ, {"MV2_TMUX_SOCKET": "/inherited/tmux.sock"}):
            first = lifecycle.start(self.root, "socket", "test", "pi", socket_path="/explicit/tmux.sock")
            directory = agents.directory(self.root, first["id"])
            session(directory)
            for result in (first, lifecycle.start(self.root, resume=first["id"])):
                launch = directory / "launches" / result["runtime"]["launch_id"]
                self.assertTrue((launch / "ready").exists())
                request = read(launch / "request.json")
                self.assertEqual(request["env"]["MV2_TMUX_SOCKET"], terminal["socket"])
            self.assertIn(unittest.mock.call("/explicit/tmux.sock"), tmux.call_args_list)
            self.assertIn(unittest.mock.call("/inherited/tmux.sock"), tmux.call_args_list)

        path = self.root / "harnesses/pi/harness.json"
        definition = read(path)
        definition["settings"]["env"] = {"IS_SANDBOX": "1", "INHERITED": "harness"}
        write(path, definition)
        backend = Mock()
        backend.create.return_value = {"socket": "/fake/socket", "session": "$1", "pane": "%1"}
        with self.subTest(part='harness env'), patch.object(lifecycle, "Tmux", return_value=backend), \
                patch.object(lifecycle.shutil, "which", return_value="/fake/executable"), \
                patch.object(lifecycle, "observe", return_value={"live": "dormant"}):
            first = lifecycle.start(self.root, "env", "task", "pi")
            directory = agents.directory(self.root, first["id"])
            session(directory)
            for result in (first, lifecycle.start(self.root, resume=first["id"])):
                request = read(directory / "launches" / result["runtime"]["launch_id"] / "request.json")
                with patch.dict(os.environ, {"INHERITED": "tmux server", "MV2_AGENT_ID": "parent"}):
                    env = runner.launch_environment(request)
                # The harness's value replaces the inherited one; this launch's identity still wins.
                self.assertEqual(env["IS_SANDBOX"], "1")
                self.assertEqual(env["INHERITED"], "harness")
                self.assertEqual(env["MV2_AGENT_ID"], first["id"])

        env = {'IS_SANDBOX': '1', 'NO_COLOR': '1', 'LITERAL': 'a "quoted" $(false) é'}
        values = self.adapter_values()
        with patch('multiverse.harnesses.claude.claude_version', return_value=(2, 1, 289)):
            for provider, bridge in (('claude', False), ('claude', True), ('pi', False), ('codex', False)):
                adapter = harnesses.BUILTINS[provider].configured(provider, {'inbox_bridge': bridge, 'env': env})
                handle = SessionHandle(values['agent_id'], provider, values['workspace'],
                                       native_id=values['session_id'], session_file=values['session_file'])
                for mode in ('spawn', 'resume'):
                    with self.subTest(part='every adapter', provider=provider, bridge=bridge, mode=mode):
                        context = LaunchContext(mode, values)
                        command = adapter.spawn(context) if mode == 'spawn' else adapter.resume(handle, context)
                        self.assertEqual({key: command.environment.get(key) for key in env}, env)
                        # A variable the harness sets is no longer removed by default; parent markers still are.
                        self.assertNotIn('NO_COLOR', command.unset_env)
                        self.assertIn('CLAUDECODE', command.unset_env)
                        if provider == 'claude' and bridge:
                            self.assertEqual(command.environment['CLAUDE_CODE_ENABLE_FUNCTION_HOOKS'], '1')
        legacy = harnesses.LegacyAdapter('custom', {'spawn': ['custom'], 'env': {'CUSTOM': 'value'}})
        self.assertEqual(legacy.spawn(LaunchContext('spawn', values)).environment, {'CUSTOM': 'value'})

        other = self.work / "other-worktree"
        other.mkdir()
        with self.subTest(part='workspace'), patch.object(lifecycle, "Tmux", return_value=backend), \
                patch.object(lifecycle.shutil, "which", return_value="/fake/executable"), \
                patch.object(lifecycle, "observe", return_value={"live": "dormant"}):
            first = lifecycle.start(self.root, "workspace", "task", "pi")
            directory = agents.directory(self.root, first["id"])
            session(directory)
            metadata = read(self.root / "universe.json")
            metadata["workspace"] = str(other)
            write(self.root / "universe.json", metadata)
            resumed = lifecycle.start(self.root, resume=first["id"])
            self.assertEqual(resumed["workspace"], str(self.work))
            self.assertEqual(backend.create.call_args.args[1], self.work)
            properties = read(directory / "properties.json")
            properties["workspace"] = str(self.work / "missing")
            write(directory / "properties.json", properties)
            with self.assertRaisesRegex(ValueError, "Workspace does not exist"):
                lifecycle.start(self.root, resume=first["id"])
            del properties["workspace"]
            write(directory / "properties.json", properties)
            legacy = lifecycle.start(self.root, resume=first["id"])
            self.assertEqual(legacy["workspace"], str(other))
            self.assertEqual(backend.create.call_args.args[1], other)

    def test_child_env_has_only_the_launch_identity_and_no_parent_markers(self):
        """For every bundled adapter, as shipped, with a replaced unset_env or setting a marker itself, the native
        child the real runner starts gets only the launch's identity and socket, and none of a parent's markers
        (CODEX_THREAD_ID, CLAUDECODE, CLAUDE_CODE_CHILD_SESSION, the IDE port, the messaging socket and token) or
        NO_COLOR unless its harness sets one; a break goes unnoticed because the child still runs, but as its parent,
        or as a Claude that inherited CLAUDE_CODE_CHILD_SESSION and saves no transcript, so resume silently starts
        fresh and usage reads nothing, or one holding the parent's inbox secret (a6a7628, a81fed1, 74887d9,
        CLAUDE-5)."""
        manifests = sorted(templates.TEMPLATES.glob("*/harnesses/*/harness.json"))
        self.assertEqual({manifest.parent.name for manifest in manifests}, {'claude', 'codex', 'pi'})
        markers = ("CODEX_THREAD_ID", "CLAUDECODE", "CLAUDE_CODE_CHILD_SESSION", "CLAUDE_CODE_SSE_PORT",
                   "CLAUDE_CODE_MESSAGING_SOCKET", "CLAUDE_CODE_MESSAGING_TOKEN")
        parent = {"MV2_AGENT_ID": "parent", "MV2_TMUX_SOCKET": "/stale", "MV2_STALE": "parent",
                  **{name: "parent" for name in markers}, "CLAUDE_CODE_CHILD_SESSION": "1", "NO_COLOR": "1",
                  "TERM": "tmux-256color"}
        values = self.adapter_values()
        # environment.md suggests replacing unset_env (for NO_COLOR, say); the markers must stay removed then.
        variants = ({}, {"unset_env": ["NO_COLOR"]}, {"env": {"CLAUDE_CODE_CHILD_SESSION": "0"}})
        for manifest in manifests:
            name = manifest.parent.name
            for number, settings in enumerate(variants):
                with self.subTest(manifest=str(manifest.relative_to(templates.TEMPLATES)), settings=settings):
                    scope = self.work / "markers" / name / str(number)
                    config = read(manifest)
                    write(scope / "harnesses" / name / "harness.json",
                          {**config, "settings": {**config["settings"], **settings}})
                    with patch('multiverse.harnesses.claude.claude_version', return_value=(2, 1, 302)):
                        command = harnesses.load(scope, name).spawn(LaunchContext('spawn', values))
                    launch_id, agent_id, universe_id = (str(uuid4()) for _ in range(3))
                    agent = scope / 'state/agents' / agent_id
                    launch = agent / 'launches' / launch_id
                    identity = {'MV2_CLI': '/child/bin/mv2', 'MV2_UNIVERSE': str(scope),
                                'MV2_AGENT_ID': agent_id, 'MV2_AGENT_DIR': str(agent),
                                'MV2_LAUNCH_ID': launch_id, 'MV2_TMUX_SOCKET': str(scope / 'absent.sock')}
                    write(scope / 'universe.json', {'id': universe_id, 'name': 'markers', 'workspace': str(self.work)})
                    # Real launches publish identity before releasing the runner, including plain launches.
                    write(agent / 'properties.json', {'id': agent_id, 'universe_id': universe_id,
                          'name': 'child', 'harness': name, 'workspace': str(self.work)})
                    output = scope / "child.json"
                    code = ('import json,os,pathlib; pathlib.Path(' + repr(str(output)) +
                            ').write_text(json.dumps(dict(os.environ)))')
                    write(agent / "runtime.json", {"launch_id": launch_id, "state": "starting"})
                    write(launch / "request.json", {"argv": [sys.executable, "-c", code],
                                                    "env": {**command.environment, **identity},
                                                    "unset_env": list(command.unset_env)})
                    (launch / "ready").touch()
                    # The real runner, so the child's environment is the one a native process gets.
                    result = subprocess.run([sys.executable, str(Path(runner.__file__)), str(launch)],
                                            env={**os.environ, **parent}, stdin=subprocess.DEVNULL,
                                            text=True, capture_output=True, timeout=10)
                    self.assertEqual(result.returncode, 0, result.stderr)
                    env = read(output)
                    self.assertEqual({key: value for key, value in env.items() if key.startswith("MV2_")}, identity)
                    # Only these names, so that a failure never prints the rest of the environment. The tmux server
                    # keeps the environment of whatever first started it, often a tool shell exporting NO_COLOR.
                    # Agent panes are interactive.
                    self.assertEqual({key: env[key] for key in (*markers, "NO_COLOR") if key in env},
                                     settings.get("env", {}))
                    self.assertEqual(env["TERM"], "tmux-256color")
                    self.assertTrue(env["PATH"].startswith("/child/bin" + os.pathsep))

        # Claude Code (2.1.301+) saves no transcript when it inherits CLAUDE_CODE_CHILD_SESSION=1, the marker it
        # sets for its own tool subprocesses; a tmux server first started from one passes it to every pane. A
        # Claude agent spawned from inside Claude also inherits the parent's IDE port and its inbox path and secret.
        names = ('CLAUDE_CODE_CHILD_SESSION', 'CLAUDE_CODE_SSE_PORT', 'CLAUDE_CODE_MESSAGING_SOCKET',
                 'CLAUDE_CODE_MESSAGING_TOKEN')
        inherited = {'CLAUDE_CODE_CHILD_SESSION': '1', **{name: 'parent' for name in names[1:]}, 'PATH': '/usr/bin'}
        path = self.root / 'harnesses/claude/harness.json'
        original = read(path)
        for settings in ({}, {'inbox_bridge': True}, {'inbox_bridge': False}, {'unset_env': ['NO_COLOR']},
                         {'env': {'CLAUDE_CODE_CHILD_SESSION': '0', 'CLAUDE_CODE_SSE_PORT': '7777'}}):
            write(path, {**original, 'settings': {**original['settings'], **settings}})
            adapter = harnesses.load(self.root, 'claude')
            for mode in ('spawn', 'resume'):
                with self.subTest(settings=settings, mode=mode), \
                        patch('multiverse.harnesses.claude.claude_version', return_value=(2, 1, 302)):
                    if mode == 'spawn':
                        command = adapter.spawn(LaunchContext('spawn', values))
                    else:
                        command = adapter.resume(SessionHandle(values['agent_id'], 'claude', str(self.work),
                                                               native_id=values['session_id']),
                                                 LaunchContext('resume', values))
                    self.assertIn('CLAUDE_CODE_CHILD_SESSION', command.unset_env)
                    with patch.dict(os.environ, inherited):
                        env = runner.launch_environment({'unset_env': list(command.unset_env),
                                                         'env': {**command.environment, 'MV2_CLI': '/mv2/bin/mv2'}})
                    self.assertEqual({name: env[name] for name in names if name in env}, settings.get('env', {}))

    def test_old_launch_callbacks_are_refused(self):
        """Every callback from an outdated launch (inbox, report, send, session-bind, runtime-update) is refused
        without output or any change, including one that waited for the launch lock while resume published;
        fragile because each new callback must check the generation, and a break lets an old launch steal the new
        one's messages or overwrite its status (797bfec, qa Q1)."""
        agent = self.agent("callback", phase="working")
        write(agent / "runtime.json", {"launch_id": "current", "status": "working"})
        message = append_message(agent, "for current launch", {})
        before = {path.name: path.read_bytes() for path in agent.iterdir() if path.is_file()}
        stale = {"MV2_LAUNCH_ID": "old"}
        actions = (["inbox"], ["inbox", "--peek"],
                   ["report", "--phase", "done"], ["session-bind", "--session-id", str(uuid4())])
        for action in actions:
            with self.subTest(entry='mv2', action=action), patch.dict(os.environ, stale):
                args = parser().parse_args(["-u", str(self.root), *action, "--agent", agent.name])
                with self.assertRaisesRegex(ValueError, "Outdated launch"):
                    run(args)
        # Integrations call each callback's own module, which must print nothing to its stdout.
        for module, args in ((inbox_tool, ()), (report, ("--phase", "done")), (runtime_update, ("--status", "idle")),
                             (session_bind, ("--session-id", str(uuid4())))):
            with self.subTest(entry='standalone', action=module.__name__):
                code, out, err = self.command(stale, partial(_common.main, module.configure, module.run),
                                              "--agent", agent.name, *args)
                self.assertEqual((code, out), (1, ''))
                self.assertIn("Outdated launch", err)
        output = io.StringIO()
        with self.subTest(entry='inbox output'), patch.dict(os.environ, stale), contextlib.redirect_stdout(output), \
                self.assertRaisesRegex(ValueError, 'Outdated launch'):
            inbox_tool.cli_run(self.root, agent=agent.name)
        self.assertEqual(output.getvalue(), '')
        # Status comes from harness integrations, not an mv2 command, and also
        # refuses an outdated launch.
        with self.assertRaises(SystemExit), contextlib.redirect_stderr(io.StringIO()):
            parser().parse_args(["-u", str(self.root), "runtime-update", "--status", "idle", "--agent", agent.name])
        with patch.dict(os.environ, stale), self.assertRaisesRegex(ValueError, "Outdated launch"):
            runtime_update.run(self.root, agent=agent.name, status="idle")
        self.assertEqual({path.name: path.read_bytes() for path in agent.iterdir() if path.is_file()}, before)
        self.assertFalse((agent / "inbox.cursor").exists())
        self.assertEqual(inbox(agent)["messages"], [message])

        with self.subTest(action='send'):
            sender, recipient = self.agent('sender'), self.agent('recipient')
            identity = {'MV2_UNIVERSE': str(self.root), 'MV2_AGENT_ID': sender.name, 'MV2_AGENT_DIR': str(sender)}
            with patch.dict(os.environ, {**identity, 'MV2_LAUNCH_ID': 'old'}), \
                    self.assertRaisesRegex(ValueError, 'Outdated launch'):
                send.run(self.root, agent='recipient', text='from a stale launch', delivery='inbox-only')
            with patch.dict(os.environ, {**identity, 'MV2_LAUNCH_ID': 'current'}):
                send.run(self.root, agent='recipient', text='from the current launch', delivery='inbox-only')
            self.assertEqual([message['text'] for message in inbox(recipient, all_messages=True)['messages']],
                             ['from the current launch'])

        with self.subTest(action='runtime-update --bridge'):
            bridged = self.agent('bridged', harness='claude')
            write(bridged / 'runtime.json', {'launch_id': 'current', 'state': 'running'})
            with patch.dict(os.environ, {'MV2_LAUNCH_ID': 'current'}):
                runtime_update.run(self.root, agent=bridged.name, status='idle', bridge='claude-mod-v1')
                runtime_update.run(self.root, agent=bridged.name, status='waiting')
            with patch.dict(os.environ, {'MV2_LAUNCH_ID': 'previous'}), \
                    self.assertRaisesRegex(ValueError, 'Outdated launch'):
                runtime_update.run(self.root, agent=bridged.name, status='working', bridge='other-bridge-v1')
            facts = read(bridged / 'runtime.json')
            self.assertEqual((facts['native_bridge'], facts['status']), ('claude-mod-v1', 'waiting'))

        with self.subTest(action='bind_session'):
            bound = self.agent('bound')
            write(bound / 'runtime.json', {'launch_id': 'new'})
            identifier = str(uuid4())
            with self.assertRaises(ValueError):
                agents.bind_session(self.root, 'bound', 'not-a-session', 'new')
            with self.assertRaisesRegex(ValueError, 'Outdated'):
                agents.bind_session(self.root, 'bound', identifier, 'old')
            self.assertNotIn('native_session_id', read(bound / 'properties.json'))
            agents.bind_session(self.root, 'bound', identifier, 'new')
            agents.bind_session(self.root, 'bound', identifier, 'new')
            with self.assertRaisesRegex(ValueError, 'different native session'):
                agents.bind_session(self.root, 'bound', str(uuid4()), 'new')

        with self.subTest(action='inbox after waiting for the launch lock'):
            raced = self.agent('race')
            write(raced / 'runtime.json', {'launch_id': 'old'})
            message = append_message(raced, 'must remain unread', {})
            validated, errors = threading.Event(), []
            original = inbox_tool.callback

            def callback(*args, **kwargs):
                value = original(*args, **kwargs)
                validated.set()
                return value

            def consume():
                try:
                    inbox_tool.run(self.root, agent=raced.name)
                except Exception as error:
                    errors.append(error)
            with patch.object(inbox_tool, 'callback', callback), patch.dict(os.environ, {'MV2_LAUNCH_ID': 'old'}):
                with lock(raced / '.launch.lock'):
                    worker = threading.Thread(target=consume)
                    worker.start()
                    self.assertTrue(validated.wait(2))
                    # Resume publishes its replacement while the old callback waits.
                    write(raced / 'runtime.json', {'launch_id': 'new'})
                worker.join(2)
            self.assertFalse(worker.is_alive())
            self.assertEqual(len(errors), 1)
            self.assertIsInstance(errors[0], ValueError)
            self.assertIn('Outdated launch', str(errors[0]))
            self.assertEqual(inbox(raced)['messages'], [message])


if __name__ == "__main__":
    unittest.main()
