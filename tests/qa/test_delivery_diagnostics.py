"""Public regressions from qa-claude 2e64236, using the private stand-in fixture."""
from qa import test_claude_delivery_advice as fixture
import json
import time

class DeliveryDiagnostics(fixture.ClaudeLaunchOnly):

    def test_damaged_inbox_state_message_names_the_real_file(self):
        """docs/cli/messaging.md: "If inbox-state.json is damaged, send still saves the new message,
            names the file, and makes no native submission. Repair or reset that file before retrying."
            The named path must be the agent's own file, in state/agents/UUID/."""
        self.addCleanup(self.mv2, 'kill', 'worker', code=None)
        self.mv2('spawn', '--name', 'worker', '--harness', 'claude-plain', '--wait')
        state = self.agent_dir('worker') / 'inbox-state.json'
        state.write_text('[]')
        result = self.mv2('send', 'worker', '--text', 'hello', code=None)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn(str(state), result.stderr)

    def test_check_exits_nonzero_while_a_failed_wake_waits_for_retry(self):
        """docs/use/troubleshooting.md: "`deliver` exits non-zero while something is still failed or
            uncertain." docs/cli/messaging.md: a wake launch that exits during startup with no consumption
            of its prompt "becomes a definite failure ... saved for explicit retry"."""
        failing = self.scratch / 'claude-fails'
        failing.write_text('#!/bin/sh\n[ "$1" = --version ] && { echo "2.1.302 (Claude Code)"; exit 0; }\necho "fake claude failing"; exit 3\n')
        failing.chmod(493)
        directory = self.work / '.multiverse/harnesses/claude-fails'
        directory.mkdir(parents=True)
        (directory / 'harness.json').write_text(json.dumps({'schema': 2, 'adapter': 'claude', 'settings': {'executable': str(failing)}}))
        self.addCleanup(self.mv2, 'kill', 'worker', code=None)
        self.mv2('spawn', '--name', 'worker', '--harness', 'claude-fails', '--wait', code=None)
        self.mv2('send', 'worker', '--text', 'hello', code=None)
        deadline = time.monotonic() + 30
        while True:
            messages = json.loads(self.mv2('inbox', '--agent', 'worker', '--all', '--json').stdout)['messages']
            if messages[-1].get('tracking', {}).get('submission_state') == 'failed':
                break
            self.assertLess(time.monotonic(), deadline, 'the wake never became a definite failure')
            time.sleep(0.5)
        result = self.mv2('deliver', 'worker', '--check', code=None)
        self.assertNotEqual(result.returncode, 0, result.stdout + result.stderr)
