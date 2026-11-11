"""Private Codex configuration, replay and caller-side protocol contracts."""
import json
import io
import os
from pathlib import Path
import subprocess
import sys
import unittest
from unittest.mock import patch

from isolation import isolate, track
from multiverse.harnesses.codex.plugins import CHECKED_VERSION, version_text
from multiverse.harnesses.codex import run, run_history
from multiverse.harnesses.types import RunDirectory, RunFailure, RunImage, RunMessage, RunOptions, RunRequest, RunRow, RunText


def row(kind, payload, **extra):
    return RunRow(format='codex-jsonl', data={'type': kind, 'payload': payload, **extra})


def replay(messages, model='gpt-6-sol'):
    return run_history.session(messages, model=model, session_id='11111111-1111-4111-8111-111111111111',
                               cwd='/private/work', at='2026-11-01T00:00:00.000Z', system='Requested instructions')


class CodexRunTests(unittest.TestCase):
    def setUp(self):
        isolate(self)

    def test_native_request_refusals_do_not_become_retryable_failures_or_echo_context(self):
        """Native setup rejects malformed replay with JSON-RPC codes, and its diagnostic may contain copied text."""
        for code, expected in ((-32600, 'invalid_request'), (-32601, 'unavailable'),
                               (-32602, 'invalid_request'), (-32001, 'rate_limit')):
            error = run.failure({'code': code, 'message': 'PRIVATE_REQUEST_FRAGMENT'})
            self.assertEqual(error.kind, expected)
            self.assertNotIn('PRIVATE_REQUEST_FRAGMENT', error.reason)

    def test_catalogue_removes_model_selected_tools_without_changing_model_identity(self):
        """Native model metadata can enable tools despite feature switches, so every shipped model must be trimmed."""
        self.assertEqual(run.catalogue()['codex'], version_text(CHECKED_VERSION))
        self.assertTrue(run.catalogue()['models'])
        for model in run.catalogue()['models']:
            with self.subTest(model=model['slug']):
                self.assertEqual(model['experimental_supported_tools'], [])
                self.assertEqual(model['tool_mode'], 'direct')
                self.assertEqual(model['shell_type'], 'disabled')
                self.assertIsNone(model['apply_patch_tool_type'])
                self.assertFalse(model['supports_search_tool'])
                self.assertFalse(model['supports_experimental_context'])
        values = run.configuration(model='gpt-6-sol', catalogue_path='/private/catalogue.json')
        for feature in run.DISABLED:
            self.assertIs(values['features.' + feature], False)
        self.assertEqual(values['web_search'], 'disabled')
        self.assertFalse(values['agents.enabled'])
        self.assertFalse(values['tools.update_plan.enabled'])
        self.assertFalse(values['tools.experimental_request_user_input.enabled'])
        self.assertEqual(values['project_doc_max_bytes'], 0)

    def test_file_login_is_observed_without_opening_credentials_and_unchecked_versions_refuse(self):
        """Only Codex may read auth.json, and upgrading its tool catalogue must disable runs until checked."""
        home = Path.home() / 'native'
        home.mkdir()
        auth = home / 'auth.json'
        auth.write_text('not read by mv2')
        environment = {'CODEX_HOME': str(home), 'HOME': str(Path.home())}
        original = Path.open
        def opened(path, *args, **kwargs):
            self.assertNotEqual(path, auth)
            self.assertNotEqual(path, home / 'auth.lock')
            return original(path, *args, **kwargs)
        with patch.object(Path, 'open', opened):
            self.assertEqual(run.login_home(environment), home)
            with patch.object(run, 'executable', return_value='/fake/codex'), patch.object(run, 'codex_version') as version:
                for observed, supported in (((0, 166, 0), True), ((0, 167, 0), True), ((0, 166, 1), False), ((0, 167, 1), True), ((0, 167, 2), False), (None, False)):
                    version.return_value = observed
                    self.assertEqual(run.options({'spawn': ['codex']}, environment).supported, supported)
                    if supported:
                        self.assertIsNone(run.options({'spawn': ['codex']}, environment).transport_retries)
            for store in ('keyring', 'auto', 'ephemeral'):
                (home / 'config.toml').write_text(f'cli_auth_credentials_store = "{store}"\n')
                with self.assertRaises(RunFailure) as caught:
                    run.login_home(environment)
                self.assertEqual(caught.exception.kind, 'unavailable')
            for restriction in ('forced_login_method = "api"', 'forced_login_method = "chatgpt"',
                                'forced_chatgpt_workspace_id = "workspace-A"'):
                (home / 'config.toml').write_text(restriction + '\n')
                with self.assertRaises(RunFailure) as caught:
                    run.login_home(environment)
                self.assertEqual(caught.exception.kind, 'unavailable')
        with self.assertRaises(RunFailure) as caught:
            run.configuration(model='gpt-6-sol', catalogue_path='/private/catalogue.json', retries=0)
        self.assertEqual(caught.exception.kind, 'invalid_request')

    def test_private_environment_has_no_identity_provider_or_debug_inheritance(self):
        """Inherited native debug and auth variables can write outside a disposable home or select another login."""
        result = run.environment({'PATH': '/bin', 'OPENAI_API_KEY': 'test-only', 'CODEX_ROLLOUT_TRACE_ROOT': '/outside',
                                  'MV2_AGENT_ID': 'original', 'HOME': '/original', 'CODEX_SQLITE_HOME': '/original',
                                  'RUST_LOG': 'trace', 'PYTHONPATH': '/outside'}, Path('/private'))
        self.assertEqual(result['PATH'], '/bin')
        self.assertEqual(result['CODEX_HOME'], '/private/codex')
        self.assertEqual(result['RUST_LOG'], 'off')
        for key in ('OPENAI_API_KEY', 'CODEX_ROLLOUT_TRACE_ROOT', 'MV2_AGENT_ID', 'PYTHONPATH'):
            self.assertNotIn(key, result)

    def test_config_aliases_never_read_credential_bytes(self):
        """Config aliases can bypass path-name checks, so observe actual reads of both credential inodes."""
        home = Path.home() / 'alias-login'
        home.mkdir()
        credentials = [home / name for name in ('auth.json', 'auth.lock')]
        for path in credentials:
            path.write_text('test_only_credential = "sentinel"')
        identities = {(path.stat().st_dev, path.stat().st_ino) for path in credentials}
        config = home / 'config.toml'
        environment = {'CODEX_HOME': str(home)}
        reads = []
        descriptors = []
        original_open, original_read, original_descriptor = io.open, os.read, os.open
        def descriptor(*args, **kwargs):
            fd = original_descriptor(*args, **kwargs)
            descriptors.append(fd)
            return fd
        def observe(fd):
            info = os.fstat(fd)
            reads.append((info.st_dev, info.st_ino))
        def read(fd, size):
            observe(fd)
            return original_read(fd, size)
        class Reader:
            def __init__(self, stream):
                self.stream = stream
            def __enter__(self):
                return self
            def __exit__(self, *args):
                return self.stream.__exit__(*args)
            def read(self, *args):
                observe(self.stream.fileno())
                return self.stream.read(*args)
        def opened(*args, **kwargs):
            return Reader(original_open(*args, **kwargs))
        for credential in credentials:
            for alias in ('symlink', 'hardlink'):
                with self.subTest(credential=credential.name, alias=alias):
                    config.unlink(missing_ok=True)
                    config.symlink_to(credential) if alias == 'symlink' else os.link(credential, config)
                    reads.clear()
                    descriptors.clear()
                    kind = None
                    with patch.object(io, 'open', opened), patch.object(os, 'read', read), \
                            patch.object(os, 'open', descriptor):
                        try:
                            run.login_home(environment)
                        except RunFailure as error:
                            kind = error.kind
                    self.assertFalse(identities.intersection(reads), 'Preflight read a credential inode')
                    self.assertEqual(kind, 'unavailable')
                    for fd in descriptors:
                        with self.assertRaises(OSError):
                            os.fstat(fd)
        config.unlink()
        config.write_text('cli_auth_credentials_store = "file"')
        reads.clear()
        with patch.object(io, 'open', opened), patch.object(os, 'read', read):
            self.assertEqual(run.login_home(environment), home)
        self.assertTrue(reads, 'The observation must see ordinary config reads')
        self.assertFalse(identities.intersection(reads))

    def test_special_or_unverifiable_config_never_blocks_preflight(self):
        """A FIFO blocks before supervisor cancellation, so malformed login homes must refuse in an owned child."""
        source = str(Path(run.__file__).resolve().parents[3])
        code = '''import sys
sys.path.insert(0, sys.argv[1])
from multiverse.harnesses.codex.run import login_home
from multiverse.harnesses.types import RunFailure
try:
    login_home({'CODEX_HOME': sys.argv[2]})
except RunFailure as error:
    print(error.kind)
else:
    print('supported')
'''
        for kind in ('fifo', 'directory', 'dangling-config', 'dangling-lock', 'directory-lock', 'oversize'):
            with self.subTest(kind=kind):
                home = Path.home() / kind
                home.mkdir()
                (home / 'auth.json').write_text('test-only')
                config = home / 'config.toml'
                if kind == 'fifo':
                    os.mkfifo(config)
                elif kind == 'directory':
                    config.mkdir()
                elif kind == 'dangling-config':
                    config.symlink_to(home / 'missing')
                elif kind == 'dangling-lock':
                    (home / 'auth.lock').symlink_to(home / 'missing')
                elif kind == 'directory-lock':
                    (home / 'auth.lock').mkdir()
                else:
                    config.write_bytes(b' ' * ((1 << 20) + 1))
                child = subprocess.Popen([sys.executable, '-I', '-c', code, source, str(home)],
                                         stdout=subprocess.PIPE, stderr=subprocess.PIPE, start_new_session=True)
                track(pid=child.pid)
                try:
                    try:
                        output, errors = child.communicate(timeout=2)
                    except subprocess.TimeoutExpired:
                        self.fail('Preflight blocked on config.toml for 2 seconds')
                    self.assertEqual(child.returncode, 0, errors.decode())
                    self.assertEqual(output.strip(), b'unavailable')
                finally:
                    if child.poll() is None:
                        child.terminate()
                        child.communicate(timeout=5)

    def test_replay_keeps_tool_history_but_removes_saved_declarations_and_runtime_configuration(self):
        """A copied tool call is conversation, but old additional_tools and dynamic metadata could restore capabilities."""
        call = {'type': 'function_call', 'call_id': 'old-call', 'name': 'exec_command', 'arguments': '{"cmd":"pwd"}'}
        output = {'type': 'function_call_output', 'call_id': 'old-call', 'output': '/old/work'}
        original = [row('session_meta', {'dynamic_tools': [{'name': 'leaked'}], 'history_base': {'path': '/source'}}),
                    row('response_item', {'type': 'additional_tools', 'role': 'developer', 'tools': [{'name': 'leaked'}]}),
                    row('response_item', call), row('response_item', output),
                    row('dynamic_tools_updated', {'tools': [{'name': 'leaked'}]}),
                    row('event_msg', {'type': 'thread_rolled_back', 'num_turns': 1}),
                    row('response_item', {'type': 'tool_search_output', 'call_id': 'search', 'status': 'completed',
                                         'execution': 'client', 'tools': [{'name': 'leaked'}]})]
        copied = replay(original)
        rows = [json.loads(line) for line in copied.content.splitlines()]
        self.assertEqual(rows[0]['payload']['dynamic_tools'], [])
        self.assertNotIn('history_base', rows[0]['payload'])
        self.assertEqual([r['payload'] for r in rows[1:3]], [call, output])
        self.assertEqual(rows[3]['payload']['tools'], [])
        self.assertEqual(len(copied.lost), 2)
        self.assertEqual(original[1].data['payload']['tools'], [{'name': 'leaked'}])

    def test_checkpoint_resets_history_and_preserves_same_model_encryption(self):
        """Compaction replaces its prefix; copied encryption must remain exact and stay bound to the writing login."""
        encrypted = {'type': 'reasoning', 'summary': [], 'encrypted_content': 'opaque-byte-for-byte'}
        messages = [RunMessage(role='user', blocks=(RunText(text='discarded prefix'),)),
                    row('session_meta', {'creator_account_id': 'workspace-A'}),
                    row('turn_context', {'model': 'gpt-6-sol'}),
                    row('compacted', {'message': '', 'replacement_history': [encrypted,
                        {'type': 'message', 'role': 'assistant', 'content': [{'type': 'output_text', 'text': 'kept'}]}]})]
        copied = replay(messages)
        rows = [json.loads(line) for line in copied.content.splitlines()]
        self.assertEqual(len(rows), 2)
        self.assertEqual(rows[1]['payload']['replacement_history'][0], encrypted)
        self.assertNotIn(b'discarded prefix', copied.content)
        copied.check_login({'workspaceRouting': {'chatgptAccountId': 'workspace-A'}})
        for observed in ({'workspaceRouting': {'chatgptAccountId': 'workspace-B'}}, {},
                         {'account': {'workspaceRouting': {'chatgptAccountId': 'workspace-A'}}}):
            with self.assertRaises(RunFailure):
                copied.check_login(observed)
        changed = replay(messages, model='gpt-6-luna')
        self.assertNotIn(b'opaque-byte-for-byte', changed.content)
        self.assertTrue(changed.lost)
        with self.assertRaises(RunFailure) as caught:
            replay([row('compacted', {'replacement_history': [{'type': 'compaction', 'encrypted_content': 'opaque'}]})])
        self.assertEqual(caught.exception.kind, 'unavailable')

    def test_built_images_and_text_keep_order_and_non_model_echoes_are_omitted(self):
        """A model-visible image or prompt cannot disappear or duplicate when messages and native rows are mixed."""
        copied = replay([RunMessage(role='user', blocks=(RunText(text='look'), RunImage(media_type='image/png', data=b'png'))),
                         row('event_msg', {'type': 'user_message', 'message': 'look'}),
                         RunMessage(role='assistant', blocks=(RunText(text='seen'),))])
        rows = [json.loads(line)['payload'] for line in copied.content.splitlines()][1:]
        self.assertEqual(len(rows), 2)
        self.assertEqual(rows[0]['content'][1]['image_url'], 'data:image/png;base64,cG5n')
        self.assertEqual(rows[1]['content'], [{'type': 'output_text', 'text': 'seen'}])

    def test_pending_settings_do_not_relabel_encrypted_history(self):
        """A settings update affects the next turn, not the writing model of reasoning still arriving in this turn."""
        encrypted = {'type': 'reasoning', 'summary': [], 'encrypted_content': 'old-model-reasoning'}
        prefix = [row('session_meta', {'creator_account_id': 'account-A'}),
                  row('turn_context', {'model': 'gpt-6-sol'}),
                  row('event_msg', {'type': 'thread_settings_applied', 'thread_settings': {'model': 'gpt-6-luna'}})]
        for model, next_turn, retained in (('gpt-6-sol', False, True), ('gpt-6-luna', False, False),
                                            ('gpt-6-luna', True, True), ('gpt-6-sol', True, False)):
            with self.subTest(model=model, next_turn=next_turn):
                messages = prefix + ([row('turn_context', {'model': 'gpt-6-luna'})] if next_turn else [])
                copied = replay(messages + [row('response_item', encrypted)], model=model)
                self.assertEqual(b'old-model-reasoning' in copied.content, retained)
                self.assertEqual(bool(copied.lost), not retained)

    def test_native_account_selects_a_stripped_copy_only_for_optional_reasoning(self):
        """An unknown current login drops optional reasoning, while opaque compaction must never be silently discarded."""
        encrypted = {'type': 'reasoning', 'summary': [], 'encrypted_content': 'source-reasoning'}
        messages = (row('session_meta', {'creator_account_id': 'account-A'}),
                    row('turn_context', {'model': 'gpt-6-sol'}), row('response_item', encrypted))
        directory = RunDirectory(path=Path('/private/test-run'), id='11111111-1111-4111-8111-111111111111')
        request = RunRequest(messages=messages, prompt='question', system='', model='gpt-6-sol')
        with patch.object(run, 'options', return_value=RunOptions(supported=True)), \
                patch.object(run, 'executable', return_value='/fake/codex'), \
                patch.object(run, 'login_home', return_value=Path('/source')), \
                patch.object(run, 'answer', side_effect=lambda *args, **kw: kw['select_history']) as answer:
            plan = run.plan({}, request, source={}, directory=directory)
            select = plan.dialogue(None)
            full_path, lost = select({'workspaceRouting': {'chatgptAccountId': 'account-A'}})
            self.assertFalse(lost)
            for account in ({}, {'workspaceRouting': {'chatgptAccountId': 'account-B'}}):
                stripped_path, lost = select(account)
                self.assertNotEqual(full_path, stripped_path)
                self.assertTrue(lost)
                # No remaining history can use a fresh thread without a second empty rollout.
                self.assertIsNone(stripped_path)
            self.assertEqual(len([f for f in plan.files if f.path.endswith('.jsonl')]), 1)
            # A retained plain message exercises actual resume of the stripped
            # copy, which needs its own stable ID and canonical native filename.
            plain = row('response_item', {'type': 'message', 'role': 'assistant',
                                        'content': [{'type': 'output_text', 'text': 'retained'}]})
            request = RunRequest(messages=(*messages, plain), prompt='question', system='', model='gpt-6-sol')
            plan = run.plan({}, request, source={}, directory=directory)
            select = plan.dialogue(None)
            full_path, _ = select({'workspaceRouting': {'chatgptAccountId': 'account-A'}})
            stripped_path, lost = select({})
            self.assertIsNotNone(stripped_path)
            self.assertNotEqual(full_path, stripped_path)
            copies = [f for f in plan.files if f.path.endswith('.jsonl')]
            identities = []
            for file in copies:
                header = json.loads(file.content.splitlines()[0])['payload']
                identities.append(header['id'])
                self.assertEqual(Path(file.path).parent.as_posix(), 'private/codex/sessions')
                self.assertRegex(Path(file.path).name, r'^rollout-\d{4}-\d{2}-\d{2}T\d{2}-\d{2}-\d{2}-'
                                 + header['id'] + r'\.jsonl$')
            self.assertEqual(len(set(identities)), 2)
            request = RunRequest(messages=(*messages, row('response_item', {
                'type': 'compaction', 'encrypted_content': 'source-compaction'})), prompt='question', system='', model='gpt-6-sol')
            plan = run.plan({}, request, source={}, directory=directory)
            select = plan.dialogue(None)
            self.assertEqual(len([f for f in plan.files if f.path.endswith('.jsonl')]), 2)
            for account in ({}, {'workspaceRouting': {'chatgptAccountId': 'account-B'}}):
                with self.assertRaises(RunFailure) as caught:
                    select(account)
                self.assertEqual(caught.exception.kind, 'unavailable')


class Channel:
    def __init__(self):
        self.sent, self.pending = [], []

    def send(self, value):
        self.sent.append(value)
        if 'id' not in value:
            return
        results = {'initialize': {'userAgent': 'codex/0.166.0'},
                   'account/read': {'account': {'type': 'chatgpt'}},
                   'config/read': {'config': {'mcp_servers': {}}},
                   'thread/start': {'thread': {'id': 'private'}, 'model': 'gpt-6-luna', 'modelProvider': 'openai'},
                   'turn/start': {'turn': {'id': 'new-turn'}}}
        self.pending.append({'id': value['id'], 'result': results[value['method']]})
        if value['method'] == 'turn/start':
            self.pending += [
                {'method': 'thread/tokenUsage/updated', 'params': {'threadId': 'private', 'tokenUsage': {
                    'total': {'inputTokens': 9999}, 'last': {'inputTokens': 21, 'outputTokens': 3}}}},
                {'method': 'item/completed', 'params': {'threadId': 'private', 'turnId': 'new-turn',
                    'item': {'id': 'answer', 'type': 'agentMessage', 'text': 'text only'}}},
                {'method': 'turn/completed', 'params': {'threadId': 'private', 'turn': {'id': 'new-turn', 'status': 'completed'}}}]

    def receive(self):
        return self.pending.pop(0)


class CodexDialogueTests(unittest.TestCase):
    def test_dialogue_uses_private_thread_no_auth_rpc_and_last_turn_usage(self):
        """Resumed totals include prior spending, and the caller must never send a credential or contact the live thread."""
        channel = Channel()
        result = run.answer(channel, RunRequest(messages=(), prompt='question', system='system', model='gpt-6-luna'),
                            cwd='/private', effort='low')
        self.assertEqual(result.text, 'text only')
        self.assertEqual(result.usage, {'input': 21, 'output': 3})
        self.assertIsNone(result.cost_usd)
        sent = {item['method']: item.get('params') for item in channel.sent}
        self.assertEqual(sent['turn/start']['threadId'], 'private')
        self.assertEqual(sent['turn/start']['environments'], [])
        self.assertEqual(sent['thread/start']['dynamicTools'], [])
        self.assertNotIn('account/login/start', sent)


if __name__ == '__main__':
    unittest.main()
