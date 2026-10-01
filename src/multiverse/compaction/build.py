"""Building summary trees: the merge loop, its retries, and many turns at once.

One turn's tree is built by asking the harness for one merge at a time until a
single root covers the turn. Two kinds of retry apply:

- A merge call that is rate limited or overloaded waits and is repeated, up to
  `rate_limit.retries` times with waits growing from `first` by `multiplier`
  to `max` seconds. Running out of these fails the turn.
- Any other failed merge (a refusal, an unusable reply, a crash) is retried at
  once, from the frontier reached so far, up to `turn_retries` times per turn.

Every error is reported, including those that a retry later recovers from.
Turns are built by at most `concurrency` workers; each takes the turn's lock,
so two processes never build the same turn, and a caller that needs a turn
another process is building can wait for it.
"""
from concurrent.futures import ThreadPoolExecutor
import json
import sys
import threading
import traceback

from .config import Builder
from .model import Node, Turn, estimate_tokens
from .protocol import CompactionSource, MergeError
from .store import Store


class BuildFailed(Exception):
    def __init__(self, turn_id: str, reason: str):
        super().__init__(reason)
        self.turn_id, self.reason = turn_id, reason


class Cancelled(Exception):
    """The caller stopped the build (Esc during /compact, or a signal)."""


class Reporter:
    """Where build events go: the agent's log and status, and an optional live listener.

    `emit` receives each event as a dict; the install entry point writes them as
    JSON lines for the harness to show while it waits.
    """

    def __init__(self, store: Store, emit=None):
        self.store, self.emit_to = store, emit
        self._lock = threading.Lock()

    def emit(self, event: dict):
        if self.emit_to is not None:
            with self._lock:
                self.emit_to(event)

    def info(self, message: str):
        self.store.log(message)
        self.emit({'event': 'log', 'message': message})

    def progress(self, **values):
        self.emit({'event': 'progress', **values})

    def error(self, turn: Turn | None, message: str, *, kind: str, retrying: bool, attempt: int | None = None,
              attempts: int | None = None, wait: float | None = None):
        where = f'turn {turn.id[:8]}' if turn else 'compaction'
        detail = f'{where}: {kind}: {message}'
        if retrying:
            detail += f' (retry {attempt}/{attempts}' + (f' in {wait:.0f}s)' if wait else ' now)')
        self.store.log(detail)
        entry = {'turn': turn.id if turn else None, 'kind': kind, 'message': message, 'retrying': retrying,
                 'attempt': attempt, 'attempts': attempts, 'wait': wait}
        try:
            self.store.add_error(entry)
        except OSError:
            pass
        self.emit({'event': 'error', **entry})


def json_lines(stream=None):
    """An `emit` that writes one JSON object per line and flushes it."""
    out = stream or sys.stdout

    def emit(event):
        out.write(json.dumps(event, ensure_ascii=False) + '\n')
        out.flush()
    return emit


class Build:
    def __init__(self, source: CompactionSource, store: Store, settings: Builder, reporter: Reporter,
                 cancel: threading.Event | None = None):
        self.source, self.store, self.settings, self.reporter = source, store, settings, reporter
        self.cancel = cancel or threading.Event()
        self._counts = threading.Lock()
        self.done = 0
        self.total = 0
        self.built = 0

    def _check(self):
        if self.cancel.is_set():
            raise Cancelled()

    def _merge(self, turn: Turn, frontier):
        attempt = 0
        limits = self.settings.rate_limit
        while True:
            self._check()
            try:
                return self.source.merge(turn, frontier, self.settings)
            except MergeError as error:
                self._check()
                if error.kind != 'rate_limit':
                    raise
                attempt += 1
                if attempt > limits.retries:
                    self.reporter.error(turn, str(error), kind=error.kind, retrying=False)
                    raise BuildFailed(turn.id, f'still rate limited after {limits.retries} retries: {error}') from error
                wait = limits.wait(attempt, error.retry_after)
                self.reporter.error(turn, str(error), kind=error.kind, retrying=True, attempt=attempt,
                                    attempts=limits.retries, wait=wait)
                if self.cancel.wait(wait):
                    raise Cancelled() from error

    def tree(self, turn: Turn, position: int = 1):
        """Build one turn's tree; returns (root, merges) or raises BuildFailed/Cancelled."""
        frontier = list(turn.leaves)
        merges, next_id, failures, steps = [], 0, 0, 0
        limit = 3 * len(frontier)
        while len(frontier) > 1:
            self._check()
            if steps >= limit:
                raise BuildFailed(turn.id, f'no root after {steps} merges')
            try:
                merge = self._merge(turn, frontier)
                if (not 0 <= merge.start < merge.end <= len(frontier) or merge.end - merge.start < 2
                        or not merge.summary.strip()):
                    raise MergeError('invalid', f'the model chose [{merge.start}, {merge.end}) of a '
                                                f'{len(frontier)}-entry frontier, or wrote no summary')
            except MergeError as error:
                self._check()
                failures += 1
                retrying = error.kind != 'usage_limit' and failures <= self.settings.turn_retries
                self.reporter.error(turn, str(error), kind=error.kind, retrying=retrying, attempt=failures,
                                    attempts=self.settings.turn_retries)
                if not retrying:
                    raise BuildFailed(turn.id, f'{error.kind}: {error}') from error
                continue
            summary = merge.summary.strip()
            node = Node(next_id, frontier[merge.start:merge.end], summary, estimate_tokens(summary))
            frontier = [*frontier[:merge.start], node, *frontier[merge.end:]]
            merges.append({'step': steps, 'start': merge.start, 'end': merge.end, 'node': next_id,
                           'frontier': len(frontier)})
            next_id += 1
            steps += 1
            self.reporter.progress(phase='building', turn=turn.id, turn_number=position, turns=self.total,
                                   turns_done=self.done, merges=steps, merges_left=len(frontier) - 1)
        return frontier[0], merges

    def turns(self, turns: list[Turn], *, wait: bool = True, skip=None) -> dict[str, str]:
        """Make sure each turn has a tree; returns {turn id: reason} for the turns that failed.

        With `wait`, a turn another process is building is waited for; without
        it (the background builder) that turn is left to its builder. `skip`
        may decide to leave a turn alone (a background retry backoff).
        """
        pending = [turn for turn in turns if len(turn.leaves) > 1 and not self.store.load_tree(turn)]
        self.total, self.done, self.built = len(pending), 0, 0
        failures: dict[str, str] = {}
        if not pending:
            return failures

        def finish():
            with self._counts:
                self.done += 1
            self.reporter.progress(phase='built', turns_done=self.done, turns=self.total)

        def one(position, turn):
            if skip is not None and skip(turn):
                finish()
                return
            announced = False
            while True:
                self._check()
                held = self.store.turn_lock(turn.id, 'install' if wait else 'background')
                if held is not None:
                    with held:
                        if self.store.load_tree(turn):
                            finish()
                            return
                        self.store.log(f'turn {turn.id[:8]}: building ({len(turn.leaves)} leaves)')
                        try:
                            root, merges = self.tree(turn, position)
                        except BuildFailed as error:
                            failures[turn.id] = error.reason
                            self.store.set_failure(turn.id, error.reason)
                            self.store.log(f'turn {turn.id[:8]}: failed: {error.reason}')
                            finish()
                            return
                        self.store.save_tree(turn, root, harness=self.source.harness, model=self.settings.model,
                                             effort=self.settings.effort, merges=merges)
                        self.store.set_failure(turn.id, None)
                        self.store.log(f'turn {turn.id[:8]}: built in {len(merges)} merges')
                        with self._counts:
                            self.built += 1
                        finish()
                        return
                if not wait:
                    finish()
                    return
                if not announced:
                    holder = self.store.turn_holder(turn.id) or {}
                    self.reporter.progress(phase='waiting', turn=turn.id, turn_number=position, turns=self.total,
                                           turns_done=self.done, holder=holder.get('purpose'))
                    announced = True
                if self.cancel.wait(1.0):
                    raise Cancelled()
                if self.store.load_tree(turn):
                    finish()
                    return

        def guarded(position, turn):
            try:
                one(position, turn)
            except Cancelled:
                raise
            except Exception as error:  # a bug must fail this turn, not leave the others hanging
                failures[turn.id] = f'unexpected error: {error}'
                self.store.log(f'turn {turn.id[:8]}: unexpected error:\n{traceback.format_exc()}')
                self.reporter.error(turn, str(error), kind='failed', retrying=False)

        with ThreadPoolExecutor(max_workers=self.settings.concurrency) as pool:
            futures = [pool.submit(guarded, position, turn) for position, turn in enumerate(pending, 1)]
            cancelled = None
            for future in futures:
                try:
                    future.result()
                except Cancelled as error:
                    self.cancel.set()
                    cancelled = error
            if cancelled:
                raise cancelled
        return failures
