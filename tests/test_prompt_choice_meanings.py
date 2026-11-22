from unittest import TestCase

from multiverse.harnesses.types import PromptChoice, PromptDisplay, WaitingPrompt
from multiverse.runtime.prompts import _prompt


class PromptChoiceMeaningTest(TestCase):
    def choice(self, **fields):
        choice = PromptChoice(id='option', label='Long label with native scope', **fields)
        return _prompt(WaitingPrompt(id='permission', kind='permission', summary='Review', choices=(choice,))).choices[0]

    def test_core_uses_canonical_labels_and_preserves_unannotated_old_choices(self):
        """Native phrasing must not change a canonical short label or silently reclassify an older saving choice."""
        labels = {
            'allow': 'Allow once', 'allow_session': 'Allow for session', 'allow_always': 'Allow always',
            'deny': 'Deny', 'deny_always': 'Deny always', 'stop': 'Stop', 'dismiss': 'Dismiss',
            'approve_plan': 'Approve plan', 'keep_planning': 'Keep planning',
        }
        for meaning, short in labels.items():
            with self.subTest(meaning=meaning):
                choice = self.choice(meaning=meaning, short='Native wording', saves_rule=meaning.endswith('_always'))
                self.assertEqual((choice.id, choice.meaning, choice.short), ('option', meaning, short))
                self.assertEqual(choice.label, 'Long label with native scope')
        for saves in (False, True):
            choice = self.choice(saves_rule=saves)
            self.assertEqual((choice.meaning, choice.short, choice.saves_rule), (None, None, saves))

    def test_inconsistent_rule_policy_and_invalid_other_labels_refuse_the_inventory(self):
        """A provider cannot present a permanent choice as temporary or hide control text in a short label."""
        for saves_rule in (True, False):
            choice = self.choice(meaning='other', short='x' * 24, saves_rule=saves_rule)
            self.assertEqual((choice.short, choice.saves_rule), ('x' * 24, saves_rule))
        for fields in ({'meaning': 'allow', 'saves_rule': True}, {'meaning': 'deny_always'},
                       {'meaning': 'guess'}, {'short': 'No meaning'}, *(
                            {'meaning': 'other', 'short': label} for label in (None, '', '   ', 'a\nb', 'a\x1b', 'x' * 25))):
            with self.subTest(fields=fields), self.assertRaises(ValueError):
                self.choice(**fields)

    def test_display_accepts_bounded_metadata_and_rejects_multiline_fields(self):
        """A malformed display could put terminal controls or arbitrarily large text in the answer screen."""
        prompt = {'id': 'approval', 'kind': 'permission', 'summary': 'Review'}
        self.assertIsNone(_prompt(prompt).display)
        display = {'title': 'x' * 80, 'subject': 'y' * 500, 'lines': ['z' * 200] * 8}
        expected = PromptDisplay(**dict(display, lines=tuple(display['lines'])))
        self.assertEqual(_prompt(dict(prompt, display=display)).display, expected)
        self.assertEqual(_prompt(WaitingPrompt(**prompt, display=expected)).display, expected)
        for fields in ({'title': 'x' * 81}, {'title': ''}, {'title': ' '}, {'subject': 'y' * 501},
                       {'subject': 'a\x1b'}, {'lines': ['x'] * 9}, {'lines': ['z' * 201]}, {'lines': ['a\nb']},
                       {'lines': [None]}, {'lines': 'one line'}):
            with self.subTest(fields=fields), self.assertRaises(ValueError):
                _prompt(dict(prompt, display={**display, **fields}))
