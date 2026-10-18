"""doctor's exit status, which scripts and agents gate on."""
import contextlib
import io
from pathlib import Path
import sys
import unittest
from unittest.mock import patch
from uuid import uuid4

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))
from multiverse.runtime import lifecycle
from multiverse.store import write
from multiverse.tools import doctor
from test_multiverse import BaseTest


def row(name, status='idle', **extra):
    return {'id': str(uuid4()), 'name': name, 'harness': 'codex', 'live': 'alive', 'status': status, 'phase': 'done',
            'summary': '', 'universe': '', 'runtime': {}, **extra}


class DoctorTest(BaseTest):
    def broken(self):
        directory = self.root / 'state/agents' / str(uuid4())
        write(directory / 'properties.json', {'id': directory.name, 'name': 'worker', 'harness': 'codex',
                                              'phase': 'done', 'summary': '', 'workspace': str(self.work)})
        write(directory / 'runtime.json', {'launch_id': str(uuid4()), 'state': 'failed',
                                           'error': 'Cannot start:\nno such file'})

    def exit_code(self):
        with contextlib.redirect_stdout(io.StringIO()):
            try:
                self.assertIsNone(doctor.cli_run(self.root))
            except SystemExit as stopped:
                return stopped.code
        return 0

    def test_doctor_exits_1_only_for_real_problems(self):
        """doctor exits 0 with no problems or only reconnect notes and 1 for a failed launch or a disconnection whose
        reconnect failed or was skipped; a break goes unnoticed because the text stays plausible while scripts and
        agents gate on the wrong status."""
        with self.subTest('no problems'):
            self.assertTrue(doctor.run(self.root)['ok'])
            self.assertEqual(self.exit_code(), 0)
        reconnected = row('worker', recovery_note='Reconnected automatically',
                          runtime={'auto_recovery': {'state': 'reconnected'}})
        with self.subTest('a reconnect note'), patch.object(lifecycle, 'listing', return_value=[reconnected]):
            result = doctor.run(self.root)
            self.assertEqual((result['ok'], result['problems']), (True, []))
            self.assertEqual(len(result['notes']), 1)
            self.assertEqual(self.exit_code(), 0)
        for state in ('failed', 'skipped'):
            reason = 'Automatic reconnect ' + state + ': native background processes are running'
            item = row('worker', status='disconnected', connection_error=reason,
                       runtime={'auto_recovery': {'state': state, 'reason': reason}})
            with self.subTest(reconnect=state), patch.object(lifecycle, 'listing', return_value=[item]):
                result = doctor.run(self.root)
                self.assertFalse(result['ok'])
                self.assertIn(reason, result['problems'][0])
                self.assertEqual(self.exit_code(), 1)
        with self.subTest('a failed launch'):
            self.broken()
            self.assertFalse(doctor.run(self.root)['ok'])
            self.assertEqual(self.exit_code(), 1)
            self.assertIs(doctor.cli_run(self.root, json=True)['ok'], False)


if __name__ == '__main__':
    unittest.main()
