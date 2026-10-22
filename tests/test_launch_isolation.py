"""Offline regressions for launch environment and first-inbox checkpoints."""
import contextlib
from functools import partial
from dataclasses import replace
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
from multiverse.universe import agents, storage as storage_records
from multiverse.runtime import lifecycle
from multiverse.runtime import runner
from multiverse import harnesses
from multiverse.harnesses.types import LaunchContext, SessionHandle, SharedServiceSpec, ServiceIdentity, NativeEndpoint, NativeResult
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
        alias_parent = self.work / 'alias-parent'
        alias_parent.symlink_to(self.work, target_is_directory=True)
        for family, selected in (
                ('claude', {'CLAUDE_CONFIG_DIR': str(alias_parent / 'claude-alias-home')}),
                ('claude', {'CLAUDE_CONFIG_DIR': None}),
                ('claude', {'CLAUDE_CONFIG_DIR': ''}),
                ('claude', {'CLAUDE_CONFIG_DIR': str(self.work / 'claude-storage')}),
                ('pi', {'PI_CODING_AGENT_DIR': None, 'CODEX_HOME': str(self.work / 'codex-from-pi')}),
                ('pi', {'PI_CODING_AGENT_DIR': ''}),
                ('pi', {'PI_CODING_AGENT_DIR': str(self.work / 'pi-storage')}),
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
                selected = {**{key: None for key in ('CLAUDE_CONFIG_DIR', 'CODEX_HOME', 'CODEX_SQLITE_HOME',
                                                    'PI_CODING_AGENT_DIR')}, **selected}
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
                            self.assertTrue(any(f'ignored {foreign}=' in line for line in result.get('notices', [])))
                        request = read(directory / 'launches' / result['runtime']['launch_id'] / 'request.json')
                        with patch.dict(os.environ, {**{key: str(self.work / ('server-' + key)) for key in selected},
                                                     'HOME': str(self.work / 'server-home')}):
                            environment = runner.launch_environment(request)
                        self.assertEqual({key: environment.get(key) for key in selected}, expected, mode)
                        self.assertEqual(environment['HOME'], caller['HOME'], mode)
                        self.assertEqual(read(directory / 'properties.json')['native_storage_environment'], expected)
                    own_key = 'CLAUDE_CONFIG_DIR' if family == 'claude' else 'CODEX_HOME'
                    if family in ('claude', 'codex') and expected.get(own_key):
                        with self.subTest('a hand-restored alias normalizes against retained canonical proof'):
                            alias = self.work / ('restored-alias-' + str(uuid4()))
                            alias.symlink_to(expected[own_key], target_is_directory=True)
                            restored = read(directory / 'properties.json')
                            restored['native_storage_environment'][own_key] = str(alias)
                            write(directory / 'properties.json', restored)
                            result = lifecycle.start(self.root, resume=result['id'])
                            self.assertEqual(result['native_storage_environment'][own_key], str(alias) if family == 'claude' else expected[own_key])
                            alias.unlink()
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
                      if key not in ('native_storage_environment', 'native_environment', 'native_service_identity', 'native_session_home')}
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
                    recorded = storage_records.recorded_environment(directory, old_properties, 'codex', tuple(storage), facts)
                    if proof in ('missing', 'partial-request'):
                        self.assertIsNone(recorded)
                    else:
                        self.assertEqual({key: recorded[key] for key in storage}, expected)
                    self.assertEqual({path: path.read_bytes() for path in paths}, before)
                    backend.reset_mock()
                    with patch.object(lifecycle, 'Tmux', return_value=backend), \
                            patch.object(lifecycle, 'observe', return_value={'live': 'dormant'}), \
                            patch.dict(os.environ, {key: str(self.work / ('wrong-caller-' + key)) for key in storage}):
                        if proof in ('missing', 'partial-request', 'request-alias'):
                            reason = 'Cannot prove unique conversation storage'
                            with self.assertRaisesRegex(ValueError, reason):
                                lifecycle.start(self.root, resume=directory.name)
                            self.assertEqual({path: path.read_bytes() for path in paths}, before)
                            backend.create.assert_not_called()
                        else:
                            result = lifecycle.start(self.root, resume=directory.name)
                            actual = read(directory / 'launches' / result['runtime']['launch_id'] / 'request.json')
                            self.assertEqual({key: actual['env'].get(key) for key in storage}, expected)
                            self.assertEqual({key: result['native_storage_environment'][key] for key in storage}, expected)

    def test_legacy_storage_adopts_only_a_unique_conversation_file(self):
        """Legacy adoption must find the exact conversation or prove no child ever started; otherwise storage can strand history, bridge preparation can reject HOME as a storage key or erase unknown keys, and two independent fresh-start decisions can repeat their notice."""
        from multiverse.harnesses.claude import project_key
        cases = ('server', 'caller', 'request', 'none', 'multiple', 'unreadable', 'same-home-alias', 'default-server', 'default-server-bridge', 'default-server-auto', 'server-spelling', 'restored-spelling', 'scope-spelling', 'empty-before-child', 'empty-may-have-child', 'empty-accepted')
        for family, found in ((family, found) for family in ('claude', 'codex') for found in cases):
            if family == 'claude' and found in ('restored-spelling', 'scope-spelling', 'default-server-bridge', 'default-server-auto'):
                continue
            with self.subTest(family=family, found=found):
                key = 'CLAUDE_CONFIG_DIR' if family == 'claude' else 'CODEX_HOME'
                executable = self.work / (family + '-version-only')
                version = '2.1.303 (Claude Code)' if family == 'claude' else 'codex-cli 0.163.0'
                executable.write_text(f'#!{sys.executable}\nprint({version!r})\n')
                executable.chmod(0o755)
                manifest = self.root / 'harnesses' / family / 'harness.json'
                definition = read(manifest)
                definition['settings'].update(inbox_bridge=found == 'default-server-bridge', executable=str(executable))
                if found == 'default-server-auto':
                    definition['settings'].pop('inbox_bridge')
                write(manifest, definition)
                directory, properties, _, _ = lifecycle._record(self.root, family + '-legacy-' + found, '', family, None, None)
                properties['workspace'] = str(self.work)
                properties['native_session_id'] = properties['id'] if found.startswith('empty-') else str(uuid4())
                write(directory / 'properties.json', properties)
                launch = str(uuid4())
                facts = {'launch_id': launch, 'state': 'dormant',
                         'tmux': {'socket': str(self.work / 'old.sock'), 'session': '$1', 'pane': '%1'}}
                write(directory / 'runtime.json', facts)
                homes = {key: self.work / family / found / key for key in ('server', 'caller', 'request')}
                server_home = self.work / family / found / 'server-user' if found.startswith('default-server') else self.work
                if found.startswith('default-server'):
                    homes['server'] = server_home / ('.claude' if family == 'claude' else '.codex')
                if found in ('server-spelling', 'restored-spelling', 'scope-spelling'):
                    actual_server = homes['server'].with_name('actual-server')
                    actual_server.mkdir(parents=True)
                    homes['server'].symlink_to(actual_server, target_is_directory=True)
                if found in ('restored-spelling', 'scope-spelling'):
                    record = {'CODEX_HOME': str(homes['server']), 'CODEX_SQLITE_HOME': None}
                    if found == 'restored-spelling':
                        properties['native_storage_environment'] = record
                    else:
                        properties['native_environment'] = {'CODEX_HOME': str(homes['server'])}
                        properties['native_service_identity'] = {'provider': 'codex', 'scope': {'CODEX_HOME': str(homes['server'])}}
                    write(directory / 'properties.json', properties)
                request = {'env': {} if found == 'restored-spelling' else {key: str(homes['request'])}, 'unset_env': []}
                request_path = directory / 'launches' / launch / 'request.json'
                if found.startswith('empty-'):
                    facts = {'launch_id': launch, 'state': 'failed', 'error': 'terminal creation failed'}
                    if found == 'empty-may-have-child':
                        facts['pid'] = 1234
                    write(directory / 'runtime.json', facts)
                    request_path.parent.mkdir(parents=True, exist_ok=True)
                    if found == 'empty-accepted':
                        write(directory / 'inbox-state.json', {str(uuid4()): {'submission_state': 'accepted'}})
                else:
                    write(request_path, request)
                for source, home in homes.items():
                    folder = home / 'projects' / project_key(str(self.work)) if family == 'claude' else home / 'sessions'
                    folder.mkdir(parents=True)
                    if found in (source, 'multiple') or (found == 'unreadable' and source == 'caller') or (found in ('same-home-alias', 'default-server', 'default-server-bridge', 'default-server-auto', 'server-spelling', 'restored-spelling', 'scope-spelling') and source == 'server'):
                        identifier = properties['native_session_id']
                        if family == 'claude':
                            (folder / (identifier + '.jsonl')).write_text(json.dumps({'type': 'user', 'sessionId': identifier}) + '\n')
                        else:
                            (folder / ('rollout-2026-10-19T19-00-00-' + identifier + '.jsonl')).write_text(
                                json.dumps({'type': 'session_meta', 'payload': {'id': identifier}}) + '\n')
                if found == 'same-home-alias':
                    import shutil
                    shutil.rmtree(homes['caller'])
                    homes['caller'].symlink_to(homes['server'], target_is_directory=True)
                backend = Mock()
                server_environment = {'HOME': str(server_home)}
                if not found.startswith('default-server'):
                    server_environment[key] = str(homes['server'])
                backend.environment.side_effect = server_environment.get
                backend.create.return_value = {'socket': str(self.work / 'new.sock'), 'session': '$2', 'pane': '%2'}
                paths = tuple(path for path in (directory / 'properties.json', directory / 'runtime.json', request_path) if path.exists())
                before = {path: path.read_bytes() for path in paths}
                original_scandir = os.scandir
                def scandir(path):
                    if found == 'unreadable' and Path(path) == homes['server'] / ('projects' if family == 'claude' else 'sessions'):
                        raise PermissionError('fixture denied candidate storage')
                    return original_scandir(path)
                # These launches are stopped fixtures. A recorded PID is evidence
                # of a possible past turn, not a process to inspect on the test host.
                with patch.object(lifecycle, 'Tmux', return_value=backend), \
                        patch.object(lifecycle, 'observe', return_value={'live': 'dormant'}), \
                        patch('multiverse.runtime.attachments._observation', return_value=(True, None)), \
                        patch.object(os, 'scandir', scandir), \
                        patch.object(lifecycle.native, 'start_shared_service', return_value=NativeEndpoint('unix-websocket', str(self.work / 'fake-service.sock'))), \
                        patch.object(lifecycle.native.NativeExecutor, 'rpc', return_value=NativeResult('ok', value={'exclude': [], 'writable_roots': []})), \
                        patch.dict(os.environ, {key: str(homes['caller'])}):
                    fresh = found == 'empty-before-child' and family == 'claude'
                    if found in ('none', 'multiple', 'unreadable') or found.startswith('empty-') and not fresh:
                        with self.assertRaisesRegex(ValueError, 'conversation.*storage'):
                            lifecycle.start(self.root, resume=directory.name)
                        self.assertEqual({path: path.read_bytes() for path in paths}, before)
                        backend.create.assert_not_called()
                    else:
                        chosen = homes['caller' if fresh else 'server' if found in ('same-home-alias', 'default-server', 'default-server-bridge', 'default-server-auto', 'server-spelling', 'restored-spelling', 'scope-spelling') else found]
                        result = lifecycle.start(self.root, resume=directory.name)
                        self.assertEqual(result['native_session_home'], str(chosen.resolve()))
                        if found in ('default-server-bridge', 'default-server-auto'):
                            self.assertEqual(result['runtime']['native_mode']['mode'], 'bridge')
                        expected = {key: str(chosen.resolve() if family == 'codex' else chosen)}
                        if found.startswith('default-server'):
                            expected['HOME'] = str(server_home)
                        if found.startswith('default-server') and family == 'claude':
                            expected[key] = None
                        if family == 'codex' and found != 'request':
                            expected['CODEX_SQLITE_HOME'] = None
                        self.assertEqual(result['native_storage_environment'], expected)
                        actual = read(directory / 'launches' / result['runtime']['launch_id'] / 'request.json')
                        self.assertEqual(actual['env'].get(key), expected[key])
                        foreign = 'CODEX_HOME' if family == 'claude' else 'CLAUDE_CONFIG_DIR'
                        self.assertNotIn(foreign, actual['unset_env'])
                        self.assertTrue(any(('starting fresh' if fresh else 'Adopted') in notice and str(chosen.resolve()) in notice for notice in result['notices']))
                        if fresh:
                            self.assertEqual(sum('fresh' in notice for notice in result['notices']), 1)
                        again = lifecycle.start(self.root, resume=directory.name)
                        self.assertEqual(again['native_storage_environment'], result['native_storage_environment'])
                        if family == 'codex' and found == 'default-server':
                            definition['settings']['inbox_bridge'] = True
                            write(manifest, definition)
                            bridged = lifecycle.start(self.root, resume=directory.name)
                            self.assertEqual(bridged['runtime']['native_mode']['mode'], 'bridge')
                            self.assertEqual(bridged['native_storage_environment'], result['native_storage_environment'])
                        if family == 'codex' and found in ('server-spelling', 'restored-spelling', 'scope-spelling'):
                            canonical = chosen.resolve()
                            moved = canonical.with_name(canonical.name + '-original')
                            replacement = canonical.with_name(canonical.name + '-different')
                            replacement.mkdir()
                            paths = [directory / 'properties.json', directory / 'runtime.json',
                                     *directory.glob('launches/*/request.json')]
                            before = {path: path.read_bytes() for path in paths}
                            calls = backend.create.call_count
                            canonical.rename(moved)
                            canonical.symlink_to(replacement, target_is_directory=True)
                            try:
                                with self.assertRaisesRegex(ValueError, 'Native session storage differs'):
                                    lifecycle.start(self.root, resume=directory.name)
                                self.assertEqual({path: path.read_bytes() for path in paths}, before)
                                self.assertEqual(backend.create.call_count, calls)
                            finally:
                                canonical.unlink()
                                moved.rename(canonical)
                        if family == 'codex' and found == 'server':
                            # The real lifecycle prepares a shared adapter twice. The second
                            # call must keep the partial storage mask from the first.
                            adapter = harnesses.load(self.root, 'codex')
                            prepare = adapter.resume
                            masks = []
                            def resume(session, context):
                                masks.append(context.storage_keys)
                                service = SharedServiceSpec(('fake-service',), 'socket', ('cli', 'daemon'),
                                    environment={'CODEX_HOME': str(chosen)},
                                    identity=ServiceIdentity('codex', {'CODEX_HOME': str(chosen)}))
                                return replace(prepare(session, context), shared_service=service)
                            with patch.object(harnesses, 'load', return_value=adapter), \
                                    patch.object(adapter, 'resume', side_effect=resume), \
                                    patch.object(lifecycle.native, 'start_shared_service',
                                                 return_value=NativeEndpoint('unix-websocket', str(self.work / 'fake-service.sock'))):
                                lifecycle.start(self.root, resume=directory.name)
                            self.assertEqual([None if mask is None else set(mask) for mask in masks], [set(expected) - {'HOME'}, set(expected) - {'HOME'}])

        # Each is independent evidence that a missing transcript may have held
        # real work. None may become a silent fresh start when another guard changes.
        from multiverse.runtime.storage_adoption import _never_started
        for evidence in ('rebound', 'phase', 'sessions.jsonl', 'session.jsonl', 'native-delivery.json',
                         'claude-receipts/receipt.json', 'request.json', 'ready', 'cleanup.json', 'roles'):
            with self.subTest(never_started_evidence=evidence):
                identifier = str(uuid4())
                directory = self.work / 'never-started-guards' / identifier
                launch = directory / 'launches' / str(uuid4())
                launch.mkdir(parents=True)
                properties = {'id': identifier, 'native_session_id': identifier, 'phase': 'starting'}
                facts = {'launch_id': launch.name, 'state': 'failed'}
                write(directory / 'runtime.json', facts)
                adapter = Mock(session_mode='assigned')
                self.assertTrue(_never_started(adapter, properties, facts, directory))
                if evidence == 'rebound':
                    properties['native_session_id'] = str(uuid4())
                elif evidence == 'phase':
                    properties['phase'] = 'working'
                else:
                    path = (launch if evidence in ('request.json', 'ready', 'cleanup.json', 'roles') else directory) / evidence
                    path.parent.mkdir(parents=True, exist_ok=True)
                    path.write_text('{}\n')
                self.assertFalse(_never_started(adapter, properties, facts, directory))

    def test_explicit_fresh_start_requires_absence_and_retires_the_old_binding(self):
        """Fresh recovery must reject unknown or present history, including a home only a retained request's terminal server names once runtime.json is lost, and retire only a proven missing conversation, or history and saved input can be stranded."""
        from multiverse.harnesses.claude import project_key
        from multiverse.universe import sessions
        for family in ('claude', 'codex', 'pi'):
            for evidence in ('absent', 'absent-again', 'absent-again-unbound', 'present', 'scope-present', 'duplicate', 'unreadable', 'live', 'unproven-pending',
                             'history-damaged-runtime', 'history-missing-runtime', 'history-new-preparation',
                             'older-damaged-request', 'older-unreadable-request', 'older-invalid-environment', 'malformed-service',
                             'caller-home-missing-runtime', 'caller-home-damaged-runtime',
                             'server-missing-runtime', 'server-damaged-runtime', 'server-new-preparation'):
                if family != 'codex' and evidence == 'scope-present':
                    continue
                historical = evidence.startswith('history-')
                if historical and family == 'pi':
                    continue
                if evidence == 'malformed-service' and family == 'pi':
                    continue
                if evidence == 'absent-again-unbound' and family != 'codex':
                    continue
                unknown_server = evidence.startswith('caller-home-')
                if unknown_server and family != 'claude':
                    continue
                server_home = evidence.startswith('server-')
                if server_home and family == 'pi':
                    continue
                with self.subTest(family=family, evidence=evidence):
                    key = {'claude': 'CLAUDE_CONFIG_DIR', 'codex': 'CODEX_HOME', 'pi': 'PI_CODING_AGENT_DIR'}[family]
                    executable = self.work / (family + '-fresh-fake')
                    version = '2.1.306 (Claude Code)' if family == 'claude' else 'codex-cli 0.163.0'
                    executable.write_text(f'#!{sys.executable}\nprint({version!r})\n')
                    executable.chmod(0o755)
                    manifest = self.root / 'harnesses' / family / 'harness.json'
                    definition = read(manifest)
                    definition['settings'].update(inbox_bridge=False, executable=str(executable))
                    write(manifest, definition)
                    directory, properties, _, _ = lifecycle._record(self.root, family + '-fresh-' + evidence, '', family, None, None)
                    old = str(uuid4())
                    old_home, caller = (self.work / family / evidence / part for part in ('old', 'caller'))
                    for home in (old_home, caller):
                        (home / 'projects').mkdir(parents=True)
                        (home / 'sessions').mkdir()
                    properties.update(workspace=str(self.work), native_session_id=old, native_session_home=str(old_home),
                                      native_storage_environment={key: str(old_home)})
                    if evidence == 'unproven-pending':
                        properties['native_fresh_pending'] = True
                    if evidence == 'malformed-service':
                        properties['native_service_identity'] = []
                    write(directory / 'properties.json', properties)
                    launch = str(uuid4())
                    write(directory / 'runtime.json', {'launch_id': launch, 'state': 'dormant',
                        'tmux': {'socket': str(self.work / 'fresh-old.sock'), 'session': '$1', 'pane': '%1'}})
                    write(directory / 'launches' / launch / 'request.json', {'env': {key: str(old_home), 'HOME': str(self.work)}, 'unset_env': []})
                    append_message(directory, 'keep this inbox', {})
                    if historical:
                        properties.pop('native_session_home')
                        properties.pop('native_storage_environment')
                        write(directory / 'properties.json', properties)
                        if evidence == 'history-missing-runtime':
                            (directory / 'runtime.json').unlink()
                        else:
                            write(directory / 'runtime.json', {'launch_id': launch if evidence == 'history-damaged-runtime' else str(uuid4()),
                                  'state': 'invalid' if evidence == 'history-damaged-runtime' else 'failed'})
                    old_request = directory / 'launches' / launch / 'request.json'
                    if unknown_server:
                        properties.pop('native_session_home')
                        properties.pop('native_storage_environment')
                        write(directory / 'properties.json', properties)
                        write(old_request, {'env': {}, 'unset_env': []})
                        if evidence == 'caller-home-missing-runtime':
                            (directory / 'runtime.json').unlink()
                        else:
                            write(directory / 'runtime.json', {'launch_id': launch, 'state': 'invalid'})
                        # The unrelated default server's account HOME must not become evidence.
                        account = self.work / evidence / 'unrelated-account'
                        decoy = account / '.claude' / 'projects' / project_key(str(self.work)) / (old + '.jsonl')
                        decoy.parent.mkdir(parents=True)
                        decoy.write_text(json.dumps({'type': 'user', 'sessionId': old}) + '\n')
                    if server_home:
                        # A pre-#23 launch inherited its storage only from the server its request names.
                        properties.pop('native_session_home')
                        properties.pop('native_storage_environment')
                        write(directory / 'properties.json', properties)
                        write(old_request, {'env': {'MV2_TMUX_SOCKET': str(self.work / 'fresh-old.sock')}, 'unset_env': []})
                        if evidence == 'server-missing-runtime':
                            (directory / 'runtime.json').unlink()
                        else:
                            write(directory / 'runtime.json', {'launch_id': launch if evidence == 'server-damaged-runtime' else str(uuid4()),
                                  'state': 'invalid' if evidence == 'server-damaged-runtime' else 'failed'})
                    if evidence.startswith('older-'):
                        # The current request is readable; only an older one is damaged.
                        latest = str(uuid4())
                        write(directory / 'runtime.json', {'launch_id': latest, 'state': 'dormant',
                              'tmux': {'socket': str(self.work / 'fresh-old.sock'), 'session': '$1', 'pane': '%1'}})
                        write(directory / 'launches' / latest / 'request.json', {'env': {key: str(caller), 'HOME': str(self.work)}})
                        if evidence == 'older-damaged-request':
                            old_request.write_text('{')
                        elif evidence == 'older-invalid-environment':
                            write(old_request, {'env': {key: None}})
                    if evidence in ('present', 'scope-present', 'duplicate') or historical or server_home:
                        if family == 'claude':
                            path = old_home / 'projects' / project_key(str(self.work)) / (old + '.jsonl')
                            path.parent.mkdir()
                            path.write_text(json.dumps({'type': 'user', 'sessionId': old}) + '\n')
                        elif family == 'codex':
                            path = old_home / 'sessions' / ('rollout-2026-10-21T12-00-00-' + old + '.jsonl')
                            path.write_text(json.dumps({'type': 'session_meta', 'payload': {'id': old}}) + '\n')
                        else:
                            (directory / 'session.jsonl').write_text(json.dumps({'type': 'session', 'id': old}) + '\n')
                        if evidence == 'scope-present':
                            properties.pop('native_session_home')
                            properties.pop('native_storage_environment')
                            properties['native_service_identity'] = {'provider': 'codex', 'scope': {'CODEX_HOME': str(old_home)}}
                            write(directory / 'properties.json', properties)
                            write(directory / 'launches' / launch / 'request.json', {'env': {}, 'unset_env': []})
                        if evidence == 'duplicate' and family != 'pi':
                            copy = caller / path.relative_to(old_home)
                            copy.parent.mkdir(parents=True, exist_ok=True)
                            copy.write_bytes(path.read_bytes())
                    before = {path: path.read_bytes() for path in directory.rglob('*') if path.is_file()}
                    backend = Mock()
                    backend.snapshot.return_value = ({}, None)
                    backend.environment.side_effect = PermissionError('unreadable server') if evidence == 'unreadable' else {key: str(caller if historical or evidence == 'scope-present' else old_home), 'HOME': str(self.work)}.get
                    if unknown_server:
                        backend.environment.side_effect = {'HOME': str(account)}.get
                    if server_home:
                        backend.environment.side_effect = {key: str(old_home), 'HOME': str(self.work)}.get
                    backend.create.return_value = {'socket': str(self.work / 'fresh-new.sock'), 'session': '$2', 'pane': '%2'}
                    servers = []
                    def terminal(socket=None):
                        servers.append(socket)
                        return backend
                    original_lstat = Path.lstat
                    original_read = Path.read_bytes
                    def read_bytes(path):
                        if evidence == 'older-unreadable-request' and path == old_request:
                            raise PermissionError('unreadable older request')
                        return original_read(path)
                    def lstat(path, *args, **kwargs):
                        if family == 'pi' and evidence == 'unreadable' and path == directory / 'session.jsonl':
                            raise PermissionError('unreadable conversation file')
                        return original_lstat(path, *args, **kwargs)
                    with patch.object(lifecycle, 'Tmux', side_effect=terminal), \
                            patch('multiverse.runtime.missing_runtime.Tmux', return_value=backend), \
                            patch.object(lifecycle, 'observe', return_value={'live': 'alive' if evidence == 'live' else 'dormant'}), \
                            patch.object(Path, 'lstat', lstat), \
                            patch.object(Path, 'read_bytes', read_bytes), \
                            patch.dict(os.environ, {key: str(caller), **({'HOME': str(caller)} if unknown_server else {})}):
                        if unknown_server:
                            os.environ.pop(key)
                        if evidence not in ('absent', 'absent-again', 'absent-again-unbound') and not unknown_server:
                            with self.assertRaisesRegex(ValueError, 'running|storage|conversation|unreadable') as refused:
                                lifecycle.start(self.root, resume=directory.name, start_fresh=evidence != 'unproven-pending')
                            if evidence in ('present', 'duplicate', 'unreadable') and family != 'pi':
                                self.assertNotIn('  Restore', str(refused.exception))
                                self.assertIn('. Restore access', str(refused.exception))
                            self.assertEqual({path: original_read(path) for path in before}, before)
                            self.assertFalse((directory / 'sessions.jsonl').exists())
                            backend.create.assert_not_called()
                            if server_home:
                                # Found through the request's server, with runtime.json naming none.
                                self.assertIn(str(self.work / 'fresh-old.sock'), servers)
                                self.assertIn(f'{str(old_home)!r} (present)', str(refused.exception))
                            if historical or server_home:
                                resumed = lifecycle.start(self.root, resume=directory.name)
                                self.assertEqual(resumed['native_session_id'], old)
                                self.assertEqual(resumed['native_session_home'], str(old_home))
                            continue
                        result = lifecycle.start(self.root, resume=directory.name, start_fresh=True)
                    self.assertEqual(result['id'], properties['id'])
                    self.assertEqual(result['name'], properties['name'])
                    self.assertEqual((directory / 'inbox.jsonl').read_bytes(), before[directory / 'inbox.jsonl'])
                    self.assertNotEqual(result.get('native_session_id'), old)
                    request = read(directory / 'launches' / result['runtime']['launch_id'] / 'request.json')
                    if unknown_server:
                        self.assertNotIn(key, request['env'])
                        self.assertEqual(request['env']['HOME'], str(caller))
                        self.assertEqual(result['native_session_home'], str(caller / '.claude'))
                        backend.environment.assert_not_called()
                    else:
                        self.assertEqual(request['env'][key], str(caller))
                    self.assertNotIn(old, ' '.join(request['argv']))
                    self.assertEqual(sum('starting fresh' in notice for notice in result['notices']), 1)
                    self.assertFalse(result['native_fresh_pending'])
                    with self.subTest('a flipped pending bit cannot start another conversation'):
                        properties_path = directory / 'properties.json'
                        published = read(properties_path)
                        write(properties_path, {**published, 'native_fresh_pending': True})
                        with patch.object(lifecycle, 'Tmux', return_value=backend), \
                                patch.object(lifecycle, 'observe', return_value={'live': 'dormant'}), \
                                self.assertRaisesRegex(ValueError, 'conversation.*published|conversation.*started'):
                            lifecycle.start(self.root, resume=directory.name)
                        write(properties_path, published)
                    rows = sessions.journal(directory)
                    self.assertEqual((rows[-1]['from'], rows[-1]['reason']), (old, 'missing-conversation'))
                    if family != 'claude' and evidence != 'absent-again-unbound':
                        with self.assertRaisesRegex(ValueError, 'retired'):
                            agents.bind_session(self.root, directory.name, old, result['runtime']['launch_id'])
                        result = agents.bind_session(self.root, directory.name, str(uuid4()), result['runtime']['launch_id'])
                    self.assertEqual(sessions.effective(directory, result)[0]['native_session_id'], result['native_session_id'])
                    with sessions.namespace(self.root), self.assertRaises(sessions.SessionConflict):
                        sessions.claim(self.root, self.work / 'another-agent', properties, old, str(old_home))
                    self.assertIsNone(lifecycle.native._session_changes(directory, result, old))
                    if evidence in ('absent-again', 'absent-again-unbound'):
                        # Retirement records a legitimate null service for plain agents.
                        self.assertIsNone(read(directory / 'properties.json')['native_service_identity'])
                        replacement = result['native_session_id']
                        if evidence == 'absent-again-unbound':
                            self.assertIsNone(replacement)
                            transcript = None
                            journal_path = directory / 'sessions.jsonl'
                            lineage = journal_path.read_bytes()
                            journal_path.unlink()
                            try:
                                with patch.object(lifecycle, 'Tmux', return_value=backend), \
                                        patch.object(lifecycle, 'observe', return_value={'live': 'dormant'}), \
                                        self.assertRaisesRegex(ValueError, 'storage|journal|conversation'):
                                    lifecycle.start(self.root, resume=directory.name, start_fresh=True)
                            finally:
                                journal_path.write_bytes(lineage)
                        elif family == 'claude':
                            transcript = caller / 'projects' / project_key(str(self.work)) / (replacement + '.jsonl')
                            row = {'type': 'user', 'sessionId': replacement}
                        elif family == 'codex':
                            transcript = caller / 'sessions' / ('rollout-2026-10-21T12-00-00-' + replacement + '.jsonl')
                            row = {'type': 'session_meta', 'payload': {'id': replacement}}
                        else:
                            transcript = directory / 'session.jsonl'
                            row = {'type': 'session', 'id': replacement}
                        if transcript is not None:
                            transcript.parent.mkdir(parents=True, exist_ok=True)
                            transcript.write_text(json.dumps(row) + '\n')
                            transcript.unlink()
                        with patch.object(lifecycle, 'Tmux', return_value=backend), \
                                patch.object(lifecycle, 'observe', return_value={'live': 'dormant'}), \
                                patch.dict(os.environ, {key: str(caller)}):
                            repeated = lifecycle.start(self.root, resume=directory.name, start_fresh=True)
                        self.assertEqual(repeated['name'], properties['name'])
                        if replacement is not None:
                            self.assertNotEqual(repeated.get('native_session_id'), replacement)
                        else:
                            self.assertIsNone(repeated.get('native_session_id'))
                        self.assertEqual((directory / 'inbox.jsonl').read_bytes(), before[directory / 'inbox.jsonl'])
                        self.assertEqual((sessions.journal(directory)[-1]['from'], sessions.journal(directory)[-1]['reason']),
                                         (replacement, 'missing-conversation'))

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

    def test_a_pi_harness_setting_its_agent_directory_is_what_spawn_records(self):
        """A Pi harness's settings.env can set PI_CODING_AGENT_DIR (Pi never reads it to find sessions): spawn records
        and runs with that value over the caller's and the tmux server's, and resume keeps it after the setting
        changes, saying it ignored the new setting or a different shell value, while --start-fresh takes the current
        setting; a break goes unnoticed because Pi still starts, in another directory
        with other settings and auth (Shao #26)."""
        executable = self.work / 'pi-stand-in'
        executable.write_text(f'#!{sys.executable}\n')
        executable.chmod(0o755)
        manifest = self.root / 'harnesses/pi/harness.json'
        definition = read(manifest)
        definition['settings'].update(inbox_bridge=False, executable=str(executable),
                                      env={'PI_CODING_AGENT_DIR': str(self.work / 'pi-from-settings')})
        write(manifest, definition)
        backend = Mock()
        backend.create.return_value = {'socket': str(self.work / 'absent.sock'), 'session': '$1', 'pane': '%1'}
        with patch.object(lifecycle, 'Tmux', return_value=backend), \
                patch.object(lifecycle, 'observe', return_value={'live': 'dormant'}), \
                patch.dict(os.environ, {'PI_CODING_AGENT_DIR': str(self.work / 'pi-from-caller')}):
            result = lifecycle.start(self.root, name='pi-settings', harness='pi')
            directory = self.root / 'state/agents' / result['id']
            (directory / 'session.jsonl').write_text(json.dumps({'type': 'session', 'id': str(uuid4())}) + '\n')
            for mode, expected in (('spawn', str(self.work / 'pi-from-settings')),
                                   ('resume', str(self.work / 'pi-from-settings')),
                                   ('shell', str(self.work / 'pi-from-settings')),
                                   ('fresh', str(self.work / 'pi-fresh-setting'))):
                if mode == 'resume':
                    definition['settings']['env'] = {'PI_CODING_AGENT_DIR': str(self.work / 'pi-setting-changed')}
                    write(manifest, definition)
                    result = lifecycle.start(self.root, resume=result['id'])
                    self.assertIn(f'ignored its harness setting PI_CODING_AGENT_DIR="{self.work / "pi-setting-changed"}"',
                                  ' '.join(result.get('notices', [])))
                    self.assertIn(f'ignored PI_CODING_AGENT_DIR="{self.work / "pi-from-caller"}"', ' '.join(result.get('notices', [])))
                elif mode == 'shell':
                    # The harness setting is gone and the shell names another directory: still kept, and said.
                    definition['settings'].pop('env')
                    write(manifest, definition)
                    os.environ['PI_CODING_AGENT_DIR'] = str(self.work / 'pi-shell-changed')
                    result = lifecycle.start(self.root, resume=result['id'])
                    self.assertIn(f'Kept its recorded PI_CODING_AGENT_DIR "{expected}"; '
                                  f'ignored PI_CODING_AGENT_DIR="{self.work / "pi-shell-changed"}".', result.get('notices', []))
                elif mode == 'fresh':
                    # An explicit fresh start is a new conversation: it takes the setting as a spawn does.
                    (directory / 'session.jsonl').unlink()
                    definition['settings']['env'] = {'PI_CODING_AGENT_DIR': expected}
                    write(manifest, definition)
                    result = lifecycle.start(self.root, resume=result['id'], start_fresh=True)
                    self.assertNotIn('ignored', ' '.join(result.get('notices', [])))
                request = read(directory / 'launches' / result['runtime']['launch_id'] / 'request.json')
                with patch.dict(os.environ, {'PI_CODING_AGENT_DIR': str(self.work / 'pi-from-server')}):
                    environment = runner.launch_environment(request)
                self.assertEqual(environment.get('PI_CODING_AGENT_DIR'), expected, mode)
                self.assertEqual(read(directory / 'properties.json')['native_storage_environment']['PI_CODING_AGENT_DIR'],
                                 expected, mode)

    def test_child_env_has_only_the_launch_identity_and_no_parent_markers(self):
        """For every bundled adapter, as shipped, with a replaced unset_env or setting a marker itself, the native
        child the real runner starts gets only the launch's identity and socket, and none of a parent's markers
        (CODEX_THREAD_ID, CLAUDECODE, CLAUDE_CODE_CHILD_SESSION, the IDE port, the messaging socket and token) or
        NO_COLOR unless its harness sets one, while the user's own CLAUDE_CODE_TMPDIR passes through; a break goes unnoticed because the child still runs, but as its parent,
        or as a Claude that inherited CLAUDE_CODE_CHILD_SESSION and saves no transcript, so resume silently starts
        fresh and usage reads nothing, or one holding the parent's inbox secret (a6a7628, a81fed1, 74887d9,
        CLAUDE-5)."""
        manifests = sorted(templates.TEMPLATES.glob("*/harnesses/*/harness.json"))
        self.assertEqual({manifest.parent.name for manifest in manifests}, {'claude', 'codex', 'pi'})
        markers = ("CODEX_THREAD_ID", "CLAUDECODE", "CLAUDE_CODE_CHILD_SESSION", "CLAUDE_CODE_SSE_PORT",
                   "CLAUDE_CODE_MESSAGING_SOCKET", "CLAUDE_CODE_MESSAGING_TOKEN",
                   # A parent Claude session's identity, which a Claude reads at startup, and its tool shells' own
                   # values: a tmux server first started from a Claude Code session passes them to every pane.
                   "CLAUDE_CODE_SESSION_ID", "CLAUDE_CODE_SESSION_ATTENDED", "CLAUDE_CODE_BRIDGE_SESSION_ID",
                   "CLAUDE_CODE_ENTRYPOINT", "CLAUDE_CODE_HOST_WORKTREE", "CLAUDE_CODE_HOST_WORKTREE_FENCE",
                   "CLAUDE_CODE_EVAL_INTERVIEW_SESSION", "CLAUDE_PID", "CLAUDE_EFFORT", "CLAUDE_CODE_EXECPATH",
                   "CLAUDE_CODE_INVOKED_SKILLS")
        # CLAUDE_CODE_TMPDIR is the user's own choice, which Claude's errors tell them to make: it passes through.
        parent = {"MV2_AGENT_ID": "parent", "MV2_TMUX_SOCKET": "/stale", "MV2_STALE": "parent",
                  **{name: "parent" for name in markers}, "CLAUDE_CODE_CHILD_SESSION": "1", "NO_COLOR": "1",
                  "TERM": "tmux-256color", "CLAUDE_CODE_TMPDIR": "/users-own/tmp"}
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
                    self.assertEqual(env.get("CLAUDE_CODE_TMPDIR"), "/users-own/tmp")
                    self.assertTrue(env["PATH"].startswith("/child/bin" + os.pathsep))

        # Claude Code (2.1.301+) saves no transcript when it inherits CLAUDE_CODE_CHILD_SESSION=1, the marker it
        # sets for its own tool subprocesses; a tmux server first started from one passes it to every pane. A
        # Claude agent spawned from inside Claude also inherits the parent's IDE port and its inbox path and secret.
        names = ('CLAUDE_CODE_CHILD_SESSION', 'CLAUDE_CODE_SSE_PORT', 'CLAUDE_CODE_MESSAGING_SOCKET',
                 'CLAUDE_CODE_MESSAGING_TOKEN')
        inherited = {'CLAUDE_CODE_CHILD_SESSION': '1', **{name: 'parent' for name in names[1:]}, 'PATH': '/usr/bin',
                     'CLAUDE_CODE_ENABLE_FUNCTION_HOOKS': '1'}
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
                    # Function hooks load the mod; a plain launch must not inherit them from a Claude-started server.
                    self.assertEqual(env.get('CLAUDE_CODE_ENABLE_FUNCTION_HOOKS'),
                                     None if settings.get('inbox_bridge') is False else '1')

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
