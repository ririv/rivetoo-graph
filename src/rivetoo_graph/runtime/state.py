from __future__ import annotations

import threading
from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Iterable
    from pathlib import Path

    from ..model import StepId, StepLabelPath
    from ..results import GraphResultStore


def _is_same_or_descendant(step_id: StepId, ancestor: StepId) -> bool:
    return step_id.segments[: len(ancestor.segments)] == ancestor.segments


@dataclass(slots=True, init=False)
class GraphExecutionState:
    """Own the mutable facts produced by exactly one Graph run.

    调用方只能提供初始 skip 请求，不能通过构造器伪造“已执行”“已完成”或
    “实际跳过”等运行事实。runtime 在状态转换发生时记录这些事实，并拒绝
    同一步同时进入互斥终态。
    """

    _skip_requests: set[StepId]
    _matched_skip_requests: set[StepId]
    _skipped_steps: set[StepId]
    _executed_steps: list[StepId]
    _completed_steps: set[StepId]
    _entered_composites: set[StepId]
    _completed_composites: set[StepId]
    _step_label_paths: dict[StepId, StepLabelPath]
    results: GraphResultStore | None
    _component_log_paths: dict[StepId, Path]
    _runtime_condition_values: dict[StepId, bool]
    _lock: threading.Lock
    _started: bool

    def __init__(
        self,
        *,
        initial_skip_requests: Iterable[StepId] = (),
        results: GraphResultStore | None = None,
    ) -> None:
        self._skip_requests = set(initial_skip_requests)
        self._matched_skip_requests = set()
        self._skipped_steps = set()
        self._executed_steps = []
        self._completed_steps = set()
        self._entered_composites = set()
        self._completed_composites = set()
        self._step_label_paths = {}
        self.results = results
        self._component_log_paths = {}
        self._runtime_condition_values = {}
        self._lock = threading.Lock()
        self._started = False

    @property
    def skipped_steps(self) -> frozenset[StepId]:
        with self._lock:
            return frozenset(self._skipped_steps)

    @property
    def executed_steps(self) -> list[StepId]:
        with self._lock:
            return list(self._executed_steps)

    @property
    def completed_steps(self) -> frozenset[StepId]:
        with self._lock:
            return frozenset(self._completed_steps)

    @property
    def entered_composites(self) -> frozenset[StepId]:
        with self._lock:
            return frozenset(self._entered_composites)

    @property
    def completed_composites(self) -> frozenset[StepId]:
        with self._lock:
            return frozenset(self._completed_composites)

    def begin_run(self) -> None:
        """声明本状态开始服务一次执行，并拒绝后续复用。"""

        with self._lock:
            if self._started:
                raise RuntimeError("graph execution state cannot be reused")
            self._started = True

    def snapshot(self) -> GraphExecutionSnapshot:
        """在同一把锁下复制一份内部一致的只读执行快照。"""

        with self._lock:
            return GraphExecutionSnapshot(
                skipped_steps=frozenset(self._skipped_steps),
                executed_steps=tuple(self._executed_steps),
                completed_steps=frozenset(self._completed_steps),
                entered_composites=frozenset(self._entered_composites),
                completed_composites=frozenset(self._completed_composites),
                step_label_paths=dict(self._step_label_paths),
                results=None if self.results is None else self.results.copy(),
                runtime_condition_values=dict(self._runtime_condition_values),
            )

    def request_skip(self, step_id: StepId) -> None:
        """请求 runtime 跳过尚未开始的节点或组合边界。"""

        with self._lock:
            started = (
                *self._executed_steps,
                *self._entered_composites,
                *self._completed_composites,
            )
            if any(_is_same_or_descendant(item, step_id) for item in started):
                raise RuntimeError(f"cannot skip an already started step: {step_id}")
            self._skip_requests.add(step_id)

    def request_skips(self, step_ids: Iterable[StepId]) -> None:
        for step_id in step_ids:
            self.request_skip(step_id)

    def is_skip_requested(self, step_id: StepId) -> bool:
        with self._lock:
            current: StepId | None = step_id
            while current is not None:
                if current in self._skip_requests:
                    self._matched_skip_requests.add(current)
                    return True
                current = current.parent
            return False

    def skip_request_ids(self) -> frozenset[StepId]:
        """返回尚未限定到某张编译图的 skip 请求快照。"""

        with self._lock:
            return frozenset(self._skip_requests)

    def match_skip_request(self, step_id: StepId) -> None:
        """记录某张编译图已经识别该 skip 请求。"""

        with self._lock:
            if step_id not in self._skip_requests:
                raise RuntimeError(f"unknown graph skip request: {step_id}")
            self._matched_skip_requests.add(step_id)

    def require_skip_requests_resolved(self) -> None:
        """拒绝没有对应到本次执行中任何节点的 skip 请求。"""

        with self._lock:
            unresolved = self._skip_requests - self._matched_skip_requests
        if unresolved:
            rendered = ", ".join(str(step_id) for step_id in sorted(unresolved))
            raise RuntimeError(f"unknown graph skip requests: {rendered}")

    def record_skipped_step(self, step_id: StepId) -> None:
        with self._lock:
            if step_id in self._step_label_paths or step_id in self._completed_steps:
                raise RuntimeError(f"cannot skip an already executed step: {step_id}")
            self._skipped_steps.add(step_id)

    def record_step(self, step_id: StepId, label_path: StepLabelPath) -> None:
        """记录 step 已开始执行及其实际挂载路径。"""

        with self._lock:
            if step_id in self._skipped_steps:
                raise RuntimeError(f"cannot execute a skipped step: {step_id}")
            existing_path = self._step_label_paths.get(step_id)
            if existing_path is not None:
                if existing_path != label_path:
                    raise RuntimeError(
                        f"step {step_id} was recorded with inconsistent label paths"
                    )
                raise RuntimeError(f"step was executed more than once: {step_id}")
            self._step_label_paths[step_id] = label_path
            self._executed_steps.append(step_id)

    def complete_step(self, step_id: StepId) -> None:
        """记录 step 的 action 或完整子图已经成功完成。"""

        with self._lock:
            if step_id not in self._step_label_paths:
                raise RuntimeError(f"cannot complete a step that was not started: {step_id}")
            if step_id in self._completed_steps:
                raise RuntimeError(f"step was completed more than once: {step_id}")
            self._completed_steps.add(step_id)

    def enter_composite(self, step_id: StepId) -> None:
        """记录非执行组合边界已经激活。"""

        with self._lock:
            if step_id in self._skipped_steps:
                raise RuntimeError(f"cannot enter a skipped composite: {step_id}")
            if step_id in self._entered_composites:
                raise RuntimeError(f"composite was entered more than once: {step_id}")
            self._entered_composites.add(step_id)

    def complete_composite(self, step_id: StepId) -> None:
        """记录组合边界的全部直接子节点已经进入终态。"""

        with self._lock:
            if step_id not in self._entered_composites:
                raise RuntimeError(
                    f"cannot complete a composite that was not entered: {step_id}"
                )
            if step_id in self._completed_composites:
                raise RuntimeError(f"composite was completed more than once: {step_id}")
            self._completed_composites.add(step_id)

    def record_result(self, step_id: StepId, value: object | None) -> None:
        with self._lock:
            if step_id not in self._step_label_paths:
                raise RuntimeError(f"cannot record a result before step start: {step_id}")
            if self.results is not None:
                self.results.record(step_id, value)

    def record_runtime_condition(self, step_id: StepId, value: bool) -> None:
        """保存 runtime 已使用的 step 条件值，供 render 只读展示。"""

        with self._lock:
            existing = self._runtime_condition_values.get(step_id)
            if existing is not None and existing is not value:
                raise RuntimeError(
                    f"runtime condition changed during one run: {step_id}"
                )
            self._runtime_condition_values[step_id] = value

    def set_component_log_path(self, step_id: StepId, value: Path) -> None:
        with self._lock:
            self._component_log_paths[step_id] = value

    def get_component_log_path(self, step_id: StepId) -> Path | None:
        with self._lock:
            return self._component_log_paths.get(step_id)


@dataclass(frozen=True, slots=True)
class GraphExecutionSnapshot:
    """从执行状态原子复制出的渲染与观察快照。"""

    skipped_steps: frozenset[StepId]
    executed_steps: tuple[StepId, ...]
    completed_steps: frozenset[StepId]
    entered_composites: frozenset[StepId]
    completed_composites: frozenset[StepId]
    step_label_paths: dict[StepId, StepLabelPath]
    results: GraphResultStore | None
    runtime_condition_values: dict[StepId, bool]
