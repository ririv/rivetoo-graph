from __future__ import annotations

import threading
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from .option import Some
from .runtime.control import GraphExecutionControl

if TYPE_CHECKING:
    from collections.abc import Iterable, Mapping
    from pathlib import Path

    from .model import Step, StepId, StepLabelPath
    from .runtime import GraphExecutionState


@dataclass(frozen=True, slots=True)
class GraphScope:
    step_ids: Mapping[Step, StepId]


@dataclass(frozen=True, slots=True)
class StepControl:
    # 运行控制接口：显式承载 skip 副作用，与只读上下文分离。
    step_id: StepId
    _state: GraphExecutionState
    _execution_control: GraphExecutionControl = field(
        default_factory=GraphExecutionControl, repr=False, kw_only=True
    )

    def stop_requested(self) -> bool:
        return self._execution_control.stop_requested()

    def require_running(self) -> None:
        self._execution_control.require_running()

    def skip_step(self, step_id: StepId) -> None:
        self._state.request_skip(step_id)

    def skip_steps(self, step_ids: Iterable[StepId]) -> None:
        self._state.request_skips(step_ids)


@dataclass(frozen=True, slots=True)
class GraphResultView:
    """Read-only access to step result records, without copying their values."""

    _store: GraphResultStore = field(repr=False)

    def get(self, step_id: StepId) -> Some[object] | None:
        return self._store.get(step_id)

    def require(self, step_id: StepId) -> Some[object]:
        return self._store.require(step_id)

    def has(self, step_id: StepId) -> bool:
        return self._store.has(step_id)


@dataclass(slots=True)
class GraphResultStore:
    """以本次图挂载分配的 `StepId` 保存执行结果的线程安全存储。

    runtime worker 与 observer/render 可能并发读写结果，因此所有访问都由
    存储自身的锁保护；调用方不应通过执行状态的锁推断这里已经同步。
    """

    _results: dict[StepId, Some[object]] = field(default_factory=dict)
    _lock: threading.Lock = field(
        default_factory=threading.Lock,
        init=False,
        repr=False,
    )
    _view: GraphResultView = field(init=False, repr=False)

    def __post_init__(self) -> None:
        # 每个 store 只创建一个 O(1) 视图；各步骤和观察者共享，不复制结果表。
        self._view = GraphResultView(self)

    @property
    def view(self) -> GraphResultView:
        return self._view

    def record(self, step_id: StepId, value: object | None) -> None:
        with self._lock:
            if step_id in self._results:
                raise RuntimeError(f"step result already recorded for id: {step_id}")
            self._results[step_id] = Some(value=value)

    def get(self, step_id: StepId) -> Some[object] | None:
        with self._lock:
            return self._results.get(step_id)

    def require(self, step_id: StepId) -> Some[object]:
        result = self.get(step_id)
        if result is None:
            raise RuntimeError(f"missing step result for id: {step_id}")
        return result

    def has(self, step_id: StepId) -> bool:
        with self._lock:
            return step_id in self._results

    def copy(self) -> GraphResultStore:
        with self._lock:
            copied = GraphResultStore(_results=dict(self._results))
        return copied


@dataclass(frozen=True, slots=True)
class StepRunContext:
    # action 的运行能力入口；步骤结果只通过图的 inputs 接线传参。
    step_id: StepId
    label_path: StepLabelPath
    control: StepControl
    step_log_path: Path | None
    _shared: object | None = field(default=None, repr=False, kw_only=True)

    def shared[T](self, expected: type[T]) -> T:
        """Return the explicitly injected shared object with a checked type."""

        if self._shared is None:
            raise RuntimeError("no shared state was provided for this graph run")
        if not isinstance(self._shared, expected):
            raise TypeError(
                "graph shared state type mismatch: "
                f"expected {expected.__qualname__}, "
                f"got {type(self._shared).__qualname__}"
            )
        return self._shared

    @property
    def graph_label_path(self) -> StepLabelPath:
        return self.label_path[:-1]
