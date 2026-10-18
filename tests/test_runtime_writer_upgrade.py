"""Actual old/current core writers remain valid across authority validation."""
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
from test_multiverse import BaseTest


FIXTURES = Path(__file__).parent / 'fixtures/runtime-writers'
GENERATOR_SOURCE = os.environ.get('MV2_GENERATE_RUNTIME_MATRIX')
GENERATOR_OUTPUT = os.environ.get('MV2_RUNTIME_MATRIX_OUTPUT')


@unittest.skipUnless(GENERATOR_SOURCE and GENERATOR_OUTPUT, 'manual historical fixture capture')
class GenerateRuntimeMatrix(BaseTest):
    def test_generate(self):
        result = subprocess.run([sys.executable, str(FIXTURES / 'generate.py'),
                                 GENERATOR_SOURCE, GENERATOR_OUTPUT], text=True,
                                stdout=subprocess.PIPE, stderr=subprocess.STDOUT, timeout=600)
        print(result.stdout, flush=True)
        self.assertEqual(result.returncode, 0, result.stdout)


class ReachedTerminal(RuntimeError):
    pass


class RuntimeWriterUpgradeTest(BaseTest):
    def fixtures(self):
        for filename in ('main52.json.gz', 'current483.json.gz'):
            data = json.loads(gzip.decompress((FIXTURES / filename).read_bytes()))
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
