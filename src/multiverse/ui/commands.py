"""Commands on an agent (message, kill, resume, attach) and the runner that keeps backend calls off the event loop."""
import asyncio
from pathlib import Path

from textual import work

from ..display import clean
from .plugin_service import in_daemon
from .screens import Form


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
            agent = dict(self.selected)
            self.push_screen(Form(f'Message {agent["name"]}', [("text", "Message", "", True, True)]),
                             lambda values: self.submit_operation("Send message", self.backend.send, Path(agent["universe"]),
                                                                  agent["id"], values["text"], describe=sent)
                             if values is not None else None)

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
    receipt = (result or {}).get('native_delivery', {})
    state = receipt.get('state', 'queued')
    detail = receipt.get('reason', 'delivery and an answer may follow')
    return f"Message saved; {state}: {detail}", state in ('error', 'unavailable')


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
