"""Private process relay and disposable-tree ownership, with Python children only."""
import json
import os
from pathlib import Path
import subprocess
import sys
import threading

from multiverse.harnesses.types import CommandSpec, RunFailure, RunFile, RunLink, RunPlan, RunReply
from multiverse.runtime import runs
from isolation import track
from test_runs import Provider, REQUEST, stopped
from test_multiverse import BaseTest, until


CHILD = '''import json,os,pathlib,shutil,signal,sys,time
root=pathlib.Path('private')
(root/'native'/'deep').mkdir(parents=True)
(root/'native'/'deep'/'session.jsonl').write_text('private native conversation')
for line in sys.stdin:
 value=json.loads(line)
 grandchild=None
 if value.get('ignore_term'):
  signal.signal(signal.SIGTERM,signal.SIG_IGN)
  grandchild=os.fork()
  if grandchild == 0:
   while True: time.sleep(1)
 if value.get('replace_nested'):
  shutil.rmtree(root/'native')
  (root/'native').symlink_to(value['replace_nested'],target_is_directory=True)
 if value.get('replace'):
  root.rename('moved-private')
  root.symlink_to(value['replace'],target_is_directory=True)
 print(json.dumps({'value':value,'pid':os.getpid(),'grandchild':grandchild,'caller':os.getppid()}),flush=True)
'''


class RpcProvider(Provider):
    def __init__(self, target, dialogue):
        super().__init__(files=False)
        self.target, self.dialogue = target, dialogue

    def run_plan(self, request, *, environment, run):
        self.plans.append(run)
        return RunPlan(command=CommandSpec((sys.executable, '-I', '-c', CHILD)),
                       files=(RunFile(path='private/seed', content=b'private seed'),),
                       disposable=('private',), links=(RunLink(path='private/auth.json', target=str(self.target)),),
                       dialogue=self.dialogue)


class RunRpcTests(BaseTest):
    def run_once(self, provider, **kwargs):
        return runs.run_once(provider, REQUEST, root=self.root, plugin='fixture',
                             environment={'PATH': os.environ['PATH']}, **kwargs)

    def no_content(self):
        if runs.base().exists():
            self.assertEqual(list(runs.base().iterdir()), [])

    def test_supervisor_relay_imports_no_harnesses(self):
        """Even importing a contract through harnesses executes adapters, which cannot run inside the cleanup owner."""
        source = str(Path(runs.__file__).resolve().parents[2])
        code = ('import sys;sys.path.insert(0,sys.argv[1]);'
                'from multiverse.runtime.run_rpc import relay,encode;'
                'encode({"ready":True});'
                'assert not any(n.startswith("multiverse.harnesses") for n in sys.modules)')
        subprocess.run([sys.executable, '-I', '-S', '-c', code, source], check=True, timeout=10)

    def test_rpc_runs_dialogue_in_caller_and_removes_native_tree_without_following_login_link(self):
        """Native caches are otherwise left behind, and recursive cleanup must never follow borrowed credential links."""
        target = self.work / 'borrowed-auth'
        target.write_text('outside sentinel')
        def dialogue(channel):
            channel.send({'private prompt': REQUEST.prompt})
            event = channel.receive()
            self.assertNotEqual(event['pid'], os.getpid())
            self.assertEqual(os.getpid(), caller)
            return RunReply(text=json.dumps(event))
        caller = os.getpid()
        result = self.run_once(RpcProvider(target, dialogue))
        self.assertTrue(stopped(json.loads(result.text)['pid']))
        self.assertEqual(target.read_text(), 'outside sentinel')
        self.no_content()

    def test_rpc_cancellation_and_dialogue_failure_confirm_private_child_cleanup(self):
        """Cancelling a caller-side dialogue must stop its supervised native process before removing its files."""
        target = self.work / 'borrowed-auth'
        target.write_text('outside sentinel')
        for cancelled in (False, True):
            event, seen = threading.Event(), []
            def dialogue(channel):
                channel.send({'hello': True})
                seen.append(channel.receive()['pid'])
                if cancelled:
                    event.set()
                    channel.receive()
                raise RunFailure('unavailable', 'Unsupported native shape')
            with self.assertRaises(RunFailure) as caught:
                self.run_once(RpcProvider(target, dialogue), cancel=event)
            self.assertEqual(caught.exception.kind, 'cancelled' if cancelled else 'unavailable')
            self.assertTrue(caught.exception.confirmed)
            self.assertTrue(stopped(seen[0]))
            self.no_content()

    def test_replaced_disposable_root_is_left_for_inspection(self):
        """A native process can replace its private root; cleanup must retain evidence and never enter its replacement."""
        outside = self.work / 'outside'
        outside.mkdir()
        target = outside / 'auth'
        target.write_text('outside sentinel')
        def dialogue(channel):
            channel.send({'replace': str(outside)})
            channel.receive()
            return RunReply(text='paid reply')
        provider = RpcProvider(target, dialogue)
        result = self.run_once(provider)
        self.assertEqual(result.text, 'paid reply')
        self.assertEqual(target.read_text(), 'outside sentinel')
        self.assertTrue((provider.plans[0].path / 'run.json').is_file())
        self.assertTrue(runs.leftovers(self.root))

    def test_timeout_stops_term_ignoring_descendants_and_nested_links_never_escape_cleanup(self):
        """A native descendant can hold pipes open and ignore TERM; nested replacements must not redirect deletion."""
        outside = self.work / 'outside'
        outside.mkdir()
        target = outside / 'auth'
        target.write_text('outside sentinel')
        seen = []
        def dialogue(channel):
            channel.send({'ignore_term': True, 'replace_nested': str(outside)})
            seen.append(channel.receive())
            channel.receive()
        with self.assertRaises(RunFailure) as caught:
            self.run_once(RpcProvider(target, dialogue), timeout=1)
        self.assertEqual(caught.exception.kind, 'timeout')
        self.assertTrue(caught.exception.confirmed)
        self.assertTrue(stopped(seen[0]['pid']))
        self.assertTrue(stopped(seen[0]['grandchild']))
        self.assertEqual(target.read_text(), 'outside sentinel')
        self.no_content()

    def test_caller_death_with_partial_control_frame_cleans_its_private_group(self):
        """EOF in the middle of a caller frame must still stop every producer and remove private content."""
        target = self.work / 'borrowed-auth'
        target.write_text('outside sentinel')
        pids = self.work / 'pids.json'
        code = '''import os,json,pathlib,struct,sys
sys.path[:0]=sys.argv[1:3]
from multiverse.runtime import runs
from test_run_rpc import RpcProvider,REQUEST
def dialogue(channel):
 channel.send({'ignore_term':True})
 value=channel.receive()
 pathlib.Path(sys.argv[5]).write_text(json.dumps(value))
 channel.connection.setblocking(True)
 channel.connection.sendall(struct.pack('!Q',1000)+b'{')
 os._exit(0)
runs.run_once(RpcProvider(pathlib.Path(sys.argv[4]),dialogue),REQUEST,root=pathlib.Path(sys.argv[3]),
              plugin='fixture',environment={'PATH':os.environ['PATH']})
'''
        source = str(Path(runs.__file__).resolve().parents[2])
        process = subprocess.Popen([sys.executable, '-I', '-c', code, source, str(Path(__file__).parent),
                                    str(self.root), str(target), str(pids)], start_new_session=True,
                                   stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
        track(pid=process.pid)
        _, errors = process.communicate(timeout=10)
        self.assertEqual(process.returncode, 0, errors.decode())
        observed = json.loads(pids.read_text())
        until(lambda: stopped(observed['pid']) and stopped(observed['grandchild']), seconds=10)
        until(lambda: not runs.base().exists() or not list(runs.base().iterdir()), seconds=10)
        self.assertEqual(target.read_text(), 'outside sentinel')
