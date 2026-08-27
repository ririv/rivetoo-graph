from __future__ import annotations

import threading
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Iterable
    from pathlib import Path

    from ..model import StepId, StepLabelPath
    from ..results import GraphResultStore


@dataclass(slots=True)
class GraphExecutionState:
    """单次 Graph run 共享的线程安全执行状态。

    `executed_steps` 是按开始顺序记录的执行轨迹，不代表 action 已完成；
    `completed_steps` 才是成功完成事实。二者必须分开，否则关闭结果存储时，
    render 无法区分仍在运行和已经结束的 step。

    状态只允许绑定一次 run，避免旧 skip、结果或条件值泄漏到下一次执行。
    """

    skipped_steps: set[StepId] = field(default_factory=set)
    executed_steps: list[StepId] = field(default_factory=list)
    completed_steps: set[StepId] = field(default_factory=set)
    entered_composites: set[StepId] = field(default_factory=set)
    completed_composites: set[StepId] = field(default_factory=set)
    step_label_paths: dict[StepId, StepLabelPath] = field(default_factory=dict)
    results: GraphResultStore | None = None
    component_log_paths: dict[StepId, Path] = field(default_factory=dict)
    runtime_condition_values: dict[StepId, bool] = field(default_factory=dict)
    # Graph 执行器可能在线程 worker 中并发运行 step；这把锁统一保护
    # 执行状态的变更，包括 skip 标记、执行轨迹和可选的结果写入。
    _lock: threading.Lock = field(
        default_factory=threading.Lock,
        init=False,
        repr=False,
    )
    _started: bool = field(default=False, init=False, repr=False)

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
                skipped_steps=frozenset(self.skipped_steps),
                executed_steps=tuple(self.executed_steps),
                completed_steps=frozenset(self.completed_steps),
                entered_composites=frozenset(self.entered_composites),
                completed_composites=frozenset(self.completed_composites),
                step_label_paths=dict(self.step_label_paths),
                results=None if self.results is None else self.results.copy(),
                runtime_condition_values=dict(self.runtime_condition_values),
            )

    def skip_step(self, step_id: StepId) -> None:
        with self._lock:
            self.skipped_steps.add(step_id)

    def skip_steps(self, step_ids: Iterable[StepId]) -> None:
        with self._lock:
            self.skipped_steps.update(step_ids)

    def is_skipped(self, step_id: StepId) -> bool:
        with self._lock:
            current: StepId | None = step_id
            while current is not None:
                if current in self.skipped_steps:
                    return True
                current = current.parent
            return False

    def record_step(self, step_id: StepId, label_path: StepLabelPath) -> None:
        """记录 step 已开始执行及其实际挂载路径。"""

        with self._lock:
            existing_path = self.step_label_paths.get(step_id)
            if existing_path is not None and existing_path != label_path:
                raise RuntimeError(
                    f"step {step_id} was recorded with inconsistent label paths"
                )
            self.step_label_paths[step_id] = label_path
            self.executed_steps.append(step_id)

    def complete_step(self, step_id: StepId) -> None:
        """记录 step 的 action 或完整子图已经成功完成。"""

        with self._lock:
            self.completed_steps.add(step_id)

    def enter_composite(self, step_id: StepId) -> None:
        """记录非执行组合边界已经激活。"""

        with self._lock:
            self.entered_composites.add(step_id)

    def complete_composite(self, step_id: StepId) -> None:
        """记录组合边界的全部直接子节点已经进入终态。"""

        with self._lock:
            self.completed_composites.add(step_id)

    def record_result(self, step_id: StepId, value: object | None) -> None:
        with self._lock:
            if self.results is not None:
                self.results.record(step_id, value)

    def record_runtime_condition(self, step_id: StepId, value: bool) -> None:
        """保存 runtime 已使用的 step 条件值，供 render 只读展示。"""

        with self._lock:
            self.runtime_condition_values[step_id] = value

    def set_component_log_path(self, step_id: StepId, value: Path) -> None:
        with self._lock:
            self.component_log_paths[step_id] = value

    def get_component_log_path(self, step_id: StepId) -> Path | None:
        with self._lock:
            return self.component_log_paths.get(step_id)


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
