from __future__ import annotations

import threading
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from .model import Step, StepId, StepLabelPath

if TYPE_CHECKING:
    from collections.abc import Iterable, Mapping
    from pathlib import Path

    from .runtime import GraphExecutionState


@dataclass(frozen=True, slots=True)
class StepResult:
    value: object | None


@dataclass(frozen=True, slots=True)
class GraphScope:
    step_ids: Mapping[Step, StepId]


@dataclass(frozen=True, slots=True)
class ParentGraphStepRef:
    step: Step
    levels_up: int

    def __post_init__(self) -> None:
        if self.levels_up < 1:
            raise ValueError("parent graph ref levels_up must be >= 1")


type StepResultRef = Step | ParentGraphStepRef


@dataclass(frozen=True, slots=True)
class StepReadContext:
    # 只读 step 上下文：提供当前图作用域和结果读取能力，不暴露任何运行时副作用。
    step_id: StepId
    label_path: StepLabelPath
    graph_step_ids: Mapping[Step, StepId]
    ancestor_graph_scopes: tuple[GraphScope, ...] = ()
    results: GraphResultView | None = None

    @property
    def graph_label_path(self) -> StepLabelPath:
        return self.label_path[:-1]

    def _resolve_parent_graph_step_id(self, step: Step, levels_up: int) -> StepId:
        if levels_up > len(self.ancestor_graph_scopes):
            raise RuntimeError(
                "parent graph ref levels_up exceeds available ancestor graph scopes: "
                f"levels_up={levels_up}, available={len(self.ancestor_graph_scopes)}, "
                f"graph_label_path={self.graph_label_path}"
            )
        scope = self.ancestor_graph_scopes[levels_up - 1]
        step_id = scope.step_ids.get(step)
        if step_id is None:
            raise RuntimeError(
                f"step '{step.label}' is not known in the requested parent graph scope"
            )
        return step_id

    def _resolve_step_id(self, ref: StepResultRef) -> StepId:
        if isinstance(ref, ParentGraphStepRef):
            return self._resolve_parent_graph_step_id(ref.step, ref.levels_up)
        step_id = self.graph_step_ids.get(ref)
        if step_id is None:
            raise RuntimeError(
                f"step '{ref.label}' is not known in graph scope {self.graph_label_path}"
            )
        return step_id

    def _require_results(self) -> GraphResultView:
        if self.results is None:
            raise RuntimeError("step results are not enabled for this graph run")
        return self.results

    def result(self, ref: StepResultRef) -> StepResult | None:
        return self._require_results().get(self._resolve_step_id(ref))

    def require_result(self, ref: StepResultRef) -> StepResult:
        return self._require_results().require(self._resolve_step_id(ref))

    def value(self, ref: StepResultRef) -> object | None:
        result = self.result(ref)
        return None if result is None else result.value

    def require_value(self, ref: StepResultRef) -> object | None:
        return self.require_result(ref).value

    def has_result(self, ref: StepResultRef) -> bool:
        return self._require_results().has(self._resolve_step_id(ref))


@dataclass(frozen=True, slots=True)
class StepControl:
    # 运行控制接口：显式承载 skip 副作用，与只读上下文分离。
    step_id: StepId
    _state: GraphExecutionState

    def skip_step(self, step_id: StepId) -> None:
        self._state.request_skip(step_id)

    def skip_steps(self, step_ids: Iterable[StepId]) -> None:
        self._state.request_skips(step_ids)


@dataclass(frozen=True, slots=True)
class GraphResultView:
    """Read-only access to step result records, without copying their values."""

    _store: GraphResultStore = field(repr=False)

    def get(self, step_id: StepId) -> StepResult | None:
        return self._store.get(step_id)

    def require(self, step_id: StepId) -> StepResult:
        return self._store.require(step_id)

    def has(self, step_id: StepId) -> bool:
        return self._store.has(step_id)


@dataclass(slots=True)
class GraphResultStore:
    """以本次图挂载分配的 `StepId` 保存执行结果的线程安全存储。

    runtime worker 与 observer/render 可能并发读写结果，因此所有访问都由
    存储自身的锁保护；调用方不应通过执行状态的锁推断这里已经同步。
    """

    _results: dict[StepId, StepResult] = field(default_factory=dict)
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
            self._results[step_id] = StepResult(value=value)

    def get(self, step_id: StepId) -> StepResult | None:
        with self._lock:
            return self._results.get(step_id)

    def require(self, step_id: StepId) -> StepResult:
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
    # action 的统一运行时入口；只读数据和副作用控制显式分离。
    view: StepReadContext
    control: StepControl
    step_log_path: Path | None

    def result(self, ref: StepResultRef) -> StepResult | None:
        return self.view.result(ref)

    def require_result(self, ref: StepResultRef) -> StepResult:
        return self.view.require_result(ref)

    def value(self, ref: StepResultRef) -> object | None:
        return self.view.value(ref)

    def require_value(self, ref: StepResultRef) -> object | None:
        return self.view.require_value(ref)

    def has_result(self, ref: StepResultRef) -> bool:
        return self.view.has_result(ref)

    @property
    def step_id(self) -> StepId:
        return self.view.step_id

    @property
    def label_path(self) -> StepLabelPath:
        return self.view.label_path

    @property
    def graph_label_path(self) -> StepLabelPath:
        return self.view.graph_label_path

    @property
    def graph_step_ids(self) -> Mapping[Step, StepId]:
        return self.view.graph_step_ids

    @property
    def results(self) -> GraphResultView | None:
        return self.view.results
