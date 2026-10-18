"""CLAUDE-13 public regression from cf27043, using a private stand-in fixture."""
import re
from qa import test_claude_delivery_advice as fixture

class LaunchOnlyFinal(fixture.ClaudeLaunchOnly):
    def test_list_advice_for_a_launch_only_agent_can_be_followed(self):
        """docs/cli/messaging.md: "List, inspect and doctor show how many messages were not delivered
            and the `mv2 deliver NAME --retry` command", and a launch-only agent "can't receive steer or
            follow-up messages, so send it inbox-only messages instead". For a running launch-only agent,
            the command that list advises must be able to succeed."""
        self.addCleanup(self.mv2, 'kill', 'worker', code=None)
        self.mv2('spawn', '--name', 'worker', '--harness', 'claude-plain', '--wait')
        self.mv2('send', 'worker', '--text', 'hello', code=None)
        listing = self.mv2('list', '--wide').stdout
        advice = re.search('mv2 deliver worker --retry', listing)
        if advice is None:
            return
        result = self.mv2('deliver', 'worker', '--retry', code=None)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
