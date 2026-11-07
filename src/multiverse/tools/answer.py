"""Read the prompts a waiting agent is blocked on, and answer one through the harness's own answer path."""
from dataclasses import asdict
import json as json_module
import sys

from ..runtime import prompts

# The outcomes that count as success: mv2's answer took, went first where the harness can't confirm more, or may
# still take. The command exits 0 for these only.
SENT = ("answered", "sent_first", "submitted")


def configure(parser):
    parser.add_argument('agent', help='The waiting agent')
    parser.add_argument('id', nargs='?', help='The prompt to answer, as listed')
    parser.add_argument('--choice', help="One of the prompt's choices, such as allow or deny")
    parser.add_argument('--message', help='Text that goes with the choice, where the choice takes one')
    parser.add_argument('--answer', action='append', default=[], metavar='QUESTION=LABEL',
                        help="An answer to one question: its number, header or text, then one of its labels, or typed "
                             "text where the question allows it. Repeat it for each question, and for each label of a "
                             "question that allows several")
    parser.add_argument('--note', action='append', default=[], metavar='QUESTION=TEXT',
                        help='A note to go with the answer to a question that takes one, once per question')
    parser.add_argument('--stdin', action='store_true',
                        help='Read the answer as JSON on standard input: {"choice", "message", "answers": {QUESTION: '
                             'LABEL or [LABELS]}, "notes": {QUESTION: TEXT}}. Use it for secret answers, which must '
                             'not appear on the command line')
    parser.add_argument('--result', metavar='ID', help="Read an earlier answer's outcome as it stands now; never sends")
    parser.add_argument('--timeout', type=float, default=30,
                        help='Seconds to wait for the outcome (default: 30); 0 returns once it is sent')
    parser.add_argument('--json', action='store_true', help='Print the complete result as JSON')


def _questions(found):
    return [question for question in found.get('questions') or ()]


def _question_id(found, key):
    """The question `key` names in the prompt `found`: its id, its header, or its number from 1, in that order."""
    questions = _questions(found)
    for question in questions:
        if key == question['id']:
            return question
    headers = [question for question in questions if question.get('header') and question['header'] == key]
    if len(headers) == 1:
        return headers[0]
    if key.isdecimal() and 1 <= int(key) <= len(questions):
        return questions[int(key) - 1]
    names = '; '.join(f"{number}. {question['header'] or question['question']}"
                      for number, question in enumerate(questions, 1))
    raise ValueError(f"Prompt {found['id']} has no question {key!r}" + (f"; its questions: {names}" if names else ''))


def _split(found, given):
    """QUESTION=LABEL split at the '=' that ends a question this prompt has, since a question's text or a typed
    answer may hold '=' too; at the first one otherwise."""
    positions = [index for index, char in enumerate(given) if char == '=' and index]
    if not positions:
        raise ValueError('--answer takes QUESTION=LABEL and --note QUESTION=TEXT')  # never the text itself
    for index in positions if found is not None else ():
        try:
            _question_id(found, given[:index])
        except ValueError:
            continue
        return given[:index], given[index + 1:]
    return given[:positions[0]], given[positions[0] + 1:]


def _resolved(found, given, *, secret_ok, what='answer'):
    """{question id: [values]} for answers (or notes) given by question number, header or id. Without the prompt
    (`found` None) only exact ids from --stdin pass, for core's own fresh check: nothing then shows a question isn't
    secret."""
    answers = {}
    for key, labels in given:
        if found is None and not secret_ok:
            raise ValueError(f'mv2 can\'t check question {key!r}: give the answer with --stdin')
        question = _question_id(found, key) if found is not None else {'id': key, 'secret': False}
        if question.get('secret') and not secret_ok:
            raise ValueError(f"{question.get('header') or question['id']} is a secret question: give its {what} with "
                             f"--stdin, so it isn't kept in your shell history or shown in the process list")
        answers.setdefault(question['id'], []).extend(labels)
    return {key: tuple(labels) for key, labels in answers.items()}


def _notes(found, given, *, secret_ok):
    """{question id: note}, one note per question."""
    notes = {}
    for key, texts in _resolved(found, given, secret_ok=secret_ok, what='note').items():
        if len(texts) > 1:
            raise ValueError(f'Give one --note for {key!r}, not {len(texts)}')
        notes[key] = texts[0]
    return notes


def _from_stdin():
    try:
        data = json_module.loads(sys.stdin.read())
    except ValueError as error:
        raise ValueError(f'--stdin needs a JSON object: {error}') from None
    if not isinstance(data, dict) or set(data) - {'choice', 'message', 'answers', 'notes'}:
        raise ValueError('--stdin needs a JSON object with only choice, message, answers and notes')
    notes = {} if data.get('notes') is None else data['notes']  # null or left out: none; anything else is checked
    if not isinstance(notes, dict) or not all(isinstance(text, str) for text in notes.values()):
        raise ValueError('notes must be an object of QUESTION: TEXT')
    answers = {} if data.get('answers') is None else data['answers']
    if not isinstance(answers, dict):
        raise ValueError('answers must be an object of QUESTION: LABEL or [LABELS]')
    pairs = []
    for key, labels in answers.items():
        labels = [labels] if isinstance(labels, str) else labels
        if not isinstance(labels, list) or not all(isinstance(label, str) for label in labels):
            raise ValueError(f'the answer to {key!r} must be a label or a list of labels')
        pairs.append((str(key), labels))
    for key in ('choice', 'message'):
        if data.get(key) is not None and not isinstance(data[key], str):
            raise ValueError(f'{key} must be text')
    return data.get('choice'), data.get('message'), pairs, [(str(key), [text]) for key, text in notes.items()]


# What core's read of a prompt the pre-read didn't list can say. An earlier answer from mv2 has an outcome of
# its own; these causes leave open whether the prompt is still there.
SENT_BEFORE = frozenset(('answered', 'sent_first', 'submitted', 'unknown'))
INCONCLUSIVE = frozenset(('unconfirmed', 'unavailable', 'busy'))


def _not_listed(root, agent, id):
    """For an answer from the command line to a prompt mv2 can't list now: what became of the prompt, read without
    sending, since nothing shows its questions aren't secret. --stdin is suggested only when it may still be open."""
    unsure = ValueError(f"Prompt {id} isn't among the prompts mv2 can read for {agent} now, so it can't check that "
                        f"none of its questions is secret. Nothing was sent. Read them again with mv2 answer {agent}, "
                        f"or give the answer with --stdin.")
    closed = asdict(prompts.result(root, agent, id))  # a damaged record is core's error, shown as it is
    if closed['state'] in SENT_BEFORE:
        raise ValueError(f"mv2 already sent an answer to prompt {id}; nothing was sent again. Read its outcome with "
                         f"mv2 answer {agent} --result {id}.")
    if closed['state'] == 'refused' and closed.get('cause') in INCONCLUSIVE:
        raise unsure
    return closed  # closed without mv2's answer, or still open: its outcome says which, and exits 1


def run(root=None, *, agent, id=None, choice=None, message=None, answer=(), note=(), stdin=False, result=None,
        timeout=30, json=False):
    answering = choice is not None or message is not None or answer or note or stdin
    if result is not None:
        if id is not None or answering or timeout != 30:
            raise ValueError('--result reads an earlier answer; it takes no prompt ID, answer or wait options')
        return asdict(prompts.result(root, agent, result))
    if id is None:
        if answering:
            raise ValueError('Name the prompt to answer: mv2 answer AGENT ID ...; mv2 answer AGENT lists them')
        return asdict(prompts.waiting(root, agent))
    if not answering:
        raise ValueError('Give --choice, --answer, --note or --stdin to answer prompt ' + id)
    if stdin and (choice is not None or message is not None or answer or note):
        raise ValueError('--stdin carries the whole answer; give no --choice, --message, --answer or --note with it')
    pairs, note_pairs = [], []
    if stdin:
        choice, message, pairs, note_pairs = _from_stdin()
    if message is not None and choice is None:
        raise ValueError('--message goes with a --choice')
    answers, notes = {}, {}
    if pairs or answer or note_pairs or note:
        found = None
        try:
            listed = prompts.waiting(root, agent)  # for question numbers and headers, and to keep secrets off argv
            found = next((asdict(item) for item in listed.prompts if item.id == id), None)
        except Exception:
            if not stdin:
                raise  # --stdin can still give exact ids: core reads the prompt again before anything is sent
        if found is None and not stdin:
            return _not_listed(root, agent, id)
        pairs = pairs or [(key, [label]) for key, label in (_split(found, given) for given in answer)]
        note_pairs = note_pairs or [(key, [text]) for key, text in (_split(found, given) for given in note)]
        answers = _resolved(found, pairs, secret_ok=stdin)
        notes = _notes(found, note_pairs, secret_ok=stdin)
    timeout = max(0.0, timeout)
    return asdict(prompts.answer(root, agent, id, choice=choice, message=message, answers=answers or None,
                                 notes=notes or None, timeout=timeout, wait=timeout > 0))


def _person_answer(found):
    """What the person chose, as core gives it: {choice, answers: {question: [labels]}, typed: {question: [text]},
    truncated}. Never a message or a secret answer, which core leaves out."""
    if not isinstance(found, dict):
        return ''
    words = lambda values: ', '.join(values) if isinstance(values, (list, tuple)) else str(values)
    parts = [f"chose {found['choice']}"] if found.get('choice') else []
    for question, labels in (found.get('answers') or {}).items():
        parts.append(f"answered {question!r} with {words(labels)}")
    for question, text in (found.get('typed') or {}).items():
        parts.append(f"typed {words(text)!r} for {question!r}")
    said = '; '.join(parts) + (' (cut short)' if parts and found.get('truncated') else '')
    return f'They {said}.' if parts else ''


# Said when core gives no reason; core and the harness normally say why, as fact when known and as possibilities
# when not, and that text is printed as given.
DEFAULT_REASONS = {'person': 'The person answered first', 'gone': 'The prompt closed before the answer took',
                   'unknown': "Sent, but mv2 can't tell whether the answer took", 'refused': 'Not sent'}


def outcome(found, agent):
    """An answer's outcome in words; ValueError (so the command exits 1) when mv2's answer did not take."""
    from ..display import clean
    state = found['state']
    reason = '\n'.join(clean(line) for line in (found.get('reason') or '').splitlines()).strip()  # all of it, as given
    if state == 'answered':
        return 'Answered.' + (" It chose a decision that saves a rule to the person's settings; the harness writes "
                              "the rule." if found.get('saved_rule') else '')
    if state == 'sent_first':  # its reason says the harness doesn't confirm it took the answer
        return reason or "Sent first, ahead of any answer from the terminal; the harness doesn't confirm it took it."
    if state == 'submitted':
        return (f"Sent; the outcome isn't known yet. Read it with mv2 answer {agent} --result {found['id']}. "
                "Don't answer again: the answer was sent.")
    said = reason or DEFAULT_REASONS.get(state, f'Answer {state}')
    if state == 'person' and _person_answer(found.get('person_answer')):  # its own sentence after core's reason
        said = said.rstrip('.') + '. ' + clean(_person_answer(found['person_answer']))
    if found.get('cause') == 'replaced':  # core's reason names the prompt it waits on now; say how to read it
        said = said.rstrip('.') + f'. Read it with mv2 answer {agent}.'
    raise ValueError(said)


def listing(found, agent):
    from ..display import clean
    if found['state'] != 'ok':
        raise ValueError(clean(found.get('reason') or f"Prompts {found['state']}"))
    lines = []
    for item in found['prompts']:
        what = clean(item['tool'] + ': ' if item.get('tool') else '') + clean(item['summary'])
        lines.append(f"{item['id']}  {item['kind']}  {what}" + (f"  (asked by subagent {clean(item['subagent'])})"
                                                                 if item.get('subagent') else ''))
        if not item.get('answerable', True):
            lines.append('    ' + clean(item.get('reason') or 'answer it in the terminal'))
            continue
        if item.get('choices'):
            lines.append('    choices: ' + ', '.join(
                f"{choice['id']} ({clean(choice['label'])})" + (" [saves a rule; person only]" if choice.get('saves_rule')
                                                               else '')
                + (' [takes --message]' if choice.get('takes_message') else '') for choice in item['choices']))
        for number, question in enumerate(item.get('questions') or (), 1):
            notes = [note for note, on in (('several labels', question.get('multiple')),
                                           ('typed text allowed', question.get('free_text')),
                                           ('secret: answer with --stdin', question.get('secret')),
                                           ('takes a --note', question.get('takes_note')),
                                           ('optional', not question.get('required', True))) if on]
            options = ' | '.join(clean(label) for label, _ in question.get('options') or ())
            lines.append(f"    {number}. " + (clean(question['header']) + ': ' if question.get('header') else '')
                         + clean(question['question']) + (f"  [{options}]" if options else '')
                         + (f"  ({', '.join(notes)})" if notes else ''))
    if found.get('coverage') != 'complete':
        if not lines:
            lines.append(f'{agent} has no prompt mv2 can see.')
        lines.append('Coverage unknown: ' + clean(found.get('reason') or 'mv2 may not see every prompt this agent waits on'))
    return '\n'.join(lines) or f'{agent} has no open prompts.'


def cli_run(root=None, **arguments):
    found = run(root, **arguments)
    agent = arguments['agent']
    if 'prompts' in found:  # a listing
        return {**found, 'ok': found['state'] == 'ok'} if arguments.get('json') else listing(found, agent)
    if arguments.get('json'):
        return {**found, 'ok': found['state'] in SENT}
    return outcome(found, agent)


if __name__ == '__main__':
    from ._common import main
    main(configure, run)
