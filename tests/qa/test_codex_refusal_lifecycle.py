"""Real Codex, public-CLI/tmux tests of the plain startup-refusal boundary.

Opt in with MV2_QA_REAL_CODEX=1; set MV2_QA_EVIDENCE to keep command logs/screens.
"""
import concurrent.futures
import json
import os
from pathlib import Path
import shlex
import shutil
import subprocess
import tempfile
import threading
import time
import unittest

from native_safety import install
install()


@unittest.skipUnless(os.environ.get('MV2_QA_REAL_CODEX') == '1', 'requires real Codex opt-in')
class PlainRefusalLifecycle(unittest.TestCase):
    def setUp(self):
        self.scratch=Path(tempfile.mkdtemp(prefix='qacrf-',dir='/tmp')).resolve()
        self.addCleanup(shutil.rmtree,self.scratch,True)
        self.work=self.scratch/'work';self.work.mkdir()
        self.home=self.scratch/'home';self.codex=self.home/'.codex';self.codex.mkdir(parents=True)
        auth=Path(os.environ.get('MV2_QA_AUTH_FILE',str(Path.home()/'.codex/auth.json')))
        if not auth.is_file(): self.skipTest('no Codex authentication fixture')
        shutil.copyfile(auth,self.codex/'auth.json')
        (self.codex/'config.toml').write_text('model = "gpt-6-luna"\nmodel_reasoning_effort = "low"\ncheck_for_update_on_startup = false\n[projects.'+json.dumps(str(self.work))+']\ntrust_level = "trusted"\n')
        settings=self.codex/'app-server-daemon/settings.json';settings.parent.mkdir()
        settings.write_text(json.dumps({'updater':{'autoUpdateEnabled':False}}))
        self.env={k:v for k,v in os.environ.items() if not k.startswith(('MV2_','CODEX_')) and k not in ('TMUX','TMUX_PANE')}
        self.env.update(HOME=str(self.home),CODEX_HOME=str(self.codex),CLAUDE_CONFIG_DIR=str(self.home/'.claude'),MV2_TMUX_SOCKET=str(self.scratch/'a.sock'))
        self.evidence=Path(os.environ.get('MV2_QA_EVIDENCE',str(self.scratch/'evidence')));self.evidence.mkdir(parents=True,exist_ok=True)
        self.log=self.evidence/(self.scratch.name+'.jsonl');self.lock=threading.Lock();self.frame=0
        self.addCleanup(self.cleanup_native)
        self.assertEqual(self.run_cli('mv2','init',str(self.work)).returncode,0)
        (self.work/'.multiverse/bootstrap.md').write_text('Disposable black-box test. Do only the explicit task. Never inspect implementation or real user homes. Keep replies short.\n')
        self.profile('codex',True,[])
        self.profile('plain',None if os.environ.get('MV2_QA_PLAIN_AUTO')=='1' else False,['--no-daemon'])

    def profile(self,name,bridge,args):
        folder=self.work/'.multiverse/harnesses'/name;folder.mkdir(exist_ok=True)
        settings={'args':args+['--dangerously-bypass-approvals-and-sandbox']}
        if bridge is not None:settings['inbox_bridge']=bridge
        (folder/'harness.json').write_text(json.dumps({'schema':2,'adapter':'codex','settings':settings}))

    def run_cli(self,*argv,env=None,timeout=65):
        chosen=env or self.env;start=time.time()
        result=subprocess.run(argv,cwd=self.work,env=chosen,capture_output=True,text=True,timeout=timeout)
        row={'at':start,'seconds':time.time()-start,'argv':argv,'executable':shutil.which(argv[0],path=chosen['PATH']),'rc':result.returncode,'stdout':result.stdout,'stderr':result.stderr}
        with self.lock:
            with self.log.open('a') as f:f.write(json.dumps(row)+'\n')
        return result

    def cleanup_native(self):
        self.run_cli('tmux','-S',str(self.scratch/'h.sock'),'kill-server')
        for f in (self.work/'.multiverse/state/agents').glob('*/properties.json'):
            self.run_cli('mv2','kill',json.loads(f.read_text())['id'])
        self.run_cli('tmux','-S',str(self.scratch/'a.sock'),'kill-server')
        self.run_cli('codex','app-server','daemon','stop')

    def agent(self,name):
        for f in (self.work/'.multiverse/state/agents').glob('*/properties.json'):
            p=json.loads(f.read_text())
            if p['name']==name:return f.parent,p,json.loads((f.parent/'runtime.json').read_text())
        raise AssertionError('missing agent '+name)

    def screen(self,name,label):
        d,p,r=self.agent(name)
        if not r.get('tmux'):return ''
        result=self.run_cli('tmux','-S',r['tmux']['socket'],'capture-pane','-p','-t',r['tmux']['pane'])
        self.frame+=1
        (self.evidence/(self.scratch.name+f'-{self.frame:03d}-'+label+'.txt')).write_text(result.stdout)
        return result.stdout

    def type_native(self,name,text):
        d,p,r=self.agent(name)
        self.assertIn('Ask Codex',self.screen(name,'before-input'))
        tags=self.run_cli('tmux','-S',r['tmux']['socket'],'show-options','-p','-t',r['tmux']['pane']).stdout
        self.assertIn(p['id'],tags);self.assertIn(r['launch_id'],tags)
        self.run_cli('tmux','-S',r['tmux']['socket'],'send-keys','-t',r['tmux']['pane'],'-l',text)
        time.sleep(.4)
        self.run_cli('tmux','-S',r['tmux']['socket'],'send-keys','-t',r['tmux']['pane'],'Enter')

    def setup_conversations(self):
        holder=self.run_cli('mv2','spawn','--name','holder','--task','Reply HOLDER_NAV_TARGET only.','--wait','--wait-timeout','45','--json')
        self.assertEqual(holder.returncode,0,holder.stderr)
        plain=self.run_cli('mv2','spawn','--name','plain','--harness','plain','--task','Run "$MV2_CLI" session-bind --session-id "$CODEX_THREAD_ID". Then reply PLAIN_NORMAL only.','--wait','--wait-timeout','45','--json')
        self.assertEqual(plain.returncode,0,plain.stderr)
        self.assertEqual(json.loads(plain.stdout)['runtime']['native_mode']['mode'],'plain')
        end=time.monotonic()+35;screen=''
        while time.monotonic()<end:
            screen=self.screen('plain','normal-start')
            if '• PLAIN_NORMAL' in screen and 'Ask Codex' in screen:break
            time.sleep(.25)
        self.assertIn('• PLAIN_NORMAL',screen)
        self.assertTrue(self.agent('plain')[1].get('native_session_id'))
        return self.agent('holder')[1]['native_session_id'],self.agent('plain')[1]['native_session_id']

    def test_later_navigation_refusal_does_not_reclassify_successful_plain_launch(self):
        """docs/use/harnesses.md: later navigation does not turn a successful launch into startup refusal."""
        held_sid,plain_sid=self.setup_conversations()
        self.type_native('plain','/resume '+held_sid)
        end=time.monotonic()+15;screen=''
        while time.monotonic()<end:
            screen=self.screen('plain','later-navigation')
            if 'This conversation is open in another app' in screen:break
            time.sleep(.25)
        self.assertIn('This conversation is open in another app',screen,'native navigation did not reach the held conversation')
        end=time.monotonic()+10
        while time.monotonic()<end:
            result=self.run_cli('mv2','list','--json')
            plain=next(x for x in json.loads(result.stdout) if x['name']=='plain')
            self.assertFalse(plain.get('launch_error'),plain)
            d,p,r=self.agent('plain')
            self.assertFalse((d/'launches'/r['launch_id']/'native-refusal.json').exists())
            time.sleep(.5)
        inspected=self.run_cli('mv2','inspect','plain').stdout
        self.assertNotIn('Last error:',inspected.split('Terminal (',1)[0])

    def hold_plain_session_on_other_server(self,sid):
        self.assertEqual(self.run_cli('mv2','kill','plain','--attachment-only').returncode,0)
        wrapper=self.scratch/'native-client.py'
        wrapper.write_text('import os,sys\ne={k:v for k,v in os.environ.items() if not k.startswith("MV2_")}\nos.execvpe("codex",["codex","resume","--remote","unix://",sys.argv[1]],e)\n')
        command=shlex.join(['python3',str(wrapper),sid])
        result=self.run_cli('tmux','-S',str(self.scratch/'h.sock'),'new-session','-d','-s','holder','-x','120','-y','36',command)
        self.assertEqual(result.returncode,0,result.stderr)
        end=time.monotonic()+25;screen=''
        while time.monotonic()<end:
            screen=self.run_cli('tmux','-S',str(self.scratch/'h.sock'),'capture-pane','-p','-t','holder').stdout
            if '• PLAIN_NORMAL' in screen and 'Ask Codex' in screen and 'Resuming session' not in screen:break
            time.sleep(.25)
        (self.evidence/(self.scratch.name+'-other-server-holder.txt')).write_text(screen)
        self.assertIn('• PLAIN_NORMAL',screen)
        self.assertNotIn('This conversation is open in another app',screen)

    def test_plain_resume_refusal_is_reported_after_resuming_screen(self):
        """docs/use/harnesses.md: loading/resuming remain observed and startup refusal reaches CLI surfaces."""
        held_sid,plain_sid=self.setup_conversations()
        self.hold_plain_session_on_other_server(plain_sid)
        frames=[]
        with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
            future=pool.submit(self.run_cli,'mv2','resume','plain','--wait','--wait-timeout','40')
            while not future.done():
                try:frames.append(self.screen('plain','resume-startup'))
                except (KeyError,FileNotFoundError):pass
                time.sleep(.1)
            result=future.result()
        (self.evidence/(self.scratch.name+'-resuming-screen-observed.txt')).write_text(str(any('Resuming session' in x for x in frames))+'\n')
        # Collect every public reporting surface before asserting, including late observations.
        time.sleep(8)
        wide=self.run_cli('mv2','list','--wide')
        inspected=self.run_cli('mv2','inspect','plain').stdout
        sent=self.run_cli('mv2','send','plain','--text','REFUSAL_PROBE_ONLY')
        self.assertNotEqual(result.returncode,0,result.stdout)
        self.assertIn('This conversation is open in another app',result.stderr)
        self.assertIn('This conversation is open in another app',wide.stdout)
        self.assertIn('Last error: This conversation is open in another app',inspected)
        self.assertNotEqual(sent.returncode,0)
        self.assertIn('This conversation is open in another app',sent.stderr)
        self.assertIn('Saved',sent.stderr)


if __name__=='__main__':unittest.main()
