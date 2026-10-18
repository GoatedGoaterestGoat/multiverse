"""QA lifecycle decided contracts from 540cb4e, pinned to this checkout."""
from qa.test_damaged_lifecycle import LifecycleFixture

class DecidedSpecQuestions(LifecycleFixture):
    """Behaviour mv2-lead decided for this pass's spec questions (Q1, Q3, Q4)."""

    def launch_id(self, name):
        return self.agents()[name]['runtime']['launch_id']

    def test_send_from_an_outdated_launch_is_refused(self):
        """Q1; docs/use/troubleshooting.md "Outdated launch": an old process can't act as the current one."""
        self.mv2('spawn', '--name', 'a', '--harness', 'sleeper', '--wait')
        self.mv2('spawn', '--name', 'b', '--harness', 'sleeper', '--wait')
        agent = self.agents()['a']['id']
        old = self.launch_id('a')
        self.mv2('kill', 'a')
        self.mv2('resume', 'a', '--wait')
        identity = dict(MV2_UNIVERSE=str(self.root), MV2_AGENT_ID=agent, MV2_AGENT_DIR=str(self.root / 'state' / 'agents' / agent))
        environment = self.env
        try:
            self.env = {**environment, **identity, 'MV2_LAUNCH_ID': old}
            stale = self.mv2('send', 'b', '--delivery', 'inbox-only', '--text', 'from a stale launch', check=False)
            self.env = {**environment, **identity, 'MV2_LAUNCH_ID': self.launch_id('a')}
            current = self.mv2('send', 'b', '--delivery', 'inbox-only', '--text', 'from the current launch', check=False)
        finally:
            self.env = environment
        self.assertEqual(current.returncode, 0, current.stderr)
        self.assertNotEqual(stale.returncode, 0, stale.stdout)
        self.assertIn('Outdated launch', stale.stderr)
        self.assertNotIn('from a stale launch', self.mv2('inbox', '--agent', 'b', '--all').stdout)

    def test_resume_never_replays_the_original_task(self):
        """Q3; docs/reference/files.md launches/UUID/task.md: the initial or wake prompt."""
        seen = self.base / 'seen.txt'
        self.harness('recorder', ['/bin/sh', '-c', 'printf "%s" "$1" > "$2"; exec sleep 600', 'sh', '{task}', str(seen)])
        self.mv2('spawn', '--name', 'a', '--harness', 'recorder', '--task', 'ORIGINAL-TASK', '--wait')
        self.until(seen.exists)
        self.assertEqual(seen.read_text(), 'ORIGINAL-TASK')
        self.mv2('kill', 'a')
        seen.unlink()
        self.mv2('resume', 'a', '--wait')
        self.until(seen.exists)
        self.assertNotIn('ORIGINAL-TASK', seen.read_text())

    def test_missing_executable_says_name_did_not_start(self):
        """Q4; docs/use/troubleshooting.md "A spawn or resume failed": mv2: NAME did not start: REASON."""
        self.harness('missing', ['/nonexistent/qa-agent'])
        result = self.mv2('spawn', '--name', 'm', '--harness', 'missing', '--wait', check=False)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('m did not start: Harness executable not found', result.stderr)

    def test_invalid_move_destination_names_the_destination(self):
        self.mv2('spawn', '--name', 'a', '--harness', 'sleeper', '--wait')
        self.mv2('kill', 'a')
        result = self.mv2('move', 'a', '../sub2', check=False)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('Not a universe or subverse: ../sub2', result.stdout + result.stderr)

    def test_task_file_is_empty_on_resume_and_history_stays_original(self):
        from pathlib import Path
        import json
        seen = self.base / 'seen-file.txt'
        self.harness('file-recorder', ['/bin/sh', '-c', 'cat "$1" > "$2"; exec sleep 600',
                                      'sh', '{task_file}', str(seen)])
        self.mv2('spawn', '--name', 'a', '--harness', 'file-recorder', '--task', 'ORIGINAL-FILE', '--wait')
        self.assertEqual(seen.read_text(), 'ORIGINAL-FILE')
        agent = self.agents()['a']['id']
        self.mv2('kill', 'a')
        seen.unlink()
        self.mv2('resume', 'a', '--wait')
        self.assertEqual(seen.read_text(), '')
        saved = json.loads((self.root / 'state' / 'agents' / agent / 'properties.json').read_text())
        self.assertEqual(saved['task'], 'ORIGINAL-FILE')
