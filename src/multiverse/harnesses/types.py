"""Adapter boundary: specifications and observations, never tmux or state writes."""
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Literal, Mapping, Protocol

CapabilityName = Literal['launch', 'resume', 'steer', 'follow_up', 'native_queue', 'wake', 'activity', 'usage', 'interrupt', 'ask']
CAPABILITIES: tuple[CapabilityName, ...] = ('launch', 'resume', 'steer', 'follow_up', 'native_queue', 'wake', 'activity', 'usage', 'interrupt', 'ask')


@dataclass(frozen=True)
class Capability:
    state: Literal['supported', 'unavailable', 'error']
    reason: str = ''
    mechanism: str | None = None


@dataclass(frozen=True)
class CommandSpec:
    argv: tuple[str, ...]
    unset_env: tuple[str, ...] = ('CODEX_THREAD_ID', 'CLAUDECODE')


@dataclass(frozen=True)
class LaunchContext:
    mode: Literal['spawn', 'resume']
    values: Mapping[str, str]
    model: str | None = None


@dataclass(frozen=True)
class NativeContext:
    properties: Mapping[str, Any]
    runtime: Mapping[str, Any] = field(default_factory=dict)
    # Results of explicit, bounded probes performed by core; no adapter subprocesses.
    probes: Mapping[str, Capability] = field(default_factory=dict)


@dataclass(frozen=True)
class NativeResult:
    state: Literal['ok', 'unavailable', 'error', 'native_queued', 'pending']
    reason: str = ''
    value: Any = None

    def to_dict(self):
        return asdict(self)


class HarnessAdapter(Protocol):
    name: str
    session_mode: str
    bootstrap: str
    default_model: str | None

    def launch(self, context: LaunchContext) -> CommandSpec: ...
    def session_id(self, value: str) -> str: ...
    def validate_resume(self, properties: Mapping[str, Any], agent_dir: Path) -> None: ...
    def capabilities(self, context: NativeContext) -> dict[str, Capability]: ...
    def probes(self) -> dict[str, CommandSpec]: ...
    def control(self, operation: str, context: NativeContext, text: str) -> CommandSpec | NativeResult: ...
