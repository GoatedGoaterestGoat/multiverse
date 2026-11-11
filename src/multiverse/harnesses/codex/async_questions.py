"""The checked native async-question envelope, separate from callback replies."""
import json

from . import prompt_codec as codec


METHOD = 'request_user_input_async'
START = '<send_user_message_question_reply>'
END = '</send_user_message_question_reply>'


def question_id(item, index):
    if (not isinstance(item, str) or not item or '\0' in item
            or type(index) is not int or index < 0):
        raise ValueError('The native async question identity is invalid')
    value = json.dumps([METHOD, item, index], ensure_ascii=False, separators=(',', ':'))
    if len(value.encode('utf-8')) > 512:
        raise ValueError('The native async question identity exceeds its exact-envelope limit')
    return value


def quoted_title(title):
    # Match AnsweredQuestion::new, including its UTF-8 boundary and CR/LF folding.
    return title.encode('utf-8')[:512].decode('utf-8', errors='ignore').replace('\n', ' ').replace('\r', ' ')


class AsyncOffer(codec.Offer):
    def public(self, identifier):
        return {'id': identifier, 'kind': 'question', 'asked_at': None, 'tool': METHOD,
                'summary': self.summary[:500], 'detail': self.detail.copy(),
                'choices': (), 'questions': tuple(self.questions), 'answerable': self.answerable,
                'reason': self.reason, 'subagent': None, 'requires_waiting': False}

    def response(self, answer):
        if not self.answerable:
            raise ValueError(self.reason or 'Answer this async question in the terminal')
        answer = codec.object_value(answer)
        values = codec.object_value(answer.get('answers', {}))
        notes = codec.object_value(answer.get('notes', {}))
        if (answer.get('choice') is not None or answer.get('message') is not None
                or notes or type(answer.get('allow_rule', False)) is not bool
                or set(values) != {'answer'}):
            raise ValueError('This async question takes one answer, without a choice, message or note')
        value = values['answer']
        if not isinstance(value, (list, tuple)) or len(value) != 1:
            raise ValueError('This async question takes one answer')
        value = codec.text(value[0]).strip()
        if not value:
            raise ValueError('The async answer is empty')
        body = json.dumps([{'answer': value, 'question': quoted_title(self.params['question']['title']),
                            'questionItemId': question_id(self.params['itemId'], self.params['questionIndex'])}],
                          ensure_ascii=False, allow_nan=False, separators=(',', ':'), sort_keys=True)
        result = START + '\n' + body + '\n' + END
        if len(result.encode('utf-8')) > codec.RESPONSE_BYTES:
            raise ValueError('The native async answer exceeds the 128 KiB input limit')
        return result


def describe(params):
    offer = AsyncOffer(METHOD, {}, 'question', 'Async question')
    try:
        params = codec.bounded(codec.object_value(params))
        for key in ('threadId', 'turnId', 'itemId'):
            codec.text(params.get(key))
        question_id(params['itemId'], params.get('questionIndex'))
        native = codec.object_value(params.get('question'))
        if set(native) - {'title', 'options'}:
            raise ValueError('This async question has fields the checked terminal does not offer')
        title = codec.text(native.get('title'))
        options = native.get('options')
        if options is not None:
            if not isinstance(options, list) or len(options) > 32:
                raise ValueError('The terminal would alter this async question\'s options')
            for label in options:
                codec.text(label)
                if len(label.encode('utf-8')) > 512:
                    raise ValueError('The terminal would remove an oversized async option')
            if len(set(options)) != len(options):
                raise ValueError('The async question has ambiguous option labels')
        offer.params = params
        offer.summary = title
        offer.questions = [codec.question('answer', title, options=tuple((v, '') for v in options or ()),
                                          free_text=True)]
        offer.detail = {'question': title}
        codec.bounded(offer.public('preview'))
    except (ValueError, TypeError, KeyError, UnicodeError, RecursionError):
        offer.params = {}
        offer.questions = []
        offer.answerable = False
        offer.reason = 'The exact async question or its terminal options cannot be represented; answer it in the terminal'
        offer.detail = {'terminal_only': True}
    return offer


def replies(content):
    """Recognize complete native user-message envelopes, never ordinary prose or labels."""
    if not isinstance(content, list):
        return ()
    body = None
    for part in content:
        if isinstance(part, dict) and part.get('type') in ('skill', 'mention'):
            continue
        if body is not None or not isinstance(part, dict) or part.get('type') != 'text':
            return ()
        body = part
    if body is None:
        return ()
    text = body.get('text')
    try:
        if not isinstance(text, str) or len(text.encode('utf-8')) > codec.RESPONSE_BYTES or '\0' in text:
            return ()
        text = text.strip()
        if text.startswith('# Context from my IDE setup:\n'):
            marker = '\n## My request for Codex:\n'
            if marker not in text:
                return ()
            text = text.rsplit(marker, 1)[1].strip()
        if not text.startswith(START) or not text.endswith(END):
            return ()
        class ObjectPairs(list):
            pass
        def invalid_constant(_):
            raise ValueError('Invalid native JSON constant')
        values = json.loads(text[len(START):-len(END)], object_pairs_hook=ObjectPairs,
                            parse_constant=invalid_constant)
        values = [values] if isinstance(values, ObjectPairs) else values
        if not isinstance(values, list) or not values:
            return ()
        required = {'answer', 'question', 'questionItemId'}
        projected = []
        for value in values:
            if not isinstance(value, ObjectPairs):
                return ()
            known = {}
            for key, part in value:
                if key not in required:
                    continue  # Native ignores extras; never retain their values.
                if key in known:
                    return ()  # Derived native deserialization rejects duplicate known fields.
                known[key] = part
            if (set(known) != required or any(not isinstance(v, str) or '\0' in v for v in known.values())
                    or len(known['questionItemId'].encode('utf-8')) > 512):
                return ()
            projected.append(known)
        return projected
    except (ValueError, TypeError, UnicodeError, RecursionError):
        return ()
