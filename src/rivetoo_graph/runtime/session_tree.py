from __future__ import annotations

from collections import deque
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from enum import Enum, auto
from typing import TYPE_CHECKING, Protocol

if TYPE_CHECKING:
    from .._native import (
        NativeActionBindings,
        NativeCompiledExecutionGraph,
        NativeMixedExecutionSession,
    )


type LeafOutcome = DynamicSubgraph | None
type LeafInvocation = LeafOutcome | Awaitable[LeafOutcome]


class BatchExecutionHost(Protocol):
    def skipped_nodes(self) -> list[int]: ...
    def skip_revision(self) -> int: ...
    def resolve_conditions(
        self, requests: list[tuple[int, int]]
    ) -> list[tuple[int, bool | None]]: ...
    def record_failure(self, node: int, error: Exception) -> None: ...
    def raise_failures(self) -> None: ...
    def apply_events(self, events: list[tuple[int, int, bool | None]]) -> None: ...
    def run_leaf(self, node: int) -> LeafInvocation: ...
    def stop_requested(self) -> bool: ...
    def request_stop(self) -> None: ...
    def require_running(self) -> None: ...


@dataclass(frozen=True, slots=True)
class SessionDefinition:
    graph: NativeCompiledExecutionGraph
    host: BatchExecutionHost
    native_actions: NativeActionBindings
    native_contexts: list[tuple[int, str | None]]


@dataclass(frozen=True, slots=True)
class DynamicSubgraph:
    """Suspend a parent action until this child session completes."""

    definition: SessionDefinition
    complete: Callable[[], None]


@dataclass(frozen=True, slots=True)
class RootSession:
    pass


@dataclass(frozen=True, slots=True)
class ChildSession:
    parent: SessionRun
    node: int
    complete: Callable[[], None]


class SessionPhase(Enum):
    NEW = auto()
    RUNNING = auto()


@dataclass(eq=False, slots=True)
class SessionRun:
    definition: SessionDefinition
    native: NativeMixedExecutionSession
    owner: RootSession | ChildSession
    phase: SessionPhase = SessionPhase.NEW
    native_running: int = 0
    python_running: int = 0
    completed: list[tuple[int, bool]] = field(default_factory=list)


@dataclass(frozen=True, slots=True)
class PythonWork:
    session: SessionRun
    node: int


class SessionTree:
    """Allocate one action budget across root and runtime-created sessions."""

    def __init__(self, root: SessionDefinition, capacity: int) -> None:
        if capacity < 1:
            raise ValueError("graph action capacity must be >= 1")
        self.capacity = capacity
        self.host = root.host
        self.finished = False
        self._occupied = 0
        self._ready: deque[SessionRun] = deque()
        self._queued: set[SessionRun] = set()
        self._waiting: dict[SessionRun, None] = {}
        self._active: set[SessionRun] = set()
        self.native_runs: dict[SessionRun, None] = {}
        self._skip_revision = self.host.skip_revision()
        self._add(root, RootSession())

    def _add(
        self, definition: SessionDefinition, owner: RootSession | ChildSession
    ) -> None:
        run = SessionRun(
            definition,
            definition.graph.mixed_session(
                definition.host.skipped_nodes(),
                definition.native_actions,
                definition.native_contexts,
                self.capacity,
            ),
            owner,
        )
        self._active.add(run)
        self.wake(run)

    def wake(self, run: SessionRun) -> None:
        # native 完成可能已被别的批次排空；稍后到达的 wait 通知不再推进终态 session。
        if run not in self._active:
            return
        if run not in self._queued:
            self._waiting.pop(run, None)
            self._queued.add(run)
            self._ready.append(run)

    def complete(self, work: PythonWork, outcome: LeafOutcome) -> None:
        run = work.session
        run.python_running -= 1
        self._occupied -= 1
        if outcome is None:
            run.completed.append((work.node, True))
            self.wake(run)
        else:
            # 父节点仍在 Rust 中等待完成，但不再占用 Python worker 或动作预算。
            self._add(
                outcome.definition, ChildSession(run, work.node, outcome.complete)
            )

    def fail(self, work: PythonWork, error: Exception) -> None:
        run = work.session
        run.python_running -= 1
        self._occupied -= 1
        run.definition.host.record_failure(work.node, error)
        run.completed.append((work.node, False))
        self.wake(run)

    def poll(self) -> list[PythonWork]:
        self.host.require_running()
        if len(self._active) > 1:
            revision = self.host.skip_revision()
            if revision != self._skip_revision:
                self._skip_revision = revision
                # 只有出现新的跨作用域 skip 请求时扫描 S 个活跃 session，O(S)。
                for run in self._active:
                    self.wake(run)
        if self._waiting and self._occupied < self.capacity:
            for run in tuple(self._waiting):
                self.wake(run)
        work: list[PythonWork] = []
        # 通常只处理收到完成通知的 session；静态图始终只有一个 session。
        # 每批成本 O(Q + A + C + T)，Q 为本批需推进的 session 数，
        # 其余分别为新动作、条件请求和状态转换数，不扫描图中的所有节点。
        while self._ready:
            run = self._ready.popleft()
            self._queued.remove(run)
            available = self.capacity - self._occupied
            if available == 0 and not run.completed and not run.native_running:
                self._waiting[run] = None
                continue
            source = run.definition
            previous_native = run.native_running
            if run.phase is SessionPhase.NEW:
                batch = run.native.start(available)
                run.phase = SessionPhase.RUNNING
            else:
                batch = run.native.advance(
                    run.completed, [], source.host.skipped_nodes(), available
                )
                run.completed = []
            issued = 0
            while True:
                actions, conditions, events, finished, native_running, failures = (
                    batch.parts()
                )
                source.host.apply_events(events)
                for node, message in failures:
                    source.host.record_failure(node, RuntimeError(message))
                work.extend(PythonWork(run, node) for node in actions)
                issued += len(actions)
                self._occupied += len(actions) + native_running - run.native_running
                run.python_running += len(actions)
                run.native_running = native_running
                if not conditions:
                    break
                resolutions = source.host.resolve_conditions(conditions)
                self.host.require_running()
                batch = run.native.advance(
                    [],
                    resolutions,
                    source.host.skipped_nodes(),
                    self.capacity - self._occupied,
                )
            if run.native_running:
                self.native_runs[run] = None
            else:
                self.native_runs.pop(run, None)
            if finished:
                self._finish(run)
            elif (
                self._occupied == self.capacity
                and not issued
                and not run.python_running
                and not run.native_running
            ):
                self._waiting[run] = None
            if previous_native > run.native_running and self._waiting:
                for waiting in tuple(self._waiting):
                    self.wake(waiting)
        return work

    def _finish(self, run: SessionRun) -> None:
        self._active.remove(run)
        self._waiting.pop(run, None)
        self.native_runs.pop(run, None)
        match run.owner:
            case RootSession():
                self.finished = True
            case ChildSession(parent=parent, node=node, complete=complete):
                try:
                    run.definition.host.raise_failures()
                except Exception as error:
                    parent.definition.host.record_failure(node, error)
                    parent.completed.append((node, False))
                else:
                    complete()
                    parent.completed.append((node, True))
                self.wake(parent)
