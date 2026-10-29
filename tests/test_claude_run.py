import json
import unittest
import os
import stat
import tempfile
from pathlib import Path
from unittest.mock import patch

from multiverse.harnesses import claude
from multiverse.harnesses.claude import run, transcripts
from multiverse.harnesses.types import (RunDirectory, RunFailure, RunImage, RunMessage, RunOutcome, RunReply,
                                        RunRequest, RunRow, RunText)


SESSION, CWD, MODEL, AT = '11111111-1111-4111-8111-111111111111', '/runs/r1', 'claude-haiku-4-5', '2026-10-29T00:00:00Z'


def stand_in(folder, version):
    """A program named claude in `folder` that only answers --version, as Claude Code `version`."""
    binary = Path(folder) / 'claude'
    binary.write_text(f'#!/bin/sh\necho "{version} (Claude Code)"\n')
    binary.chmod(0o700)
    return binary


def chain(rows):
    """Whether rows after the title form one linear parent chain."""
    talk = rows
    return all(row['parentUuid'] == (talk[i - 1]['uuid'] if i else None) for i, row in enumerate(talk))


class ClaudeRunTest(unittest.TestCase):
    def test_a_built_conversation_is_stored_the_way_claude_stores_a_response(self):
        """Claude reads a run only as a stored session; fragile because a response's rows must share one message ID
        with end_turn on the last only, and recorded usage must be empty, or Claude splits the reply, or refuses
        "Prompt is too long" before sending anything."""
        rows = run.session([RunMessage(role='user', blocks=(RunText(text='Look'),
                                                           RunImage(media_type='image/png', data=b'\x89PNG'))),
                            RunMessage(role='assistant', blocks=(RunText(text='One'), RunText(text='Two')))],
                           session_id=SESSION, cwd=CWD, model=MODEL, at=AT)
        user, first, second = rows
        self.assertEqual(user['message']['content'][1],
                         {'type': 'image', 'source': {'type': 'base64', 'media_type': 'image/png', 'data': 'iVBORw=='}})
        self.assertEqual(first['message']['id'], second['message']['id'])
        self.assertEqual([r['message']['stop_reason'] for r in (first, second)], [None, 'end_turn'])
        self.assertEqual({(r['message']['model'], json.dumps(r['message']['usage'])) for r in (first, second)},
                         {(MODEL, '{}')})
        self.assertTrue(chain(rows))
        self.assertEqual({(r['sessionId'], r['cwd'], r['userType']) for r in rows}, {(SESSION, CWD, 'external')})
        for bad in (RunMessage(role='assistant', blocks=(RunImage(media_type='image/png', data=b'x'),)),
                    RunMessage(role='system', blocks=(RunText(text='x'),)), RunMessage(role='user', blocks=(RunText(text=1),)),
                    RunRow(format='codex-jsonl', data={'type': 'user', 'message': {}})):
            with self.subTest(bad=bad), self.assertRaises(run.Unusable):
                run.session([bad, RunMessage(role='assistant', blocks=(RunText(text='ok'),))],
                            session_id=SESSION, cwd=CWD, model=MODEL, at=AT)

    def test_passed_through_rows_keep_their_model_and_lose_their_usage(self):
        """Rows copied from an agent's transcript; fragile because their recorded usage would make Claude refuse a
        small run as too long, a kept sidechain link or old parent would break the chain, and their model must stay:
        copied thinking carries the writing model's signature."""
        prompt = {'type': 'user', 'uuid': 'u1', 'parentUuid': 'elsewhere', 'sessionId': 'agent', 'cwd': '/work',
                  'isSidechain': False, 'message': {'role': 'user', 'content': 'Hi'}}
        answer = {'type': 'assistant', 'uuid': 'a1', 'parentUuid': 'u1', 'logicalParentUuid': 'x', 'sessionId': 'agent',
                  'message': {'id': 'msg_1', 'role': 'assistant', 'model': 'claude-opus-4-6', 'stop_reason': 'end_turn',
                              'content': [{'type': 'thinking', 'thinking': 't', 'signature': 's'}],
                              'usage': {'input_tokens': 190000}}}
        rows = run.session([RunRow(format='claude-jsonl', data=row) for row in (prompt, answer, prompt, answer)],
                           session_id=SESSION, cwd=CWD, model=MODEL, at=AT)
        self.assertTrue(chain(rows))
        self.assertEqual(len({r['uuid'] for r in rows}), 4)  # a row passed twice gets a uuid of its own
        self.assertEqual([r['message'].get('usage') for r in rows], [None, {}, None, {}])
        self.assertEqual(rows[1]['message']['model'], 'claude-opus-4-6')
        self.assertFalse({'isSidechain', 'logicalParentUuid'} & set(rows[1]) | {'isSidechain'} & set(rows[0]))
        self.assertEqual({(r['sessionId'], r['cwd']) for r in rows}, {(SESSION, CWD)})
        self.assertEqual(answer['message']['usage'], {'input_tokens': 190000})  # the caller's row is left alone
        with self.assertRaises(run.Unusable):
            run.session([RunRow(format='claude-jsonl', data={'type': 'system', 'uuid': 's'})], session_id=SESSION, cwd=CWD,
                        model=MODEL, at=AT)

    def test_what_claude_would_refuse_is_refused_before_anything_is_spent(self):
        """Claude fails these only after a paid request (a 400 tool_history_mismatch), or answers a prompt that
        follows a user message wrongly; fragile because Claude stores parallel results as separate user rows and
        one response as several assistant rows, which must be read as one message each."""
        def a(*blocks, mid='m'):
            return {'type': 'assistant', 'message': {'id': mid, 'role': 'assistant', 'content': list(blocks)}}
        def u(*blocks):
            return {'type': 'user', 'message': {'role': 'user', 'content': list(blocks)}}
        text, call = {'type': 'text', 'text': 'x'}, lambda i: {'type': 'tool_use', 'id': i}
        result = lambda i: {'type': 'tool_result', 'tool_use_id': i}
        cases = {
            'parallel calls answered in separate rows': ([u(text), a(call('c1'), mid='m1'), a(call('c2'), mid='m1'),
                                                          u(result('c1')), u(result('c2')), a(text, mid='m2')], True),
            'ends with a user message': ([u(text), a(text), u(text)], False),
            'a call never answered': ([u(text), a(call('c1')), a(text, mid='m2')], False),
            'a call answered at the end': ([u(text), a(call('c1'))], False),
            'a result with no call before it': ([u(text), a(text), u(result('c9')), a(text, mid='m2')], False),
            'one call ID twice': ([u(text), a(call('c1')), u(result('c1')), a(call('c1'), mid='m2'), u(result('c1')),
                                   a(text, mid='m3')], False),
            'one result twice': ([u(text), a(call('c1')), u(result('c1')), u(result('c1')), a(text, mid='m2')], False),
        }
        for name, (rows, fine) in cases.items():
            with self.subTest(name):
                if fine:
                    run.check(rows)
                else:
                    with self.assertRaises(run.Unusable):
                        run.check(rows)

    def test_the_command_runs_one_tool_less_turn_on_the_given_prompt(self):
        """The run's command line; fragile because without --system-prompt-snapshot off a resumed session could
        answer under a prompt Claude recorded instead of the run's own, and a NUL in the prompt stops Claude."""
        argv = run.command('claude', prompt='Merge\x00 these', session_id=SESSION, model='haiku', effort=None,
                           system='Only JSON')
        self.assertEqual(argv[:4], ['claude', '-p', '--resume', SESSION])
        self.assertEqual(argv[argv.index('--tools') + 1], '')
        self.assertEqual(argv[-6:], ['--system-prompt', 'Only JSON', '--system-prompt-snapshot', 'off', '--', 'Merge these'])
        self.assertNotIn('--effort', argv)  # a model that takes no effort gets none
        low = run.command('claude', prompt='p', session_id=SESSION, model='m', effort='low', system='s')
        self.assertEqual(low[low.index('--effort'):low.index('--effort') + 2], ['--effort', 'low'])
        # A prompt is text whatever it starts with: Claude reads anything after `--` as the prompt, so
        # `--resume=ID` can never pick another conversation. A dash-led system prompt is --system-prompt's value.
        for prompt in ('--version', '--resume=11111111-1111-4111-8111-111111111111', '-p'):
            for session_id in (SESSION, None):
                with self.subTest(prompt=prompt, session=session_id):
                    argv = run.command('claude', prompt=prompt, session_id=session_id, model='haiku', effort=None,
                                       system='--version')
                    self.assertEqual(argv[-2:], ['--', prompt])
                    self.assertEqual(argv.count('--'), 1)
                    self.assertEqual(argv[argv.index('--system-prompt') + 1], '--version')
        self.assertEqual(run.environment(3)['CLAUDE_CODE_MAX_RETRIES'], '3')
        self.assertEqual(run.environment(0)['CLAUDE_CODE_DISABLE_REFUSAL_FALLBACK'], '1')
        for bad in (-1, 16, True, '3'):
            with self.subTest(retries=bad), self.assertRaises(run.Unusable):
                run.environment(bad)

    def test_claudes_result_maps_to_one_kind(self):
        """GOAT's policy keys on these kinds (retry, back off, stop, fall back to another model); fragile because
        Claude's typed api_error must win over its loose text, and its text rules stay the fallback for new kinds."""
        def env(**fields):
            return json.dumps({'type': 'result', 'is_error': True, 'result': 'API Error', **fields})
        cases = {
            'a typed usage limit': (1, env(api_error='usage_limit_reached', api_error_status=429), 'usage_limit'),
            'a typed history mismatch': (1, env(api_error='tool_history_mismatch', api_error_status=400),
                                         'invalid_request'),
            'a typed safety stop': (1, env(api_error='safety_monitor_blocked'), 'refused'),
            'a typed sign-in problem': (1, env(api_error='gateway_signin_required', api_error_status=401), 'failed'),
            'a new typed kind falls back to the status': (1, env(api_error='brand_new', api_error_status=529),
                                                          'rate_limit'),
            'a refusal': (0, json.dumps({'is_error': False, 'stop_reason': 'refusal', 'result': ''}), 'refused'),
            'too long': (1, env(result='Prompt is too long'), 'too_long'),
            'a missing model': (1, env(api_error_status=404), 'invalid_request'),
            'overloaded': (1, env(api_error_status=529), 'rate_limit'),
            'a server error': (1, env(api_error_status=503), 'rate_limit'),
            'the usage limit in words': (1, env(result="You've hit your limit"), 'usage_limit'),
            'anything else': (1, env(api_error_status=400), 'failed'),
            'no JSON, rate limited': (1, 'Error: 429 Too Many Requests', 'rate_limit'),
            'no JSON at all': (1, '', 'failed'),
        }
        for name, (code, stdout, kind) in cases.items():
            with self.subTest(name):
                self.assertEqual(run.reply(code, stdout, '', model=MODEL)['kind'], kind)
        ok = run.reply(0, json.dumps({'is_error': False, 'result': '{"a": 1}', 'stop_reason': 'end_turn',
                                      'total_cost_usd': 0.0012, 'modelUsage': {'claude-haiku-4-5-20251001': {}},
                                      'usage': {'input_tokens': 9, 'output_tokens': 4, 'server_tool_use': {}}}),
                       '', model=MODEL)
        self.assertEqual(ok, {'kind': None, 'text': '{"a": 1}', 'model': 'claude-haiku-4-5-20251001', 'stop': 'end_turn',
                              'usage': {'input': 9, 'output': 4}, 'cost_usd': 0.0012})
        long = run.reply(1, env(result='x' * 1000), 'y' * 1000, model=MODEL)['message']
        self.assertLessEqual(len(long), run.LIMIT)


    def test_the_plan_writes_the_session_claude_resumes_and_reads_its_reply(self):
        """What core writes and runs for a Claude run; fragile because the session file must sit in the selected
        store's project folder for the run directory's real path under the run's own ID, or Claude resumes nothing,
        and messages Claude would refuse must fail before anything is written or spent; and the Claude whose version
        is checked must be the one that runs, though the run's PATH (a harness setting) differs from this process's."""
        adapter = claude.ClaudeAdapter.configured('claude', {})
        installed = tempfile.TemporaryDirectory()
        self.addCleanup(installed.cleanup)
        binary = stand_in(installed.name, '2.1.312')
        environment = {'PATH': installed.name}
        with tempfile.TemporaryDirectory() as folder:
            directory = RunDirectory(path=Path(folder), id=SESSION)
            request = RunRequest(messages=(RunMessage(role='user', blocks=(RunText(text='Hi'),)),
                                           RunMessage(role='assistant', blocks=(RunText(text='Hello.'),))),
                                 prompt='Go on', system='One word', model='haiku', transport_retries=2)
            plan = adapter.run_plan(request, environment=environment, run=directory)
            real = os.path.realpath(folder)
        (file,) = plan.files
        self.assertEqual((file.root, file.path), ('store', f'projects/{transcripts.project_key(real)}/{SESSION}.jsonl'))
        rows = [json.loads(line) for line in file.content.decode().splitlines()]
        self.assertEqual({(r['sessionId'], r['cwd']) for r in rows}, {(SESSION, real)})
        argv = list(plan.command.argv)
        self.assertEqual(argv[0], os.path.abspath(binary))  # the one run_options checked, not a name to look up
        self.assertEqual(argv[argv.index('--resume') + 1], SESSION)
        self.assertEqual(argv[argv.index('--model') + 1], 'claude-haiku-5-5')  # resolved for the provider
        self.assertEqual(plan.command.environment['CLAUDE_CODE_MAX_RETRIES'], '2')
        self.assertIn('CLAUDE_CODE_ENABLE_FUNCTION_HOOKS', plan.command.unset_env)
        self.assertNotIn('PATH', plan.command.environment)  # only the run's own variables, never the inherited ones
        reply = plan.interpret(RunOutcome(returncode=0, stdout=json.dumps(
            {'is_error': False, 'result': 'Fine.', 'stop_reason': 'end_turn', 'total_cost_usd': 0.0001,
             'modelUsage': {'claude-haiku-5-5': {}}, 'usage': {'input_tokens': 5, 'output_tokens': 2}}), stderr=''))
        self.assertEqual(reply, RunReply(text='Fine.', model='claude-haiku-5-5', usage={'input': 5, 'output': 2},
                                         cost_usd=0.0001, stop='end_turn'))
        with self.assertRaises(RunFailure) as failed:
            plan.interpret(RunOutcome(returncode=1, stdout=json.dumps({'is_error': True, 'result': 'Prompt is too long'}),
                                      stderr=''))
        self.assertEqual(failed.exception.kind, 'too_long')
        with self.assertRaises(RunFailure) as failed:  # a failed run still reports what it spent, for the run records
            plan.interpret(RunOutcome(returncode=0, stdout=json.dumps(
                {'is_error': False, 'stop_reason': 'refusal', 'result': '', 'total_cost_usd': 0.0002,
                 'modelUsage': {'claude-haiku-5-5': {}}, 'usage': {'input_tokens': 7, 'output_tokens': 1}}), stderr=''))
        self.assertEqual((failed.exception.kind, failed.exception.usage, failed.exception.cost_usd, failed.exception.model),
                         ('refused', {'input': 7, 'output': 1}, 0.0002, 'claude-haiku-5-5'))
        for name, bad in (('ends with a user message', replace_messages(request, request.messages[:1])),
                          ('an effort Claude would swap', replace_messages(request, request.messages, effort='max',
                                                                          model='claude-haiku-4-5'))):
            with self.subTest(name), tempfile.TemporaryDirectory() as folder:
                with self.assertRaises(RunFailure) as failed:
                    adapter.run_plan(bad, environment=environment, run=RunDirectory(path=Path(folder), id=SESSION))
                self.assertEqual(failed.exception.kind, 'invalid_request')
        # A long run directory: the session goes under Claude's own shortened name. The pair below is a real
        # folder Claude Code 2.1.312 made for that path.
        self.assertEqual(transcripts.project_key('/private/tmp/mv2key.ZtDr/aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa/bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb'), '-private-tmp-mv2key-ZtDr-aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa-bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb-7w0e0d')
        self.assertEqual(transcripts.project_key('/x'), '-x')  # short names are unchanged
        # Paths whose hash is negative (one accented, one with a character outside the BMP), named by running
        # Claude Code 2.1.312's own naming functions in Node.
        self.assertEqual(transcripts.project_key('/private/tmp/runs/xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx0'),
                         '-private-tmp-runs-xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx-xqxi78')
        self.assertEqual(transcripts.project_key('/Users/a/é/xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx100'),
                         '-Users-a---xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx-7x43q1')
        self.assertEqual(transcripts.project_key('/tmp/😀/xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx0'),
                         '-tmp----xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx-uijbbp')
        with tempfile.TemporaryDirectory() as folder:
            deep = Path(folder) / ('d' * 200)
            deep.mkdir()
            name = adapter.run_plan(request, environment=environment, run=RunDirectory(path=deep, id=SESSION)).files[0].path
            self.assertEqual(name.split('/')[1], transcripts.project_key(os.path.realpath(deep)))
            self.assertLess(len(name.split('/')[1]), 210)
        with tempfile.TemporaryDirectory() as folder:  # a prompt on its own: nothing to resume, nothing written
            alone = adapter.run_plan(replace_messages(request, ()), environment=environment,
                                     run=RunDirectory(path=Path(folder), id=SESSION))
            self.assertEqual(alone.files, ())
            self.assertNotIn('--resume', alone.command.argv)
            self.assertEqual((alone.command.argv[1], alone.command.argv[-2:]), ('-p', ('--', 'Go on')))
        with tempfile.TemporaryDirectory() as old_folder, tempfile.TemporaryDirectory() as new_folder:
            old, new = stand_in(old_folder, '2.1.266'), stand_in(new_folder, '2.1.312')
            for name, inherited, selected, supported in (("the run's PATH has a Claude new enough", old, new, True),
                                                         ("the run's PATH has one too old", new, old, False)):
                with self.subTest(name), patch.dict(os.environ, {'PATH': str(inherited.parent)}), \
                        tempfile.TemporaryDirectory() as folder:
                    selected_environment = {'PATH': str(selected.parent)}
                    self.assertEqual(adapter.run_options(environment=selected_environment).supported, supported)
                    plan = adapter.run_plan(request, environment=selected_environment,
                                            run=RunDirectory(path=Path(folder), id=SESSION))
                    self.assertEqual(plan.command.argv[0], os.path.abspath(selected))
        self.assertFalse(adapter.run_options(environment={'PATH': ''}).supported)  # not installed
        with tempfile.TemporaryDirectory() as folder, self.assertRaises(RunFailure) as failed:
            adapter.run_plan(request, environment={'PATH': ''}, run=RunDirectory(path=Path(folder), id=SESSION))
        self.assertEqual(failed.exception.kind, 'unavailable')


def replace_messages(request, messages, **changes):
    from dataclasses import replace
    return replace(request, messages=messages, **changes)


if __name__ == '__main__':
    unittest.main()
