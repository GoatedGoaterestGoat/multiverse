"""A resume keeps the native database which contains the saved conversation."""
import json
import os
from pathlib import Path
import shutil
import subprocess
import unittest
from unittest.mock import patch

from native_processes import PrivateDaemonGroups
from native_safety import install, isolated_home
from multiverse.runtime import lifecycle
from multiverse.store import read, write
from multiverse.universe import agents
from test_multiverse import TerminalBase

install()


@unittest.skipUnless(shutil.which('codex') and shutil.which('tmux'), 'Codex and tmux required')
class StorageResumeNativeTest(TerminalBase):
    def test_saved_database_root_wins_over_a_different_caller_override(self):
        if subprocess.check_output(['codex', '--version'], stdin=subprocess.DEVNULL,
                                   text=True).strip() != 'codex-cli 0.163.0':
            self.skipTest('native storage evidence pinned to 0.163.0')
        home = isolated_home(self.work / 'native-home', config=
            'model="fixture"\nmodel_provider="fixture"\n'
            '[model_providers.fixture]\nname="Private storage fixture"\n'
            'base_url="http://127.0.0.1:9/v1"\nwire_api="responses"\nrequires_openai_auth=false\n'
            f'[projects.{json.dumps(str(self.work))}]\ntrust_level="trusted"\n')
        managed = home / 'packages/standalone/current/codex'
        managed.parent.mkdir(parents=True)
        managed.symlink_to(Path(shutil.which('codex')).resolve())
        database_a, database_b = self.work / 'database-a', self.work / 'database-b'
        database_a.mkdir()
        database_b.mkdir()
        environment = {**os.environ, 'CODEX_HOME': str(home), 'CODEX_SQLITE_HOME': str(database_a)}
        write(self.root / 'harnesses/codex/harness.json', {'schema': 2, 'adapter': 'codex',
                                                       'settings': {'inbox_bridge': True}})
        groups = PrivateDaemonGroups()
        self.enterContext(groups.observe_starts())
        result = None

        def stop_daemon():
            try:
                subprocess.run(['codex', 'app-server', 'daemon', 'stop'], env=environment,
                               stdin=subprocess.DEVNULL, capture_output=True, timeout=30, check=True)
            finally:
                groups.close()

        def stop_agent():
            if result is not None:
                lifecycle.kill(self.root, result['id'])

        self.addCleanup(stop_daemon)
        self.addCleanup(stop_agent)
        with patch.dict(os.environ, environment):
            result = lifecycle.start(self.root, 'saved-database', '', 'codex', socket_path=self.socket,
                                     wait=True, wait_timeout=30)
        self.assertTrue(result['ready'], result)
        directory = agents.directory(self.root, result['id'])
        before = read(directory / 'properties.json')
        self.assertEqual(before['native_environment']['CODEX_SQLITE_HOME'], str(database_a))
        databases = list(database_a.glob('*.sqlite'))
        self.assertTrue(databases, 'Native server did not open a database in the requested initial root')
        original_files = {path.name: path.stat().st_ino for path in databases}
        lifecycle.kill(self.root, result['id'])
        with patch.dict(os.environ, {**environment, 'CODEX_SQLITE_HOME': str(database_b)}):
            resumed = lifecycle.start(self.root, resume=result['id'], socket_path=self.socket,
                                      wait=True, wait_timeout=30)
        self.assertTrue(resumed['ready'], resumed)
        after = read(directory / 'properties.json')
        self.assertEqual(after['native_session_id'], before['native_session_id'])
        self.assertEqual(after['native_service_identity'], before['native_service_identity'])
        self.assertEqual(after['native_environment']['CODEX_SQLITE_HOME'], str(database_a))
        self.assertEqual({path.name: path.stat().st_ino for path in database_a.glob('*.sqlite')}, original_files)
        self.assertEqual(list(database_b.iterdir()), [], 'Caller override opened a different native database')
        print('Saved SQLITE scope: retained A; same native thread/service; caller B has no files.')
