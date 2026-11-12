"""Commands on an agent (message, focus, inbox, kill, resume, attach) and the runner that keeps backend calls off the
event loop."""
import asyncio
from pathlib import Path

from textual import work

from ..display import clean
from ..tools.inbox import readable
from ..tools.unfocus import summary as unfocus_summary
from ..universe import focus
from ..tools.answer import outcome as answer_outcome
from .plugin_service import error_text, in_daemon
from .prompt_screen import AnswerScreen, prompt_line
from .screens import ChoiceScreen, Details, Form

# The focus lengths the dashboard offers: (choice, label, backend arguments).
FOCUS_LENGTHS = (("5m", "5 minutes", {"duration": "5m"}), ("20m", "20 minutes", {"duration": "20m"}),
                 ("1h", "1 hour", {"duration": "1h"}), ("turn", "Until its next turn ends", {"turn": True}),
                 ("on", "Until unfocused", {}))


class AgentCommands:
    """MultiverseApp's actions on the selected agent, and the operation runner they share."""
    def submit_operation(self, label, callback, *args, after=None, describe=None, plugin=False):
        """Run a backend call off the event loop. `describe(result)` may give the
        status line as (message, error); otherwise it says the label completed.

        `plugin` marks a call into plugin code. It runs in a daemon thread, and q does not wait for it: one
        that never returns is left behind, where the dashboard's own operations make q wait until they finish."""
        if self.operating:
            return
        self.operating = True
        self.operating_plugin = plugin
        self.set_status(label + "…")
        self.execute_operation(label, callback, args, after, describe, plugin)

    @work(group="operation", exit_on_error=False)
    async def execute_operation(self, label, callback, args, after, describe, plugin=False):
        succeeded, result = False, None
        try:
            result = await (in_daemon(callback, *args) if plugin else asyncio.to_thread(callback, *args))
            message, error = (describe(result) if describe else None) or (label + " complete.", False)
            self.set_status(message, error=error)
            succeeded = True
        except Exception as error:
            reason = self.code_watch.explain(error)  # old code mixed with new code gets its own sentence
            self.set_status(f"{label} failed: {reason}", error=True)
            self.notify(reason, title=label, severity="error", markup=False)
        finally:
            self.operating = self.operating_plugin = False
        # A scan started before the action may contain old state. Finish it, then
        # read again so a completed mutation cannot be hidden until the next timer.
        if self.scanning:
            await self.workers.wait_for_complete([w for w in self.workers if w.group == "scan"])
        if succeeded and after:
            after(result)
        self.refresh_snapshot()

    def damaged_selection(self, recoverable=None):
        """True, with the status line saying why, when the selected agent's record cannot be read: nothing can act on it.

        `recoverable` is an action the backend can still take on such a record (the row's damaged.recover says so):
        for that one the answer is False and the action goes on."""
        found = (self.selected or {}).get("damaged")
        if not isinstance(found, dict):
            return False
        if recoverable is not None and found.get("recover") == recoverable:
            return False
        self.set_status(f"{clean(self.selected['name'])} cannot be used: {clean(found.get('file', ''))}: "
                        f"{clean(found.get('reason', ''))}", error=True)
        return True

    def action_message(self):
        if self.ready() and self.selected and not self.damaged_selection():
            self.message_form(dict(self.selected))

    def message_form(self, agent):
        """The message form; while the agent is focused, its inbox-only form."""
        held = agent.get("focus")
        # The person's message to a focused agent goes to its inbox too.
        title, delivery, description = (f'Message {agent["name"]}', "steer", "") if not held else (
            f'Message {agent["name"]} (focused)', "inbox-only",
            f'{agent["name"]} is focused {focus.ends(held)}, so this message goes to its inbox without '
            f'interrupting it. To answer it directly, attach with Enter.')
        # If it focuses while the form is open, mv2 saves the message to its inbox instead, and sent() says so.
        self.push_screen(Form(title, [("text", "Message", "", True, True)], description=clean(description)),
                         lambda values: self.submit_operation("Send message", self.backend.send, Path(agent["universe"]),
                                                              agent["id"], values["text"], delivery, describe=sent)
                         if values is not None else None)

    def action_toggle_focus(self):
        if not (self.ready() and self.selected) or self.damaged_selection():
            return
        agent = dict(self.selected)
        root, name = Path(agent["universe"]), agent["name"]
        if agent.get("focus"):
            self.submit_operation("Unfocus", self.backend.unfocus, root, agent["id"], describe=unfocused)
            return
        # A turn focus ends on a reported turn end, so it is offered only while the harness reports turns.
        lengths = [entry for entry in FOCUS_LENGTHS
                   if entry[0] != "turn" or agent.get("status") in ("working", "waiting", "idle")]
        def chosen(key):
            arguments = next((entry[2] for entry in lengths if entry[0] == key), None)
            if arguments is not None:
                self.submit_operation("Focus", self.backend.focus, root, agent["id"], arguments.get("duration"),
                                      arguments.get("turn", False), describe=focused)
        self.push_screen(ChoiceScreen(f"Focus {name}", [entry[:2] for entry in lengths], description=clean(
            f"While {name} is focused, steer and follow-up messages to it are refused and their senders are "
            f"told to send inbox-only, so nothing buries what it needs from you. Esc cancels.")), chosen)

    def action_inbox(self):
        if self.ready() and self.selected:
            self.show_inbox(dict(self.selected))

    @work(group="inbox-view", exclusive=True, exit_on_error=False)
    async def show_inbox(self, row):
        """The agent's unread inbox messages as it will read them; showing them here never marks them read."""
        name = clean(row["name"])
        try:
            messages = await asyncio.to_thread(self.backend.unread, Path(row["universe"]), row["id"])
            text = readable(Path(row["universe"]), {"messages": messages})  # reads each sender's record
        except Exception as error:
            self.set_status(f"{name}: its inbox can't be read: {error_text(error, kind=False)}", error=True)
            return
        if not messages:
            self.set_status(f"{name} has no unread inbox messages.")
            return
        one = len(messages) == 1
        note = (f"{'This message stays' if one else 'These messages stay'} unread for {name}: showing "
                f"{'it' if one else 'them'} here doesn't mark {'it' if one else 'them'} read.")
        self.push_screen(Details(f"{name} · {len(messages)} unread inbox message{'' if one else 's'}",
                                 note + "\n\n" + "\n".join(clean(line) for line in text.splitlines())))

    def action_answer(self):
        """Answer what the selected agent waits on, or a question it has open while it works; core says which."""
        if self.ready() and self.selected:
            self.open_answer(dict(self.selected))

    @work(group="answer-view", exclusive=True, exit_on_error=False)
    async def open_answer(self, row):
        """Read the agent's prompts fresh, then open the one to answer; nothing is sent until the person answers."""
        name = clean(row["name"])
        waiting = row.get("status") == "waiting"
        try:
            found = await asyncio.to_thread(self.backend.waiting_prompts, Path(row["universe"]), row["id"])
        except Exception as error:
            self.set_status(f"{name}: its prompts can't be read: {error_text(error, kind=False)}", error=True)
            return
        if not waiting and found.get("state") == "ok" and not found.get("prompts"):
            # none listed is no open questions only as far as mv2 can see; a failed read says its own reason below
            self.set_status(f"{name} isn't waiting on a prompt and has no open questions"
                            + ("." if found.get("coverage") == "complete" else
                               f" mv2 can see; it may not see every one: "
                               + clean(found.get("reason") or "coverage unknown") + "."))
            return
        if found.get("state") != "ok":
            self.set_status(f"{name}: " + clean(found.get("reason") or "its prompts can't be read"), error=True)
            return
        open_prompts = [item for item in found.get("prompts") or () if item.get("answerable", True)]
        if not open_prompts:
            closed = (found.get("prompts") or [None])[0]
            self.set_status(f"{name}: " + (clean(closed.get("reason") or "answer it in the terminal") if closed else
                                           "no prompt mv2 can see"
                                           + (f" ({clean(found.get('reason'))})" if found.get("reason") else "") + "."))
            return
        if len(open_prompts) == 1:
            self.answer_screen(row, open_prompts[0])
            return

        def picked(prompt_id):
            if prompt_id:
                self.answer_screen(row, next(item for item in open_prompts if item["id"] == prompt_id))
        self.push_screen(ChoiceScreen(f"{name} {'waits on' if waiting else 'has'} {len(open_prompts)} "
                                      f"{'prompts' if waiting else 'open questions'}",
                                      [(item["id"], prompt_line(item)) for item in open_prompts]), picked)

    def answer_screen(self, row, item):
        def answered(values):
            if values is None:  # Esc: nothing was sent
                return
            agent = row["id"]

            def describe(result):
                try:
                    said, ok = answer_outcome(result, row["name"]), True
                except ValueError as refused:
                    said, ok = str(refused), False
                self.prompt_outcomes[agent] = (said, ok)
                return said, not ok
            self.submit_operation(f"Answer {clean(row['name'])}", self.backend.answer_prompt, Path(row["universe"]),
                                  agent, item["id"], values, describe=describe,
                                  after=lambda result: self.inspector.show_prompts(None) if self.inspector.row else None)
        self.push_screen(AnswerScreen(row["name"], item), answered)

    def action_stop(self):
        self.action_kill()

    def action_kill(self):
        if self.ready() and self.selected and self.damaged_selection("kill"):
            return
        # A record whose runtime file is missing is not "alive" to the dashboard, but kill can find its terminal.
        recovers = isinstance(self.selected, dict) and (self.selected.get("damaged") or {}).get("recover") == "kill"
        if self.ready() and self.selected and (recovers or self.selected["live"] in ('alive', 'ending', 'stopped', 'dormant')):
            agent = dict(self.selected)
            description = ("Ctrl+Enter finds this agent's tagged terminal (its runtime record is missing) and stops it."
                           if recovers else
                           "Ctrl+Enter cancels native work only; no terminal remains to close."
                           if agent['live'] == 'dormant' else
                           "Ctrl+Enter ends running work and removes its tmux wrapper.")
            self.push_screen(Form(f'Kill {agent["name"]}?', description=description +
                                  " Files and conversation remain for resume. Esc cancels."),
                             lambda values: self.submit_operation("Kill agent", self.backend.kill, Path(agent["universe"]),
                                                                  agent["id"], describe=killed)
                             if values is not None else None)

    def action_resume(self):
        if self.ready() and self.selected and self.damaged_selection():
            return
        if self.ready() and self.selected and self.selected["live"] in ('stopped', 'dormant'):
            agent = dict(self.selected)
            self.submit_operation("Resume agent", self.backend.resume, Path(agent["universe"]), agent["id"])

    def action_attach(self):
        if self.ready() and self.selected and self.damaged_selection():
            return
        if self.ready() and self.selected and self.selected["live"] == "alive":
            self.operating = True
            self.attach_agent(dict(self.selected))

    @work(group="attach", exit_on_error=False)
    async def attach_agent(self, agent):
        try:
            if self.backend.demo:
                self.set_status("Demo terminal. In a live universe, Enter opens the agent's tmux pane.")
            else:
                attach_error = None
                with self.suspend():
                    # Textual resumes the terminal only after a normal exit.
                    # Report attach failures after leaving suspension.
                    try:
                        # Suspend stops the terminal writer. Do not yield to UI
                        # timers here: rendering can fill its queue and deadlock
                        # the event loop before the driver can be resumed.
                        self.backend.attach(Path(agent["universe"]), agent["id"])
                    except Exception as error:
                        attach_error = error
                if attach_error is not None:
                    raise attach_error
        except Exception as error:
            self.set_status(f"Attach failed: {self.code_watch.explain(error)}", error=True)
        finally:
            self.operating = False
            self.refresh_snapshot()


def sent(result):
    """Status for a sent message: saved, then whatever native delivery reported."""
    result = result or {}
    if result.get('state') == 'held':
        return "It is focused now, so the message was saved to its inbox instead.", False
    if result.get('delivery') == 'inbox-only':
        return "Saved to its inbox; it reads it at its next inbox check.", False
    receipt = result.get('native_delivery', {})
    state = receipt.get('state', 'queued')
    detail = receipt.get('reason', 'delivery and an answer may follow')
    return f"Message saved; {state}: {detail}", state in ('error', 'unavailable')


def focused(result):
    return f"Focused {result['agent']} {result['ends']}: messages to it go to its inbox.", False


def unfocused(result):
    """Status for an unfocus: messages reach the agent again, and what waits in its inbox."""
    return unfocus_summary(result), False


def killed(result):
    """Status for a kill: what native cancellation confirmed, when it reported."""
    native = (result or {}).get('native_cancellation')
    if not native:
        return None
    detail = native.get('reason', 'Native cancellation complete')
    if 'cancelled_submissions' in native:
        detail += (f" ({native['cancelled_submissions']} queued cancelled, "
                   f"{native['interrupted_turns']} turn interrupts)")
    incomplete = native.get('state') != 'ok'
    return ('Terminal closed: ' if incomplete else 'Kill complete: ') + detail, incomplete
