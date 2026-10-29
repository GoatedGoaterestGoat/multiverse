"""Disposable one-shot children; no native model or daemon is used."""
from concurrent.futures import ThreadPoolExecutor
from contextlib import redirect_stderr
from dataclasses import replace
import io
import json
import os
from pathlib import Path
import shutil
import signal
import subprocess
import sys
import threading
import time
from unittest.mock import patch
from uuid import uuid4

from multiverse.harnesses.types import (CommandSpec, ModelCheck, RunFailure, RunFile, RunOptions, RunPlan,
                                       RunReply, RunRequest)
from multiverse.plugins import access, agent_files
from multiverse.runtime import run_files, run_supervisor, runs
from multiverse.runtime.process import process_snapshot, process_started
from multiverse.tools import doctor
from isolation import track
from test_multiverse import BaseTest, until

REQUEST = RunRequest(messages=(), prompt='private prompt', system='private system', model='fake')


class Provider:
    name = 'fake'

    def __init__(self, code='print("answer")', *, files=True, overlay=None):
        self.code, self.files, self.overlay, self.environments, self.plans = code, files, overlay or {}, [], []

    def run_options(self, *, environment):
        self.environments.append(dict(environment))
        return RunOptions(supported=True, formats=('fake',), transport_retries=(0, 2))

    def check_model(self, model, *, effort, environment):
        self.environments.append(dict(environment))
        return ModelCheck(ok=model != 'bad', reason='unsupported model')

    def session_home(self, environment):
        return environment['STORE']

    def run_plan(self, request, *, environment, run):
        self.environments.append(dict(environment))
        self.plans.append(run)
        return RunPlan(command=CommandSpec((sys.executable, '-c', self.code), environment=self.overlay),
                       files=(RunFile(path='input', content=request.prompt.encode()),
                              RunFile(root='store', path='projects/' + run.id + '/' + run.id + '.jsonl',
                                      content=request.system.encode())) if self.files else (),
                       interpret=lambda outcome: RunReply(text=outcome.stdout, model='fake',
                                                          usage={'input': 3}, cost_usd=.001))


def stopped(pid):
    value = process_snapshot(pid)
    if value is not None:
        return value[1].startswith('Z')
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return True
    return False


class RunsTest(BaseTest):
    def setUp(self):
        super().setUp()
        self.environment = {'HOME': str(self.work / 'recorded-home'), 'PATH': os.environ['PATH'],
                            'STORE': str(self.work / 'store'), 'EMPTY': '', 'UNSET': None}

    def run_once(self, adapter=None, request=REQUEST, **kwargs):
        return runs.run_once(adapter or Provider(), request, root=self.root, plugin='fixture',
                             environment=self.environment, **kwargs)

    def no_content(self):
        self.assertEqual(list(runs.base().rglob('run.json')), [])
        self.assertEqual(list((self.work / 'store').rglob('*.jsonl')), [])

    def test_private_files_environment_and_parallel_calls_are_isolated(self):
        """Concurrent runs use separate private files and a scrubbed snapshot; a break can mix contexts or inherit
        a live agent's authority without changing the visible model answer."""
        before = {str(p): p.read_bytes() for p in self.root.rglob('*') if p.is_file()}
        code = ('import os,json,pathlib,stat\n'
                'p=pathlib.Path.cwd()\n'
                'print(json.dumps({"cwd":str(p),"mode":stat.S_IMODE(p.stat().st_mode),'
                '"input_mode":stat.S_IMODE((p/"input").stat().st_mode),"input":(p/"input").read_text(),'
                '"pgid":os.getpgrp(),"pid":os.getpid(),"env":dict(os.environ),'
                '"journal":json.loads((p/"run.json").read_text())}))\n')
        dirty = {'MV2_AGENT_ID': 'other', 'CLAUDE_CODE_SESSION_ID': 'other', 'CLAUDE_CODE_ENABLE_FUNCTION_HOOKS': 'yes',
                 'CODEX_THREAD_ID': 'other', 'KEEP': 'value'}
        self.environment.update(dirty)
        adapter = Provider(code, overlay=dirty)
        with ThreadPoolExecutor(max_workers=2) as pool:
            answers = list(pool.map(lambda _: self.run_once(adapter), range(2)))
        outputs = [json.loads(answer.text) for answer in answers]
        self.assertNotEqual(outputs[0]['cwd'], outputs[1]['cwd'])
        for answer, output in zip(answers, outputs):
            self.assertTrue(output['cwd'].startswith(str(runs.base()) + '/fixture/fake/'))
            self.assertNotIn('recorded-home', output['cwd'])
            self.assertEqual((output['mode'], output['input_mode']), (0o700, 0o600))
            self.assertEqual(output['input'], REQUEST.prompt)
            self.assertEqual(output['pgid'], output['pid'])
            self.assertNotEqual(output['pgid'], os.getpgrp())
            self.assertEqual(output['env']['EMPTY'], '')
            self.assertNotIn('UNSET', output['env'])
            self.assertEqual(output['env']['HOME'], self.environment['HOME'])
            self.assertEqual(output['env']['KEEP'], 'value')
            for key in dirty.keys() - {'KEEP'}:
                self.assertNotIn(key, output['env'])
            journal = json.dumps(output['journal'])
            for content in (REQUEST.prompt, REQUEST.system, 'print(', 'KEEP'):
                self.assertNotIn(content, journal)
            self.assertEqual(output['journal']['child']['pid'], output['pid'])
            self.assertTrue(answer.duration > 0)
        self.assertTrue(all(environment == adapter.environments[0] for environment in adapter.environments))
        self.assertEqual(before, {str(p): p.read_bytes() for p in self.root.rglob('*') if p.is_file()})
        self.no_content()

    def test_housekeeping_never_hides_a_paid_reply_or_writes_to_the_callers_terminal(self):
        """Housekeeping must not discard paid replies, write to the terminal or reject a dotfiles home."""
        for condition in ('sweep problem', 'dotfiles home', 'cleanup after success'):
            with self.subTest(condition):
                counter = self.work / ('calls-' + condition.replace(' ', '-'))
                code = ('from pathlib import Path\n' +
                        f'with Path({str(counter)!r}).open("a") as out: out.write("run\\n")\n' +
                        ('Path("native-scratch").write_text("unrecorded native file")\n' if condition == 'cleanup after success' else '') +
                        'print("paid answer")\n')
                provider = Provider(code)
                git = Path.home() / '.git'
                if condition == 'dotfiles home': git.mkdir()
                errors = io.StringIO()
                try:
                    with redirect_stderr(errors):
                        if condition == 'sweep problem':
                            with patch.object(run_files, 'leftovers', return_value=['unverified leftover']):
                                reply = self.run_once(provider)
                        else:
                            reply = self.run_once(provider)
                    self.assertEqual((reply.text, reply.usage, reply.cost_usd), ('paid answer\n', {'input': 3}, .001))
                    self.assertEqual(counter.read_text(), 'run\n')
                    self.assertEqual(errors.getvalue(), '')
                    if condition == 'cleanup after success':
                        directory = provider.plans[0].path
                        self.assertTrue((directory / 'run.json').is_file())
                        self.assertEqual((directory / 'native-scratch').read_text(), 'unrecorded native file')
                        self.assertTrue(any(str(directory) in problem for problem in runs.leftovers(self.root)))
                    else:
                        self.no_content()
                finally:
                    if git.exists(): git.rmdir()
                    for run in provider.plans:
                        if run.path.exists(): shutil.rmtree(run.path)

    def test_refusals_happen_before_content_or_processes(self):
        """A refused model, request, plan, scope or access level must spend nothing and create no files."""
        cancelled = threading.Event(); cancelled.set()
        rows = [('cancel', Provider(), REQUEST, {'cancel': cancelled}, RunFailure),
                ('model', Provider(), replace(REQUEST, model='bad'), {}, RunFailure),
                ('input bound', Provider(), replace(REQUEST, prompt='x' * (runs.INPUT_BYTES + 1)), {}, RunFailure),
                ('missing agent', Provider(), REQUEST, {'agent_directory': self.root / 'state/agents/missing'}, agent_files.AgentGone),
                ('outside agent', Provider(), REQUEST, {'agent_directory': self.work}, RunFailure)]
        with patch.object(runs.subprocess, 'Popen') as spawn:
            for label, provider, request, arguments, error in rows:
                with self.subTest(label):
                    with self.assertRaises(error) as caught:
                        self.run_once(provider, request, **arguments)
                    if label == 'input bound':
                        self.assertEqual(caught.exception.kind, 'too_long')
            with self.subTest('observe cannot spend'), access.within('observe', 'test'):
                with self.assertRaises(access.Denied):
                    self.run_once()
            with self.subTest('bad target'):
                provider = Provider()
                original = provider.run_plan
                provider.run_plan = lambda *a, **kw: replace(original(*a, **kw), files=(RunFile(path='../outside', content=b'x'),))
                with self.assertRaises(RunFailure):
                    self.run_once(provider)
            for label, destination in (('universe', self.root / 'runs'),
                                       ('agent', self.work / 'other/state/agents/someone/runs')):
                with self.subTest(base=label), patch.object(runs, 'base', return_value=destination):
                    with self.assertRaises(RunFailure) as caught:
                        self.run_once()
                    self.assertEqual(caught.exception.kind, 'unavailable')
                    self.assertFalse(destination.exists())
            spawn.assert_not_called()
        self.assertFalse(runs.base().exists())

    def test_collisions_and_linked_store_paths_are_never_overwritten(self):
        """An exclusive session file cannot follow a link or overwrite an existing conversation, even if that
        collision appears only after the run plan was built."""
        for collision in ('file', 'symlink'):
            with self.subTest(collision):
                provider = Provider()
                original = provider.run_plan
                protected = self.work / ('protected-' + collision)
                protected.mkdir()
                def plan(*args, **kwargs):
                    result = original(*args, **kwargs)
                    target = self.work / 'store' / result.files[1].path
                    if collision == 'file':
                        target.parent.mkdir(parents=True)
                        target.write_bytes(b'existing conversation')
                    else:
                        target.parent.parent.mkdir(parents=True, exist_ok=True)
                        target.parent.symlink_to(protected, target_is_directory=True)
                    provider.target = target
                    return result
                provider.run_plan = plan
                with self.assertRaises(RunFailure): self.run_once(provider)
                if collision == 'file': self.assertEqual(provider.target.read_bytes(), b'existing conversation')
                else: self.assertEqual(list(protected.iterdir()), [])
                self.assertEqual(list(runs.base().rglob('run.json')), [])

    def worker(self, marker, *, helper=False):
        # The native stand-in publishes atomically, after its own helper exists.
        return ('import os,pathlib,subprocess,sys,time,signal,json\n'
                'signal.signal(signal.SIGTERM, signal.SIG_IGN)\n' +
                ('child=subprocess.Popen([sys.executable,"-c","import signal,time;signal.signal(signal.SIGTERM,signal.SIG_IGN);time.sleep(60)"])\n'
                 if helper else 'child=None\n') +
                f'p=pathlib.Path({str(marker)!r});q=p.with_suffix(".pending")\n'
                'q.write_text(json.dumps([os.getpid(),child.pid if child else None]));q.replace(p)\n'
                'time.sleep(60)\n')

    def test_timeout_cancel_and_removal_stop_only_the_private_group(self):
        """Cancellation and removal must end a run's helpers too, without signalling other agents or recreating
        a removed folder; ordinary successful replies would not expose those leaks."""
        unrelated = subprocess.Popen([sys.executable, '-c', 'import time;time.sleep(60)'], start_new_session=True)
        track(pid=unrelated.pid)
        self.addCleanup(lambda: unrelated.wait(timeout=5))
        self.addCleanup(lambda: unrelated.terminate() if unrelated.poll() is None else None)
        with self.subTest('macOS may deny a signal to a zombie-only group'):
            zombie = subprocess.Popen([sys.executable, '-c', 'pass'], start_new_session=True)
            track(pid=zombie.pid)
            try:
                until(lambda: run_supervisor._exited(zombie))  # Observe without reaping the group leader.
                with patch.object(run_supervisor.os, 'killpg', side_effect=PermissionError):
                    self.assertTrue(run_supervisor._stop(zombie))
            finally:
                zombie.wait(timeout=5)
        for action in ('timeout', 'cancel', 'agent removed', 'universe replaced'):
            with self.subTest(action):
                marker = self.work / ('ready-' + action.replace(' ', '-'))
                agent = self.root / 'state/agents' / str(uuid4()); agent.mkdir()
                cancelled = threading.Event()
                with ThreadPoolExecutor(max_workers=1) as pool:
                    future = pool.submit(self.run_once, Provider(self.worker(marker, helper=True)),
                                         agent_directory=agent, timeout=3 if action == 'timeout' else 30, cancel=cancelled)
                    until(marker.exists)
                    pids = json.loads(marker.read_text())
                    for pid in pids: track(pid=pid)
                    moved = None
                    if action == 'cancel': cancelled.set()
                    if action == 'agent removed': agent.rename(agent.with_suffix('.moved'))
                    if action == 'universe replaced':
                        moved = self.root.with_name('moved-universe'); self.root.rename(moved); self.root.mkdir()
                    try:
                        expected = agent_files.Gone if 'removed' in action or 'replaced' in action else RunFailure
                        with self.assertRaises(expected) as caught: future.result(timeout=12)
                        if expected is RunFailure:
                            self.assertEqual(caught.exception.kind, 'cancelled' if action == 'cancel' else action)
                            self.assertTrue(caught.exception.confirmed)
                        for pid in pids: until(lambda: stopped(pid), seconds=5)
                        if action == 'agent removed': self.assertFalse(agent.exists())
                        if moved is not None: self.assertEqual(list(self.root.iterdir()), [])
                    finally:
                        if moved is not None:
                            self.root.rmdir(); moved.rename(self.root)
                self.assertIsNone(unrelated.poll())
                self.no_content()

    def test_parent_death_and_output_bounds_cannot_leave_a_writer(self):
        """The supervisor must outlive a killed plugin worker and clean its process and files, and must drain both
        output pipes with bounds instead of deadlocking or silently truncating a successful answer."""
        marker = self.work / 'parent-death-ready'
        boot = self.work / 'caller.py'
        boot.write_text('from test_runs import Provider,REQUEST\nfrom multiverse.runtime import runs\n' +
                        f'runs.run_once(Provider({self.worker(marker)!r}),REQUEST,root={str(self.root)!r},plugin="fixture",'
                        f'environment={self.environment!r},timeout=30)\n')
        caller = subprocess.Popen([sys.executable, str(boot)], env={**os.environ,
            'PYTHONPATH': os.pathsep.join((str(Path(__file__).resolve().parent), str(Path(runs.__file__).parents[2])))},
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True)
        track(pid=caller.pid)
        try:
            until(marker.exists)
            pid = json.loads(marker.read_text())[0]; track(pid=pid)
            journal = next(runs.base().rglob('run.json'))
            track(pid=json.loads(journal.read_text())['supervisor']['pid'])
            caller.kill(); caller.wait(timeout=5)
            until(lambda: stopped(pid), seconds=10)
            until(lambda: not list(runs.base().rglob('run.json')), seconds=10)
        finally:
            if caller.poll() is None: caller.kill()
            caller.wait(timeout=5)
        for name, limit in (('stdout', 16 << 20), ('stderr', 64 << 10)):
            with self.subTest(name):
                code = f'import os,time;os.write({1 if name == "stdout" else 2}, b"s" * {limit + 1});time.sleep(60)'
                with self.assertRaises(RunFailure) as caught: self.run_once(Provider(code), timeout=15)
                self.assertIn('size bound', caught.exception.reason)
                self.assertNotIn('ssss', caught.exception.reason)
                self.no_content()

    def test_cleanup_proof_and_doctor_preserve_uncertain_targets(self):
        """A leftover cleanup needs dead producer identities and unchanged files; otherwise it can delete a
        replacement's data, while doctor must never mutate the evidence it reports."""
        token = process_started(os.getpid())
        dead = {'pid': 2147483647, 'started': token}
        for condition in ('dead', 'live', 'owner live', 'ended owner live', 'ended supervisor live',
                          'ended child live', 'unknown', 'file replaced', 'symlink', 'missing file'):
            with self.subTest(condition):
                directory = runs.base() / 'fixture/fake' / str(uuid4())
                record = {'run_id': directory.name, 'directory': str(directory), 'universe': str(self.root),
                          'store': None, 'declared': [{'root': 'run', 'path': 'input'}], 'owner': dead,
                          'supervisor': {'pid': os.getpid(), 'started': token} if condition == 'live' else dead,
                          'child': None}
                run_files.create(directory, record)
                run_files.write_file(directory, record, directory / 'input', b'secret')
                if condition.startswith('ended '): record['phase'] = 'ended'
                for key in ('owner', 'supervisor', 'child'):
                    if condition in (key + ' live', 'ended ' + key + ' live'):
                        record[key] = {'pid': os.getpid(), 'started': token}
                if condition == 'unknown': record['owner'] = {'pid': 2147483647}
                if condition == 'file replaced':
                    (directory/'input').rename(directory/'saved'); (directory/'input').write_text('someone else')
                if condition == 'symlink':
                    (directory/'input').unlink(); (directory/'input').symlink_to(self.work / 'outside')
                if condition == 'missing file': (directory/'input').unlink()
                run_files.save(directory, record)
                before = (directory/'run.json').read_bytes()
                with patch('multiverse.runtime.lifecycle.listing', return_value=[]), patch.object(doctor, '_plugins', return_value=[]):
                    found = doctor.check(self.root)
                self.assertEqual((directory/'run.json').read_bytes(), before)
                active = condition in ('live', 'owner live', 'ended supervisor live', 'ended child live')
                self.assertEqual(any(str(directory) in a for a, _ in found), not active)
                problems = run_files.leftovers(runs.base(), sweep=True)
                if condition in ('dead', 'missing file', 'ended owner live'):
                    self.assertFalse(directory.exists(), problems)
                else:
                    self.assertTrue(directory.exists())
                    if condition == 'file replaced': self.assertEqual((directory/'input').read_text(), 'someone else')
                    if condition == 'symlink': self.assertTrue((directory/'input').is_symlink())
                    shutil.rmtree(directory)
