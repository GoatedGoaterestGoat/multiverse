"""Claude's implementation of the compaction protocol (multiverse.compaction.protocol).

- turns()   reads the session's transcript under Claude's `projects` directory
            across every compaction, leaving out rows a compaction added.
- completed() takes the mod's turn-end marker: when the turn ended, in
            milliseconds.
- merge()   runs one merge step through `claude -p` over a synthetic session.
- render()  builds the messages the multiverse mod's `session.compact` hook returns.
Which rows an install added is read back from the transcript: Claude writes
the compaction's boundary, then one row per message the install returned. Each
install is recorded in `installed.jsonl` the first time it is read.

The mod triggers an install from the session's own `/compact`; see
`mod/hooks/compaction.ts`.
"""
from datetime import datetime, timezone
import glob
import json
from pathlib import Path
import subprocess
import threading

from ....compaction.store import Store
from . import merge as merging
from . import render as rendering
from . import transcript


class TranscriptError(ValueError):
    pass


# Claude Code's --effort levels.
EFFORTS = ('low', 'medium', 'high', 'xhigh', 'max')
# Claude's builder settings where compaction.json gives none. Sonnet 5.5 builds;
# Sonnet 4.6 (which refused nothing in testing but builds nearly flat trees)
# takes over a turn Sonnet 5.5 still refuses. claude_max_retries is Claude
# Code's own quick retries inside each call (CLAUDE_CODE_MAX_RETRIES), before ours.
BUILDER_DEFAULTS = {'model': 'sonnet', 'fallback_model': 'claude-sonnet-4-6', 'effort': 'medium',
                    'claude_max_retries': 3}


def check_builder(builder):
    """Raise ValueError naming the field when Claude cannot build trees with these settings."""
    if not builder.model:
        raise ValueError('model must name a Claude model')
    if builder.effort not in EFFORTS:
        raise ValueError(f'effort must be one of {", ".join(EFFORTS)}')
    unknown = set(builder.options) - {'claude_max_retries'}
    if unknown:
        raise ValueError(f'unknown Claude settings: {", ".join(sorted(unknown))}')
    retries = builder.options.get('claude_max_retries')
    if isinstance(retries, bool) or not isinstance(retries, int) or not 0 <= retries <= 15:
        raise ValueError('claude_max_retries must be a whole number from 0 to 15')


class ClaudeCompaction:
    harness = 'claude'
    keeps_images = False  # render() builds every message, and a built message holds only text
    builder_defaults = BUILDER_DEFAULTS
    check_builder = staticmethod(check_builder)

    def __init__(self, adapter, session):
        if not session.native_id:
            raise TranscriptError('No Claude session ID recorded for this agent')
        if not session.session_file:
            raise TranscriptError('No agent directory recorded for this agent')
        self.adapter, self.session = adapter, session
        self.agent_dir = Path(session.session_file).parent
        self.store = Store(self.agent_dir)
        self.rows_by_uuid: dict[str, dict] = {}
        self.order: list[str] = []
        self._processes: set[subprocess.Popen] = set()
        self._lock = threading.Lock()
        self._cancelled = False

    # Paths and process control --------------------------------------------

    @staticmethod
    def claude_home() -> Path:
        from .. import claude_home
        return claude_home()

    def transcript_path(self) -> Path:
        pattern = str(self.claude_home() / 'projects' / '*' / f'{self.session.native_id}.jsonl')
        found = sorted((Path(path) for path in glob.glob(pattern)), key=lambda path: path.stat().st_mtime)
        if not found:
            raise TranscriptError(f'Claude has not written a transcript for session {self.session.native_id} yet')
        return found[-1]

    def work_directory(self) -> Path:
        """The working directory of merge calls: a Claude project of its own."""
        path = self.store.directory / 'work'
        path.mkdir(parents=True, exist_ok=True)
        return path.resolve()

    def executable(self) -> str:
        return self.adapter.spec['spawn'][0]

    def unset_env(self) -> tuple[str, ...]:
        return tuple(self.adapter.spec.get('unset_env', []))

    def harness_env(self) -> dict[str, str]:
        return dict(self.adapter.spec.get('env', {}))

    def call(self, argv, cwd, env, timeout):
        """Run one Claude call; `cancel()` from another thread stops it."""
        with self._lock:
            if self._cancelled:
                return 1, '', 'cancelled'
            process = subprocess.Popen(argv, cwd=cwd, env=env, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                                       stderr=subprocess.PIPE, text=True)
            self._processes.add(process)
        try:
            stdout, stderr = process.communicate(timeout=timeout)
        except subprocess.TimeoutExpired:
            process.kill()
            process.communicate()
            return 1, '', f'timed out after {timeout} s'
        finally:
            with self._lock:
                self._processes.discard(process)
        return process.returncode, stdout, stderr

    def cancel(self):
        with self._lock:
            self._cancelled = True
            for process in self._processes:
                try:
                    process.terminate()
                except OSError:
                    pass

    # Protocol ----------------------------------------------------------------

    def unavailable(self):
        if not getattr(self.adapter, 'settings', {}).get('inbox_bridge'):
            return 'compaction needs the multiverse mod: set "inbox_bridge": true in harnesses/claude/harness.json'
        return None

    def turns(self):
        rows = transcript.read_rows(self.transcript_path())
        records = self.store.installed()
        records += self._record_installs(rows, records)
        added = {row['uuid'] for record in records for row in record.get('rows', [])}
        self.rows_by_uuid = {row['uuid']: row for row in rows if row.get('uuid')}
        self.order = [row['uuid'] for row in transcript.chain(rows) if row.get('uuid')]
        return transcript.turns(rows, added)

    def completed(self, turns, marker):
        """Every turn whose prompt was stored before the turn end the marker gives; without one, all but the newest."""
        try:
            ended = datetime.fromtimestamp(int(marker) / 1000, timezone.utc)
        except (TypeError, ValueError, OverflowError):
            return turns[:-1]
        complete = []
        for turn in turns:
            row = self.rows_by_uuid.get(turn.prompt.refs[0], {}) if turn.prompt else {}
            try:
                started = datetime.fromisoformat(row['timestamp'])
            except (KeyError, TypeError, ValueError):
                started = None
            if started is not None and started > ended:
                break
            complete.append(turn)
        return complete

    def merge(self, turn, frontier, builder):
        return merging.run(self, turn, frontier, builder)

    def render(self, turns, frontiers, offered, images):
        return rendering.render(turns, frontiers)

    # Recovery ------------------------------------------------------------------

    def _record_installs(self, rows, records) -> list[dict]:
        """Records of our installs not yet recorded, read from the transcript.

        An install saves its payload, then Claude writes a boundary and one row
        per message: the newest payload saved before the boundary says how many
        rows follow it and what each stands for. Without one, the history cannot
        be told apart from what the install added, and reading stops.
        """
        recorded = {record.get('boundary') for record in records}
        used = {record.get('install') for record in records}
        recovered = []
        for found in transcript.unrecorded_boundaries(rows, recorded):
            boundary = found['boundary']
            payload = self._payload_before(boundary.get('timestamp') or '', used)
            if payload is None or len(found['after']) < payload.get('count', 0) or not payload.get('count'):
                raise TranscriptError(
                    f'A compaction at {boundary.get("timestamp")} (row {boundary["uuid"]}) has no record of the '
                    'rows it added, so the conversation before it cannot be told apart from them')
            entries = [{'uuid': row['uuid'], 'stands_for': source.get('stands_for')}
                       for row, source in zip(found['after'][:payload['count']], payload.get('sources') or [])]
            record = {'install': payload['install_id'], 'boundary': boundary['uuid'], 'rows': entries}
            self.store.add_installed(record)
            self.store.log(f'recorded install {payload["install_id"]}: {len(entries)} rows after boundary {boundary["uuid"]}')
            used.add(payload['install_id'])
            recovered.append(record)
        return recovered

    def _payload_before(self, timestamp: str, used: set) -> dict | None:
        try:
            boundary = datetime.fromisoformat(timestamp)
        except ValueError:
            return None
        best, best_at = None, None
        for path in (self.store.directory / 'installs').glob('*.json'):
            try:
                payload = json.loads(path.read_text())
                created = datetime.fromisoformat(payload['created_at'])
            except (OSError, ValueError, KeyError, TypeError):
                continue
            if payload.get('install_id') in used or created > boundary:
                continue
            if best_at is None or created > best_at:
                best, best_at = payload, created
        return best
