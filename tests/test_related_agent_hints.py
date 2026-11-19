"""Missing local agents get advice, never an implicit target in another universe."""
from contextlib import chdir, redirect_stderr
from io import StringIO
import os
from pathlib import Path
import shlex
import stat
import time
from unittest.mock import patch
from uuid import uuid4

from test_multiverse import BaseTest
from multiverse import cli, store
from multiverse.tools import deliver, inbox, inspect, report, send
from multiverse.universe import agent_hints, agents, templates


class RelatedAgentHintsTest(BaseTest):
    def test_missing_records_across_siblings_share_the_search_retry_budget(self):
        """One leftover per sibling can otherwise hide a healthy match after the budget is spent waiting again."""
        qa = self.scope(self.root, 'qa')
        remote = self.agent(qa, 'remote')
        siblings = [self.scope(self.root, f'old-{number}') for number in range(12)]
        for scope in siblings:
            (scope / 'state/agents/leftover').mkdir()
        folders, elapsed = agent_hints.Search.folders, [0.]
        def ordered(search, path):
            return iter([qa, *siblings]) if path == self.root / 'subverses' else folders(search, path)
        with chdir(self.work), patch.object(agent_hints.Search, 'folders', ordered), \
                patch.object(agent_hints.time, 'monotonic', side_effect=lambda: elapsed[0]), \
                patch.object(agent_hints.time, 'sleep', side_effect=lambda delay: elapsed.__setitem__(0, elapsed[0] + delay)):
            with self.assertRaisesRegex(ValueError, '-u .multiverse/subverses/qa'):
                send.run(self.root, agent='remote', text='PRIVATE', delivery='inbox-only')
        self.assertLess(elapsed[0], .03)
        self.assertFalse((remote / 'inbox.jsonl').exists())

    def test_nested_limit_advice_searches_the_top_universe(self):
        """Recursive listing from a nested subverse cannot find a parent or sibling without selecting the tree top."""
        qa = self.scope(self.root, 'qa')
        deep = self.scope(qa, 'deep')
        self.agent(qa, 'remote')
        def limited(search, path):
            search.deadline = 0
            search.limited = True
            return None
        with chdir(self.work), patch.object(agent_hints.Search, 'record', limited):
            text = self.missing(deep, 'remote')
        self.assertIn('mv2 -u .multiverse list --recursive', text)
        self.assertNotIn('Related matches:', text)
        with chdir(self.work), patch.object(agent_hints, 'BUDGET', 0):
            text = self.missing(deep, 'remote')
        self.assertIn('could not verify its top universe', text)
        self.assertIn("From the related tree's top universe, run mv2 list --recursive", text)
        self.assertNotIn('mv2 -u .multiverse/subverses/qa/subverses/deep list --recursive', text)

    def test_a_record_gap_after_the_shared_retry_budget_stays_unverified(self):
        """A late replacement gap must yield incomplete advice rather than a completed missing-agent claim."""
        qa = self.scope(self.root, 'qa')
        old = self.scope(self.root, 'old')
        (old / 'state/agents/leftover').mkdir()
        remote = self.agent(qa, 'remote')
        folders, stat, missed = agent_hints.Search.folders, Path.lstat, []
        def ordered(search, path):
            return iter([qa, old]) if path == self.root / 'subverses' else folders(search, path)
        def gap(path, *args, **kwargs):
            if path == remote / 'properties.json' and not missed:
                missed.append(path)
                raise FileNotFoundError(2, 'replacing', str(path))
            return stat(path, *args, **kwargs)
        with chdir(self.work), patch.object(agent_hints.Search, 'folders', ordered), patch.object(Path, 'lstat', gap):
            with self.assertRaises(ValueError) as raised:
                send.run(self.root, agent='remote', text='PRIVATE', delivery='inbox-only')
        self.assertIn('search reached its limit', str(raised.exception))
        self.assertIn('mv2 -u .multiverse list --recursive', str(raised.exception))
        self.assertNotIn('Related matches:', str(raised.exception))
        self.assertEqual(len(missed), 1)
        self.assertFalse((remote / 'inbox.jsonl').exists())

    def test_recordless_neighbours_do_not_spend_the_hint_budget_one_at_a_time(self):
        """Missing properties in leftover folders must not exhaust advice time before a healthy subverse match."""
        qa = self.scope(self.root, 'qa')
        remote = self.agent(qa, 'remote')
        for number in range(12):
            (qa / 'state/agents' / f'leftover-{number}').mkdir()
        folders, elapsed = agent_hints.Search.folders, [0.]
        def ordered(search, path):
            return iter(sorted(folders(search, path), key=lambda directory: directory == remote))
        with chdir(self.work), patch.object(agent_hints.Search, 'folders', ordered), \
                patch.object(agent_hints.time, 'monotonic', side_effect=lambda: elapsed[0]), \
                patch.object(agent_hints.time, 'sleep', side_effect=lambda delay: elapsed.__setitem__(0, elapsed[0] + delay)):
            with self.assertRaisesRegex(ValueError, '-u .multiverse/subverses/qa'):
                send.run(self.root, agent='remote', text='PRIVATE', delivery='inbox-only')
        self.assertFalse((remote / 'inbox.jsonl').exists())

    def test_an_incomplete_search_explains_how_to_look_in_subverses(self):
        """When optional lookup reaches its bound, a bare not-found message hides the explicit subverse route."""
        qa = self.scope(self.root, 'qa')
        remote = self.agent(qa, 'remote')
        with chdir(self.work), patch.object(agent_hints, 'BUDGET', 0):
            with self.assertRaises(ValueError) as raised:
                send.run(self.root, agent='remote', text='PRIVATE', delivery='inbox-only')
            report_advice = self.missing(self.root, 'remote', universe_option='--blocked-on-universe')
        self.assertIn('mv2 -u .multiverse list --recursive', str(raised.exception))
        self.assertIn('-u PATH', str(raised.exception))
        self.assertIn('related tree', str(raised.exception))
        self.assertNotIn('Related matches:', str(raised.exception))
        self.assertIn('--blocked-on-universe PATH', report_advice)
        self.assertNotIn('-u PATH', report_advice)
        self.assertFalse((remote / 'inbox.jsonl').exists())

    def scope(self, parent, name):
        return templates.create(parent / 'subverses' / name, name, workdir=self.work)

    def agent(self, root, name):
        directory = root / 'state/agents' / str(uuid4())
        store.write(directory / 'properties.json', {'id': directory.name, 'name': name, 'harness': 'claude',
                    'universe_id': store.read(root / 'universe.json')['id'], 'workspace': str(self.work)})
        return directory

    def missing(self, root, name, **kwargs):
        with self.assertRaises(agents.AgentNotFound) as raised:
            agents.directory(root, name, **kwargs)
        return str(raised.exception)

    def test_related_matches_never_replace_local_resolution_or_choose_a_universe(self):
        """Cross-universe hints must not turn a missing or ambiguous local name into an implicit target."""
        qa, ops = self.scope(self.root, 'qa'), self.scope(self.root, 'ops')
        deep = self.scope(qa, 'deep')
        parent = self.agent(self.root, 'parent-worker')
        helper = self.agent(qa, 'helper')
        self.agent(ops, 'helper')
        nested = self.agent(deep, 'nested')
        legacy = self.agent(qa, 'legacy')
        value = store.read(legacy / 'properties.json')
        value.pop('id')
        store.write(legacy / 'properties.json', value)
        with chdir(self.work):
            text = self.missing(self.root, 'helper')
            self.assertIn('subverse qa', text)
            self.assertIn('subverse ops', text)
            self.assertIn('-u .multiverse/subverses/qa', text)
            self.assertIn('-u .multiverse/subverses/ops', text)
            self.assertIn('parent universe', self.missing(qa, 'parent-worker'))
            self.assertIn('-u .multiverse', self.missing(qa, 'parent-worker'))
            self.assertIn('subverse qa/deep', self.missing(ops, 'nested'))
            for reference in (helper.name, helper.name[:12]):
                self.assertIn(helper.name, self.missing(self.root, reference))
            self.assertIn(nested.name, self.missing(qa, 'nested'))
            self.assertIn(legacy.name, self.missing(self.root, 'legacy'))
            # A missing reference stays the same error when no related agent matches.
            self.assertEqual(self.missing(self.root, 'absent'),
                             "No agent named absent here. Run mv2 list to see this universe's agents.")
        local = self.agent(self.root, 'helper')
        with patch.object(agent_hints, 'hint', side_effect=AssertionError('successful lookup searched neighbours')):
            self.assertEqual(agents.directory(self.root, 'helper'), local)
            self.assertEqual(agents.directory(self.root, parent.name), parent)
            self.agent(self.root, 'helper')
            with self.assertRaises(agents.AmbiguousAgent):
                agents.directory(self.root, 'helper')

    def test_commands_preserve_the_right_flag_and_failed_sends_save_nothing_elsewhere(self):
        """Send's error formatter and blocked-on's separate universe flag must preserve actionable related matches."""
        qa = self.scope(self.root, 'qa')
        remote = self.agent(qa, 'remote')
        local = self.agent(self.root, 'local')
        before = (local / 'properties.json').read_bytes()
        with chdir(self.work):
            with self.assertRaisesRegex(agents.AgentNotFound, '--blocked-on-universe .multiverse/subverses/qa'):
                report.run(self.root, agent='local', phase='blocked', blocked_on='remote')
            self.assertEqual((local / 'properties.json').read_bytes(), before)
            with self.assertRaisesRegex(ValueError, '-u .multiverse/subverses/qa'):
                send.run(self.root, agent='remote', text='PRIVATE', delivery='inbox-only')
            self.assertFalse((remote / 'inbox.jsonl').exists())
            output = []
            result = send.run(self.root, agent='remote,local', text='PRIVATE', delivery='inbox-only',
                              _present=output.append)
            self.assertFalse(result['ok'])
            self.assertIn('-u .multiverse/subverses/qa', result['recipients'][0]['reason'])
            self.assertIn('-u .multiverse/subverses/qa', output[0])
            self.assertEqual(result['recipients'][1]['delivery'], 'inbox-only')
            self.assertIn('PRIVATE', (local / 'inbox.jsonl').read_text())
            self.assertFalse((remote / 'inbox.jsonl').exists())
            for call in (lambda: inbox.unread(self.root, 'remote'), lambda: inspect.facts(self.root, 'remote'),
                         lambda: deliver.run(self.root, agent='remote', check=True)):
                with self.subTest(command=call), self.assertRaisesRegex(agents.AgentNotFound, '-u .multiverse/subverses/qa'):
                    call()
            with self.assertRaisesRegex(ValueError, '^Could not find one matching agent. Run mv2 list and use its name.$'):
                send.run(self.root, agent='absent', text='PRIVATE', delivery='inbox-only')
            result = report.run(self.root, agent='local', phase='blocked', blocked_on='remote', blocked_on_universe='qa')
            self.assertEqual(result['blocked_on']['id'], remote.name)

    def test_scope_labels_and_printed_paths_survive_literal_names_and_cli_parsing(self):
        """A printed path must parse as an option value, and structural labels must preserve literal subverse names."""
        for workspace_name in ('-project', 'space project', "quote'project"):
            with self.subTest(workspace=workspace_name), chdir(self.work):
                workspace = self.work / workspace_name
                parent = templates.create(workspace / '.multiverse', workspace_name, workdir=workspace)
                scope = self.scope(parent, 'subverses')
                self.agent(scope, 'remote')
                nested = self.scope(scope, 'qa')
                self.agent(nested, 'nested')
                for option in ('-u', '--blocked-on-universe'):
                    with self.subTest(option=option):
                        advice = self.missing(parent, 'remote', universe_option=option).split('add ', 1)[1].removesuffix('.')
                        tokens = shlex.split(advice)
                        argv = ([*tokens, 'inspect', 'remote'] if option == '-u' else
                                ['report', '--phase', 'blocked', '--blocked-on', 'remote', *tokens])
                        try:
                            with redirect_stderr(StringIO()):
                                parsed = cli.parser().parse_args(argv)
                        except SystemExit:
                            self.fail(f'The printed option cannot be parsed: {advice}')
                        value = parsed.root if option == '-u' else parsed.blocked_on_universe
                        self.assertEqual((self.work / value).resolve(), scope)
                self.assertIn('in subverse subverses:', self.missing(parent, 'remote'))
                self.assertIn('in subverse subverses/qa:', self.missing(parent, 'nested'))

    def test_damaged_neighbours_and_search_limits_are_read_only_and_nonblocking(self):
        """Optional hints must skip damaged files and cycles without blocking, writing state or hiding healthy neighbours."""
        qa = self.scope(self.root, 'qa')
        helper = self.agent(qa, 'helper')
        malformed = self.agent(qa, 'malformed')
        value = store.read(malformed / 'properties.json')
        store.write(malformed / 'properties.json', {**value, 'harness': []})
        broken = self.scope(self.root, 'broken')
        nested = self.scope(broken, 'nested')
        self.agent(nested, 'nested-helper')
        (broken / 'universe.json').write_text('{broken')
        fifo = self.scope(self.root, 'fifo')
        (fifo / 'universe.json').unlink()
        os.mkfifo(fifo / 'universe.json')
        for kind in ('fifo', 'huge', 'truncated', 'directory'):
            path = qa / 'state/agents' / kind / 'properties.json'
            path.parent.mkdir()
            if kind == 'fifo':
                os.mkfifo(path)
            elif kind == 'directory':
                path.mkdir()
            else:
                path.write_text('x' * (agent_hints.FILE_BYTES + 1) if kind == 'huge' else '{broken')
        (qa / 'subverses/loop').symlink_to(self.root, target_is_directory=True)
        outside = templates.create(self.work / 'outside', 'outside', workdir=self.work)
        self.agent(outside, 'outside-worker')
        (self.root / 'subverses/outside-link').symlink_to(outside, target_is_directory=True)
        def snapshot():
            return {str(p): p.read_bytes() for p in self.root.rglob('*') if stat.S_ISREG(p.lstat().st_mode)}
        before = snapshot()
        started = time.monotonic()
        self.assertIn(helper.name, self.missing(self.root, 'helper'))
        self.assertIn('nested-helper', self.missing(self.root, 'nested-helper'))
        self.assertNotIn('Related matches', self.missing(self.root, 'outside-worker'))
        self.assertNotIn('Related matches', self.missing(self.root, 'malformed'))
        self.assertLess(time.monotonic() - started, 1)
        self.assertEqual(snapshot(), before)
        with patch.object(agent_hints, 'BUDGET', 0):
            self.assertNotIn('Related matches', self.missing(self.root, 'helper'))
        scanner = os.scandir
        def inaccessible(path):
            if Path(path) == qa / 'state/agents':
                raise PermissionError('unreadable neighbour')
            return scanner(path)
        with patch.object(agent_hints.os, 'scandir', side_effect=inaccessible):
            self.assertIn('nested-helper', self.missing(self.root, 'nested-helper'))
