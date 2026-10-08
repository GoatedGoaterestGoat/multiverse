"""One terminal child per launch; no central supervisor or model loop."""
import asyncio
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

from multiverse.universe.agents import update_runtime, bind_session, runtime
from multiverse.store import lock, now, read, write


STARTUP_SECONDS = 2
OUTPUT_LINES = 40
OUTPUT_BYTES = 8192


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
        facts = runtime(self.agent_dir)
        if facts.get('launch_id') != self.launch_id or facts.get('state') not in ('starting', 'running'):
            return
        bound = read(self.agent_dir / 'properties.json').get('native_session_id')
        recovery = ('Kill then resume the agent; native work may still be running.' if bound else
                    'Kill this unbound launch and spawn again.')
        reason = 'Native relay lost. ' + recovery
        update_runtime(self.agent_dir, self.launch_id, status='unknown', native_relay={
            'state': 'lost', 'reason': reason, 'detail': detail, 'at': now()})
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
        self.thread.join()

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
            facts = read(self.agent_dir / 'runtime.json')
            if (facts.get('launch_id') == self.launch_id and facts.get('state') == 'running'
                    and facts.get('status') != status):
                changes = {}
                if status != 'unknown':
                    changes = {'recovery_snapshot': None,
                               'native_status_version': facts.get('native_status_version', 0) + 1}
                update_runtime(self.agent_dir, self.launch_id, status=status, **changes)
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
                    task = asyncio.create_task(rpc.watch(*specification, self._publish))
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
        self.thread.join()

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
    startup_checked = False
    remove_wrapper = False
    request = {}
    diagnostic = {}

    def stop(_signum, _frame):
        raise SystemExit(128 + _signum)

    previous_handlers = {signum: signal.signal(signum, stop) for signum in (signal.SIGHUP, signal.SIGTERM)}
    # The native CLI decides whether Ctrl+C interrupts a turn or exits. A caught
    # handler resets to the default on exec, unlike inheriting SIG_IGN.
    previous_handlers[signal.SIGINT] = signal.signal(signal.SIGINT, lambda *_: None)
    try:
        deadline = time.monotonic() + 15
        while not (launch / "ready").exists():
            if time.monotonic() > deadline:
                raise RuntimeError("Spawn did not finish registering this pane")
            time.sleep(0.05)
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
                       runner_pid=os.getpid(), startup_checked=False, attachment=attachment or None)
        if request.get('rpc_proxy'):
            from multiverse import harnesses
            from multiverse.runtime import rpc
            properties = read(agent_dir / 'properties.json')
            adapter = harnesses.load(Path(env['MV2_UNIVERSE']), properties['harness'])
            def bound(native_id):
                if not request['rpc_proxy'].get('binds_session') and attachment.get('readiness') != 'legacy-binding':
                    raise ValueError('Relay did not declare validated session binding')
                bind_session(Path(env['MV2_UNIVERSE']), agent_dir.name, native_id, launch.name)
                update_runtime(agent_dir, launch.name, state='running', native_relay={'state': 'ready'})
                from multiverse.runtime.recovery import reconnected
                reconnected(agent_dir, launch.name)
            proxy = rpc.LaunchProxy(request['rpc_proxy'], adapter.launch_proxy(request['rpc_proxy']['settings']),
                bound,
                lambda error: update_runtime(agent_dir, launch.name, error='Native launch relay: ' + error),
                UsagePublisher(agent_dir, launch.name), RelayPublisher(agent_dir, launch.name))
            try:
                proxy.start()
            except Exception as error:
                fallback = request.get('fallback')
                if fallback is None or getattr(proxy.protocol, 'bound', False):
                    raise
                proxy.close()
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
                                   native_service=None, attachment=fallback.get('attachment'))
                    from multiverse.universe.mailbox import end_startup_messages
                    end_startup_messages(agent_dir, launch.name,
                        'Launch selected plain mode before delivery; message saved. '
                        'Use deliver AGENT --retry after a bridge resume or when the agent is stopped.')
                if service_dir:
                    shutil.rmtree(service_dir, ignore_errors=True)
                    service_dir = None
                attachment = fallback.get('attachment') or {}
        process = subprocess.Popen(request["argv"], env=env)
        update_runtime(agent_dir, launch.name, pid=process.pid)
        from multiverse.universe.mailbox import finish_wake
        if request.get('control_endpoint'):
            startup = StartupDelivery(agent_dir, launch.name, Path(env['MV2_UNIVERSE']),
                                      proxy.protocol if proxy is not None and request.get('resume_message_id') else None)
            startup.start()
        if attachment.get('observe_status'):
            watcher = StatusWatcher(agent_dir, launch.name, Path(env['MV2_UNIVERSE']))
            watcher.start()
        if attachment.get('recovery') == 'idle-existing-service':
            from multiverse.runtime.recovery import RecoveryMonitor
            recovery_monitor = RecoveryMonitor(agent_dir, launch.name, Path(env['MV2_UNIVERSE']))
            recovery_monitor.start()
        try:
            code = process.wait(timeout=STARTUP_SECONDS)
        except subprocess.TimeoutExpired:
            startup_checked = True
            finish_wake(agent_dir, launch.name, request.get('resume_message_id'), True)
            update_runtime(agent_dir, launch.name, startup_checked=True)
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
        if watcher is not None:
            watcher.close()
            watcher = None
        return code
    except Exception as error:
        print(f"Multiverse launch failed: {error}", file=sys.stderr)
        diagnostic = launch_diagnostic(agent_dir, launch.name, f'Multiverse launch failed: {error}')
        update_runtime(agent_dir, launch.name, state="failed", status="unknown", error=str(error), **diagnostic)
        return 1
    finally:
        if request.get('resume_message_id') and not startup_checked:
            from multiverse.universe.mailbox import finish_wake
            finish_wake(agent_dir, launch.name, request['resume_message_id'], False,
                        diagnostic.get('reason') or 'Runner ended before launch readiness',
                        uncertain=process is not None)
        if startup is not None:
            startup.close()
        elif request.get('control_endpoint'):
            from multiverse.runtime.native import flush_startup
            flush_startup(Path(request['env']['MV2_UNIVERSE']), agent_dir, launch.name,
                          diagnostic.get('reason') or 'Runner ended before bridge readiness; use deliver AGENT --retry')
        if proxy is not None:
            proxy.close()
        if watcher is not None:
            watcher.close()
        if recovery_monitor is not None:
            recovery_monitor.close()
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
        if remove_wrapper:
            schedule_wrapper_cleanup(launch)
        for signum, handler in previous_handlers.items():
            signal.signal(signum, handler)


if __name__ == "__main__":
    if sys.argv[1:2] == ['--cleanup-wrapper']:
        cleanup_wrapper(Path(sys.argv[2]))
    else:
        sys.exit(main())
