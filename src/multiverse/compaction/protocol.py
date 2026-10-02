"""What a harness provides so its sessions can use tree compaction.

A harness adapter that supports compaction has a `compaction(session)` method
returning a CompactionSource for that exact session, and may have
`compaction_state(root)`: its settings, or why the universe's compaction.json
cannot be used, for its launches, capabilities and `mv2 doctor`. The generic code in this
package calls only the methods below; reading a native transcript, running a
model and installing the result stay in the harness's own package.

A harness integration drives three entry points (`python -m multiverse.compaction`):
`install` when the session compacts, `background --turn-ended MARKER` when a
turn ends, and `guard` to refuse the harness's own compaction. After an
install, the harness must be able to tell the rows it added from the
conversation (`turns()` leaves them out); each install's payload is kept in the
agent's compaction directory for that.
"""
from dataclasses import dataclass
from typing import Any, Protocol

from .model import Entry, Turn


class MergeError(Exception):
    """One merge step failed; `kind` says whether waiting can help.

    rate_limit   the model was rate limited, overloaded or failed transiently:
                 wait and retry the same step
    usage_limit  the account's usage limit is reached: a retry soon cannot help
    refused      the model refused the merge
    invalid      the reply was not a usable merge
    failed       anything else: the harness could not run, timed out or crashed
    """
    KINDS = ('rate_limit', 'usage_limit', 'refused', 'invalid', 'failed')

    def __init__(self, kind: str, message: str, retry_after: float | None = None):
        if kind not in self.KINDS:
            raise ValueError(f'Unknown merge error kind: {kind}')
        super().__init__(message)
        self.kind, self.retry_after = kind, retry_after


@dataclass(frozen=True)
class Merge:
    """Collapse frontier[start:end] into one node with this summary."""
    start: int
    end: int
    summary: str


class CompactionSource(Protocol):
    """One session's transcript and model, as compaction needs them."""

    harness: str
    # Whether render() can deliver prompt images; when it cannot, every prompt's
    # images are left out and the budget spends nothing on them.
    keeps_images: bool
    # This harness's builder settings where compaction.json gives none, in the
    # file's shape: its model, fallback model and effort, and its own options.
    builder_defaults: dict

    def check_builder(self, builder: Any) -> None:
        """Raise ValueError naming the field when this harness cannot build trees with `builder`."""
        ...

    def unavailable(self) -> str | None:
        """Why this session cannot install a compaction (its harness integration is off), or None."""
        ...

    def turns(self) -> list[Turn]:
        """The whole conversation, oldest turn first, as it was before any compaction.

        Rows that a compaction added (summaries, copies of kept rows) are left
        out, so turns and their leaves keep the same identity across compactions.
        A turn that is not conversation (Claude: a display-only command such as
        `/compact`) is marked `dropped` with the reason; compaction never builds,
        keeps or renders it.
        """
        ...

    def completed(self, turns: list[Turn], marker: str | None) -> list[Turn]:
        """The turns known to have finished, oldest first.

        `marker` is what the harness's turn-end signal passed to the background
        builder, opaque here (for Claude, when the turn ended, in milliseconds).
        Every turn up to the one it names is complete. Without a marker, every
        turn but the newest, which may still be running.
        """
        ...

    def merge(self, turn: Turn, frontier: list[Entry], builder: Any) -> Merge:
        """Ask the model for the next merge of this turn's frontier.

        `builder` is this harness's builder settings (config.Builder: its
        model, effort and own options, checked by check_builder). Raises
        MergeError; never retries itself beyond the harness's own short
        transport retries.
        """
        ...

    def render(self, turns: list[Turn], frontiers: dict[str, list[Entry]], offered: Any,
               images: set[str]) -> dict[str, Any]:
        """The harness's installable form of the compacted conversation.

        `turns` are the turns that stay (dropped ones are not passed);
        `frontiers` maps each to the entries it keeps, and `images` names the
        turns whose prompt keeps its images (the others are noted as omitted).
        `offered` is what the harness's install trigger passed in (for Claude,
        the rows it may keep).
        The result is stored as the install's payload and must include
        `messages`, which the install streams to the trigger, and `count`, the
        number of rows the harness will store for them.
        """
        ...

    def cancel(self) -> None:
        """Stop any model call in progress; called from another thread."""
        ...
