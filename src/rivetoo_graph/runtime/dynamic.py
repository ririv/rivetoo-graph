from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import TYPE_CHECKING

from .._native import NativeCompiledExecutionGraph
from ..errors import GraphError
from ..frozen import FrozenRuntimeCondition, FrozenStep, RuntimeConditionalEdge
from ..results import GraphScope, StepReadContext

if TYPE_CHECKING:
    from collections.abc import Callable, Mapping

    from ..model import Step, StepId, StepLabelPath
    from .control import GraphExecutionControl
    from .run import DynamicGraphRun
    from .state import GraphExecutionState

logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class DynamicGraphScheduling:
    """一层 FrozenGraph 对应的 Rust 调度结构与 Python 宿主侧表。"""

    run: DynamicGraphRun
    graph_label_path: StepLabelPath
    ancestor_graph_scopes: tuple[GraphScope, ...]
    native_graph: NativeCompiledExecutionGraph
    steps: tuple[FrozenStep, ...]
    conditions: tuple[FrozenRuntimeCondition, ...]
    graph_step_ids: Mapping[Step, StepId]

    @classmethod
    def materialize(
        cls,
        run: DynamicGraphRun,
        *,
        graph_label_path: StepLabelPath,
        ancestor_graph_scopes: tuple[GraphScope, ...],
    ) -> DynamicGraphScheduling:
        steps = run.steps
        node_indexes = {step.id: index for index, step in enumerate(steps)}
        conditions: list[FrozenRuntimeCondition] = []
        condition_reads: list[list[int]] = []

        def register_condition(condition: FrozenRuntimeCondition) -> int:
            index = len(conditions)
            conditions.append(condition)
            condition_reads.append(
                [node_indexes[step_id] for step_id in condition.reads]
            )
            return index

        prerequisites: list[list[tuple[int, int | None]]] = []
        when_conditions: list[int | None] = []
        for step in steps:
            prerequisites.append(
                [
                    (
                        node_indexes[
                            prerequisite.step_id
                            if isinstance(prerequisite, RuntimeConditionalEdge)
                            else prerequisite
                        ],
                        register_condition(prerequisite.condition)
                        if isinstance(prerequisite, RuntimeConditionalEdge)
                        else None,
                    )
                    for prerequisite in step.after
                ]
            )
            when_conditions.append(
                None if step.when is None else register_condition(step.when)
            )

        return cls(
            run=run,
            graph_label_path=graph_label_path,
            ancestor_graph_scopes=ancestor_graph_scopes,
            native_graph=NativeCompiledExecutionGraph(
                [0] * len(steps),
                [],
                [None],
                prerequisites,
                when_conditions,
                condition_reads,
                [],
            ),
            steps=steps,
            conditions=tuple(conditions),
            graph_step_ids=run.observation_step_ids,
        )

    def step(self, index: int) -> FrozenStep:
        return self.steps[index]

    def condition(self, index: int) -> FrozenRuntimeCondition:
        return self.conditions[index]

    def label_path(self, step: FrozenStep) -> StepLabelPath:
        return self.run.resolve_step_label_path(
            step,
            graph_label_path=self.graph_label_path,
        )


class NativeDynamicSchedulerHost:
    """把一层 dynamic Rust 调度回调接到 Python action 与结果侧表。"""

    def __init__(
        self,
        scheduling: DynamicGraphScheduling,
        state: GraphExecutionState,
        *,
        run_leaf: Callable[[FrozenStep], None],
        execution_control: GraphExecutionControl | None = None,
    ) -> None:
        self.scheduling = scheduling
        self.state = state
        self.run_leaf_callback = run_leaf
        self.execution_control = execution_control

    def is_skipped(self, node: int) -> bool:
        return self.state.is_skipped(self.scheduling.step(node).id)

    def evaluate_condition(self, node: int, condition: int) -> bool:
        step = self.scheduling.step(node)
        value = self.scheduling.condition(condition).when(
            StepReadContext(
                step_id=step.id,
                label_path=self.scheduling.label_path(step),
                graph_step_ids=self.scheduling.graph_step_ids,
                ancestor_graph_scopes=self.scheduling.ancestor_graph_scopes,
                results=self.state.results,
            )
        )
        if not isinstance(value, bool):
            raise GraphError(f"dynamic runtime condition must return bool: {step.id}")
        return value

    def record_condition(self, node: int, value: bool) -> None:
        self.state.record_runtime_condition(self.scheduling.step(node).id, value)

    def record_condition_skip(self, node: int) -> None:
        step = self.scheduling.step(node)
        log_disabled_step_condition(step, label_path=self.scheduling.label_path(step))
        self.state.skip_step(step.id)

    def enter_composite(self, node: int) -> None:
        raise GraphError(
            "dynamic shallow scheduler emitted composite lifecycle: "
            f"{self.scheduling.step(node).id}"
        )

    def complete_composite(self, node: int) -> None:
        raise GraphError(
            "dynamic shallow scheduler completed composite lifecycle: "
            f"{self.scheduling.step(node).id}"
        )

    def run_leaf(self, node: int) -> None:
        self.run_leaf_callback(self.scheduling.step(node))

    def stop_requested(self) -> bool:
        return (
            False
            if self.execution_control is None
            else self.execution_control.stop_requested()
        )

    def request_stop(self) -> None:
        if self.execution_control is not None:
            self.execution_control.request_stop()

    def require_running(self) -> None:
        if self.execution_control is not None:
            self.execution_control.require_running()


def log_disabled_step_condition(
    step: FrozenStep,
    *,
    label_path: StepLabelPath,
) -> None:
    condition = step.when
    if not isinstance(condition, FrozenRuntimeCondition):
        raise GraphError(
            f"disabled dynamic step must carry runtime condition: {step.id}"
        )
    reads = ",".join(str(step_id.segments) for step_id in condition.reads) or "-"
    logger.debug(
        "Condition: skip %s (reads=%s; value=false)",
        ".".join(label_path),
        reads,
    )
