"""Readable output of spawn, resume, list, inspect and doctor; `--json` keeps the original records."""
import contextlib
import io
from pathlib import Path
import sys
import unittest
from unittest.mock import patch
from uuid import uuid4

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))
from multiverse.runtime import lifecycle
from multiverse import cli
from multiverse.store import write
from multiverse.tools import doctor, inspect, list_agents, resume, spawn
from multiverse import display
from test_multiverse import BaseTest


def row(name, live='alive', status='idle', phase='done', summary='', universe=None, **extra):
    return {'id': str(uuid4()), 'name': name, 'harness': 'codex', 'live': live, 'status': status, 'phase': phase,
            'summary': summary, 'universe': universe or '', 'runtime': {}, **extra}


class ListTest(BaseTest):
    def listing(self, *rows):
        return patch.object(lifecycle, 'listing', return_value=[{**r, 'universe': r['universe'] or str(self.root)} for r in rows])

    def test_shorten_cuts_at_a_word_and_only_a_single_long_word_inside(self):
        text = 'Reviewing the compaction status line and its tests, then the docs'
        for limit in (20, 30, 40, 50):
            short = display.shorten(text, limit)
            self.assertLessEqual(len(short), limit)
            self.assertTrue(short.endswith('…'))
            self.assertIn(short[:-1], [' '.join(text.split()[:count]) for count in range(1, 12)], short)
        self.assertEqual(display.shorten(text, 200), text)
        self.assertEqual(display.shorten('a  b\n c', 20), 'a b c')
        self.assertEqual(display.shorten('x' * 100, 10), 'x' * 9 + '…')  # no word to stop at
        self.assertEqual(display.shorten(None), '')

    def test_state_says_what_a_live_agent_does_otherwise_why_it_is_not_live(self):
        self.assertEqual(display.state(row('a', status='working')), 'working')
        self.assertEqual(display.state(row('a', status='waiting')), 'waiting')
        self.assertEqual(display.state(row('a', status='unknown')), 'alive')
        self.assertEqual(display.state(row('a', live='dormant', status='—')), 'dormant')
        self.assertEqual(display.state(row('a', live='stopped', status='—')), 'stopped')

    def test_rows_have_no_ids_and_no_universe_for_this_universe(self):
        with self.listing(row('alpha', status='working', phase='working', summary='Reading the diff'),
                          row('beta', live='dormant', status='—')):
            text = list_agents.run(self.root)
        self.assertEqual(text.splitlines()[0].split(), ['alpha', 'working', 'working', 'Reading', 'the', 'diff'])
        self.assertEqual(text.splitlines()[1].split(), ['beta', 'dormant', 'done'])
        self.assertNotIn(str(self.root), text)

    def test_other_subverses_and_universes_show_where_they_are(self):
        sub = str(self.root / 'subverses' / 'pr-review')
        deep = str(self.root / 'subverses' / 'pr-review' / 'subverses' / 'lint')
        with self.listing(row('here'), row('there', universe=sub), row('deeper', universe=deep),
                          row('away', universe='/elsewhere/.multiverse')):
            lines = list_agents.run(self.root, recursive=True).splitlines()
        self.assertTrue(lines[0].endswith('done'))
        self.assertTrue(lines[1].endswith('[pr-review]'))
        self.assertTrue(lines[2].endswith('[pr-review/lint]'))
        self.assertTrue(lines[3].endswith('[/elsewhere/.multiverse]'))

    def test_agents_waiting_on_a_person_come_first_and_the_rest_keep_their_order(self):
        with self.listing(row('a'), row('b', status='waiting'), row('c', status='working'), row('d', status='waiting')):
            names = [line.split()[0] for line in list_agents.run(self.root).splitlines()]
        self.assertEqual(names, ['b', 'd', 'a', 'c'])

    def test_summaries_are_shortened_unless_wide(self):
        summary = 'word ' * 40
        with self.listing(row('a', summary=summary)):
            short = list_agents.run(self.root).splitlines()
            wide = list_agents.run(self.root, wide=True).splitlines()
        self.assertEqual(len(short), 1)
        self.assertTrue(short[0].endswith('…'))
        self.assertLessEqual(len(short[0]), 24 + 2 + 8 + 2 + 8 + 2 + 80 + 1)
        self.assertEqual(wide[0].split(None, 3)[3], summary.strip())

    def test_an_error_keeps_what_happened_and_the_end_of_the_tail_unless_wide(self):
        tail = [f'terminal line {n}' for n in range(1, 9)]
        error = 'Native process exited with code 1 during startup.\n' + '\n'.join(tail) + '\nNo conversation found with session ID: x\n'
        with self.listing(row('a', launch_error=error), row('b', launch_error='Launch failed\nsecond line'), row('c', launch_error='y' * 500)):
            lines = list_agents.run(self.root).splitlines()
            wide = list_agents.run(self.root, wide=True).splitlines()
        self.assertEqual(lines[1], '  ! Native process exited with code 1 during startup. | … | terminal line 8 | No conversation found with session ID: x')
        self.assertEqual(lines[3], '  ! Launch failed | second line')  # short errors show whole
        self.assertTrue(lines[5].endswith('…'))
        self.assertLessEqual(len(lines[5]), 4 + 200)
        self.assertIn('terminal line 1 | terminal line 2', wide[1])
        self.assertNotIn('…', wide[1])

    def test_json_and_an_empty_list_keep_their_old_shape(self):
        rows = [row('a')]
        with self.listing(*rows):
            self.assertEqual([r['name'] for r in list_agents.run(self.root, json=True)], ['a'])
        with patch.object(lifecycle, 'listing', return_value=[]):
            self.assertEqual(list_agents.run(self.root), 'No agents.')

    def test_automatic_reconnect_is_a_note_not_an_error(self):
        with self.listing(row('worker', recovery_note='Reconnected automatically after native relay loss')):
            text = list_agents.run(self.root)
            original = list_agents.run(self.root, json=True)
        self.assertEqual(text.splitlines()[-1], '  Note: Reconnected automatically.')
        self.assertNotIn('  ! ', text)
        self.assertTrue(original[0]['recovery_note'])


class InspectTest(BaseTest):
    CAPTURE = 'line one\n' + '─' * 200 + ' title ─\n❯ prompt\n\n\n\n'

    def inspect(self, item, capture=CAPTURE, **options):
        with patch.object(inspect, 'facts', return_value=item), \
                patch.object(inspect, 'run', return_value=capture):
            return inspect.cli_run(self.root, agent=item['name'], **options)

    def test_key_facts_come_first_then_the_end_of_the_terminal(self):
        item = row('worker', status='working', phase='working', summary='Reading   the\ndiff',
                   runtime={'control_endpoint': {'transport': 'x'}})
        lines = self.inspect(item).splitlines()
        self.assertEqual(lines[0], 'worker · Codex, bridge · working')
        self.assertEqual(lines[1], 'Reported: working — Reading the diff')
        self.assertEqual(lines[2:], ['', 'Terminal (last 3 lines):', 'line one', '─── title ─', '❯ prompt'])

    def test_lines_limits_the_terminal_and_blank_space_below_the_screen_is_dropped(self):
        text = self.inspect(row('w'), capture='\n'.join(f'row {n}' for n in range(100)) + '\n\n\n', lines=2)
        self.assertEqual(text.splitlines()[-3:], ['Terminal (last 2 lines):', 'row 98', 'row 99'])

    def test_the_last_error_is_shown_when_there_is_one(self):
        text = self.inspect(row('w', launch_error='Launch failed\nbecause of this'))
        self.assertIn('Last error: Launch failed | because of this', text)
        self.assertNotIn('Last error', self.inspect(row('w')))

    def test_a_launch_that_fell_back_to_plain_says_why(self):
        reason = 'Claude Code 2.0.5 is older than 2.1.280, which the multiverse mod needs'
        item = row('w', runtime={'native_mode': {'policy': 'auto', 'mode': 'plain', 'fallback_reason': reason}})
        self.assertIn('Note: ' + reason, self.inspect(item).splitlines())
        self.assertEqual(self.inspect(item, json=True)['fallback'], reason)
        plain = self.inspect(row('w', runtime={'native_mode': {'policy': 'auto', 'mode': 'bridge', 'fallback_reason': None}}))
        self.assertNotIn('Note:', plain)
        self.assertIsNone(self.inspect(row('w'), json=True)['fallback'])

    def test_an_agent_without_a_live_terminal_says_why_not_with_the_reason_text_of_the_pane(self):
        for live, words in (('dormant', 'it is dormant.'), ('stopped', 'it is stopped.'), ('unknown', 'its state is unknown.')):
            with self.subTest(live=live):
                text = self.inspect(row('w', live=live, status='—'), capture='No live pane; no retained launch diagnostics.')
                self.assertTrue(text.endswith('No live terminal: ' + words), text)
                self.assertNotIn('retained', text)

    def test_json_has_the_facts_and_the_whole_capture(self):
        result = self.inspect(row('w', summary='s'), json=True)
        self.assertEqual((result['name'], result['mode'], result['live'], result['phase'], result['summary']),
                         ('w', 'plain', 'alive', 'done', 's'))
        self.assertEqual(result['terminal'], self.CAPTURE)
        dormant = self.inspect(row('w', live='dormant'), capture='gone', json=True)
        self.assertIsNone(dormant['terminal'])
        self.assertEqual(dormant['note'], 'gone')

    def test_the_python_function_still_returns_the_raw_capture(self):
        with patch.object(lifecycle, 'terminal_action', return_value='raw') as action:
            self.assertEqual(inspect.run(self.root, agent='w'), 'raw')
        self.assertEqual(action.call_args.args[1:], ('w', 'inspect'))

    def test_reconnect_note_and_additive_json_metadata(self):
        meta = {'state': 'reconnected', 'reason': 'Reconnected automatically'}
        item = row('worker', recovery_note=meta['reason'], runtime={'auto_recovery': meta})
        text = self.inspect(item)
        self.assertIn('Note: Reconnected automatically.', text)
        self.assertNotIn('Last error:', text)
        self.assertEqual(self.inspect(item, json=True)['recovery'], meta)


class DoctorTest(BaseTest):
    def broken(self):
        directory = self.root / 'state/agents' / str(uuid4())
        write(directory / 'properties.json', {'id': directory.name, 'name': 'worker', 'harness': 'codex', 'phase': 'done',
                                              'summary': '', 'workspace': str(self.work)})
        write(directory / 'runtime.json', {'launch_id': 'l', 'state': 'failed', 'error': 'Cannot start:\nno such file'})

    def test_sentences_for_none_one_and_several_problems(self):
        self.assertEqual(doctor.sentences([]), 'No problems.')
        self.assertEqual(doctor.sentences([('a', 'It broke')]), '1 problem: It broke.')
        self.assertEqual(doctor.sentences([('a', 'One.'), ('b', 'Two')]), '2 problems:\n- One.\n- Two.')

    def test_a_failed_launch_reads_as_a_sentence_and_json_keeps_the_recorded_problem(self):
        self.broken()
        found = doctor.check(self.root)
        self.assertEqual(len(found), 1)
        self.assertEqual(found[0][1], "worker's last launch failed: Cannot start: | no such file.")  # lines of a reason are joined with " | "
        result = doctor.run(self.root)
        self.assertEqual((result['ok'], result['scope']), (False, str(self.root)))
        self.assertEqual(result['problems'], [found[0][0]])
        self.assertTrue(result['problems'][0].startswith('worker: '))

    def test_the_command_prints_sentences_and_exits_1_only_when_something_is_wrong(self):
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            self.assertIsNone(doctor.cli_run(self.root))
        self.assertEqual(out.getvalue(), 'No problems.\n')
        self.broken()
        out = io.StringIO()
        with contextlib.redirect_stdout(out), self.assertRaises(SystemExit) as stopped:
            doctor.cli_run(self.root)
        self.assertEqual(stopped.exception.code, 1)
        self.assertTrue(out.getvalue().startswith("1 problem: worker's last launch failed"))
        self.assertEqual(doctor.cli_run(self.root, json=True)['ok'], False)

    def test_successful_reconnect_notes_do_not_fail_doctor(self):
        item = row('worker', recovery_note='Reconnected automatically',
                   runtime={'auto_recovery': {'state': 'reconnected'}})
        output = io.StringIO()
        with patch.object(lifecycle, 'listing', return_value=[item]), contextlib.redirect_stdout(output):
            result = doctor.run(self.root)
            doctor.cli_run(self.root)
        self.assertTrue(result['ok'])
        self.assertEqual(result['problems'], [])
        self.assertEqual(result['notes'], ['worker reconnected automatically.'])
        self.assertEqual(output.getvalue(), 'No problems.\nNote: worker reconnected automatically.\n')

    def test_disconnected_and_failed_or_skipped_reconnect_are_problems(self):
        for state in ('failed', 'skipped'):
            reason = 'Automatic reconnect ' + state + ': native background processes are running'
            item = row('worker', status='disconnected', connection_error=reason,
                       runtime={'auto_recovery': {'state': state, 'reason': reason}})
            with self.subTest(state=state), patch.object(lifecycle, 'listing', return_value=[item]):
                self.assertFalse(doctor.run(self.root)['ok'])
                self.assertIn(reason, doctor.run(self.root)['problems'][0])
                with contextlib.redirect_stdout(io.StringIO()), self.assertRaises(SystemExit) as exit:
                    doctor.cli_run(self.root)
                self.assertEqual(exit.exception.code, 1)


class LaunchTextTest(BaseTest):
    def record(self, **extra):
        return {'name': 'codex-ui', 'id': str(uuid4()), 'harness': 'codex',
                'runtime': {'native_mode': {'mode': 'bridge'}, 'control_endpoint': {}}, **extra}

    def test_label_names_the_harness_and_how_it_is_connected(self):
        self.assertEqual(display.label(self.record()), 'Codex, bridge')
        self.assertEqual(display.label({'harness': 'claude', 'runtime': {'control_endpoint': {'transport': 'unix-lines'}}}), 'Claude, bridge')
        self.assertEqual(display.label({'harness': 'pi', 'runtime': {}}), 'Pi, plain')
        self.assertEqual(display.label({'harness': 'custom', 'runtime': {'native_mode': {'mode': 'bridge without compaction'}}}),
                         'custom, bridge without compaction')

    def test_spawn_says_started_and_ready_only_after_a_wait(self):
        with patch.object(lifecycle, 'start', return_value=self.record()):
            self.assertEqual(spawn.cli_run(self.root, name='codex-ui'), 'Started codex-ui (Codex, bridge).')
        with patch.object(lifecycle, 'start', return_value=self.record(ok=True, ready=True)) as start:
            self.assertEqual(spawn.cli_run(self.root, name='codex-ui', wait=True), 'Started codex-ui (Codex, bridge): ready.')
        self.assertTrue(start.call_args.kwargs['wait'])

    def test_resume_says_resumed(self):
        with patch.object(lifecycle, 'start', return_value=self.record()):
            self.assertEqual(resume.cli_run(self.root, agent='lifecycle'), 'Resumed codex-ui (Codex, bridge).')
        with patch.object(lifecycle, 'start', return_value=self.record(ok=True, ready=True)):
            self.assertEqual(resume.cli_run(self.root, agent='lifecycle', wait=True), 'Resumed codex-ui (Codex, bridge): ready.')

    def test_a_launch_that_failed_to_become_ready_raises_with_its_reason(self):
        failed = self.record(ok=False, ready=False, error='Launch ended before becoming ready (stopped)\nlast output line')
        with patch.object(lifecycle, 'start', return_value=failed):
            with self.assertRaisesRegex(ValueError, r'^codex-ui did not start: Launch ended before becoming ready \(stopped\)\nlast output'):
                spawn.cli_run(self.root, name='codex-ui', wait=True)
            with self.assertRaisesRegex(ValueError, r'^codex-ui did not resume: Launch ended'):  # the agent's own name
                resume.cli_run(self.root, agent='an-id-or-a-name', wait=True)

    def test_a_launch_error_loses_its_record_id_and_recovery_advice(self):
        try:
            try:
                raise OSError('tmux server unreachable')
            except OSError as inner:
                raise RuntimeError("Agent 'codex-ui' (9ae0d4ec-0000-4000-8000-000000000000) launch failed: tmux server "
                                   "unreachable. Failed record preserved. Resume this agent by its full UUID: 9ae0d4ec.") from inner
        except RuntimeError as error:
            failure = error
        with patch.object(lifecycle, 'start', side_effect=failure):
            with self.assertRaises(ValueError) as caught:
                spawn.cli_run(self.root, name='codex-ui')
            self.assertEqual(str(caught.exception), 'codex-ui did not start: tmux server unreachable')
            record = spawn.cli_run(self.root, name='codex-ui', json=True)
            self.assertIs(record['ok'], False)
            self.assertEqual(record['error'], str(failure))  # JSON keeps the original failure.

    def test_json_keeps_the_whole_record_and_the_python_functions_are_unchanged(self):
        record = self.record(ok=False, error='x')
        with patch.object(lifecycle, 'start', return_value=record):
            self.assertIs(spawn.cli_run(self.root, name='codex-ui', json=True), record)
            self.assertIs(spawn.run(self.root, name='codex-ui'), record)
            self.assertIs(resume.run(self.root, agent='lifecycle'), record)


class DispatchTest(BaseTest):
    """The command line reaches the readable handlers; the Python `run` functions stay as they were."""

    def test_every_command_dispatches_to_its_readable_handler(self):
        with patch.object(lifecycle, 'listing', return_value=[]):
            self.assertEqual(cli.run(cli.parser().parse_args(['-u', str(self.root), 'list']), cli=True), 'No agents.')
            self.assertEqual(cli.run(cli.parser().parse_args(['-u', str(self.root), 'list']), cli=False), 'No agents.')
            self.assertEqual(cli.run(cli.parser().parse_args(['-u', str(self.root), 'list', '--json']), cli=True), [])
        for command, arguments in (('spawn', ['spawn', '--name', 'x']), ('resume', ['resume', 'x']),
                                   ('inspect', ['inspect', 'x']), ('doctor', ['doctor'])):
            with self.subTest(command=command):
                self.assertTrue(hasattr(cli.tool(command), 'cli_run'))
                self.assertFalse(cli.parser().parse_args(['-u', str(self.root), *arguments]).json)
                self.assertTrue(cli.parser().parse_args(['-u', str(self.root), *arguments, '--json']).json)


if __name__ == '__main__':
    unittest.main()
