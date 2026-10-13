"""What stays in mv2 of tree compaction, which is the checkout plugin plugins/compaction (GOAT).

The plugin's own tests are GOAT's (plugins/compaction/tests), which mv2's suite
runs through test_plugin_checkouts when the submodule is there. These are mv2's:
the `python -m multiverse.compaction` stub that older launches' PreCompact guard
calls, and the packaging of the plugins mv2 still bundles.
"""
import json
import os
from pathlib import Path
import subprocess
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))
from test_multiverse import BaseTest

SOURCE = str(Path(__file__).resolve().parents[1] / 'src')
CLEARED = ('MV2_UNIVERSE', 'MV2_AGENT_ID', 'MV2_AGENT_DIR', 'MV2_LAUNCH_ID', 'MV2_PLUGIN_PATH')


class StubTest(BaseTest):
    def guard(self):
        environment = {key: value for key, value in os.environ.items() if key not in CLEARED}
        return subprocess.run([sys.executable, '-m', 'multiverse.compaction', 'guard'], input='{}', text=True,
                              capture_output=True, cwd=self.work, env={**environment, 'PYTHONPATH': SOURCE})

    def test_the_stub_never_lets_a_guard_pass(self):
        # An older launch's guard is `python -m multiverse.compaction guard || exit 2`: whatever the stub answers
        # (the plugin's guard, or an error when the plugin is not here), it must not be 0, or Claude compacts.
        result = self.guard()
        self.assertNotEqual(result.returncode, 0, result.stdout + result.stderr)


class PackagingTest(unittest.TestCase):
    def test_every_bundled_plugins_claude_manifest_is_packaged(self):
        # Without its hidden .claude-plugin/plugin.json an installed copy's Claude directory is refused. `**` does
        # not reach hidden directories in package-data, so only an explicit pattern counts.
        import tomllib
        from pathlib import PurePosixPath
        import multiverse
        package = Path(multiverse.__file__).parent
        pyproject = tomllib.loads((package.parent.parent / 'pyproject.toml').read_text())
        patterns = [pattern for pattern in pyproject['tool']['setuptools']['package-data']['multiverse'] if '**' not in pattern]
        manifests = sorted(package.glob('plugins/builtin/*/claude/.claude-plugin/plugin.json'))
        self.assertTrue(manifests)
        for manifest in manifests:
            relative = PurePosixPath(manifest.relative_to(package).as_posix())
            with self.subTest(manifest=str(relative)):
                self.assertTrue(any(relative.match(pattern) for pattern in patterns))


if __name__ == '__main__':
    unittest.main()
