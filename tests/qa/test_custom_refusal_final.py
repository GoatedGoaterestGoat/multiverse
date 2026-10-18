"""LC-23 public scenario from cdf5b4c, pinned to a private stand-in fixture."""
from qa import test_launch_only_advice as fixture
import json

class LaunchOnlyRefusalIsFinal(fixture.LifecycleFixture):
    """mv2-lead's decision for LC-23 / CLAUDE-13: a launch-only refusal is a final history state, never counted as not
    delivered, never retried, and doctor stays quiet."""

    def test_launch_only_refusal_is_not_an_open_problem(self):
        self.launch_only_agent_with_failed_steer()
        self.mv2('send', 'a', '--delivery', 'follow-up', '--text', 'also refused', check=False)
        self.assertNotIn('not delivered', self.mv2('list').stdout)
        doctor = self.mv2('doctor', check=False)
        self.assertEqual(doctor.returncode, 0, doctor.stdout)
        retry = self.mv2('deliver', 'a', '--retry', check=False)
        self.assertNotIn('Not delivered', retry.stdout + retry.stderr)
        self.mv2('kill', 'a')
        stopped = self.mv2('deliver', 'a', '--retry', check=False)
        self.assertNotIn('Not delivered', stopped.stdout + stopped.stderr)

    def test_repaired_ledger_check_gives_manual_advice_without_retry_loop(self):
        self.mv2('spawn', '--name', 'a', '--harness', 'sleeper', '--wait')
        directory = self.root / 'state' / 'agents' / self.agents()['a']['id']
        path = directory / 'inbox-state.json'
        path.write_text('[]')
        self.mv2('send', 'a', '--text', 'saved while damaged', check=False)
        path.unlink()
        checked = self.mv2('deliver', 'a', '--check', check=False)
        self.assertIn('--delivery inbox-only', checked.stdout + checked.stderr)
        self.assertNotIn('resume it', checked.stdout + checked.stderr)
        history = json.loads(self.mv2('inbox', '--agent', 'a', '--all', '--json').stdout)['messages']
        self.assertEqual(history[-1]['tracking']['submission_state'], 'launch-only')
        self.assertEqual(self.mv2('deliver', 'a', '--retry').returncode, 0)
        self.assertEqual(self.mv2('doctor').returncode, 0)
