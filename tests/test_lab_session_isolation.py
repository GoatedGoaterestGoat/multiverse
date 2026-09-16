"""Independent LAB-SESSION-004 regression using real runner subprocesses."""
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from uuid import uuid4

SOURCE = Path(os.environ.get('MV2_SESSION_TEST_SOURCE', Path(__file__).resolve().parents[1] / 'src'))
sys.path.insert(0, str(SOURCE))
from multiverse.store import read, write


class CrossHarnessParentIdentity(unittest.TestCase):
    def test_every_shipped_manifest_clears_both_parent_markers(self):
        manifests = sorted((SOURCE / 'multiverse/templates').glob('*/harnesses/*/harness.json'))
        self.assertGreaterEqual(len(manifests), 5, 'Include base and both reusable role templates')
        for manifest in manifests:
            with self.subTest(manifest=str(manifest.relative_to(SOURCE))):
                spec = read(manifest)
                with tempfile.TemporaryDirectory(prefix='mv2-parent-markers-') as temp:
                    directory = Path(temp) / 'agent'
                    launch_id = str(uuid4())
                    launch = directory / 'launches' / launch_id
                    launch.mkdir(parents=True)
                    output = Path(temp) / 'child.json'
                    code = ('import json,os,pathlib; pathlib.Path(' + repr(str(output)) +
                            ').write_text(json.dumps({k:v for k,v in os.environ.items() '
                            'if k in ("CODEX_THREAD_ID","CLAUDECODE","MV2_AGENT_ID")}))')
                    write(directory / 'runtime.json', {'launch_id': launch_id})
                    write(launch / 'request.json', {
                        'argv': [sys.executable, '-c', code],
                        'env': {'MV2_CLI': str(Path(temp) / 'mv2'), 'MV2_AGENT_ID': 'child-agent'},
                        'unset_env': spec.get('unset_env', [])})
                    (launch / 'ready').touch()
                    result = subprocess.run([sys.executable, str(SOURCE / 'multiverse/runner.py'), str(launch)],
                        env={**os.environ, 'CODEX_THREAD_ID': 'synthetic-parent-codex',
                             'CLAUDECODE': 'synthetic-parent-claude', 'MV2_AGENT_ID': 'parent-agent'},
                        text=True, capture_output=True, timeout=10)
                    self.assertEqual(result.returncode, 0, result.stderr)
                    seen = read(output)
                    inherited = sorted(set(seen) & {'CODEX_THREAD_ID', 'CLAUDECODE'})
                    print(str(manifest.relative_to(SOURCE)), 'inherited_native_markers=', inherited)
                    self.assertEqual(seen.get('MV2_AGENT_ID'), 'child-agent')
                    self.assertEqual(inherited, [])


if __name__ == '__main__':
    unittest.main()
