"""Connection-local native /btw routing; the launch root owns only its primary.

Side conversations inherit native tools and permissions. This is identity and
request correlation, never an execution sandbox or an mv2 agent registration.
"""
from pathlib import Path
from copy import deepcopy

from ..types import NativeResult, normalize_session_id
from .launch import LaunchProtocol
from .plugins import server_version
from .protocol import object_field, result


MAX_PENDING = 64
CLEANUP_METHODS = ('turn/interrupt', 'thread/unsubscribe')
MAX_CLEANUP = 3  # Native interrupt, one turn-ID-race retry, then unsubscribe.


def request_id(value):
    return isinstance(value, str) or type(value) is int


def local_error(reason, identifier=None):
    from ..types import LocalProtocolError
    return LocalProtocolError(reason, request_id=identifier if request_id(identifier) else None)


class LaunchConnection:
    def __init__(self, root, connection_id, owns_primary):
        self.root, self.connection_id, self.owns_primary = root, connection_id, owns_primary
        # Native response IDs are meaningful only on their originating connection.
        self.primary = LaunchProtocol(root.settings)
        self.initializing = None
        self.version = None
        self.fork_request = None
        self.side_id = None
        self.side_requests = {}
        self.reselections = set()
        self.primary_frame = False
        self.ended = False

    @property
    def bound(self):
        return self.root.bound

    @property
    def launch_refusal(self):
        return self.root.launch_refusal if self.primary_frame else None

    def _sync(self):
        self.primary.native_id = self.root.native_id
        self.primary.bound = self.root.bound
        self.primary.launch_refusal = self.root.launch_refusal

    def _pending(self, identifier):
        return (identifier is not None and
                (identifier in self.side_requests or identifier in self.reselections
                 or identifier == self.fork_request or identifier == self.initializing
                 or identifier == self.primary.pending or identifier == self.primary.prompt_request))

    def _fork(self, value):
        identifier = value.get('id')
        if (not request_id(identifier) or not self.owns_primary() or not self.root.bound
                or self.root.launch_refusal or self.version != (0, 163, 0)):
            raise local_error('Native /btw requires the owning, bound Codex 0.163.0 terminal; '
                              'otherwise use a separate Multiverse launch', identifier)
        params = object_field(value, 'params')
        if (self.fork_request is not None or self.side_id is not None or self._pending(identifier)
                or params.get('threadId') != self.root.native_id
                or params.get('ephemeral') is not True or params.get('excludeTurns') is not True
                or params.get('experimentalPredictionMode', False) is not False
                or params.get('deferGoalContinuation', False) is not False
                or any(params.get(key) is not None for key in ('path', 'lastTurnId', 'beforeTurnId'))
                or (params.get('cwd') is not None
                    and (not isinstance(params['cwd'], str)
                         or Path(params['cwd']).resolve() != Path(self.root.settings['workspace']).resolve()))):
            raise local_error('Only one native ephemeral /btw fork of this launch primary is allowed', identifier)
        params = deepcopy(params)
        if 'agent_environment' in self.root.settings:
            from .identity import session_environment
            from .plugins import config_tree
            native = config_tree(object_field(params, 'config').items()) if params.get('config') is not None else {}
            params['config'] = session_environment(native, self.root.settings['agent_environment'],
                                                   self.root.settings.get('storage_keys'))
        self.fork_request = identifier
        return {**value, 'params': params}

    def outgoing(self, value):
        if self.ended:
            raise local_error('This native connection is closed', value.get('id'))
        self._sync()
        method, identifier = value.get('method'), value.get('id')
        if method is None:
            return value  # Replies to native host requests have a separate ID namespace.
        params = value.get('params')
        native_id = params.get('threadId') if isinstance(params, dict) else None
        if method == 'thread/fork':
            if isinstance(params, dict) and ('ephemeral' not in params or params['ephemeral'] is False):
                # Persistent navigation is a primary policy error, not a /btw
                # failure. Preserve the ordinary recorded navigation diagnostic.
                raise ValueError('Fork into a separate Multiverse launch to preserve session identity')
            try:
                return self._fork(value)
            except (TypeError, KeyError, ValueError) as error:
                raise local_error(str(error), identifier) from error
        if self.side_id is not None and native_id == self.side_id:
            if method in ('thread/start', 'thread/resume'):
                raise local_error('A /btw side conversation cannot replace this launch binding', identifier)
            if 'id' in value and not request_id(identifier):
                raise local_error('Native side request ID must be text or an integer')
            if request_id(identifier):
                cleanup = method in CLEANUP_METHODS
                count = sum((pending in CLEANUP_METHODS) == cleanup for pending in self.side_requests.values())
                if self._pending(identifier) or count >= (MAX_CLEANUP if cleanup else MAX_PENDING):
                    raise local_error('Native side request correlation is full or already in use', identifier)
                self.side_requests[identifier] = method
            return value  # Native tools, permissions and host requests are intentionally unchanged.
        if identifier is not None and (identifier in self.side_requests or identifier == self.fork_request):
            raise local_error('Native request ID is already in use by the side conversation', identifier)
        if method == 'initialize' and request_id(identifier):
            self.initializing = identifier
        if method in ('thread/start', 'thread/resume') and 'id' in value:
            if self.root.bound:
                if method != 'thread/resume' or native_id != self.root.native_id:
                    return self.primary.outgoing(value)  # Preserve the primary navigation rejection.
                if not self.owns_primary():
                    raise local_error('Only the owning terminal may select this launch primary', identifier)
                if (any(params.get(key) is not None for key in ('path', 'history'))
                        or (params.get('cwd') is not None
                            and (not isinstance(params['cwd'], str)
                                 or Path(params['cwd']).resolve() != Path(self.root.settings['workspace']).resolve()))):
                    raise local_error('Native primary selection cannot replace its saved history or workspace', identifier)
                if not request_id(identifier) or self._pending(identifier) or len(self.reselections) >= MAX_PENDING:
                    raise local_error('Native primary selection ID is full or already in use', identifier)
                self.reselections.add(identifier)
                return value  # A UI return is not a new launch/grant/bootstrap handshake.
            claimed = getattr(self.root, '_binding_connection', None)
            if claimed is not None and claimed != self.connection_id:
                raise ValueError('Another native connection is preparing this launch binding')
            self.root._binding_connection = self.connection_id
        return self.primary.outgoing(value)

    def _forked(self, value):
        identifier, self.fork_request = self.fork_request, None
        try:
            observed = result(value)
            if isinstance(observed, NativeResult):
                return  # Native error is forwarded as-is and permits retry.
            thread = object_field(observed, 'thread')
            native_id = normalize_session_id(thread.get('id'))
            if (native_id == self.root.native_id or thread.get('forkedFromId') != self.root.native_id
                    or thread.get('ephemeral') is not True or thread.get('path') is not None
                    or not isinstance(thread.get('cwd'), str)
                    or Path(thread['cwd']).resolve() != Path(self.root.settings['workspace']).resolve()):
                raise ValueError('Native /btw response is not the requested ephemeral side conversation')
            self.side_id = native_id
        except (TypeError, KeyError, ValueError) as error:
            raise local_error(str(error), identifier) from error

    def incoming(self, value):
        self.primary_frame = False
        if self.ended:
            return None
        self._sync()
        response = 'method' not in value
        identifier = value.get('id')
        if response and 'id' in value and not request_id(identifier):
            raise ValueError('Native response ID must be text or an integer')
        if response and identifier is not None and identifier == self.initializing:
            self.initializing = None
            observed = value.get('result')
            self.version = server_version(observed.get('userAgent')) if isinstance(observed, dict) else None
            return None
        if response and identifier is not None and identifier == self.fork_request:
            self._forked(value)
            return None
        if response and request_id(identifier) and identifier in self.side_requests:
            method = self.side_requests.pop(identifier)
            if method == 'thread/unsubscribe' and isinstance(value.get('result'), dict):
                if value['result'].get('status') in ('unsubscribed', 'notSubscribed', 'notLoaded'):
                    self.side_id = None
                    self.side_requests.clear()
            return None
        params = value.get('params')
        native_id = (params.get('threadId') or params.get('conversationId')) if isinstance(params, dict) else None
        if self.side_id is not None and native_id == self.side_id:
            return None
        if response and request_id(identifier) and identifier in self.reselections:
            self.reselections.remove(identifier)
            observed = result(value)
            if not isinstance(observed, NativeResult):
                thread = object_field(observed, 'thread')
                if (thread.get('id') != self.root.native_id or not isinstance(thread.get('cwd'), str)
                        or Path(thread['cwd']).resolve() != Path(self.root.settings['workspace']).resolve()):
                    raise ValueError('Native primary selection differs from this launch binding')
            return None
        correlated = response and identifier is not None and identifier in (self.primary.pending, self.primary.prompt_request)
        self.primary_frame = correlated or (native_id is not None and native_id == self.root.native_id)
        native_id = self.primary.incoming(value)
        if native_id is not None:
            if getattr(self.root, '_binding_connection', None) != self.connection_id:
                raise ValueError('Native binding response belongs to another connection')
            self.root.native_id, self.root.bound = self.primary.native_id, self.primary.bound
        if self.primary_frame:
            if self.primary.launch_refusal is not None:
                self.root.launch_refusal = self.primary.launch_refusal
            if self.owns_primary() or getattr(self.root, '_binding_connection', None) == self.connection_id:
                for key in ('prompt_request', 'prompt_submitted', 'prompt_error'):
                    setattr(self.root, key, getattr(self.primary, key))
        return native_id

    def primary_event(self, direction, value):
        if self.ended or not self.root.bound or self.owns_primary() is not True:
            return None
        from .queue_events import PrimaryEvents
        if not hasattr(self.root, '_queue_events'):
            self.root._queue_events = PrimaryEvents()
        return self.root._queue_events.observe(direction, value, self.root.native_id)

    def after_bind(self, upstream_path):
        return self.primary.after_bind(upstream_path)

    def usage(self, value):
        return self.root.usage(value) if self.primary_frame else None

    def closed(self):
        self.ended = True
        self.fork_request = self.side_id = self.initializing = None
        self.side_requests.clear()
        self.reselections.clear()
        self.primary.pending = self.primary.pending_method = self.primary.prompt_request = None
        if not self.root.bound and getattr(self.root, '_binding_connection', None) == self.connection_id:
            self.root._binding_connection = None
