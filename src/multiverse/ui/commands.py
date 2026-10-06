"""Commands on an agent (message, kill, resume, attach, compaction) and the runner that keeps backend calls off the event loop."""
import asyncio
from pathlib import Path

from textual import work

from .compaction import CompactionScreen
from .screens import Form



class AgentCommands:
    """MultiverseApp's actions on the selected agent, and the operation runner they share."""
    def submit_operation(self, label, callback, *args, after=None, describe=None):
        """Run a backend call off the event loop. `describe(result)` may give the
        status line as (message, error); otherwise it says the label completed."""
        if self.operating:
            return
        self.operating = True
        self.set_status(label + "…")
        self.execute_operation(label, callback, args, after, describe)

    @work(group="operation", exit_on_error=False)
    async def execute_operation(self, label, callback, args, after, describe):
        succeeded, result = False, None
        try:
            result = await asyncio.to_thread(callback, *args)
            message, error = (describe(result) if describe else None) or (label + " complete.", False)
            self.set_status(message, error=error)
            succeeded = True
        except Exception as error:
            self.set_status(f"{label} failed: {error}", error=True)
            self.notify(str(error), title=label, severity="error", markup=False)
        finally:
            self.operating = False
        # A scan started before the action may contain old state. Finish it, then
        # read again so a completed mutation cannot be hidden until the next timer.
        if self.scanning:
            await self.workers.wait_for_complete([w for w in self.workers if w.group == "scan"])
        if succeeded and after:
            after(result)
        self.refresh_snapshot()

    def action_compaction(self):
        if self.ready():
            root = self.target_scope()
            self.push_screen(CompactionScreen(root, self.backend, self.scope_name(root)), self.compaction_saved)

    def compaction_saved(self, overview):
        if overview:
            self.set_status("Compaction settings saved. Restart agents for a changed enabled setting to apply.")

    def action_message(self):
        if self.ready() and self.selected:
            agent = dict(self.selected)
            self.push_screen(Form(f'Message {agent["name"]}', [("text", "Message", "", True, True)]),
                             lambda values: self.submit_operation("Send message", self.backend.send, Path(agent["universe"]),
                                                                  agent["id"], values["text"], describe=sent)
                             if values is not None else None)

    def action_stop(self):
        self.action_kill()

    def action_kill(self):
        if self.ready() and self.selected and self.selected["live"] in ('alive', 'ending', 'stopped', 'dormant'):
            agent = dict(self.selected)
            description = ("Ctrl+Enter cancels native work only; no terminal remains to close."
                           if agent['live'] == 'dormant' else
                           "Ctrl+Enter ends running work and removes its tmux wrapper.")
            self.push_screen(Form(f'Kill {agent["name"]}?', description=description +
                                  " Files and conversation remain for resume. Esc cancels."),
                             lambda values: self.submit_operation("Kill agent", self.backend.kill, Path(agent["universe"]),
                                                                  agent["id"], describe=killed)
                             if values is not None else None)

    def action_resume(self):
        if self.ready() and self.selected and self.selected["live"] in ('stopped', 'dormant'):
            agent = dict(self.selected)
            self.submit_operation("Resume agent", self.backend.resume, Path(agent["universe"]), agent["id"])

    def action_attach(self):
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
            self.set_status(f"Attach failed: {error}", error=True)
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
