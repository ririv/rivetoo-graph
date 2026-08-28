from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from ..compile import CompiledComposite, CompiledStep, GraphCompilation
from ..errors import GraphError
from ..observe import GraphObservationEvent, GraphObservationPhase
from ..results import StepReadContext

if TYPE_CHECKING:
    from collections.abc import Callable

    from ..model import StepId
    from ..observe import GraphObserver
    from .control import GraphExecutionControl
    from .state import GraphExecutionState

logger = logging.getLogger(__name__)


class CompiledExecutionHost:
    """把 Rust execution session 的批量事件接回 Python 运行设施。"""

    def __init__(
        self,
        compilation: GraphCompilation,
        state: GraphExecutionState,
        *,
        observer: GraphObserver | None,
        run_leaf: Callable[[CompiledStep], None],
        execution_control: GraphExecutionControl | None = None,
        record_composites_as_steps: bool = False,
    ) -> None:
        self.compilation = compilation
        self.state = state
        self.observer = observer
        self.run_leaf_callback = run_leaf
        self.execution_control = execution_control
        self.record_composites_as_steps = record_composites_as_steps
        # 初始化一次遍历 V 个 native 节点，时间/空间均为 O(V)；之后每次
        # advance 只处理尚未转发的新增请求 ΔS，不再扫描整张图。
        self._native_index_by_step_id = {
            node.id: index for index, node in enumerate(self.compilation._native_nodes)
        }
        self._forwarded_skip_step_ids: set[StepId] = set()

    def skipped_nodes(self) -> list[int]:
        nodes: list[int] = []
        for step_id in self.state.skip_request_ids() - self._forwarded_skip_step_ids:
            index = self._native_index_by_step_id.get(step_id)
            if index is None:
                continue
            self._forwarded_skip_step_ids.add(step_id)
            self.state.match_skip_request(step_id)
            nodes.append(index)
        return nodes

    def resolve_conditions(
        self,
        requests: list[tuple[int, int]],
    ) -> list[tuple[int, bool]]:
        return [
            (condition, self.evaluate_condition(node, condition))
            for node, condition in requests
        ]

    def apply_events(self, events: list[tuple[int, int, bool | None]]) -> None:
        for kind, node, value in events:
            match kind:
                case 0:
                    if value is None:
                        raise GraphError("condition event is missing its value")
                    self.record_condition(node, value)
                case 1:
                    self.record_condition_skip(node)
                case 2:
                    self.enter_composite(node)
                case 3:
                    self.complete_composite(node)
                case 4:
                    step_id = self.compilation._native_node(node).id
                    self.state.record_skipped_step(step_id)
                case _:
                    raise GraphError(f"unknown native execution event: {kind}")

    def evaluate_condition(self, node: int, condition: int) -> bool:
        compiled_node = self.compilation._native_node(node)
        compiled_condition = self.compilation._native_condition(condition)
        context = compiled_node.context
        value = compiled_condition.when(
            StepReadContext(
                step_id=compiled_node.id,
                label_path=context.label_path,
                graph_step_ids=context.graph_step_ids,
                ancestor_graph_scopes=context.ancestor_graph_scopes,
                results=self.state.results,
            )
        )
        if not isinstance(value, bool):
            raise GraphError(
                f"compiled runtime condition must return bool: {compiled_node.id}"
            )
        return value

    def record_condition(self, node: int, value: bool) -> None:
        self.state.record_runtime_condition(
            self.compilation._native_node(node).id,
            value,
        )

    def record_condition_skip(self, node: int) -> None:
        compiled_node = self.compilation._native_node(node)
        condition = compiled_node.when
        if condition is None:
            raise GraphError(
                f"disabled compiled step has no runtime condition: {compiled_node.id}"
            )
        reads = ",".join(str(read.id.segments) for read in condition.reads) or "-"
        logger.debug(
            "Condition: skip %s (reads=%s; value=false)",
            ".".join(compiled_node.context.label_path),
            reads,
        )

    def enter_composite(self, node: int) -> None:
        composite = self._require_composite(node)
        if self.record_composites_as_steps:
            self.state.record_step(composite.id, composite.label_path)
        self.state.enter_composite(composite.id)
        _emit_composite_observation(
            composite,
            state=self.state,
            observer=self.observer,
            phase=GraphObservationPhase.BEFORE,
        )

    def complete_composite(self, node: int) -> None:
        composite = self._require_composite(node)
        if self.record_composites_as_steps:
            self.state.complete_step(composite.id)
        self.state.complete_composite(composite.id)
        _emit_composite_observation(
            composite,
            state=self.state,
            observer=self.observer,
            phase=GraphObservationPhase.AFTER_SUCCESS,
        )

    def run_leaf(self, node: int) -> None:
        compiled_node = self.compilation._native_node(node)
        if not isinstance(compiled_node, CompiledStep):
            raise GraphError(
                f"native scheduler submitted non-leaf node: {compiled_node.id}"
            )
        self.run_leaf_callback(compiled_node)

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

    def _require_composite(self, node: int) -> CompiledComposite:
        compiled_node = self.compilation._native_node(node)
        if not isinstance(compiled_node, CompiledComposite):
            raise GraphError(
                "native scheduler emitted composite lifecycle for leaf: "
                f"{compiled_node.id}"
            )
        return compiled_node


def _emit_composite_observation(
    composite: CompiledComposite,
    *,
    state: GraphExecutionState,
    observer: GraphObserver | None,
    phase: GraphObservationPhase,
) -> None:
    if observer is None:
        return
    message = (
        composite.observation.before
        if phase is GraphObservationPhase.BEFORE
        else composite.observation.after_success
    )
    if message is None:
        return
    observer.emit(
        GraphObservationEvent(
            phase=phase,
            message=message,
            source_graph=composite.source_graph,
            graph_label=composite.graph_label,
            graph_label_path=composite.label_path,
            graph_step_ids=composite.graph_step_ids,
            results=None if state.results is None else state.results,
        )
    )
