"""One terminal child per launch; no central supervisor or model loop."""
import asyncio
from datetime import datetime, timezone
import os
from pathlib import Path
import shutil
import signal
import subprocess
import sys
import time
import threading

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from multiverse.universe.agents import update_runtime, mutate_runtime, bind_session, runtime
from multiverse.store import lock, now, read, write
from multiverse.runtime.process import process_started


STARTUP_SECONDS = 2
OUTPUT_LINES = 40
OUTPUT_BYTES = 8192
REFUSAL_STARTUP_SECONDS = 60


def bounded_output(text):
    """Keep a small, printable terminal tail, including partial Unicode safely."""
    text = ''.join(character for character in text if character.isprintable() or character in '\n\t')
    text = '\n'.join(text.rstrip().splitlines()[-OUTPUT_LINES:])
    return text.encode('utf-8')[-OUTPUT_BYTES:].decode('utf-8', errors='ignore')


def launch_diagnostic(agent_dir, launch_id, reason):
    """Snapshot only this launch's owned pane; never redirect the child's TTY."""
    from multiverse.terminal.tmux import owned
    facts = runtime(agent_dir)
    if facts.get('launch_id') != launch_id:
        return {}
    output, unavailable = '', None
    try:
        backend, pane = owned(read(agent_dir / 'properties.json'), facts, states=('alive', 'ending', 'stopped'))
        output = bounded_output(backend.run('capture-pane', '-p', '-S', f'-{OUTPUT_LINES}', '-t', pane))
    except (OSError, ValueError, KeyError, RuntimeError, subprocess.TimeoutExpired) as error:
        unavailable = bounded_output(str(error))
    reason = bounded_output(reason)
    if output:
        reason += '\nLast terminal output:\n' + output
    else:
        reason += '\nTerminal output unavailable' + (': ' + unavailable if unavailable else ' (pane was empty)')
    relative = Path('launches') / launch_id / 'output-tail.txt'
    try:
        (agent_dir / relative).parent.mkdir(parents=True, exist_ok=True)
        (agent_dir / relative).write_text(output, encoding='utf-8')
    except OSError as error:
        return {'reason': reason + '\nCould not save terminal output: ' + bounded_output(str(error))}
    return {'reason': reason, 'output_tail_file': str(relative)}


def cleanup_wrapper(launch):
    """Run outside the pane so removing it cannot interrupt its own cleanup."""
    from multiverse.terminal.tmux import owned, observe
    agent_dir = launch.parent.parent
    if not (agent_dir / 'properties.json').exists():
        return
    with lock(agent_dir / '.launch.lock'):
        facts = runtime(agent_dir)
        if facts.get('launch_id') != launch.name or 'exit_code' not in facts or facts.get('ending_at'):
            return  # A replacement or an explicit kill owns its own teardown.
        try:
            properties = read(agent_dir / 'properties.json')
            try:
                backend, pane = owned(properties, facts, states=('stopped',))
            except ValueError:
                if observe(properties, facts)['live'] != 'dormant':
                    raise
            else:
                # Detach before removing the pane: tmux would otherwise move
                # its clients into another agent's remaining session.
                clients = backend.run('list-clients', '-t', pane, '-F', '#{client_name}\t#{pane_id}')
                for client in clients.splitlines():
                    name, _, viewed_pane = client.partition('\t')
                    if name and viewed_pane == pane:
                        try:
                            backend.run('detach-client', '-t', name)
                        except RuntimeError:
                            # A client can detach itself between observation and
                            # the command. Only a still-attached client blocks removal.
                            remaining = backend.run('list-clients', '-t', pane, '-F', '#{client_name}\t#{pane_id}')
                            if client in remaining.splitlines():
                                raise
                backend.run('kill-pane', '-t', pane)
            update_runtime(agent_dir, launch.name, state='dormant', status='unknown', dormant_at=now(),
                           wrapper_cleanup={'state': 'done'})
        except (OSError, ValueError, KeyError, RuntimeError, subprocess.TimeoutExpired) as error:
            update_runtime(agent_dir, launch.name,
                           error=f'Automatic wrapper cleanup failed: {error}; use kill AGENT to retry',
                           wrapper_cleanup={'state': 'failed', 'reason': str(error)})


def schedule_wrapper_cleanup(launch):
    agent_dir = launch.parent.parent
    try:
        with lock(agent_dir / '.launch.lock'):
            facts = runtime(agent_dir)
            if facts.get('launch_id') != launch.name or facts.get('ending_at'):
                return
            update_runtime(agent_dir, launch.name, wrapper_cleanup={'state': 'pending'})
            subprocess.Popen([sys.executable, str(Path(__file__).resolve()), '--cleanup-wrapper', str(launch)],
                             stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                             start_new_session=True)
    except (OSError, ValueError) as error:
        if runtime(agent_dir).get('launch_id') == launch.name:
            update_runtime(agent_dir, launch.name,
                           error=f'Automatic wrapper cleanup failed: {error}; use kill AGENT to retry',
                           wrapper_cleanup={'state': 'failed', 'reason': str(error)})


class UsagePublisher:
    """Publish adapter observations only into their exact running launch."""
    def __init__(self, agent_dir, launch_id):
        self.agent_dir, self.launch_id = agent_dir, launch_id

    def __call__(self, usage):
        try:
            facts = read(self.agent_dir / 'runtime.json')
            properties = read(self.agent_dir / 'properties.json')
            if (facts.get('launch_id') != self.launch_id or facts.get('state') != 'running'
                    or not usage.get('native_session_id')
                    or properties.get('native_session_id') != usage['native_session_id']):
                return
            previous = facts.get('usage')
            previous = previous if isinstance(previous, dict) else {}
            if usage != {key: value for key, value in previous.items() if key != 'reported_at'}:
                update_runtime(self.agent_dir, self.launch_id, usage={**usage, 'reported_at': now()})
        except (OSError, ValueError, TypeError):
            pass  # A stale launch or unavailable runtime cannot affect native work.


class RelayPublisher:
    """Keep fatal transport loss separate from optional launch diagnostics."""
    def __init__(self, agent_dir, launch_id):
        self.agent_dir, self.launch_id = agent_dir, launch_id

    def __call__(self, detail):
        try:
            bound = read(self.agent_dir / 'properties.json').get('native_session_id')
        except FileNotFoundError:
            return  # Removed agents have no transport state to publish.
        recovery = ('Kill then resume the agent; native work may still be running.' if bound else
                    'Kill this unbound launch and spawn again.')
        reason = 'Native relay lost. ' + recovery
        def changes(facts):
            if facts.get('state') not in ('starting', 'running'):
                return None
            # Legacy records may not yet have an independent native status.
            # Save it before transport loss makes the display unknown.
            result = {'status': 'unknown', 'native_relay': {
                'state': 'lost', 'reason': reason, 'detail': detail, 'at': now()}}
            known = facts.get('native_status_value', facts.get('status'))
            if known in ('working', 'waiting', 'idle'):
                result['native_status_value'] = known
            return result
        try:
            if mutate_runtime(self.agent_dir, self.launch_id, changes) is None:
                return
        except (FileNotFoundError, ValueError):
            return  # A replaced launch cannot publish transport loss.
        from multiverse.runtime.recovery import schedule
        schedule(self.agent_dir, self.launch_id)


class StatusWatcher:
    """Core-owned observer, bounded by one terminal launch and exact binding."""
    def __init__(self, agent_dir, launch_id, root):
        self.agent_dir, self.launch_id, self.root = agent_dir, launch_id, root
        self.stopped = threading.Event()
        self.thread = threading.Thread(target=self._run, name='native-status', daemon=True)

    def start(self):
        self.thread.start()

    def close(self):
        self.stopped.set()
        self.thread.join(5)
        if self.thread.is_alive():
            raise TimeoutError('Native status connection is still closing')

    def _run(self):
        try:
            asyncio.run(self._observe())
        except Exception:
            # Optional observations must not take down the interactive terminal.
            self._publish('unknown')

    def _publish(self, status):
        if status not in ('working', 'waiting', 'idle', 'unknown'):
            status = 'unknown'
        try:
            def changes_for(facts):
                if facts.get('state') != 'running':
                    return None
                known = facts.get('native_status_value', facts.get('status'))
                changes = {}
                if known in ('working', 'waiting', 'idle'):
                    changes['native_status_value'] = known
                if status != 'unknown':
                    changes['native_status_value'] = status
                    if known != status:
                        changes.update(recovery_snapshot=None,
                                       native_status_version=facts.get('native_status_version', 0) + 1)
                # An optional observer never restores the primary connection.
                lost = (facts.get('native_relay') or {}).get('state') == 'lost'
                changes['status'] = 'unknown' if lost else status
                return changes if any(facts.get(key) != value for key, value in changes.items()) else None
            mutate_runtime(self.agent_dir, self.launch_id, changes_for)
        except (OSError, ValueError):
            pass  # A new launch or a removed agent can never inherit this stream.

    async def _observe(self):
        from multiverse import harnesses
        from multiverse.runtime import rpc
        from multiverse.harnesses.types import SessionHandle
        properties = read(self.agent_dir / 'properties.json')
        adapter = harnesses.load(self.root, properties['harness'])
        describe = getattr(adapter, 'status_watch', None)
        if describe is None:
            return
        task = None
        native_id = None
        try:
            while not self.stopped.is_set():
                facts = read(self.agent_dir / 'runtime.json')
                properties = read(self.agent_dir / 'properties.json')
                if facts.get('launch_id') != self.launch_id or facts.get('state') not in ('starting', 'running'):
                    return
                if facts.get('state') == 'starting':
                    await asyncio.sleep(.1)
                    continue
                if task is not None:
                    if properties.get('native_session_id') != native_id or task.done():
                        return
                elif properties.get('native_session_id'):
                    session = SessionHandle.from_records(properties, facts, self.agent_dir)
                    specification = describe(session)
                    if specification is None:
                        return
                    native_id = session.native_id
                    from multiverse.runtime.attachments import connection_logger
                    task = asyncio.create_task(rpc.watch(*specification, self._publish,
                                              record_connection=connection_logger(self.agent_dir / 'launches' / self.launch_id)))
                await asyncio.sleep(1)
        finally:
            if task is not None:
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)
            self._publish('unknown')


class StartupDelivery:
    """Deliver this launch's durable startup queue once its bridge is ready."""
    def __init__(self, agent_dir, launch_id, root, prompt_protocol=None):
        self.agent_dir, self.launch_id, self.root = agent_dir, launch_id, root
        self.prompt_protocol = prompt_protocol
        self.stopped = threading.Event()
        self.thread = threading.Thread(target=self._run, name='startup-delivery', daemon=True)

    def start(self):
        self.thread.start()

    def close(self):
        self.stopped.set()
        self.thread.join(5)
        if self.thread.is_alive():
            raise TimeoutError('Startup delivery is still closing')

    def _run(self):
        from multiverse.runtime import lifecycle
        from multiverse import harnesses
        from multiverse.runtime import native
        error = 'Launch ended before startup delivery; use deliver AGENT --retry'
        try:
            adapter = harnesses.load(self.root, read(self.agent_dir / 'properties.json')['harness'])
            while not self.stopped.is_set():
                result = lifecycle.wait_ready(self.root, self.agent_dir, self.launch_id, adapter, .5, include_delivery=False)
                if result['ready']:
                    if self.prompt_protocol is not None:
                        if self.prompt_protocol.prompt_error:
                            error = 'Interactive starting prompt failed: ' + self.prompt_protocol.prompt_error
                            break
                        if not self.prompt_protocol.prompt_submitted:
                            self.stopped.wait(.05)
                            continue
                    error = None
                    break
                if not result.get('timed_out'):
                    error = result['error'] + '; startup messages remain saved for deliver AGENT --retry'
                    break
        except Exception as failure:
            error = f'Startup delivery failed: {failure}; messages remain saved for deliver AGENT --check'
        try:
            facts = runtime(self.agent_dir)
            if error and facts.get('launch_id') == self.launch_id and facts.get('reason'):
                error = facts['reason'] + '\nStartup messages remain saved for deliver AGENT --retry'
            native.flush_startup(self.root, self.agent_dir, self.launch_id, error)
        except Exception as failure:
            try:
                update_runtime(self.agent_dir, self.launch_id, startup_delivery='failed', delivery_error=str(failure))
            except (OSError, ValueError):
                pass


class RefusalMonitor:
    """Observe only this owned pane's current visible native startup screen."""
    def __init__(self, agent_dir, launch_id, root, adapter, executable):
        self.agent_dir, self.launch_id, self.root = agent_dir, launch_id, root
        self.describe, self.executable = adapter.launch_refusal, executable
        self.supports = getattr(adapter, 'supports_launch_refusal', None)
        self.started = getattr(adapter, 'launch_started', None)
        self.stopped = threading.Event()
        self.thread = threading.Thread(target=self._run, name='native-launch-refusal', daemon=True)

    def start(self):
        self.thread.start()

    def close(self):
        self.stopped.set()
        self.thread.join(5)
        if self.thread.is_alive():
            raise TimeoutError('Native refusal observation is still closing')

    def _settled(self, state):
        def changes(facts):
            if (facts.get('state') not in ('starting', 'running') or facts.get('ending_at')
                    or facts.get('native_refusal') or (facts.get('attachment') or {}).get('readiness') != 'process'):
                return None
            return {'native_startup_observation': {'state': state, 'at': now()}}
        try:
            if mutate_runtime(self.agent_dir, self.launch_id, changes) is not None:
                from multiverse.runtime.lifecycle import note_ready
                note_ready(self.root, self.agent_dir, self.launch_id)
        except (OSError, ValueError):
            pass  # Never recreate a removed launch or follow a replacement.

    def _run(self):
        from multiverse.plugins.harness import version_of
        from multiverse.runtime import native, refusal
        from multiverse.terminal.tmux import owned
        deadline = time.monotonic() + REFUSAL_STARTUP_SECONDS
        facts = runtime(self.agent_dir)
        if (facts.get('launch_id') != self.launch_id or facts.get('ending_at')
                or facts.get('state') not in ('starting', 'running')
                or (facts.get('native_relay') or {}).get('state') == 'lost'):
            return
        # Count setup time too, so the bounded observation finishes before a
        # default readiness wait begun after setup. Freeze the wall-clock age
        # into a monotonic deadline once; later clock changes cannot extend it.
        try:
            began = datetime.fromisoformat(facts['started_at'])
            age = (datetime.now(timezone.utc) - began).total_seconds()
            deadline -= min(REFUSAL_STARTUP_SECONDS, max(0, age))
        except (KeyError, TypeError, ValueError, OverflowError):
            pass
        versions = facts.get('native_versions')
        version = (versions.get('cli') if isinstance(versions, dict) else None) or version_of(self.executable)
        if self.supports is not None and not self.supports(version):
            self._settled('unsupported')
            return  # No known native layout: do no pane observation or launch locking.
        while not self.stopped.is_set() and time.monotonic() < deadline:
            try:
                # Never wait behind a long send/stop. The snapshot is exact and
                # contains no scrollback, command transcript or inferred status.
                with native._snapshot_lock(self.agent_dir):
                    facts = runtime(self.agent_dir)
                    if (facts.get('launch_id') != self.launch_id or facts.get('ending_at')
                            or facts.get('state') not in ('starting', 'running') or facts.get('native_refusal')
                            or (facts.get('native_relay') or {}).get('state') == 'lost'):
                        return
                    if ((facts.get('attachment') or {}).get('readiness') in ('binding', 'legacy-binding')
                            and facts.get('state') == 'running'
                            and (facts.get('native_relay') or {}).get('state') == 'ready'):
                        return  # Structured binding succeeded; later navigation is not a launch refusal.
                    properties = read(self.agent_dir / 'properties.json')
                    backend, pane = owned(properties, facts)
                    text = backend.run('capture-pane', '-p', '-t', pane)[-32768:]
                    cursor = backend.run('display-message', '-p', '-t', pane, '#{cursor_flag}\t#{cursor_x}\t#{cursor_y}\t#{pane_width}').split('\t')
                    # Recheck tags after both observations as well as before.
                    owned(properties, facts)
                x = y = width = None
                if len(cursor) in (3, 4):
                    try:
                        x, y = int(cursor[1]), int(cursor[2])
                    except ValueError:
                        pass  # Unknown position cannot identify a successful composer.
                if len(cursor) == 4:
                    try:
                        width = int(cursor[3])
                        if width <= 0:
                            width = None
                    except ValueError:
                        pass
                snapshot = {'cli_version': version, 'text': text,
                            'cursor_visible': {'0': False, '1': True}.get(cursor[0]) if len(cursor) in (3, 4) else None,
                            'cursor_x': x, 'cursor_y': y, 'width': width, 'native_id': properties.get('native_session_id')}
                evidence = self.describe(snapshot)
                if evidence is not None:
                    refusal.record(self.agent_dir, self.launch_id, evidence)
                    return
                if callable(self.started) and self.started(snapshot) is True:
                    self._settled('started')
                    return  # Settles startup observation only; no binding or permission authority.
            except (OSError, ValueError, RuntimeError, KeyError, subprocess.SubprocessError):
                pass  # Unavailable or unknown evidence cannot identify a refusal.
            self.stopped.wait(min(.25, max(0, deadline - time.monotonic())))
        if not self.stopped.is_set():
            self._settled('unverified')


def launch_environment(request):
    env = {key: value for key, value in os.environ.items() if not key.startswith('MV2_')}
    for key in request.get('unset_env', []):
        env.pop(key, None)
    env.update(request['env'])
    cli_prefix = str(Path(env['MV2_CLI']).parent) + os.pathsep
    if not env.get('PATH', '').startswith(cli_prefix):
        env['PATH'] = cli_prefix + env.get('PATH', '')
    return env


def main():
    launch = Path(sys.argv[1])
    agent_dir = launch.parent.parent
    process = None
    service_dir = None
    watcher = None
    proxy = None
    startup = None
    recovery_monitor = None
    refusal_monitor = None
    startup_checked = False
    exited = False  # agent.exited recorded: once per launch, whichever way the runner ends
    remove_wrapper = False
    request = {}
    diagnostic = {}
    roles = {}
    from multiverse.runtime import attachments, refusal, registration
    runner_started = process_started(os.getpid())
    attachments.begin(launch, runner_started)

    def stop(_signum, _frame):
        raise SystemExit(128 + _signum)

    previous_handlers = {signum: signal.signal(signum, stop) for signum in (signal.SIGHUP, signal.SIGTERM)}
    # The native CLI decides whether Ctrl+C interrupts a turn or exits. A caught
    # handler resets to the default on exec, unlike inheriting SIG_IGN.
    previous_handlers[signal.SIGINT] = signal.signal(signal.SIGINT, lambda *_: None)
    try:
        registration.wait_for_release(launch)
        request = read(launch / "request.json")
        env = launch_environment(request)
        if request.get('rpc_proxy') or (request.get('control_endpoint') and not request.get('shared_service')):
            endpoint = Path(request['rpc_proxy']['socket_path'] if request.get('rpc_proxy') else request['control_endpoint']['address'])
            if endpoint != Path('/tmp') / ('mv2-native-' + launch.name) / 'rpc.sock':
                raise ValueError('Native control socket does not belong to this launch')
            service_dir = endpoint.parent
        # A bridge resume already has a saved ID, but it must validate the new
        # native reply (including requested grants) before accepting more input.
        attachment = request.get('attachment') or {}
        if 'attachment' not in request and request.get('rpc_proxy'):
            # Old saved requests keep binding readiness but gain no recovery.
            from dataclasses import asdict
            from multiverse.runtime.native import legacy_attachment
            attachment = asdict(legacy_attachment(request.get('shared_service')))
        update_runtime(agent_dir, launch.name, state="starting" if attachment.get('readiness') in ('binding', 'legacy-binding') else "running", status="unknown",
                       runner_pid=os.getpid(), runner_started=runner_started,
                       startup_checked=False, attachment=attachment or None)
        if request.get('rpc_proxy'):
            from multiverse import harnesses
            from multiverse.runtime import rpc
            properties = read(agent_dir / 'properties.json')
            adapter = harnesses.load(Path(env['MV2_UNIVERSE']), properties['harness'])
            def bound(native_id):
                if runtime(agent_dir).get('native_refusal'):
                    raise ValueError(runtime(agent_dir)['native_refusal']['message'])
                if not request['rpc_proxy'].get('binds_session') and attachment.get('readiness') != 'legacy-binding':
                    raise ValueError('Relay did not declare validated session binding')
                bind_session(Path(env['MV2_UNIVERSE']), agent_dir.name, native_id, launch.name)
                update_runtime(agent_dir, launch.name, state='running', native_relay={'state': 'ready'})
                from multiverse.runtime.recovery import reconnected
                reconnected(agent_dir, launch.name)
            proxy = rpc.LaunchProxy(request['rpc_proxy'], adapter.launch_proxy(request['rpc_proxy']['settings']),
                bound,
                lambda error: update_runtime(agent_dir, launch.name, error='Native launch relay: ' + error),
                UsagePublisher(agent_dir, launch.name), RelayPublisher(agent_dir, launch.name),
                record_refusal=lambda evidence: refusal.record(agent_dir, launch.name, evidence),
                record_connection=attachments.connection_logger(launch, started=runner_started))
            roles['relay'] = attachments.Role(launch, 'relay', started=runner_started)
            try:
                proxy.start()
            except Exception as error:
                fallback = request.get('fallback')
                if fallback is None or getattr(proxy.protocol, 'bound', False):
                    raise
                proxy.close()
                roles['relay'].close()
                proxy = None
                selection = {**(fallback.get('native_mode') or {}), 'fallback_reason': f'Native launch relay unavailable: {error}'}
                request = {**request, 'argv': fallback['argv'], 'unset_env': fallback['unset_env'],
                           'env': {**fallback.get('environment', {}), **request['env']},
                           'control_endpoint': None, 'shared_service': False, 'rpc_proxy': None,
                           'native_mode': selection, 'fallback': None, 'attachment': fallback.get('attachment')}
                write(launch / 'request.json', request)
                env = launch_environment(request)
                # A sender may have queued input while relay startup was still
                # pending. Plain mode cannot deliver it; retain a retryable failure
                # atomically with disabling the startup queue for future senders.
                with lock(agent_dir / '.launch.lock'):
                    facts = runtime(agent_dir)
                    if facts.get('launch_id') != launch.name or facts.get('ending_at'):
                        raise ValueError('Launch changed or ended before terminal fallback')
                    from multiverse.terminal.tmux import owned
                    backend, pane = owned(properties, facts)
                    selected_mouse = bool((fallback.get('attachment') or {}).get('mouse'))
                    if selected_mouse != bool(attachment.get('mouse')):
                        if selected_mouse:
                            backend.run('set-option', '-t', pane, 'mouse', 'on')
                        else:
                            # Restore inherited behavior after our launch override.
                            backend.run('set-option', '-u', '-t', pane, 'mouse')
                    update_runtime(agent_dir, launch.name, state='running', control_endpoint=None,
                                   startup_delivery=None, native_mode=selection, native_relay=None, error=None,
                                   native_service=None, attachment=fallback.get('attachment'),
                                   # Recorded by lifecycle for the fallback; no plugin code runs here.
                                   plugins=fallback.get('plugins') or {'plugins': {}, 'status_items': []})
                    with lock(agent_dir / '.properties.lock'):
                        current_properties = read(agent_dir / 'properties.json')
                        if request.get('previous_agent_identity') is None:
                            current_properties.pop('agent_identity', None)
                        else:
                            current_properties['agent_identity'] = request['previous_agent_identity']
                        if request.get('previous_agent_environment') is None:
                            current_properties.pop('agent_environment', None)
                        else:
                            current_properties['agent_environment'] = request['previous_agent_environment']
                        write(agent_dir / 'properties.json', current_properties)
                    from multiverse.universe.mailbox import end_startup_messages
                    end_startup_messages(agent_dir, launch.name,
                        'Launch selected plain mode before delivery; message saved. '
                        'Use deliver AGENT --retry after a bridge resume or when the agent is stopped.')
                if service_dir:
                    shutil.rmtree(service_dir, ignore_errors=True)
                    service_dir = None
                attachment = fallback.get('attachment') or {}
        # The native UI owns this terminal; only its launch and interactive attach inherit stdin.
        if request.get('observe_refusal') and attachment.get('readiness') == 'process':
            update_runtime(agent_dir, launch.name, native_startup_observation={'state': 'pending'})
        process = subprocess.Popen(request["argv"], env=env, stdin=None)
        update_runtime(agent_dir, launch.name, pid=process.pid, pid_started=process_started(process.pid))
        if request.get('observe_refusal'):
            from multiverse import harnesses
            adapter = harnesses.load(Path(env['MV2_UNIVERSE']), read(agent_dir / 'properties.json')['harness'])
            refusal_monitor = RefusalMonitor(agent_dir, launch.name, Path(env['MV2_UNIVERSE']), adapter, request['argv'][0])
            roles['refusal observer'] = attachments.Role(launch, 'refusal observer', started=runner_started)
            refusal_monitor.start()
        from multiverse.universe.mailbox import finish_wake
        if request.get('control_endpoint'):
            startup = StartupDelivery(agent_dir, launch.name, Path(env['MV2_UNIVERSE']),
                                      proxy.protocol if proxy is not None and request.get('resume_message_id') else None)
            roles['startup delivery'] = attachments.Role(launch, 'startup delivery', started=runner_started)
            startup.start()
        if attachment.get('observe_status'):
            watcher = StatusWatcher(agent_dir, launch.name, Path(env['MV2_UNIVERSE']))
            roles['status observer'] = attachments.Role(launch, 'status observer', started=runner_started)
            watcher.start()
        if attachment.get('recovery') == 'idle-existing-service':
            from multiverse.runtime.recovery import RecoveryMonitor
            recovery_monitor = RecoveryMonitor(agent_dir, launch.name, Path(env['MV2_UNIVERSE']))
            roles['recovery monitor'] = attachments.Role(launch, 'recovery monitor', started=runner_started)
            recovery_monitor.start()
        try:
            code = process.wait(timeout=STARTUP_SECONDS)
        except subprocess.TimeoutExpired:
            startup_checked = True
            native_refusal = runtime(agent_dir).get('native_refusal')
            finish_wake(agent_dir, launch.name, request.get('resume_message_id'), not native_refusal,
                        native_refusal['message'] if native_refusal else '', uncertain=bool(native_refusal))
            update_runtime(agent_dir, launch.name, startup_checked=True)
            from multiverse.runtime.lifecycle import note_ready
            note_ready(Path(env['MV2_UNIVERSE']), agent_dir, launch.name)
            code = process.wait()
        ending = runtime(agent_dir).get('ending_at')
        remove_wrapper = not ending and (startup_checked or code in (-signal.SIGINT, 130))
        diagnostic = {}
        if not ending and (code != 0 or not startup_checked):
            reason = f'Native process exited with code {code}' + (' during startup.' if not startup_checked else '.')
            diagnostic = launch_diagnostic(agent_dir, launch.name, reason)
        update_runtime(agent_dir, launch.name,
                       state="stopped" if code == 0 or ending else "failed",
                       exit_code=code, status="unknown", stopped_at=now(), **diagnostic)
        # Once, here, where the process's end is known; later cleanup writes record nothing.
        from multiverse.runtime.lifecycle import _event
        exited = True
        _event(agent_dir, 'agent.exited', launch.name, {
            'state': "stopped" if code == 0 or ending else "failed", 'exit_code': code,
            'reason': diagnostic.get('reason')})
        if watcher is not None:
            watcher.close()
            roles['status observer'].close()
            watcher = None
        return code
    except Exception as error:
        print(f"Multiverse launch failed: {error}", file=sys.stderr)
        diagnostic = launch_diagnostic(agent_dir, launch.name, f'Multiverse launch failed: {error}')
        update_runtime(agent_dir, launch.name, state="failed", status="unknown", error=str(error), **diagnostic)
        if not exited:  # failed before the process ended, or before it could start: still its one exit
            from multiverse.runtime.lifecycle import _event
            _event(agent_dir, 'agent.exited', launch.name, {
                'state': 'failed', 'exit_code': None, 'reason': f'Multiverse launch failed: {error}'})
        return 1
    finally:
        cleanup_errors = []
        if request.get('resume_message_id') and not startup_checked:
            try:
                from multiverse.universe.mailbox import finish_wake
                finish_wake(agent_dir, launch.name, request['resume_message_id'], False,
                            diagnostic.get('reason') or 'Runner ended before launch readiness',
                            uncertain=process is not None)
            except Exception as error:
                cleanup_errors.append('startup bookkeeping: ' + str(error))
        def close_role(name, instance):
            if instance is None:
                return
            try:
                instance.close()
                roles[name].close()
            except Exception as error:
                cleanup_errors.append(f'{name}: {error}')
        if startup is not None:
            close_role('startup delivery', startup)
        elif request.get('control_endpoint'):
            try:
                from multiverse.runtime.native import flush_startup
                flush_startup(Path(request['env']['MV2_UNIVERSE']), agent_dir, launch.name,
                              diagnostic.get('reason') or 'Runner ended before bridge readiness; use deliver AGENT --retry')
            except Exception as error:
                cleanup_errors.append('startup bookkeeping: ' + str(error))
        close_role('refusal observer', refusal_monitor)
        close_role('relay', proxy)
        close_role('status observer', watcher)
        close_role('recovery monitor', recovery_monitor)
        if process is not None and process.poll() is None:
            process.terminate()
            try:
                process.wait(timeout=2)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait()
        # Shared services deliberately outlive terminal clients and monitors.
        if service_dir:
            shutil.rmtree(service_dir, ignore_errors=True)
        attachments.finish(launch, cleanup_errors)
        if remove_wrapper:
            schedule_wrapper_cleanup(launch)
        for signum, handler in previous_handlers.items():
            signal.signal(signum, handler)


if __name__ == "__main__":
    if sys.argv[1:2] == ['--cleanup-wrapper']:
        cleanup_wrapper(Path(sys.argv[2]))
    else:
        sys.exit(main())
