"""Claude's launch screen: its input box, or a prompt the person answers before the session starts."""
import json
from pathlib import Path
import unittest

from multiverse.harnesses.claude import screen

SCREENS = Path(__file__).parent / 'fixtures/claude/screens-2.1.314.json'

# Claude Code 2.1.330's dialog for an `--add-dir` folder under no trusted folder, as captured at 80 columns.
ADD_DIR = """────────────────────────────────────────────────────────────────────────────────
 Adding a new directory:
 /work/agent
 This session hasn't worked here before. Is this a directory you created or one
 you trust?
 Claude Code'll be able to read, edit, and execute files here. Its skills,
 commands, and agents load and can run commands, and so can the git settings of
 its repository. Your answer holds for this session.
 Security guide
 ❯ No, exit
   Yes, add it
 Enter to confirm · Esc to cancel
"""

# Claude Code 2.1.330's trust dialog for a folder whose settings declare hooks.
HOOKED = """────────────────────────────────────────────────────────────────────────────────
 Accessing workspace:

 /work/hooked

 Quick safety check: Is this a project you created or one you trust? (Like your
 own code, a well-known open source project, or work from your team). If not,
 take a moment to review what's in this folder first.

 Claude Code'll be able to read, edit, and execute files here.

 ⚠ This folder configures hooks that can run commands without asking, declared
 in .claude/settings.json

 Trusting this folder loads .claude/settings.json (runs commands).
 Security guide

 ❯ No, exit
   Yes, I trust this folder

 Enter to confirm · Esc to cancel
"""


class ClaudeScreenTest(unittest.TestCase):
    def test_a_startup_prompt_is_named_and_a_dialog_inside_the_session_is_not_one(self):
        """mv2 reads a launch's screen to say it waits at a prompt only the person can answer: calling a tool's
        approval or /model's picker one would report a running agent stuck at launch, and missing one says Started
        while the agent waits for good."""
        found = {item['name']: item['text'] for item in json.loads(SCREENS.read_text())}
        found['add-dir dialog'], found['hooked trust dialog'] = ADD_DIR, HOOKED
        # Output above the dialog's own rule, another section of the screen, never names it.
        found['trust dialog under another section'] = '─' * 50 + '\n  Boot checks\n  /work/old\n' + found['trust dialog']
        found['trust dialog under another selection'] = ('─' * 50 + '\n  Earlier selection:\n  /work/old\n  ❯ Previous choice\n'
                                                         '  Esc to go back\n' + found['trust dialog'])
        prompts = {'trust dialog': {'title': 'Accessing workspace', 'detail': '/private/tmp/mv2-pane-try'},
                   'add-dir dialog': {'title': 'Adding a new directory', 'detail': '/work/agent'},
                   'hooked trust dialog': {'title': 'Accessing workspace', 'detail': '/work/hooked'},
                   'trust dialog under another section': {'title': 'Accessing workspace',
                                                          'detail': '/private/tmp/mv2-pane-try'},
                   'trust dialog under another selection': {'title': 'Accessing workspace',
                                                            'detail': '/private/tmp/mv2-pane-try'}}
        for name, text in found.items():
            with self.subTest(name):
                self.assertEqual(screen.startup_prompt(text), prompts.get(name))
                # The input box is up on every screen of a started session but its own dialogs.
                self.assertEqual(screen.ready(text), name not in (*prompts, 'model dialog', 'permission dialog',
                                                                  'question dialog'))


if __name__ == '__main__':
    unittest.main()
