"""An unmanaged native side conversation never replaces the mv2 primary."""
from copy import deepcopy
import unittest
from uuid import uuid4

from multiverse.harnesses.codex.launch import LaunchProtocol
from multiverse.harnesses.codex.status import notification as status_notification
from multiverse.harnesses.types import LocalProtocolError, SessionHandle


# Pinned 0.167 request inventory; production instead permits only known reads.
MUTATIONS = (
    'thread/sample', 'thread/sample/cancel', 'thread/archive',
    'thread/delete', 'thread/increment_elicitation', 'thread/decrement_elicitation',
    'thread/name/set', 'thread/prediction/request', 'thread/goal/set',
    'thread/goal/clear', 'thread/queue/add', 'thread/queue/update',
    'thread/queue/delete', 'thread/queue/reorder', 'thread/queue/start',
    'thread/metadata/update', 'thread/attachment/add', 'thread/attachment/remove',
    'thread/section/move', 'thread/settings/update', 'thread/memoryMode/set',
    'thread/unarchive', 'thread/compact/start', 'thread/guardianReview', 'thread/shellCommand',
    'thread/approveGuardianDeniedAction', 'thread/backgroundTerminals/clean', 'thread/backgroundTerminals/terminate',
    'thread/revert', 'thread/readState/update', 'thread/inject_items',
    'turn/start', 'turn/settings/update', 'turn/steer',
    'turn/interrupt', 'thread/realtime/start', 'thread/realtime/appendAudio',
    'thread/realtime/appendText', 'thread/realtime/appendSpeech', 'thread/realtime/stop',
    'thread/futureMutation', 'thread/futureMutation/read', 'other/mutation',
)

class CodexSideTest(unittest.TestCase):
    def setUp(self):
        self.main, self.side = str(uuid4()), str(uuid4())
        self.settings = {'native_id': self.main, 'workspace': '/fixture/workspace',
                         'bootstrap': 'primary bootstrap', 'roots': [],
                         'resume_permissions': {'sandbox': 'danger-full-access', 'approvalPolicy': 'never'}}
        self.protocol = LaunchProtocol(self.settings)
        self.protocol.bound = True
        self.owner = True
        self.view = self.connection('tui', lambda: self.owner)
        self.initialize(self.view)

    def connection(self, name, owner):
        factory = getattr(self.protocol, 'for_connection', None)
        return factory(name, owner) if factory else self.protocol

    def initialize(self, view, version='codex/0.166.0'):
        view.outgoing({'id': 1, 'method': 'initialize', 'params': {}})
        view.incoming({'id': 1, 'result': {'userAgent': version}})

    def fork(self, identifier=10, **changes):
        return {'id': identifier, 'method': 'thread/fork', 'params': {
            'threadId': self.main, 'ephemeral': True, 'excludeTurns': True, **changes}}

    def forked(self, identifier=10, **changes):
        return {'id': identifier, 'result': {'thread': {
            'id': self.side, 'forkedFromId': self.main, 'ephemeral': True,
            'cwd': '/fixture/workspace', 'path': None, **changes}}}

    def open_side(self):
        request = self.fork()
        self.assertEqual(self.view.outgoing(request), request)
        self.assertIsNone(self.view.incoming(self.forked()))

    def primary_state(self):
        return deepcopy({key: getattr(self.protocol, key) for key in (
            'native_id', 'bound', 'pending', 'pending_method', 'launch_refusal',
            'prompt_request', 'prompt_submitted', 'prompt_error', 'grant_ceiling', 'current_grants',
            'grants_native_id')})

    def navigation_bound(self, policy=None, **settings):
        policy = deepcopy(policy or {'approvalPolicy': 'never', 'approvalsReviewer': 'user',
            'sandbox': {'type': 'dangerFullAccess'}, 'activePermissionProfile': None, 'runtimeWorkspaceRoots': []})
        root = LaunchProtocol({**self.protocol.settings, 'navigation_cli': [0, 166, 0],
                              'navigation_home': '/private/codex', **settings})
        owner = [False]
        view = root.for_connection('owner', lambda: owner[0])
        self.initialize(view)
        view.outgoing({'id': 2, 'method': 'thread/resume', 'params': {'threadId': self.main}})
        view.incoming({'id': 2, 'result': {'thread': {'id': self.main, 'cwd': '/fixture/workspace'}, **policy}})
        owner[0] = True  # Core accepted the exact initial binding before calling after_bind.
        view.after_bind('/fixture/native.sock')
        return root, view

    def test_side_thread_lifecycle_leaves_the_primary_untouched(self):
        """A /btw side thread's whole lifecycle, down to closing one saturated with requests, leaves the primary
        binding, refusal, usage and status untouched and allows the next /btw; a break goes unnoticed because messages,
        status or context quietly come from the side thread, launch identity leaks into its tools, or a full side cannot close."""
        before = self.primary_state()
        error = {'error': {'code': -32603, 'message': 'Native side error'}}
        self.view.outgoing(self.fork())
        self.assertIsNone(self.view.incoming({'id': 10, **error}), 'A native fork error is local and allows a retry')
        with self.subTest('fork tools keep the same identity and exclusions as the primary'):
            self.protocol.settings['agent_environment'] = {'MV2_IDENTITY_MODE': 'codex-agent-v1',
                'MV2_AGENT_ID': 'primary', 'MV2_UNIVERSE': '/fixture/universe', 'MV2_AGENT_DIR': '/fixture/agent'}
            request = self.fork(config={'shell_environment_policy': {
                'set': {'MV2_LAUNCH_ID': 'old', 'MV2_CONTROL_SOCKET': '/old', 'MV2_AGENT_ID': 'wrong',
                        'CLAUDE_CONFIG_DIR': '/daemon/claude-home',
                        'PI_CODING_AGENT_DIR': '/daemon/pi-home', 'USER_SETTING': 'keep'}, 'exclude': ['AWS_*']}})
            original = deepcopy(request)
            forwarded = self.view.outgoing(request)
            policy = forwarded['params']['config']['shell_environment_policy']
            self.assertEqual(policy['set'], {**self.protocol.settings['agent_environment'], 'USER_SETTING': 'keep'})
            self.assertEqual(set(policy['exclude']),
                {'AWS_*', 'MV2_*', 'CODEX_HOME', 'CODEX_SQLITE_HOME', 'CLAUDE_CONFIG_DIR', 'PI_CODING_AGENT_DIR'})
            self.assertEqual(request, original, 'The caller frame is not mutated')
            self.view.incoming(self.forked())
        for identifier, method in enumerate(('thread/inject_items', 'turn/start', 'turn/interrupt'), 20):
            request = {'id': identifier, 'method': method, 'params': {'threadId': self.side}}
            self.assertEqual(self.view.outgoing(request), request)
            self.assertIsNone(self.view.incoming({'id': identifier, 'result': {}}))
        session = SessionHandle('agent', 'codex', '/fixture/workspace', native_id=self.main)
        for status in ('idle', 'active'):
            frame = {'method': 'thread/status/changed', 'params': {'threadId': self.side, 'status': {'type': status}}}
            self.assertIsNone(self.view.incoming(frame))
            self.assertIsNone(status_notification(session, frame))
        def usage(native):
            return {'method': 'thread/tokenUsage/updated', 'params': {
                'threadId': native, 'tokenUsage': {'last': {'totalTokens': 12345}, 'modelContextWindow': 100000}}}
        self.assertIsNone(self.view.incoming(usage(self.side)))
        self.assertIsNone(self.view.usage(usage(self.side)))
        self.view.incoming(usage(self.main))
        observed = self.view.usage(usage(self.main))
        self.assertEqual(observed['native_session_id'], self.main)
        self.assertEqual(observed['context']['tokens'], 12345)
        self.assertEqual(self.primary_state(), before)
        # Returning to the primary is a UI selection, not a new launch: no bootstrap or grants are reapplied.
        request = {'id': 30, 'method': 'thread/resume', 'params': {
            'threadId': self.main, 'approvalPolicy': None, 'sandbox': None, 'excludeTurns': True}}
        self.assertEqual(self.view.outgoing(request), request)
        self.assertIsNone(self.view.incoming({'id': 30, 'result': {'thread': {
            'id': self.main, 'cwd': '/fixture/workspace'}}}))
        self.assertEqual(self.primary_state(), before)
        with self.assertRaises(LocalProtocolError):
            self.view.outgoing({'id': 31, 'method': 'thread/resume', 'params': {'threadId': self.side}})
        request = {'id': 40, 'method': 'thread/unsubscribe', 'params': {'threadId': self.side}}
        self.view.outgoing(request)
        self.assertIsNone(self.view.incoming({'id': 40, **error}))
        with self.assertRaises(LocalProtocolError, msg='An unconfirmed close keeps the side open'):
            self.view.outgoing(self.fork(41))
        # A saturated side still has bounded room for the native interrupt, its retry and the close.
        for identifier in range(100, 164):
            self.view.outgoing({'id': identifier, 'method': 'thread/read', 'params': {'threadId': self.side}})
        for identifier, method in ((200, 'turn/interrupt'), (201, 'turn/interrupt'), (202, 'thread/unsubscribe')):
            self.view.outgoing({'id': identifier, 'method': method, 'params': {'threadId': self.side}})
        with self.assertRaises(LocalProtocolError):
            self.view.outgoing({'id': 203, 'method': 'turn/interrupt', 'params': {'threadId': self.side}})
        self.assertEqual(len(self.view.side_requests), 67)
        self.assertIsNone(self.view.incoming({'id': 202, 'result': {'status': 'unsubscribed'}}))
        self.assertIsNone(self.view.side_id)
        self.assertEqual(self.view.side_requests, {})
        self.assertEqual(self.primary_state(), before)
        reopened = self.view.outgoing(self.fork(43))
        self.assertEqual({key: value for key, value in reopened['params'].items() if key != 'config'},
                         self.fork(43)['params'], 'A closed side must not prevent the next /btw')
        self.assertIsNone(self.view.incoming(self.forked(43)))
        self.protocol.launch_refusal = {'message': 'primary refusal'}
        idle = {'type': 'idle'}
        for frame in ({'method': 'thread/status/changed', 'params': {'threadId': self.side, 'status': idle}},
                      {'method': 'thread/tokenUsage/updated', 'params': {'threadId': self.side}},
                      {'method': 'item/tool/call', 'id': 'tool', 'params': {'threadId': self.side}}):
            self.assertIsNone(self.view.incoming(frame))
            self.assertIsNone(self.view.launch_refusal)
            self.assertIsNone(self.view.usage(frame))

    def test_navigation_commits_only_an_admitted_correlated_primary(self):
        """Primary switches require exact request/response evidence and core commit, or messages and callbacks silently follow the wrong thread."""
        self.protocol.settings.update(navigation_cli=[0, 166, 0], navigation_home='/private/codex')
        full = {'cwd': '/fixture/workspace', 'config': {}, 'model': None, 'modelProvider': None,
                'developerInstructions': None, 'approvalPolicy': None, 'sandbox': None, 'permissions': None}
        for reason in ('new', 'clear', 'resume'):
            with self.subTest(reason=reason):
                root, view = self.navigation_bound()
                target = str(uuid4())
                params = {**full, **({'threadId': target} if reason == 'resume' else
                    {'threadSource': 'user', 'ephemeral': False, 'historyMode': 'paginated',
                     'sessionStartSource': 'clear' if reason == 'clear' else None})}
                request = {'id': 20, 'method': 'thread/resume' if reason == 'resume' else 'thread/start', 'params': params}
                transformed = view.outgoing(request)
                event = view.navigation('outgoing', transformed)
                self.assertEqual((event['action'], event['previous'], event['reason']), ('begin', self.main, reason))
                self.assertEqual(event['target'], target if reason == 'resume' else None)
                self.assertEqual(root.native_id, self.main)
                self.assertEqual(transformed['params']['sandbox'], 'danger-full-access')
                self.assertEqual(transformed['params']['approvalPolicy'], 'never')
                view.navigation_done(event, {'token': 'admitted'})
                for method in (*MUTATIONS, 'thread/fork', 'thread/resume'):
                    with self.subTest(phase='pending', method=method):
                        with self.assertRaises(LocalProtocolError):
                            params = {'threadId': self.main}
                            if method == 'thread/fork':
                                params.update(ephemeral=True, excludeTurns=True)
                            view.outgoing({'id': 21, 'method': method, 'params': params})
                for method in ('thread/read', 'thread/turns/list', 'thread/queue/list', 'thread/goal/get',
                               'thread/items/list', 'thread/items/read', 'thread/unsubscribe'):
                    frame = {'id': 22, 'method': method, 'params': {'threadId': self.main}}
                    self.assertEqual(view.outgoing(frame), frame, 'Pending selection preserves reads and subscription cleanup')
                reply = {'id': 20, 'result': {'thread': {'id': target, 'cwd': '/fixture/workspace',
                    'ephemeral': False, 'parentThreadId': None, 'canAcceptDirectInput': True, 'status': {'type': 'idle'},
                    'path': '/private/codex/sessions/rollout.jsonl'}, 'cwd': '/fixture/workspace',
                    'sandbox': {'type': 'dangerFullAccess'}, 'approvalPolicy': 'never',
                    'approvalsReviewer': 'user', 'activePermissionProfile': None, 'runtimeWorkspaceRoots': []}}
                self.assertIsNone(view.incoming(reply), 'A switch never invokes initial binding')
                event = view.navigation('incoming', reply)
                self.assertEqual((event['action'], event['token'], event['target']), ('commit', 'admitted', target))
                self.assertEqual(root.native_id, self.main, 'No root publication before core commits')
                view.navigation_done(event, {'native_id': target})
                self.assertEqual(root.native_id, target)
                self.assertTrue(root.prompt_submitted, 'The initial spawn prompt cannot replay')
                self.assertIsNone(view.incoming(reply), 'A duplicate success cannot cause a second transition')
                self.assertIsNone(view.navigation('incoming', reply))
                for method in MUTATIONS:
                    with self.subTest(phase='committed', method=method):
                        for other in (self.main, str(uuid4()), None):
                            with self.assertRaises(LocalProtocolError):
                                view.outgoing({'id': 23, 'method': method, 'params': {'threadId': other}})
                for method in ('thread/read', 'thread/turns/list', 'thread/queue/list', 'thread/goal/get',
                               'thread/items/list', 'thread/items/read', 'thread/unsubscribe'):
                    frame = {'id': 23, 'method': method, 'params': {'threadId': self.main}}
                    self.assertEqual(view.outgoing(frame), frame, 'Historical reads and native detach stay available')
                for params in ({'conversationId': self.main}, {'threadId': target, 'conversationId': self.main}):
                    with self.assertRaises(LocalProtocolError):
                        view.outgoing({'id': 23, 'method': 'other/mutation', 'params': params})
                self.assertEqual(view.outgoing({'id': 23, 'method': 'thread/read', 'params': {'threadId': self.main}})['id'], 23)
                self.assertEqual(view.outgoing({'id': 24, 'method': 'turn/start', 'params': {'threadId': target}})['id'], 24)
                self.assertIsNone(view.primary.prompt_request, 'Navigation cannot arm startup prompt tracking again')
                for method in MUTATIONS:
                    frame = {'id': 25, 'method': method, 'params': {'threadId': target}}
                    self.assertEqual(view.outgoing(frame), frame, 'Current-primary mutations keep their native handling')
                view.outgoing({'id': 30, 'method': 'thread/fork', 'params': {
                    'threadId': target, 'ephemeral': True, 'excludeTurns': True}})
                view.incoming({'id': 30, 'result': {'thread': {'id': self.side, 'forkedFromId': target,
                    'ephemeral': True, 'cwd': '/fixture/workspace', 'path': None}}})
                self.assertEqual(view.outgoing({'id': 31, 'method': 'turn/start',
                    'params': {'threadId': self.side}})['id'], 31, 'A validated /btw side retains its own input route')

        # Refusals are local; they cannot poison the launch or publish another primary.
        for label, changes in (
                ('background minimal', {'method': 'thread/resume', 'params': {'threadId': str(uuid4())}}),
                ('incomplete full resume', {'method': 'thread/resume', 'params': {'threadId': str(uuid4())}}),
                ('dynamic namespace', {'id': 'tui-dynamic-2'}),
                ('ephemeral', {'params': {'ephemeral': True}}),
                ('feature source', {'params': {'threadSource': 'recap'}}),
                ('startup source', {'params': {'sessionStartSource': 'startup'}}),
                ('other workspace', {'params': {'cwd': '/another/worktree'}}),
                ('history override', {'params': {'history': []}}),
                ('named permissions', {'params': {'permissions': 'custom'}}),
                ('unknown history', {'params': {'historyMode': 'legacy'}}),
                ('bool request', {'id': True}), ('reused request', {'id': 1})):
            with self.subTest(refusal=label):
                root, view = self.navigation_bound()
                request = {'id': 20, 'method': 'thread/start', 'params': {**full, 'threadSource': 'user',
                    'ephemeral': False, 'historyMode': 'paginated'}}
                request.update({key: value for key, value in changes.items() if key != 'params'})
                if label == 'background minimal':
                    request['params'] = changes['params']
                else:
                    request['params'].update(changes.get('params', {}))
                if label == 'incomplete full resume':
                    del request['params']['config']
                with self.assertRaises(LocalProtocolError):
                    view.outgoing(request)
                self.assertEqual(root.native_id, self.main)
                self.assertIsNone(root.launch_refusal)

        for failure in ('admission', 'native-error', 'commit', 'malformed-success'):
            with self.subTest(failure=failure):
                root, view = self.navigation_bound()
                request = {'id': 20, 'method': 'thread/start', 'params': {**full, 'threadSource': 'user',
                    'ephemeral': False, 'historyMode': 'paginated'}}
                forwarded = view.outgoing(request)
                event = view.navigation('outgoing', forwarded)
                if failure == 'admission':
                    view.navigation_failed(event, 'queue is not empty')
                else:
                    view.navigation_done(event, {'token': 'token'})
                    target = str(uuid4())
                    reply = ({'id': 20, 'error': {'code': -32000, 'message': 'native refusal'}}
                             if failure == 'native-error' else {'id': 20, 'result': {'thread': {
                                'id': target, 'cwd': '/fixture/workspace', 'ephemeral': False,
                                'status': {'type': 'idle'}, 'canAcceptDirectInput': True,
                                'path': '/private/codex/sessions/rollout.jsonl'}, 'cwd': '/fixture/workspace',
                                'sandbox': {'type': 'dangerFullAccess'}, 'approvalPolicy': 'never',
                    'approvalsReviewer': 'user', 'activePermissionProfile': None, 'runtimeWorkspaceRoots': []}})
                    if failure == 'malformed-success':
                        reply['result']['thread']['ephemeral'] = True
                        with self.assertRaises(LocalProtocolError):
                            view.incoming(reply)
                    else:
                        self.assertIsNone(view.incoming(reply))
                        event = view.navigation('incoming', reply)
                        if failure == 'native-error':
                            self.assertEqual(event['action'], 'abort')
                            view.navigation_done(event, None)
                        else:
                            view.navigation_failed(event, 'claim conflict')
                self.assertEqual(root.native_id, self.main)
                self.assertIsNone(root.launch_refusal)
                self.assertEqual(root.grants_native_id, self.main)
                self.assertEqual(root.current_grants, root.grant_ceiling)
                input = {'id': 21, 'method': 'turn/start', 'params': {'threadId': self.main}}
                if failure in ('admission', 'native-error'):
                    self.assertEqual(view.outgoing(input), input)
                else:
                    with self.assertRaises(LocalProtocolError):
                        view.outgoing(input)

        for version in ('0.166.0', '0.167.0'):
            with self.subTest(navigation_version=version):
                root, view = self.navigation_bound(navigation_cli=list(map(int, version.split('.'))))
                self.initialize(view, 'codex/' + version)
                request = {'id': 20, 'method': 'thread/start', 'params': {**full,
                    'threadSource': 'user', 'ephemeral': False, 'historyMode': 'paginated'}}
                try:
                    forwarded = view.outgoing(request)
                except LocalProtocolError as error:
                    self.fail(f'The checked navigation release refused its supported request: {error}')
                self.assertIsNotNone(forwarded)
                self.assertEqual(root.native_id, self.main)

        for owner, cli, daemon, side in ((False, [0, 166, 0], '0.166.0', False),
                (True, [0, 164, 0], '0.166.0', False), (True, [0, 166, 0], '0.164.0', False),
                (True, [0, 166, 0], '0.167.0', False), (True, [0, 167, 0], '0.166.0', False),
                (True, [0, 168, 0], '0.168.0', False),
                (True, [0, 166, 0], '0.166.0', True)):
            with self.subTest(owner=owner, cli=cli, daemon=daemon, side=side):
                root, view = self.navigation_bound(navigation_cli=cli)
                view.owns_primary = lambda: owner
                self.initialize(view, 'codex/' + daemon)
                if side:
                    view.outgoing(self.fork(2))
                    view.incoming(self.forked(2))
                with self.assertRaises(LocalProtocolError):
                    view.outgoing({'id': 20, 'method': 'thread/start', 'params': {**full,
                        'threadSource': 'user', 'ephemeral': False, 'historyMode': 'paginated'}})
                self.assertEqual(root.native_id, self.main)

        for bad in ('thread', 'nested cwd', 'runtime cwd', 'storage', 'parent', 'status', 'input authority', 'grants'):
            with self.subTest(native_success=bad):
                root, view = self.navigation_bound()
                target = str(uuid4())
                request = {'id': 20, 'method': 'thread/resume', 'params': {**full, 'threadId': target}}
                view.outgoing(request)
                event = view.navigation('outgoing', request)
                view.navigation_done(event, {'token': 'token'})
                reply = {'id': 20, 'result': {'thread': {'id': target, 'cwd': '/fixture/workspace',
                    'ephemeral': False, 'parentThreadId': None, 'status': {'type': 'idle'},
                    'canAcceptDirectInput': True, 'path': '/private/codex/sessions/rollout.jsonl'},
                    'cwd': '/fixture/workspace', 'sandbox': {'type': 'dangerFullAccess'}, 'approvalPolicy': 'never',
                    'approvalsReviewer': 'user', 'activePermissionProfile': None, 'runtimeWorkspaceRoots': []}}
                thread = reply['result']['thread']
                if bad == 'thread': thread['id'] = str(uuid4())
                elif bad == 'nested cwd': thread['cwd'] = '/other'
                elif bad == 'runtime cwd': reply['result']['cwd'] = '/other'
                elif bad == 'storage': thread['path'] = '/another/home/sessions/rollout.jsonl'
                elif bad == 'parent': thread['parentThreadId'] = str(uuid4())
                elif bad == 'status': thread['status'] = {'type': 'active'}
                elif bad == 'input authority': thread['canAcceptDirectInput'] = False
                else: reply['result']['sandbox'] = {'type': 'readOnly'}
                with self.assertRaises(LocalProtocolError):
                    view.incoming(reply)
                self.assertEqual(root.native_id, self.main)
                self.assertIsNone(root.launch_refusal)
                with self.assertRaises(LocalProtocolError):
                    view.outgoing({'id': 21, 'method': 'turn/start', 'params': {'threadId': self.main}})

        # Ordinary reattach keeps native grants even though its static --add-dir roots are empty.
        policy = {'approvalPolicy': 'never', 'approvalsReviewer': 'user', 'activePermissionProfile': None,
                  'runtimeWorkspaceRoots': [], 'sandbox': {'type': 'workspaceWrite',
                  'writableRoots': ['/outside/agent'], 'networkAccess': False,
                  'excludeTmpdirEnvVar': True, 'excludeSlashTmp': True}}
        for reason in ('new', 'clear'):
            for wider in (False, True):
                with self.subTest(retained_grants=reason, wider_client=wider):
                    root, view = self.navigation_bound(policy, roots=[],
                        resume_permissions={'sandbox': 'workspace-write', 'approvalPolicy': 'never'})
                    if wider:
                        root.settings.update(workspace_policy={'writable_roots': ['/default-wider']},
                            plugin_config={'sandbox_workspace_write': {'writable_roots': ['/plugin-wider']},
                                           'check_for_update_on_startup': False})
                    target = str(uuid4())
                    request = {'id': 20, 'method': 'thread/start', 'params': {**full,
                        'threadSource': 'user', 'ephemeral': False, 'historyMode': 'paginated',
                        'sessionStartSource': 'clear' if reason == 'clear' else None,
                        'runtimeWorkspaceRoots': ['/wider'] if wider else [],
                        'config': {'sandbox_workspace_write': {'writable_roots': ['/wider'],
                                   'network_access': True}} if wider else {}}}
                    forwarded = view.outgoing(request)
                    sandbox = forwarded['params']['config']['sandbox_workspace_write']
                    self.assertEqual(sandbox, {'writable_roots': ['/outside/agent'], 'network_access': False,
                                              'exclude_tmpdir_env_var': True, 'exclude_slash_tmp': True})
                    self.assertEqual(forwarded['params']['runtimeWorkspaceRoots'], [])
                    self.assertEqual(forwarded['params']['approvalsReviewer'], 'user')
                    if wider:
                        self.assertIs(forwarded['params']['config']['check_for_update_on_startup'], False)
                    event = view.navigation('outgoing', forwarded)
                    view.navigation_done(event, {'token': 'token'})
                    reply = {'id': 20, 'result': {**deepcopy(policy), 'cwd': '/fixture/workspace',
                        'thread': {'id': target, 'cwd': '/fixture/workspace', 'ephemeral': False,
                                   'parentThreadId': None, 'status': {'type': 'idle'},
                                   'canAcceptDirectInput': True, 'path': '/private/codex/sessions/rollout.jsonl'}}}
                    self.assertIsNone(view.incoming(reply))
                    self.assertEqual(root.native_id, self.main)
                    self.assertEqual(getattr(root, 'grants_native_id', None), self.main)
                    event = view.navigation('incoming', reply)
                    view.navigation_done(event, {'native_id': target})
                    self.assertEqual(root.grants_native_id, target)
                    self.assertEqual(root.grant_ceiling, policy)

        for owner in (self.main, self.side):
            with self.subTest(settings_owner=owner):
                root, view = self.navigation_bound(policy,
                    resume_permissions={'sandbox': 'workspace-write', 'approvalPolicy': 'never'})
                reduced = {**deepcopy(policy), 'cwd': '/fixture/workspace',
                           'sandboxPolicy': {**policy['sandbox'], 'writableRoots': []}}
                reduced.pop('sandbox')
                view.incoming({'method': 'thread/settings/updated', 'params': {'threadId': owner,
                               'threadSettings': reduced}})
                request = {'id': 20, 'method': 'thread/start', 'params': {**full, 'threadSource': 'user',
                           'ephemeral': False, 'historyMode': 'paginated'}}
                if owner == self.main:
                    with self.assertRaises(LocalProtocolError):
                        view.outgoing(request)
                else:
                    self.assertEqual(root.current_grants, policy)
                    forwarded = view.outgoing(request)
                    self.assertEqual(forwarded['params']['config']['sandbox_workspace_write']['writable_roots'],
                                     ['/outside/agent'])

        # Native settings omit runtime roots even when a model/effort change leaves permissions alone.
        notification_policy = {**deepcopy(policy), 'runtimeWorkspaceRoots': ['/workspace/z', '/agent/a'],
            'sandbox': {**policy['sandbox'], 'writableRoots': ['/writable/z', '/writable/a']}}
        projection = {key: deepcopy(value) for key, value in notification_policy.items()
                      if key not in ('sandbox', 'runtimeWorkspaceRoots')}
        projection.update(cwd='/fixture/workspace', sandboxPolicy=deepcopy(notification_policy['sandbox']),
                          model='different-model', effort='high')
        notifications = [('native projection', {}, True),
            ('explicit same roots', {'runtimeWorkspaceRoots': ['/agent/a', '/workspace/z']}, True),
            ('sandbox permutation', {'sandboxPolicy': {**projection['sandboxPolicy'],
                'writableRoots': ['/writable/a', '/writable/z', '/writable/a']}}, True),
            ('different cwd', {'cwd': '/different/workspace'}, False),
            ('relative cwd', {'cwd': 'fixture/workspace'}, False),
            ('null cwd', {'cwd': None}, False),
            ('approval change', {'approvalPolicy': 'on-request'}, False),
            ('reviewer change', {'approvalsReviewer': 'guardian_subagent'}, False),
            ('profile change', {'activePermissionProfile': {'id': 'other'}}, False),
            ('sandbox partial', {'sandboxPolicy': {'type': 'workspaceWrite'}}, False),
            ('sandbox reduced', {'sandboxPolicy': {**projection['sandboxPolicy'], 'writableRoots': []}}, False),
            ('sandbox wider', {'sandboxPolicy': {**projection['sandboxPolicy'], 'writableRoots': ['/wider']}}, False),
            ('sandbox malformed', {'sandboxPolicy': {**projection['sandboxPolicy'], 'networkAccess': 0}}, False),
            ('network change', {'sandboxPolicy': {**projection['sandboxPolicy'], 'networkAccess': True}}, False),
            ('tmp change', {'sandboxPolicy': {**projection['sandboxPolicy'], 'excludeSlashTmp': False}}, False),
            ('runtime reduction', {'runtimeWorkspaceRoots': ['/agent/a']}, False),
            ('runtime widening', {'runtimeWorkspaceRoots': ['/agent/a', '/workspace/z', '/wider']}, False),
            ('runtime malformed', {'runtimeWorkspaceRoots': ['relative']}, False),
            ('runtime null', {'runtimeWorkspaceRoots': None}, False)]
        for missing in (*projection,):
            if missing not in ('model', 'effort'):
                notifications.append(('missing ' + missing, {missing: None}, False))
        for name, changes, retained in notifications:
            with self.subTest(native_settings=name):
                root, view = self.navigation_bound(notification_policy,
                    resume_permissions={'sandbox': 'workspace-write', 'approvalPolicy': 'never'})
                before, ceiling, epoch = root.current_grants, deepcopy(root.grant_ceiling), root.grants_epoch
                settings = {**deepcopy(projection), **deepcopy(changes)}
                if name.startswith('missing '):
                    settings.pop(name.removeprefix('missing '))
                view.incoming({'method': 'thread/settings/updated', 'params': {
                    'threadId': self.main, 'threadSettings': settings}})
                self.assertEqual(root.grant_ceiling, ceiling)
                if retained:
                    self.assertIs(root.current_grants, before, 'A projection preserves but never publishes grant authority')
                    self.assertEqual(root.grants_epoch, epoch)
                    forwarded = view.outgoing({'id': 20, 'method': 'thread/start', 'params': {**full,
                        'threadSource': 'user', 'ephemeral': False, 'historyMode': 'paginated'}})
                    self.assertEqual(forwarded['params'].get('runtimeWorkspaceRoots'), ceiling['runtimeWorkspaceRoots'])
                else:
                    self.assertIsNone(root.current_grants)
                    with self.assertRaises(LocalProtocolError):
                        view.outgoing({'id': 20, 'method': 'thread/start', 'params': {**full,
                            'threadSource': 'user', 'ephemeral': False, 'historyMode': 'paginated'}})

        # An unchanged projection cannot recreate missing authority, including after an observed root reduction.
        for invalidate in ('outgoing reduction', 'unbound policy'):
            with self.subTest(sticky_native_settings=invalidate):
                root, view = self.navigation_bound(notification_policy,
                    resume_permissions={'sandbox': 'workspace-write', 'approvalPolicy': 'never'})
                if invalidate == 'outgoing reduction':
                    view.outgoing({'id': 10, 'method': 'thread/settings/update', 'params': {
                        'threadId': self.main, 'runtimeWorkspaceRoots': ['/agent/a']}})
                else:
                    root.current_grants = None
                epoch = root.grants_epoch
                view.incoming({'method': 'thread/settings/updated', 'params': {
                    'threadId': self.main, 'threadSettings': deepcopy(projection)}})
                self.assertIsNone(root.current_grants)
                self.assertEqual(root.grants_epoch, epoch)

        for method in ('thread/settings/update', 'turn/settings/update', 'thread/resume'):
            with self.subTest(requested_permission_change=method):
                root, view = self.navigation_bound(policy,
                    resume_permissions={'sandbox': 'workspace-write', 'approvalPolicy': 'never'})
                frame = {'id': 10, 'method': method, 'params': {'threadId': self.main,
                         'sandboxPolicy': {**policy['sandbox'], 'writableRoots': []}}}
                self.assertEqual(view.outgoing(frame), frame)
                with self.assertRaises(LocalProtocolError):
                    view.outgoing({'id': 20, 'method': 'thread/start', 'params': {**full, 'threadSource': 'user',
                        'ephemeral': False, 'historyMode': 'paginated'}})

        root, view = self.navigation_bound(policy,
            resume_permissions={'sandbox': 'workspace-write', 'approvalPolicy': 'never'})
        view.outgoing({'id': 10, 'method': 'thread/resume', 'params': {'threadId': self.main}})
        changed = deepcopy(policy)
        changed['sandbox']['writableRoots'] = []
        view.incoming({'id': 10, 'result': {**changed, 'thread': {'id': self.main, 'cwd': '/fixture/workspace'}}})
        self.assertIsNone(root.current_grants, 'A UI reselect cannot hide changed effective grants')

        for mode in ('workspaceWrite', 'readOnly', 'dangerFullAccess'):
            with self.subTest(effective_policy=mode):
                exact = {**deepcopy(policy), 'runtimeWorkspaceRoots': ['/runtime/workspace'], 'sandbox':
                    ({**policy['sandbox'], 'networkAccess': True, 'excludeTmpdirEnvVar': False,
                      'excludeSlashTmp': False} if mode == 'workspaceWrite' else
                     {'type': 'readOnly', 'networkAccess': False} if mode == 'readOnly' else {'type': mode})}
                root, view = self.navigation_bound(exact, resume_permissions={})
                forwarded = view.outgoing({'id': 20, 'method': 'thread/start', 'params': {**full,
                    'threadSource': 'user', 'ephemeral': False, 'historyMode': 'paginated'}})
                self.assertEqual(forwarded['params']['sandbox'], {
                    'workspaceWrite': 'workspace-write', 'readOnly': 'read-only',
                    'dangerFullAccess': 'danger-full-access'}[mode])
                self.assertEqual(forwarded['params']['runtimeWorkspaceRoots'], ['/runtime/workspace'])
                if mode == 'workspaceWrite':
                    self.assertEqual(forwarded['params']['config']['sandbox_workspace_write'], {
                        'writable_roots': ['/outside/agent'], 'network_access': True,
                        'exclude_tmpdir_env_var': False, 'exclude_slash_tmp': False})
                else:
                    self.assertNotIn('sandbox_workspace_write', forwarded['params']['config'])

        for key in ('permissions', 'permission_profile', 'active_permission_profile', 'profiles', 'profile'):
            with self.subTest(configured_profile=key):
                root, view = self.navigation_bound(policy,
                    resume_permissions={'sandbox': 'workspace-write', 'approvalPolicy': 'never'})
                with self.assertRaises(LocalProtocolError):
                    view.outgoing({'id': 20, 'method': 'thread/start', 'params': {**full,
                        'threadSource': 'user', 'ephemeral': False, 'historyMode': 'paginated',
                        'config': {key: {'custom': 'unmapped'}}}})

        # Native lists preserve their original order, whereas the verified envelope uses path sets.
        ordered = {**deepcopy(policy), 'runtimeWorkspaceRoots': ['/workspace/z', '/agent/a'],
                   'sandbox': {**policy['sandbox'], 'writableRoots': ['/writable/z', '/writable/a']}}
        sandbox = ordered['sandbox']
        overrides = [
            ('runtime permutation', {'runtimeWorkspaceRoots': ordered['runtimeWorkspaceRoots']}, True),
            ('sandbox permutation', {'sandboxPolicy': sandbox}, True),
            ('both permutations', {'runtimeWorkspaceRoots': ordered['runtimeWorkspaceRoots'],
                                   'sandboxPolicy': sandbox}, True),
            ('duplicate paths', {'runtimeWorkspaceRoots': [*ordered['runtimeWorkspaceRoots'], '/workspace/z'],
                'sandboxPolicy': {**sandbox, 'writableRoots': [*sandbox['writableRoots'], '/writable/z']}}, True),
            ('null overrides', {'runtimeWorkspaceRoots': None, 'sandboxPolicy': None}, True),
            ('absent overrides', {}, True),
            ('runtime added', {'runtimeWorkspaceRoots': [*ordered['runtimeWorkspaceRoots'], '/wider']}, False),
            ('runtime removed', {'runtimeWorkspaceRoots': ordered['runtimeWorkspaceRoots'][:1]}, False),
            ('runtime malformed', {'runtimeWorkspaceRoots': ['/relative', 1]}, False),
            ('runtime relative', {'runtimeWorkspaceRoots': ['relative']}, False),
            ('runtime wrong type', {'runtimeWorkspaceRoots': 'wrong'}, False),
            ('sandbox added', {'sandboxPolicy': {**sandbox, 'writableRoots': [*sandbox['writableRoots'], '/wider']}}, False),
            ('sandbox removed', {'sandboxPolicy': {**sandbox, 'writableRoots': sandbox['writableRoots'][:1]}}, False),
            ('sandbox partial', {'sandboxPolicy': {'type': 'workspaceWrite'}}, False),
            ('sandbox malformed', {'sandboxPolicy': {**sandbox, 'networkAccess': 1}}, False),
            ('sandbox network', {'sandboxPolicy': {**sandbox, 'networkAccess': True}}, False),
            ('sandbox tmp', {'sandboxPolicy': {**sandbox, 'excludeSlashTmp': False}}, False),
        ]
        for name, fields, retained in overrides:
            with self.subTest(typed_override=name):
                root, view = self.navigation_bound(ordered,
                    resume_permissions={'sandbox': 'workspace-write', 'approvalPolicy': 'never'})
                ceiling = deepcopy(root.grant_ceiling)
                frame = {'id': 10, 'method': 'turn/start', 'params': {'threadId': self.main, **deepcopy(fields)}}
                self.assertEqual(view.outgoing(frame), frame)
                self.assertEqual(root.current_grants is not None, retained)
                self.assertEqual(root.grant_ceiling, ceiling)
                view.incoming({'id': 10, 'result': {'turn': {'id': 'ordinary-turn'}}})
                request = {'id': 20, 'method': 'thread/start', 'params': {**full, 'threadSource': 'user',
                           'ephemeral': False, 'historyMode': 'paginated'}}
                if retained:
                    forwarded = view.outgoing(request)
                    self.assertEqual(forwarded['params'].get('runtimeWorkspaceRoots'), ceiling['runtimeWorkspaceRoots'])
                    self.assertEqual(forwarded['params']['config']['sandbox_workspace_write']['writable_roots'],
                                     ceiling['sandbox']['writableRoots'])
                else:
                    with self.assertRaises(LocalProtocolError):
                        view.outgoing(request)

        # Missing or unsupported response metadata disables navigation, never an otherwise valid bind or /btw.
        bad_policies = []
        for key in policy:
            bad = deepcopy(policy)
            bad.pop(key)
            bad_policies.append((f'missing {key}', bad))
        for key, value in (('approvalPolicy', 'untrusted'), ('approvalsReviewer', 'guardian_subagent'),
                           ('activePermissionProfile', {'type': 'named', 'name': 'custom'}),
                           ('runtimeWorkspaceRoots', ['relative']), ('runtimeWorkspaceRoots', None)):
            bad_policies.append((key, {**deepcopy(policy), key: value}))
        for key, value in (('writableRoots', ['relative']), ('networkAccess', 1),
                           ('excludeSlashTmp', None), ('readOnlyAccess', {'type': 'restricted'})):
            bad = deepcopy(policy)
            bad['sandbox'][key] = value
            bad_policies.append((key, bad))
        for name, bad in bad_policies:
            with self.subTest(unmappable_initial_grants=name):
                root, view = self.navigation_bound(bad, resume_permissions={})
                self.assertTrue(root.bound)
                self.assertIsNone(root.launch_refusal)
                with self.assertRaises(LocalProtocolError):
                    view.outgoing({'id': 20, 'method': 'thread/start', 'params': {**full, 'threadSource': 'user',
                        'ephemeral': False, 'historyMode': 'paginated'}})
                fork = {'id': 21, 'method': 'thread/fork', 'params': {'threadId': self.main,
                        'ephemeral': True, 'excludeTurns': True}}
                self.assertEqual(view.outgoing(fork), fork)

        for key, value in (('writableRoots', []), ('writableRoots', ['/wider']), ('networkAccess', True),
                           ('excludeTmpdirEnvVar', False), ('excludeSlashTmp', False)):
            with self.subTest(changed_returned_policy=key, value=value):
                root, view = self.navigation_bound(policy,
                    resume_permissions={'sandbox': 'workspace-write', 'approvalPolicy': 'never'})
                target = str(uuid4())
                request = {'id': 20, 'method': 'thread/resume', 'params': {**full, 'threadId': target}}
                forwarded = view.outgoing(request)
                self.assertNotIn('sandbox_workspace_write', forwarded['params']['config'])
                view.navigation_done(view.navigation('outgoing', forwarded), {'token': 'token'})
                changed = deepcopy(policy)
                changed['sandbox'][key] = value
                reply = {'id': 20, 'result': {**changed, 'cwd': '/fixture/workspace', 'thread': {
                    'id': target, 'cwd': '/fixture/workspace', 'ephemeral': False,
                    'status': {'type': 'idle'}, 'canAcceptDirectInput': True,
                    'path': '/private/codex/sessions/rollout.jsonl'}}}
                with self.assertRaises(LocalProtocolError):
                    view.incoming(reply)
                self.assertEqual(root.native_id, self.main)
                self.assertEqual(root.grants_native_id, self.main)
                self.assertEqual(root.current_grants, policy)

        # A settings update can race the core commit after its native response; it must not revive old grants.
        root, view = self.navigation_bound(policy,
            resume_permissions={'sandbox': 'workspace-write', 'approvalPolicy': 'never'})
        target = str(uuid4())
        request = {'id': 20, 'method': 'thread/start', 'params': {**full, 'threadSource': 'user',
            'ephemeral': False, 'historyMode': 'paginated'}}
        forwarded = view.outgoing(request)
        view.navigation_done(view.navigation('outgoing', forwarded), {'token': 'token'})
        reply = {'id': 20, 'result': {**deepcopy(policy), 'cwd': '/fixture/workspace', 'thread': {
            'id': target, 'cwd': '/fixture/workspace', 'ephemeral': False, 'status': {'type': 'idle'},
            'canAcceptDirectInput': True, 'path': '/private/codex/sessions/rollout.jsonl'}}}
        view.incoming(reply)
        event = view.navigation('incoming', reply)
        view.incoming({'method': 'thread/settings/updated', 'params': {'threadId': target, 'threadSettings': {}}})
        # A later matching projection cannot undo invalidation staged before the durable commit.
        view.incoming({'method': 'thread/settings/updated', 'params': {
            'threadId': target, 'threadSettings': {**deepcopy(projection),
                'sandboxPolicy': deepcopy(policy['sandbox'])}}})
        view.navigation_done(event, {'native_id': target})
        self.assertEqual(root.native_id, target)
        self.assertIsNone(root.current_grants)
        with self.assertRaises(LocalProtocolError):
            view.outgoing({**request, 'id': 21})

        # Native metadata is staged, and cannot become navigation authority until core accepts the owning bind.
        root = LaunchProtocol({**self.settings, 'navigation_cli': [0, 166, 0], 'navigation_home': '/private/codex'})
        accepted = [False]
        view = root.for_connection('owner', lambda: accepted[0])
        self.initialize(view)
        view.outgoing({'id': 2, 'method': 'thread/resume', 'params': {'threadId': self.main}})
        initial = {'id': 2, 'result': {'thread': {'id': self.main, 'cwd': '/fixture/workspace'},
                   'approvalPolicy': 'never', 'approvalsReviewer': 'user', 'sandbox': {'type': 'dangerFullAccess'},
                   'activePermissionProfile': None, 'runtimeWorkspaceRoots': []}}
        view.incoming(initial)
        view.after_bind('/fixture/native.sock')
        self.assertIsNone(root.grant_ceiling)
        accepted[0] = True
        view.after_bind('/fixture/native.sock')
        self.assertIsNotNone(root.grant_ceiling)
        initial['result']['sandbox']['type'] = 'workspaceWrite'
        self.assertEqual(root.grant_ceiling['sandbox'], {'type': 'dangerFullAccess'})

    def test_only_the_exact_fork_from_the_current_owner_is_allowed(self):
        """Only the exact checked thread/fork shape from the current owner is allowed, and any other request stays
        local; fragile because it pins Codex's fork parameters, and a looser check rebinds the primary."""
        before = self.primary_state()
        for version in (None, 'unparseable', 'codex/0.162.1', 'codex/0.165.0'):
            with self.subTest(version=version):
                view = self.connection('unknown-' + str(version), lambda: True)
                self.initialize(view, version)
                with self.assertRaises(LocalProtocolError):
                    view.outgoing(self.fork())
        for version in ('0.163.0', '0.164.0', '0.166.0', '0.167.0'):
            with self.subTest(supported=version):
                view = self.connection('checked-' + version, lambda: True)
                self.initialize(view, 'codex/' + version)
                self.assertEqual(view.outgoing(self.fork()), self.fork())
        for change in ({'ephemeral': None}, {'ephemeral': 1}, {'threadId': self.side},
                       {'path': '/other/rollout'}, {'lastTurnId': 'turn'}, {'beforeTurnId': 'turn'},
                       {'excludeTurns': False}, {'experimentalPredictionMode': True}, {'cwd': '/other/workspace'}):
            with self.subTest(fork=change), self.assertRaises(LocalProtocolError):
                self.view.outgoing(self.fork(**change))
        for identifier in (None, True, [], {}):
            with self.subTest(identifier=identifier):
                with self.assertRaises(LocalProtocolError) as caught:
                    self.view.outgoing(self.fork(identifier))
                self.assertIsNone(caught.exception.request_id)
        for params in (None, [], 'bad'):
            with self.subTest(params=params), self.assertRaises(LocalProtocolError):
                self.view.outgoing({**self.fork(), 'params': params})
        for change in ({'path': '/other/rollout'}, {'history': []}, {'cwd': '/other'}):
            with self.subTest(reselection=change), self.assertRaises(LocalProtocolError):
                self.view.outgoing({'id': 20, 'method': 'thread/resume', 'params': {'threadId': self.main, **change}})
        other = self.connection('control', lambda: False)
        self.initialize(other)
        with self.subTest('another connection'), self.assertRaises(LocalProtocolError):
            other.outgoing(self.fork())
        self.owner = False
        with self.subTest('a former owner'), self.assertRaises(LocalProtocolError):
            self.view.outgoing(self.fork())
        self.owner = True
        for change in ({'id': self.main}, {'id': 'not-a-uuid'}, {'ephemeral': False},
                       {'forkedFromId': str(uuid4())}, {'cwd': '/other'}, {'path': '/stored/side'}):
            with self.subTest(reply=change):
                self.assertEqual(self.view.outgoing(self.fork()), self.fork())
                with self.assertRaises(LocalProtocolError):
                    self.view.incoming(self.forked(**change))
        self.open_side()
        with self.assertRaises(LocalProtocolError) as caught:
            self.view.outgoing({'id': False, 'method': 'turn/start', 'params': {'threadId': self.side}})
        self.assertIsNone(caught.exception.request_id)
        self.assertTrue(self.protocol.bound)
        self.assertEqual(self.primary_state(), before)

    def test_primary_queue_events_preserve_interrupt_order_and_ignore_the_side(self):
        """Primary interrupt ordering controls durable queue pause; a side or late old turn must not silently pause a newer primary turn."""
        observe = getattr(self.view, 'primary_event', lambda direction, frame: None)
        def incoming(kind, turn, status=None, native=None):
            return {'method': 'turn/' + kind, 'params': {'threadId': native or self.main,
                    'turn': {'id': turn, **({'status': status} if status else {})}}}
        def event(kind, turn, **fields):
            return {'kind': kind, 'native_session_id': self.main, 'turn_id': turn, **fields}
        rows = [
            ('incoming', incoming('started', 'one'), event('started', 'one')),
            ('outgoing', {'id': 30, 'method': 'turn/interrupt',
                'params': {'threadId': self.main, 'turnId': 'one'}}, event('interrupt', 'one')),
            ('incoming', {'id': 30, 'result': {}}, None),
            ('incoming', incoming('completed', 'one', 'interrupted'), event('completed', 'one', status='interrupted')),
            ('incoming', incoming('started', 'two'), event('started', 'two')),
            ('incoming', incoming('completed', 'one', 'interrupted'), None),
            ('incoming', incoming('completed', 'two', 'completed'), event('completed', 'two', status='finished')),
            ('incoming', incoming('started', 'side', native=self.side), None),
            ('incoming', incoming('completed', 'side', 'interrupted', self.side), None),
            ('outgoing', {'id': 31, 'method': 'turn/interrupt',
                'params': {'threadId': self.main, 'turnId': ''}}, None),
        ]
        for direction, frame, expected in rows:
            with self.subTest(direction=direction, frame=frame):
                self.assertEqual(observe(direction, frame), expected)
        self.owner = False
        self.assertIsNone(observe('incoming', incoming('started', 'other-client')))
        self.owner = True
        self.view.closed()
        self.assertIsNone(observe('incoming', incoming('started', 'late')))
