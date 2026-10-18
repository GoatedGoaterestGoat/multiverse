"""Custom recipes receive this resume's prompt, never the saved original task."""
from unittest.mock import patch
from multiverse import harnesses
from multiverse.runtime import lifecycle
from multiverse.store import read, write
from test_multiverse import TerminalBase


class ResumePlaceholderTest(TerminalBase):
    def test_wake_values_and_task_file_both_contain_only_wake_prompt(self):
        folder = self.root / 'harnesses' / 'recorder'
        folder.mkdir()
        output = self.work / 'seen-task'
        file_output = self.work / 'seen-file'
        command = ['/bin/sh', '-c', 'printf "%s" "$1" > "$3"; cat "$2" > "$4"; exec sleep 600',
                   'sh', '{task}', '{task_file}', str(output), str(file_output)]
        write(folder / 'harness.json', {'schema': 1, 'spawn': command, 'resume': command})
        adapter = harnesses.load(self.root, 'recorder')
        # This declared adapter consumes its verified interactive prompt through
        # its recipe; unlike a built-in it needs no additional positional args.
        adapter.resume_prompt_args = ()
        with patch.object(harnesses, 'load', return_value=adapter):
            first = lifecycle.start(self.root, name='recorded', task='ORIGINAL', harness='recorder',
                                    socket_path=self.socket, wait=True)
            lifecycle.kill(self.root, first['id'])
            resumed = lifecycle.start(self.root, resume=first['id'], resume_prompt='ONLY-WAKE',
                                      socket_path=self.socket, wait=True)
        self.assertTrue(resumed['ready'])
        self.assertEqual(output.read_text(), 'ONLY-WAKE')
        self.assertEqual(file_output.read_text(), 'ONLY-WAKE')
        self.assertEqual(resumed['task'], 'ORIGINAL')
        self.assertEqual(read(self.root / 'state' / 'agents' / first['id'] / 'properties.json')['task'], 'ORIGINAL')
