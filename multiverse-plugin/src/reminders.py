"""Conditional reminders appended to agent-facing inter-agent tool results.

Keep the policies here, separate from tool implementations. Transports invoke
this after an operation, so setting a description immediately removes its
reminder. Lifecycle helpers and SecondBrain's direct Python calls do not
receive these agent-facing messages.
"""

from __future__ import annotations

from .agent_descriptions import MAX_DESCRIPTION_CHARS, read_description


def active_reminders(name: str | None, cwd: str | None) -> list[str]:
    if not name or not cwd:
        return []
    reminders = []
    if read_description(cwd, name) is None:
        reminders.append(
            "You have no agent description set. When ready, use set_description "
            f"with a very short, concise description (at most {MAX_DESCRIPTION_CHARS} "
            "characters) covering responsibilities, completed tasks, ongoing work, "
            "or expertise so other agents and the user can see what to delegate to you. "
            "Update it at your discretion as your work changes."
        )
    return reminders


def append_reminders(result: str, name: str | None, cwd: str | None) -> str:
    reminders = active_reminders(name, cwd)
    if not reminders:
        return result
    return result + "\n\n[Multiverse reminder]\n" + "\n".join(reminders)
