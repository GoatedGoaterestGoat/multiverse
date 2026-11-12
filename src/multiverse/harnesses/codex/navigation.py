"""Checked remote-TUI selection evidence; core alone commits lineage and claims.

A successful explicit navigation RPC is the boundary, not acknowledgement that
native history hydration or widget replacement subsequently succeeded.
"""
from copy import deepcopy
from pathlib import Path

from ..types import NativeResult, normalize_session_id
from .launch import LaunchProtocol
from .protocol import object_field, result
from .side import local_error

VERSION = (0, 167, 2)
VERSIONS = frozenset(((0, 166, 0), (0, 167, 0), (0, 167, 1), VERSION))
# Pinned app-server-protocol common.rs reads. New thread-scoped methods fail
# closed instead of accidentally granting stale-widget mutation authority.
READ_ONLY = frozenset(('thread/goal/get', 'thread/queue/list', 'thread/attachment/list',
    'thread/attachmentOwner/list', 'thread/memory/read', 'thread/backgroundTerminals/list',
    'thread/list', 'thread/search', 'thread/searchOccurrences', 'thread/loaded/list', 'thread/read',
    'thread/turns/list', 'thread/items/list', 'thread/items/read', 'thread/timeline/list',
    'thread/realtime/listVoices'))
NAVIGATION = frozenset(('thread/start', 'thread/resume', 'thread/fork'))


def _mutates_thread(method, params):
    if not isinstance(method, str) or method in READ_ONLY or method in NAVIGATION or method == 'thread/unsubscribe':
        return False
    return (method.startswith(('thread/', 'turn/'))
            or isinstance(params, dict) and any(key in params for key in ('threadId', 'conversationId')))


def _workspace(value, expected):
    try:
        return (isinstance(value, str) and bool(value) and Path(value).is_absolute()
                and Path(value).resolve() == Path(expected).resolve())
    except (OSError, RuntimeError, ValueError):
        return False


class Selection:
    def __init__(self, connection):
        self.connection = connection
        self.last_integer = -1
        self.event = None

    @property
    def root(self):
        return self.connection.root

    def outgoing(self, value):
        """Return a transformed candidate, or None for ordinary native traffic."""
        self.event = None
        method, identifier = value.get('method'), value.get('id')
        params = value.get('params')
        native_id = params.get('threadId') if isinstance(params, dict) else None
        pending = getattr(self.root, '_selection', None)
        if pending and method in ('thread/start', 'thread/resume', 'thread/fork'):
            raise local_error('Finish or restore the pending conversation selection before opening another conversation', identifier)
        if _mutates_thread(method, params):
            if pending:
                raise local_error('Conversation selection is unresolved; restore the recorded conversation with mv2 resume', identifier)
            targets = ([params[key] for key in ('threadId', 'conversationId') if key in params]
                       if isinstance(params, dict) else []) or [None]
            if (getattr(self.root, '_selection_committed', False)
                    and any(target != self.root.native_id
                            and (self.connection.side_id is None or target != self.connection.side_id)
                            for target in targets)):
                raise local_error('This terminal is showing an earlier conversation; use mv2 resume to restore the recorded conversation', identifier)
        previous_integer = self.last_integer
        if isinstance(method, str) and type(identifier) is int:
            self.last_integer = max(self.last_integer, identifier)
        if (not self.root.bound or method not in ('thread/start', 'thread/resume')
                or method == 'thread/resume' and native_id == self.root.native_id):
            return None
        # Background minimal resumes and dynamic/feature starts are not evidence
        # of explicit primary selection and keep the existing local refusal.
        if not isinstance(params, dict):
            raise local_error('Native conversation selection requires an object of parameters', identifier)
        cli = self.root.settings.get('navigation_cli')
        if (not self.connection.owns_primary() or not isinstance(cli, (list, tuple))
                or any(type(part) is not int for part in cli) or tuple(cli) not in VERSIONS
                or self.connection.version != tuple(cli)):
            raise local_error('Conversation navigation requires the owning verified Codex bridge on matching checked CLI and daemon releases; use mv2 resume', identifier)
        if (pending or self.connection.side_id is not None or self.connection.fork_request is not None
                or self.connection.primary.pending is not None or self.connection.primary.prompt_request is not None):
            raise local_error('Finish the current selection, input or /btw conversation before switching', identifier)
        if (type(identifier) is not int or identifier < 0 or identifier > 2**63 - 1
                or identifier <= previous_integer or self.connection._pending(identifier)):
            raise local_error('Native selection requires a new checked terminal request ID', identifier)
        if not _workspace(params.get('cwd'), self.root.settings['workspace']):
            raise local_error('Conversation navigation must keep this agent workspace; another folder or worktree is not supported', identifier)
        if any(params.get(key) is not None for key in ('path', 'history', 'lastTurnId', 'beforeTurnId')):
            raise local_error('Conversation navigation cannot replace saved history or use a rollout path', identifier)
        if params.get('config') is not None and not isinstance(params['config'], dict):
            raise local_error('Native selection config must be an object', identifier)
        if method == 'thread/start':
            if (params.get('threadSource') != 'user' or params.get('ephemeral') is not False
                    or params.get('historyMode') != 'paginated'
                    or params.get('sessionStartSource') not in (None, 'clear')):
                raise local_error('Only an explicit persistent /new or /clear can replace this conversation', identifier)
            target = None
            reason = 'clear' if params.get('sessionStartSource') == 'clear' else 'new'
        else:
            # Full explicit remote resumes have a cwd and the client config
            # fields; PreserveExistingThread background resumes have no cwd.
            if not all(key in params for key in ('model', 'modelProvider', 'config', 'developerInstructions',
                                                  'approvalPolicy', 'sandbox', 'permissions')):
                raise local_error('Only a full explicit /resume can replace this conversation', identifier)
            try:
                target = normalize_session_id(native_id)
            except ValueError as error:
                raise local_error(str(error), identifier) from error
            reason = 'resume'
        if params.get('permissions') is not None:
            raise local_error('Named permission profiles cannot be mapped safely for conversation navigation', identifier)
        if (self.root.current_grants is None or self.root.grant_ceiling is None
                or self.root.grants_native_id != self.root.native_id):
            raise local_error(self.root.grants_error, identifier)
        home = self.root.settings.get('navigation_home')
        if not isinstance(home, str) or not Path(home).is_absolute():
            raise local_error('Conversation navigation needs recorded storage; use a normal mv2 resume first', identifier)
        settings = {**self.root.settings, 'native_id': target}
        if target is None:
            settings['navigation_grants'] = deepcopy(self.root.current_grants)
        parser = LaunchProtocol(settings)
        try:
            transformed = parser.outgoing(deepcopy(value))
        except (OSError, RuntimeError, ValueError, TypeError, KeyError) as error:
            raise local_error(f'Native selection configuration cannot be mapped safely: {error}', identifier) from error
        event = {'action': 'begin', 'previous': self.root.native_id, 'target': target, 'reason': reason,
                 'correlation': f'{self.connection.connection_id}:{identifier}'}
        self.root._selection = {'connection': self.connection.connection_id, 'id': identifier,
                                'parser': parser, 'event': event, 'home': home,
                                'grant_epoch': self.root.grants_epoch}
        self.event = event
        return transformed

    def incoming(self, value):
        """Keep a correlated selection response out of initial binding."""
        self.event = None
        pending = getattr(self.root, '_selection', None)
        if (not pending or pending['connection'] != self.connection.connection_id
                or 'method' in value or type(value.get('id')) is not int or value['id'] != pending['id']):
            return False
        if 'token' not in pending:
            raise local_error('Native selection replied before admission was confirmed', pending['id'])
        event = {**pending['event'], 'token': pending['token']}
        try:
            observed = result(value)
            if isinstance(observed, NativeResult):
                self.event = {**event, 'action': 'abort'}
            else:
                thread = object_field(observed, 'thread')
                if (thread.get('ephemeral') is not False or thread.get('parentThreadId') is not None
                        or thread.get('canAcceptDirectInput') is not True
                        or not isinstance(thread.get('status'), dict)
                        or thread['status'].get('type') != 'idle'
                        or not _workspace(thread.get('cwd'), self.root.settings['workspace'])
                        or not _workspace(observed.get('cwd'), self.root.settings['workspace'])):
                    raise ValueError('Native selection did not return an idle persistent primary conversation')
                path = thread.get('path')
                home = Path(pending['home'])
                if (not isinstance(path, str) or not Path(path).is_absolute()
                        or not any(Path(path).resolve().is_relative_to(home / store)
                                   for store in ('sessions', 'archived_sessions'))):
                    raise ValueError('Native selection rollout differs from the recorded Codex storage')
                native_id = pending['parser'].incoming(value)
                if native_id is None or native_id == event['previous']:
                    raise ValueError('Native selection did not return a different exact conversation')
                if (pending['parser'].candidate_grants is None
                        or pending['parser'].candidate_grants != self.root.grant_ceiling):
                    raise ValueError('Native selection did not preserve the complete validated launch permissions')
                pending['policy_native_id'] = native_id
                self.event = {**event, 'action': 'commit', 'target': native_id, 'home': pending['home']}
        except (OSError, RuntimeError, ValueError, TypeError, KeyError) as error:
            # Native may have switched already. Keep both durable and in-memory
            # fences; this is never initial-launch refusal or permission to retry.
            raise local_error(f'Conversation selection could not be verified: {error}; restore with mv2 resume', pending['id']) from error
        return True

    def done(self, event, observed):
        pending = getattr(self.root, '_selection', None)
        if not pending or pending['connection'] != self.connection.connection_id:
            raise ValueError('Conversation selection has no matching terminal correlation')
        if event['action'] == 'begin':
            if not isinstance(observed, dict) or not isinstance(observed.get('token'), str) or not observed['token']:
                raise ValueError('Core did not admit the exact conversation selection')
            pending['token'] = observed['token']
        elif event['action'] == 'commit':
            if not isinstance(observed, dict) or observed.get('native_id') != event['target']:
                raise ValueError('Core committed a different conversation selection')
            self.root._selection_committed = True
            self.root.native_id, self.root.bound = event['target'], True
            self.root.grants_native_id = event['target']
            if self.root.grants_epoch == pending['grant_epoch'] and self.root.current_grants is not None:
                self.root.current_grants = deepcopy(pending['parser'].candidate_grants)
            # Navigation is not a new launch or a replay of its initial prompt.
            self.root.prompt_request = None
            self.root.prompt_submitted = True
            self.root.prompt_error = None
            self.connection.primary.prompt_request = None
            self.connection.primary.prompt_submitted = True
            self.connection.primary.prompt_error = None
            if hasattr(self.root, '_queue_events'):
                del self.root._queue_events
            self.root._selection = None
            self.connection._sync()
        elif event['action'] == 'abort':
            self.root._selection = None
        self.event = None

    def failed(self, event):
        if event.get('action') == 'begin':
            pending = getattr(self.root, '_selection', None)
            if pending and pending['connection'] == self.connection.connection_id and 'token' not in pending:
                self.root._selection = None  # Definitely not forwarded; core also releases its own marker.
        self.event = None
