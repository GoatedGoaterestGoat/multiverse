"""Custom recipes receive this resume's prompt, never the saved original task."""
from unittest.mock import patch
from multiverse import harnesses
from multiverse.runtime import lifecycle
from multiverse.store import read, write
from test_multiverse import TerminalBase, until


class ResumePlaceholderTest(TerminalBase):
    def test_wake_values_and_task_file_both_contain_only_wake_prompt(self):
        """On resume a custom recipe's {task} and {task_file} carry only the wake prompt, or nothing without one, while
        properties keep the original task; a break goes unnoticed because a resumed agent redoes its whole task, which
        looks like work (797bfec, qa Q3)."""
        folder = self.root / 'harnesses' / 'recorder'
        folder.mkdir()
        output = self.work / 'seen-task'
        file_output = self.work / 'seen-file'
        # Each launch writes both values whole, then renames them into place, so a file that exists is complete.
        command = ['/bin/sh', '-c', 'printf "%s" "$1" > "$3.new"; cat "$2" > "$4.new"; mv "$3.new" "$3"; '
                   'mv "$4.new" "$4"; exec sleep 600', 'sh', '{task}', '{task_file}', str(output), str(file_output)]
        write(folder / 'harness.json', {'schema': 1, 'spawn': command, 'resume': command})
        adapter = harnesses.load(self.root, 'recorder')
        # This declared adapter consumes its verified interactive prompt through
        # its recipe; unlike a built-in it needs no additional positional args.
        adapter.resume_prompt_args = ()

        def launched(**options):
            for path in (output, file_output):
                path.unlink(missing_ok=True)
            with patch.object(harnesses, 'load', return_value=adapter):
                result = lifecycle.start(self.root, socket_path=self.socket, wait=True, **options)
            self.assertTrue(result['ready'], result)
            until(lambda: output.exists() and file_output.exists())
            return result, output.read_text(), file_output.read_text()

        first, task, task_file = launched(name='recorded', task='ORIGINAL', harness='recorder')
        self.assertEqual((task, task_file), ('ORIGINAL', 'ORIGINAL'))
        for prompt, expected in ((None, ''), ('ONLY-WAKE', 'ONLY-WAKE')):
            with self.subTest(resume_prompt=prompt):
                lifecycle.kill(self.root, first['id'])
                resumed, task, task_file = launched(resume=first['id'], resume_prompt=prompt)
                self.assertEqual((task, task_file), (expected, expected))
                self.assertEqual(resumed['task'], 'ORIGINAL')
                self.assertEqual(read(self.root / 'state' / 'agents' / first['id'] / 'properties.json')['task'],
                                 'ORIGINAL')
