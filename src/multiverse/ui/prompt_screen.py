"""Answer a waiting agent's prompt: its choices or questions, as the harness offers them; core checks and sends."""
from rich.text import Text
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Vertical, VerticalScroll
from textual.screen import ModalScreen
from textual.widgets import Footer, Input, Label, OptionList, SelectionList, Static
from textual.widgets.option_list import Option

from ..display import clean
from . import theme

DETAIL_LINES, DETAIL_WIDTH = 12, 200  # what the screen shows of a prompt's detail; `mv2 answer --json` has it all


def prompt_line(item):
    """`permission · Bash: rm -rf build` for a prompt as core lists it."""
    return f"{item['kind']} · " + (clean(item['tool']) + ': ' if item.get('tool') else '') + clean(item['summary'])


def detail_text(item):
    lines = []
    for key, value in (item.get('detail') or {}).items():
        text = ' '.join(clean(value).split()) if isinstance(value, str) else clean(value)
        lines.append(f"{clean(key)}: {text[:DETAIL_WIDTH]}" + ('…' if len(text) > DETAIL_WIDTH else ''))
    return '\n'.join(lines[:DETAIL_LINES] + (['…'] if len(lines) > DETAIL_LINES else []))


class AnswerScreen(ModalScreen[dict | None]):
    """One open prompt: its choices (with a message where the chosen one takes it) or its questions (an option, several
    where the question allows it, typed text where it allows that, hidden for a secret). Nothing counts until the
    person picks it: a choice with Enter or a click, an option with Space. Ctrl+Enter answers, Esc closes without
    sending anything. It dismisses with {choice, message, answers: {question id: [labels]}, notes: {question id:
    note}}."""
    BINDINGS = [Binding("escape", "cancel", "Cancel"), Binding("ctrl+enter", "submit", "Answer")]
    DEFAULT_CSS = """
    AnswerScreen { align: center middle; background: $mv-crust 75%; }
    AnswerScreen #answer-body { height: auto; max-height: 30; }
    AnswerScreen .prompt-detail { height: auto; color: $mv-subtext0; margin-bottom: 1; }
    AnswerScreen OptionList, AnswerScreen SelectionList { height: auto; max-height: 8; border: none; background: $mv-base; }
    AnswerScreen .question { height: auto; margin-top: 1; }
    AnswerScreen #answer-error { height: auto; color: $mv-red; }
    """

    def __init__(self, agent, item):
        super().__init__()
        self.agent, self.item = agent, item
        self.choices = list(item.get('choices') or ())
        self.questions = list(item.get('questions') or ())
        self.picked = None  # the choice the person picked: the list's highlight alone never counts

    def compose(self) -> ComposeResult:
        with Vertical(id="dialog"):
            yield Label(f"Answer {clean(self.agent)} · " + prompt_line(self.item), id="dialog-title", markup=False)
            with VerticalScroll(id="answer-body"):
                detail = detail_text(self.item)
                if detail:
                    yield Static(detail, classes="prompt-detail", markup=False)
                if self.item.get('subagent'):
                    yield Static(f"Asked by its subagent {clean(self.item['subagent'])}.", classes="prompt-detail",
                                 markup=False)
                if self.choices:
                    yield OptionList(*(Option(self.choice_label(choice), id=choice['id']) for choice in self.choices),
                                     id="prompt-choices")
                    yield Static(self.pick_hint(), id="prompt-picked", markup=False)
                    yield Input(placeholder="message to go with this choice", id="prompt-message")
                for number, question in enumerate(self.questions):
                    yield Label(self.question_label(question), classes="question", markup=False)
                    options = question.get('options') or ()
                    if options:  # nothing picked until the person picks (Space): a list would start on its first
                        yield SelectionList(*((clean(label) + (f"  {clean(about)}" if about else ''), label)
                                              for label, about in options), id=f"q{number}")
                    if question.get('free_text') or question.get('secret'):
                        hint = ("the answer; it isn't shown or recorded" if question.get('secret')
                                else 'or type an answer' if options else 'your answer')
                        yield Input(placeholder=hint, password=bool(question.get('secret')), id=f"q{number}-text")
                    if question.get('takes_note'):  # Codex: a note that goes with the option picked
                        hint = ("a note to go with it; it isn't shown or recorded" if question.get('secret')
                                else 'a note to go with it (optional)')
                        yield Input(placeholder=hint, password=bool(question.get('secret')), id=f"q{number}-note")
            yield Static("", id="answer-error", markup=False)
        yield Footer()

    @staticmethod
    def choice_label(choice):
        label = Text(clean(choice['label']))
        label.append(f"  {clean(choice['id'])}", style=theme.OVERLAY1)
        if choice.get('saves_rule'):
            label.append("  saves a rule to your settings", style=theme.PEACH)
        return label

    @staticmethod
    def question_label(question):
        notes = [note for note, on in (('Space picks; choose any' if question.get('multiple') else 'Space picks one',
                                        bool(question.get('options'))), ('secret', question.get('secret')),
                                       ('optional', not question.get('required', True))) if on]
        return ((clean(question['header']) + ': ' if question.get('header') else '') + clean(question['question'])
                + (f"  ({', '.join(notes)})" if notes else ''))

    def pick_hint(self):
        return ("Answer the questions, or pick a choice instead: Enter on it." if self.questions
                else "Enter picks a choice.")

    def on_mount(self):
        if self.choices:
            choices = self.query_one("#prompt-choices", OptionList)
            choices.highlighted = 0
            self.show_message_box()
        fields = self.query("#answer-body SelectionList, #answer-body Input") if self.questions else ()
        if fields:
            fields.first().focus()  # a form: its questions first, its choices (decline, cancel) only if picked
        elif self.choices:
            choices.focus()

    def chosen(self):
        """The choice the person picked, or None."""
        return next((choice for choice in self.choices if choice['id'] == self.picked), None)

    def show_message_box(self):
        """The message box, for the picked choice when it takes one (or, before a pick, the highlighted one)."""
        choice = self.chosen()
        if choice is None and self.choices:
            index = self.query_one("#prompt-choices", OptionList).highlighted
            choice = self.choices[index] if index is not None else None
        self.query_one("#prompt-message", Input).display = bool(choice and choice.get('takes_message'))

    def on_option_list_option_highlighted(self, event):
        if event.option_list.id == "prompt-choices":
            self.show_message_box()

    def on_option_list_option_selected(self, event):
        event.stop()
        if event.option_list.id != "prompt-choices":
            return
        if self.questions and self.picked == event.option.id:
            self.picked = None  # Enter on the picked choice again takes it back: the form's answers go instead
            self.query_one("#prompt-picked", Static).update(self.pick_hint())
            self.show_message_box()
            return
        self.picked = event.option.id
        choice = self.chosen()
        self.query_one("#prompt-picked", Static).update(
            f"Picked: {clean(choice['label'])}" + ("; the answers above won't be sent. Enter on it again takes it back."
                                                   if self.questions else ''))
        self.show_message_box()
        if choice.get('takes_message'):
            self.query_one("#prompt-message", Input).focus()
        # Enter only picks; Ctrl+Enter answers. Enter on the first highlighted choice (Allow) would otherwise approve
        # the call with one stray key.

    def action_cancel(self):
        self.dismiss(None)

    def notes(self):
        """{question id: note} for the questions that take one and were given one."""
        found = {}
        for number, question in enumerate(self.questions):
            box = self.query(f"#q{number}-note")
            note = box.first().value.strip() if box else ''
            if note:
                found[question['id']] = note
        return found

    def answers(self):
        """{question id: [labels]} as picked or typed; ValueError naming a required question left empty."""
        found, notes = {}, self.notes()
        for number, question in enumerate(self.questions):
            labels = []
            typed = self.query(f"#q{number}-text")
            text = typed.first().value.strip() if typed else ''
            picked = self.query(f"#q{number}")
            if text:
                labels = [text]
            elif picked:
                labels = list(picked.first().selected)
                if len(labels) > 1 and not question.get('multiple'):
                    raise ValueError(f"Pick one answer to {question.get('header') or question['question']}.")
            if labels:
                found[question['id']] = labels
            elif question['id'] in notes and not question.get('options'):
                pass  # a note alone answers a question that offers no labels
            elif question.get('required', True):
                raise ValueError(f"Answer {question.get('header') or question['question']}.")
        return found

    def action_submit(self):
        """A prompt takes one of two answers: the choice picked (with its message), or, with none picked, its
        questions' answers and notes. A picked choice goes alone, whatever was typed into the questions."""
        error = self.query_one("#answer-error", Static)
        choice = self.chosen()
        if choice is not None:
            message = self.query_one("#prompt-message", Input).value.strip()
            self.dismiss({"choice": choice['id'], "message": message if message and choice.get('takes_message') else None,
                          "answers": {}, "notes": {}})
            return
        if self.choices and not self.questions:
            error.update("Pick a choice: Enter on it, or a click.")
            return
        try:
            answers = self.answers()
        except ValueError as problem:
            error.update(str(problem))
            return
        self.dismiss({"choice": None, "message": None, "answers": answers, "notes": self.notes()})
