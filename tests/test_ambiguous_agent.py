"""Ambiguous references list actual names and IDs through the public CLI."""
import json
import os
import subprocess
import sys
from multiverse.store import write
from test_multiverse import BaseTest, ROOT


class AmbiguousAgentTest(BaseTest):
    def test_send_and_inspect_name_each_match_without_saving(self):
        ids = ('a1234567-1111-4111-8111-111111111111', 'a1234567-2222-4222-8222-222222222222')
        for identifier, name in zip(ids, ('alpha', 'beta')):
            write(self.root / 'state' / 'agents' / identifier / 'properties.json',
                  {'id': identifier, 'name': name, 'harness': 'claude'})
        for command in (('send', 'a1234567', '--text', 'must not be saved'), ('inspect', 'a1234567')):
            with self.subTest(command=command):
                result = subprocess.run([sys.executable, str(ROOT / 'bin/mv2'), '-u', str(self.root), *command],
                    capture_output=True, text=True, stdin=subprocess.DEVNULL, timeout=15, env=os.environ)
                self.assertEqual(result.returncode, 1)
                for text in (*ids, 'alpha', 'beta'):
                    self.assertIn(text, result.stdout + result.stderr)
                self.assertNotIn('Traceback', result.stdout + result.stderr)
        self.assertEqual(list(self.root.glob('state/agents/*/inbox.jsonl')), [])
