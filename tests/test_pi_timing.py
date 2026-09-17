"""Installed native Pi timing, using an offline provider and real disposable shell tools."""
import json
import os
from pathlib import Path
import shutil
import time
import unittest
from unittest.mock import patch

from multiverse import agents
from multiverse.store import append_message, inbox, read, write
from test_multiverse import TerminalBase, until


@unittest.skipUnless(shutil.which('pi') and shutil.which('tmux'), 'Pi and tmux required')
class PiTimingTest(TerminalBase):
    def test_steering_arrives_after_tool_before_followup_without_aborting(self):
        path = self.root / 'harnesses/pi/harness.json'
        spec = read(path)
        spec['settings'] = {'inbox_bridge': True, 'args': ['--offline', '--no-extensions', '--no-skills',
                            '--no-context-files', '--no-prompt-templates', '--no-approve', '--extension',
                            str(Path(__file__).with_name('steering-provider.ts')), '--provider', 'mv2-steering', '--model', 'test']}
        write(path, spec)
        with patch.dict(os.environ, PI_CODING_AGENT_DIR=str(self.work / 'pi-home'), PI_OFFLINE='1'):
            result = agents.start(self.root, 'timing', 'Run the offline tool sequence.', 'pi', socket_path=self.socket)
        directory = agents.directory(self.root, result['id'])
        try:
            until(lambda: (directory / 'tool-1-start').exists(), seconds=25)
            sent = time.time() * 1000
            urgent = append_message(directory, 'URGENT_SAFE_BOUNDARY', {}, delivery='steer')
            normal = append_message(directory, 'NORMAL_AFTER_TURN', {}, delivery='follow-up')
            timing = directory / 'timing.jsonl'
            until(lambda: len(timing.read_text().splitlines()) >= 4, seconds=25)
            records = [json.loads(line) for line in timing.read_text().splitlines()]
            second = json.dumps(records[1]['context'])
            third = json.dumps(records[2]['context'])
            fourth = json.dumps(records[3]['context'])
            self.assertIn('URGENT_SAFE_BOUNDARY', second)
            self.assertNotIn('NORMAL_AFTER_TURN', second)
            self.assertNotIn('NORMAL_AFTER_TURN', third)
            self.assertIn('NORMAL_AFTER_TURN', fourth)
            for index in (1, 2):
                self.assertTrue((directory / f'tool-{index}-end').exists(), 'Tool must complete normally')
            until(lambda: not inbox(directory)['messages'])
            native = (directory / 'session.jsonl').read_text()
            self.assertIn(urgent['id'], native)
            self.assertIn(normal['id'], native)
            print('Pi timing evidence:', json.dumps({'urgent_ms': records[1]['at'] - sent,
                  'normal_ms': records[3]['at'] - sent, 'tool_duration_ms': 4000,
                  'urgent_before_second_tool': True, 'normal_after_final_turn': True}))
        except Exception:
            print(agents.terminal_action(self.root, result['id'], 'inspect'))
            raise


if __name__ == '__main__':
    unittest.main()
