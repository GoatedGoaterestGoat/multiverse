"""Run the source-owned Pi extension against deterministic native lifecycle fakes."""
from pathlib import Path
import shutil
import subprocess
import unittest


class PiBridgeTest(unittest.TestCase):
    @unittest.skipUnless(shutil.which('node'), 'Node is required for Pi bridge tests')
    def test_bridge_lifecycle_and_delivery(self):
        node = shutil.which('node')
        major = int(subprocess.check_output([node, '--version'], text=True).lstrip('v').split('.')[0])
        if major < 22:
            self.skipTest('Node 22+ required for native TypeScript test loading')
        result = subprocess.run([node, '--experimental-strip-types', str(Path(__file__).with_name('pi_bridge_test.mjs'))],
                                text=True, capture_output=True, timeout=15)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)


if __name__ == '__main__':
    unittest.main()
