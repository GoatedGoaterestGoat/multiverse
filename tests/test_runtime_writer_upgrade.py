"""Actual old/current core writers remain valid across authority validation."""
from contextlib import ExitStack
from functools import cache
import gzip
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import unittest
from unittest.mock import Mock, patch

from multiverse import harnesses
from multiverse.runtime import attachments, lifecycle, missing_runtime, native
from multiverse.store import read, write
from multiverse.universe import relocate
from test_multiverse import BaseTest


FIXTURES = Path(__file__).parent / 'fixtures/runtime-writers'
GENERATOR_SOURCE = os.environ.get('MV2_GENERATE_RUNTIME_MATRIX')
GENERATOR_OUTPUT = os.environ.get('MV2_RUNTIME_MATRIX_OUTPUT')
GENERATOR_ENDING = os.environ.get('MV2_RUNTIME_MATRIX_ENDING') == '1'


@unittest.skipUnless(GENERATOR_SOURCE and GENERATOR_OUTPUT, 'manual historical fixture capture')
class GenerateRuntimeMatrix(BaseTest):
    def test_generate(self):
        """Regenerates the real-writer fixtures from a given mv2 checkout when asked (skipped otherwise); kept with the
        records it produces so the matrix can be rebuilt for a new writer."""
        generator = 'generate-ending.py' if GENERATOR_ENDING else 'generate.py'
        result = subprocess.run([sys.executable, str(FIXTURES / generator),
                                 GENERATOR_SOURCE, GENERATOR_OUTPUT], text=True,
                                stdout=subprocess.PIPE, stderr=subprocess.STDOUT, timeout=600)
        print(result.stdout, flush=True)
        self.assertEqual(result.returncode, 0, result.stdout)


class ReachedTerminal(RuntimeError):
    pass


@cache
def fixture_text(filename):
    """One decompressed fixture, read once for every test that uses it."""
    return gzip.decompress((FIXTURES / filename).read_bytes()).decode()


class RuntimeWriterUpgradeTest(BaseTest):
    def fixtures(self, filenames=('main52.json.gz', 'current483.json.gz')):
        for filename in filenames:
            data = json.loads(fixture_text(filename))
            # Only relocate private paths; identity, authority and sidecar bytes
            # otherwise retain the actual writer's data and absence of fields.
            work = data['provenance']['work']
            data = json.loads(json.dumps(data).replace(str(Path(work).resolve()), str(self.work))
                              .replace(work, str(self.work)))
            yield filename, data

    def install(self, data, row):
        write(self.root / 'universe.json', data['universe'])
        directory = self.root / 'state/agents' / row['properties']['id']
        shutil.rmtree(directory, ignore_errors=True)
        write(directory / 'properties.json', row['properties'])
        write(directory / 'runtime.json', row['runtime'])
        launch = directory / 'launches' / row['runtime']['launch_id']
        launch.mkdir(parents=True, exist_ok=True)
        for name, value in row['sidecars'].items():
            write(launch / name, value)
        if row['ready']:
            (launch / 'ready').touch()
        (directory / '.launch.lock').touch()
        write(self.root / 'harnesses' / row['profile'] / 'harness.json', data['harnesses'][row['profile']])
        return directory

    def test_every_actual_writer_snapshot_retains_valid_authority(self):
        """Every runtime record real older (main52) and current mv2 wrote, at each lifecycle stage and for each harness
        profile, keeps valid authority; fragile because any tightening of damage() silently flags every agent started by
        an older mv2 as damaged after an upgrade."""
        profiles = set()
        for filename, data in self.fixtures():
            self.assertEqual(len(data['records']), 181)
            for row in data['records']:
                profiles.add(row['profile'])
                with self.subTest(writer=filename, profile=row['profile'], stage=row['label']):
                    directory = self.install(data, row)
                    self.assertIsNone(missing_runtime.damage(directory, row['runtime']))
                    if filename == 'main52.json.gz':
                        self.assertNotIn('pid_started', row['runtime'])
                        self.assertNotIn('runner_started', row['runtime'])
        self.assertEqual(len(profiles), 9)

    def test_live_records_refuse_duplicates_and_ended_records_reach_resume_preparation(self):
        """On real writer records, resume refuses live ones as duplicates and takes ended ones to terminal creation for
        each real adapter; fragile because resume reads adapter fields older writers lack, exercised only after an
        upgrade."""
        # Process/IPC observations describe the recorded stage. This exercises
        # the real resume entry point and each real adapter, up to (but never
        # through) creating a new terminal. The capture generator separately
        # ran actual private stop/resume cycles with both historical writers.
        stages = {'running', 'bridge-starting', 'bridge-validated', 'stopped', 'failed-child',
                  'dormant-hold', 'failed-before-terminal', 'failed-after-terminal'}
        for filename, data in self.fixtures():
            for row in data['records']:
                if row['label'] not in stages:
                    continue
                with self.subTest(writer=filename, profile=row['profile'], stage=row['label']):
                    directory = self.install(data, row)
                    props, facts = row['properties'], row['runtime']
                    adapter = harnesses.load(self.root, row['profile'])
                    alive = row['label'] in ('running', 'bridge-starting', 'bridge-validated')
                    live = 'alive' if alive else 'dormant'
                    if row['profile'].startswith('pi-') and row['ready']:
                        path = directory / 'session.jsonl'
                        path.parent.mkdir(parents=True, exist_ok=True)
                        path.write_text(json.dumps({'type': 'session', 'id': props['id']}) + '\n')
                    backend = Mock()
                    backend.create.side_effect = ReachedTerminal('upgrade reached terminal creation')
                    environment = {'CODEX_HOME': str(self.work / 'codex'),
                                   'CLAUDE_CONFIG_DIR': str(self.work / 'claude')}
                    with patch.dict(os.environ, environment), \
                         patch.object(lifecycle, 'Tmux', return_value=backend), \
                         patch.object(lifecycle, 'observe', return_value={'live': live}), \
                         patch.object(lifecycle.shutil, 'which', return_value=sys.executable), \
                         patch.object(attachments, 'wait'), patch.object(attachments, 'problem', return_value=None), \
                         patch.object(native, 'session_snapshot', return_value=(props, facts)), \
                         patch.object(native, 'start_shared_service', return_value=None), \
                         patch('multiverse.harnesses.claude.bridge_problem', return_value=None), \
                         patch('multiverse.harnesses.claude.claude_version', return_value=(2, 1, 302)):
                        if alive:
                            with self.assertRaisesRegex(ValueError, 'running or its liveness is unknown'):
                                lifecycle.start(self.root, resume=props['id'])
                            backend.create.assert_not_called()
                        elif not props.get('native_session_id') and not row['ready'] and adapter.family == 'codex':
                            with self.assertRaisesRegex(ValueError, 'No native session ID recorded'):
                                lifecycle.start(self.root, resume=props['id'])
                            backend.create.assert_not_called()
                        elif adapter.family == 'pi' and not row['ready']:
                            with self.assertRaisesRegex(ValueError, 'Native session file is missing'):
                                lifecycle.start(self.root, resume=props['id'])
                        else:
                            with self.assertRaisesRegex(RuntimeError, 'upgrade reached terminal creation') as caught:
                                lifecycle.start(self.root, resume=props['id'])
                            self.assertIsInstance(caught.exception.__cause__, ReachedTerminal)
                            backend.create.assert_called_once()

    def test_actual_kill_writer_ending_records_stay_valid_and_refuse_duplicates(self):
        """The 18 ending records a real kill wrote stay valid and refuse a duplicate launch; fragile because ending is a
        short window that only real kill timing produces."""
        for filename, data in self.fixtures(('ending-main52.json.gz', 'ending-current483.json.gz')):
            rows = [row for row in data['records'] if row['label'] == 'ending']
            self.assertEqual(len(rows), 9)
            self.assertEqual(len({row['profile'] for row in rows}), 9)
            for row in rows:
                with self.subTest(writer=filename, profile=row['profile']):
                    directory = self.install(data, row)
                    props, facts = row['properties'], row['runtime']
                    self.assertEqual(facts['state'], 'ending')
                    self.assertIsNone(missing_runtime.damage(directory, facts))
                    before = (directory / 'runtime.json').read_bytes()
                    backend = Mock()
                    with patch.object(lifecycle, 'Tmux', return_value=backend), \
                         patch.object(lifecycle, 'observe', return_value={'live': 'ending'}), \
                         patch.object(attachments, 'wait'), \
                         patch.object(native, 'session_snapshot', return_value=(props, facts)), \
                         patch.object(missing_runtime, 'discover', side_effect=AssertionError('healthy record discovery')):
                        with self.assertRaisesRegex(ValueError, 'running or its liveness is unknown'):
                            lifecycle.start(self.root, resume=props['id'])
                    backend.create.assert_not_called()
                    self.assertEqual((directory / 'runtime.json').read_bytes(), before)

    def test_real_writer_records_keep_their_relocation_boundary(self):
        """On real writer records, ended agents may be moved or removed and live ones may not; a break either strands
        old agents or trashes a running one, and only real writer bytes show it (1eb2abc)."""
        checked = 0
        filenames = ('main52.json.gz', 'current483.json.gz',
                     'ending-main52.json.gz', 'ending-current483.json.gz')
        for filename, data in self.fixtures(filenames):
            for row in data['records']:
                if filename.startswith('ending-') and row['label'] != 'ending':
                    continue
                with self.subTest(writer=filename, profile=row['profile'], stage=row['label']):
                    directory = self.install(data, row)
                    facts = row['runtime']
                    ended = facts['state'] in ('stopped', 'dormant', 'failed')
                    live = 'dormant' if ended else 'ending' if facts['state'] == 'ending' else 'alive'
                    # Only the recorded process observation is substituted. A
                    # historical healthy record must never need damaged-record
                    # discovery or touch its captured terminal/PIDs.
                    with patch.object(relocate, 'observe', return_value={'live': live, 'reason': ''}), \
                         patch.object(attachments, 'problem', return_value=None), \
                         patch.object(missing_runtime, 'require_stopped', side_effect=AssertionError('healthy authority')):
                        with ExitStack() as stack:
                            if ended:
                                relocate._hold(stack, [directory])
                            else:
                                with self.assertRaisesRegex(ValueError, 'stop it first'):
                                    relocate._hold(stack, [directory])
                    self.assertEqual(read(directory / 'runtime.json'), facts)
                    checked += 1
        self.assertEqual(checked, 380)
