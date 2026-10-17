"""mv2 packages every bundled plugin's Claude directory with its hidden manifest.

Tree compaction is the checkout plugin plugins/compaction (GOAT), whose tests are
its own; mv2's suite runs them through test_plugin_checkouts.
"""
from pathlib import Path
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))


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
