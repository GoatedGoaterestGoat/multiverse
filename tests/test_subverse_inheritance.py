"""A new subverse copies its parent's definitions without adopting its live state or refresh mistakes."""
import json
import os
from pathlib import Path
import shutil
from unittest.mock import patch

from test_multiverse import BaseTest
from multiverse import universe
from multiverse.store import read, write
from multiverse.tools import subverse, refresh
from multiverse.ui.backend import LocalBackend
from multiverse.universe import inheritance, templates, shipped_harnesses


class SubverseInheritanceTest(BaseTest):
    def test_default_copies_current_parent_definitions_without_private_state(self):
        """A clean-base fallback silently drops parent edits, while copying the whole parent leaks live state."""
        path = self.root / 'harnesses/claude/harness.json'
        data = read(path)
        data['description'] = 'Parent custom Claude'
        write(path, data)
        (self.root / 'bootstrap.md').write_text('PARENT INSTRUCTIONS\n')
        write(self.root / 'views/default.json', {'title': 'Parent view', 'columns': ['name']})
        write(self.root / 'skills/custom/entry.json', {'description': 'Local skill'})
        (self.root / 'skills/custom/SKILL.md').write_text('LOCAL SKILL\n')
        write(self.root / 'commands/local/entry.json', {'description': 'Local command'})
        (self.root / 'commands/local/prompt.md').write_text('LOCAL COMMAND\n')
        (self.root / 'commands/help/prompt.md').unlink()
        templates.create(self.root / 'subverses/existing', 'existing', workdir=self.work)
        write(self.root / 'plugins.json', {'schema': 1, 'enabled': {'remote': True}, 'access': {'remote': 'act'}})
        write(self.root / 'plugin-settings/remote.json', {'schema': 1, 'rate_per_minute': 25})
        managed = self.root / 'plugins/example/README.md'
        managed.parent.mkdir(parents=True)
        managed.write_text('A template-managed plugin definition\n')
        manifest = read(self.root / templates.MANIFEST)
        manifest['files']['plugins/example/README.md'] = templates.fingerprint(managed)
        write(self.root / 'artifacts/form.json', {'title': 'Parent template form'})
        manifest['files']['artifacts/form.json'] = templates.fingerprint(self.root / 'artifacts/form.json')
        (self.root / 'artifacts/private-output.txt').write_text('GENERATED DO NOT COPY')
        write(self.root / 'plugin-secrets/secrets.json', {'value': 'PRIVATE STORE DO NOT COPY'})
        manifest['files']['plugin-secrets/secrets.json'] = templates.fingerprint(self.root / 'plugin-secrets/secrets.json')
        manifest['files']['state/private'] = templates.fingerprint(managed)
        manifest['private'] = 'NOT INSTALL PROVENANCE'
        write(self.root / templates.MANIFEST, manifest)
        parent_id = read(self.root / 'universe.json')['id']
        secret_store = Path.home() / '.multiverse/plugin-secrets'
        secret_store.mkdir(parents=True, mode=0o700)
        write(secret_store / 'secrets.json', {parent_id: {'remote': {'api_key': 'FAKE EXTERNAL SECRET'}}})
        write(secret_store / 'index.json', {parent_id: {'remote': {'api_key': {'store': 'file', 'at': 'test'},
                                                              'token': {'store': 'keychain', 'at': 'test'}}}})
        (secret_store / 'secrets.json').chmod(0o600)
        external = {path.name: path.read_bytes() for path in secret_store.iterdir()}
        for name in ('state/agents/private/inbox.jsonl', 'state/agents/private/runtime.json',
                     'state/plugins/remote/token', 'auth.json', 'trusted-plugins.json'):
            target = self.root / name
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text('PRIVATE DO NOT COPY\n')
        child = Path(subverse.run(self.root, name='qa')['universe'])
        for name in ('bootstrap.md', '.gitignore', 'artifacts/form.json', 'views/default.json', 'harnesses/claude/harness.json',
                     'skills/custom/SKILL.md', 'commands/local/prompt.md', 'plugins.json', 'plugin-settings/remote.json'):
            self.assertTrue((child / name).is_file(), name)
            self.assertEqual((child / name).read_bytes(), (self.root / name).read_bytes(), name)
        self.assertEqual((child / 'plugins/example/README.md').read_bytes(), managed.read_bytes())
        inherited = read(child / templates.MANIFEST)
        self.assertNotIn('private', inherited)
        self.assertNotIn('state/private', inherited['files'])
        self.assertFalse((child / 'artifacts/private-output.txt').exists())
        self.assertNotEqual(read(child / 'universe.json')['id'], read(self.root / 'universe.json')['id'])
        self.assertFalse((child / 'plugin-secrets').exists())
        self.assertNotIn('plugin-secrets/secrets.json', read(child / templates.MANIFEST)['files'])
        self.assertNotIn(b'FAKE EXTERNAL SECRET', b''.join(path.read_bytes() for path in child.rglob('*') if path.is_file()))
        self.assertEqual(universe.workspace(child), universe.workspace(self.root))
        self.assertEqual(list((child / 'state/agents').iterdir()), [])
        self.assertEqual(list((child / 'subverses').iterdir()), [])
        self.assertFalse((child / 'commands/help/prompt.md').exists())
        for name in ('state/plugins/remote/token', 'auth.json', 'trusted-plugins.json'):
            self.assertFalse((child / name).exists(), name)
        (self.root / 'bootstrap.md').write_text('PARENT LATER\n')
        self.assertEqual((child / 'bootstrap.md').read_text(), 'PARENT INSTRUCTIONS\n')
        (child / 'bootstrap.md').write_text('CHILD INSTRUCTIONS\n')
        grand = Path(subverse.run(child, name='deep')['universe'])
        self.assertEqual((grand / 'bootstrap.md').read_text(), 'CHILD INSTRUCTIONS\n')
        # The keychain service "mv2 plugin secret" also keys accounts by universe ID, never by a copied parent ID.
        identities = {read(scope / 'universe.json')['id'] for scope in (self.root, child, grand)}
        self.assertEqual(len(identities), 3)
        self.assertEqual({path.name: path.read_bytes() for path in secret_store.iterdir()}, external)
        clean = Path(subverse.run(self.root, name='clean', template='base')['universe'])
        self.assertEqual((clean / 'bootstrap.md').read_bytes(), (templates.TEMPLATES / 'base/bootstrap.md').read_bytes())
        self.assertFalse((clean / 'commands/local').exists())
        dashboard = LocalBackend().subverse(self.root, {'name': 'dashboard'})
        self.assertEqual((dashboard / 'bootstrap.md').read_text(), 'PARENT LATER\n')
        other = self.work / 'other-workspace'
        other.mkdir()
        placed = Path(subverse.run(self.root, name='placed', workspace=str(other))['universe'])
        self.assertEqual(universe.workspace(placed), other)

    def test_inherited_refresh_uses_the_parents_main_era_base(self):
        """Recording inherited edits as shipped can later restore deleted permission flags or lose the new keys."""
        source = self.work / 'template'
        shutil.copytree(templates.TEMPLATES / 'Silicon23', source)
        path = 'harnesses/claude/harness.json'
        current = (source / path).read_text()
        old = next(text for text in shipped_harnesses.TEXTS.values()
                   if json.loads(text).get('adapter') == 'claude'
                   and json.loads(text).get('settings') == {'args': ['--dangerously-skip-permissions']})
        (source / path).write_text(old)
        parent = templates.create(self.work / 'parent', 'parent', str(source), self.work)
        manifest = read(parent / templates.MANIFEST)
        manifest.pop('bases')  # Main-era record: exact installed fingerprints, no saved base texts.
        write(parent / templates.MANIFEST, manifest)
        before = (parent / templates.MANIFEST).read_bytes()
        edited = read(parent / path)
        edited['settings'].pop('args')
        edited['settings']['model'] = 'sonnet'
        write(parent / path, edited)
        (source / path).write_text(current)
        child = Path(subverse.run(parent, name='qa')['universe'])
        self.assertEqual(read(child / 'universe.json')['template'], str(source))
        self.assertEqual(read(child / path), edited)
        inherited = read(child / templates.MANIFEST)
        self.assertEqual(inherited['files'][path], manifest['files'][path])
        self.assertEqual(inherited['bases'][path], old)
        self.assertEqual((parent / templates.MANIFEST).read_bytes(), before)
        refresh.run(child, apply=True)
        settings = read(child / path)['settings']
        self.assertNotIn('args', settings)
        self.assertEqual(settings['model'], 'sonnet')
        self.assertIn('models', settings)
        self.assertIn('model_names', settings)

    def test_unprovable_parent_install_refuses_without_partial_creation(self):
        """Guessing an install base after missing or damaged provenance can turn inherited deletions into defaults."""
        saved = (self.root / templates.MANIFEST).read_bytes()
        for name, value in [('missing', None), ('damaged', b'[]'), ('unknown', json.dumps({
                'schema': 1, 'template': 'missing-template', 'files': {}}).encode()),
                ('bad-base', json.dumps({**read(self.root / templates.MANIFEST),
                                       'bases': {'harnesses/claude/harness.json': '{}'}}).encode())]:
            with self.subTest(name=name):
                path = self.root / templates.MANIFEST
                path.unlink(missing_ok=True)
                if value is not None:
                    path.write_bytes(value)
                with self.assertRaisesRegex(ValueError, '--template'):
                    subverse.run(self.root, name=name)
                self.assertFalse((self.root / 'subverses' / name).exists())
                clean = Path(subverse.run(self.root, name=name + '-clean', template='base')['universe'])
                self.assertTrue((clean / 'harnesses/claude/harness.json').is_file())
        (self.root / templates.MANIFEST).write_bytes(saved)

    def test_unsafe_definition_or_changed_provenance_leaves_no_child(self):
        """A link can expose private state and a torn copy can attach local edits to another template's base."""
        private = self.work / 'private'
        private.mkdir()
        (private / 'secret').write_text('DO NOT COPY')
        target = self.root / 'commands/unsafe'
        self.addCleanup(target.unlink, missing_ok=True)
        for kind in ('directory-link', 'file-link', 'fifo', 'changed-manifest'):
            target.unlink(missing_ok=True)
            with self.subTest(kind=kind):
                if kind == 'directory-link':
                    target.symlink_to(private, target_is_directory=True)
                elif kind == 'file-link':
                    target.symlink_to(private / 'secret')
                elif kind == 'fifo':
                    os.mkfifo(target)
                original = inheritance._snapshot
                def snapshot(root, path):
                    result = original(root, path)
                    if kind == 'changed-manifest' and root == self.root and path == 'bootstrap.md':
                        record = read(root / templates.MANIFEST)
                        record['template'] = 'Silicon23'
                        write(root / templates.MANIFEST, record)
                    return result
                with patch.object(inheritance, '_snapshot', side_effect=snapshot):
                    with self.assertRaises((ValueError, OSError)) as raised:
                        subverse.run(self.root, name=kind)
                if kind == 'file-link':
                    self.assertIsInstance(raised.exception, ValueError)
                    self.assertIn(str(self.root), str(raised.exception))
                    self.assertIn('commands/unsafe', str(raised.exception))
                    self.assertIn('--template', str(raised.exception))
                target.unlink(missing_ok=True)
                self.assertFalse((self.root / 'subverses' / kind).exists())
                self.assertEqual(list((self.root / 'subverses').glob('.mv2-subverse-*')), [])
