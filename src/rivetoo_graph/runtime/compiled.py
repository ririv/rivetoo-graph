from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from ..compile import (
    CompiledComposite,
    CompiledNativeStep,
    CompiledStep,
    GraphCompilation,
)
from ..errors import GraphError
from ..model import Graph
from ..observe import GraphObservationEvent, GraphObservationPhase
from ..option import Nothing
from .control import GraphExecutionStopped

if TYPE_CHECKING:
    from collections.abc import Callable

    from ..model import StepId
    from ..observe import GraphObserver
    from .control import GraphExecutionControl
    from .session_tree import LeafInvocation
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
        run_leaf: Callable[[CompiledStep], LeafInvocation],
        execution_control: GraphExecutionControl | None = None,
        record_composites_as_steps: bool = False,
    ) -> None:
        self.compilation = compilation
        self.state = state
        # inputs 是显式的数据依赖；执行生产者前即开启记录，无需调用方另配 store。
        # 最多检查 C 个已编译条件，O(C) 时间、O(1) 额外空间，不扫描全部步骤。
        if any(condition.inputs for condition in compilation._native_conditions):
            state._enable_results()
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
        self._failures: list[Exception] = []
        self.state._register_event_paths(
            (node.id, node.context.label_path) for node in compilation._native_nodes
        )

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

    def skip_revision(self) -> int:
        return self.state.skip_revision

    def resolve_conditions(
        self,
        requests: list[tuple[int, int]],
    ) -> list[tuple[int, bool | None]]:
        resolutions: list[tuple[int, bool | None]] = []
        for node, condition in requests:
            try:
                value = self.evaluate_condition(node, condition)
            except GraphExecutionStopped:
                raise
            except Exception as error:
                self.record_failure(node, error)
                resolutions.append((condition, None))
            else:
                resolutions.append((condition, value))
        return resolutions

    def record_failure(self, node: int, error: Exception) -> None:
        path = "/".join(self.compilation._native_node(node).context.label_path)
        error.add_note(f"Graph node: {path}")
        logger.error("Graph node failed: %s: %s", path, error)
        self._failures.append(error)

    def raise_failures(self) -> None:
        if len(self._failures) == 1:
            raise self._failures[0]
        if self._failures:
            raise ExceptionGroup("Graph execution failed", self._failures)

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
                case 5:
                    step = self._require_native_step(node)
                    self.state.record_step(step.id, step.context.label_path)
                case 6:
                    step = self._require_native_step(node)
                    self.state.record_result(step.id, None)
                    self.state.complete_step(step.id)
                case 7:
                    self.state.record_failed_step(
                        self.compilation._native_node(node).id
                    )
                case 8:
                    self.state.record_blocked_step(
                        self.compilation._native_node(node).id
                    )
                case _:
                    raise GraphError(f"unknown native execution event: {kind}")

    def evaluate_condition(self, node: int, condition: int) -> bool:
        compiled_node = self.compilation._native_node(node)
        compiled_condition = self.compilation._native_condition(condition)
        # 编译引用已经绑定到具体挂载；每个输入只查询一次，不创建条件上下文。
        results = self.state.results
        if compiled_condition.inputs and results is None:
            raise GraphError("runtime condition requires an initialized result store")
        inputs = (
            ()
            if results is None
            else tuple(results.get(ref.id) for ref in compiled_condition.inputs)
        )
        value = compiled_condition.when(
            *(Nothing() if result is None else result for result in inputs)
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
        condition = compiled_node.condition
        if condition is None:
            raise GraphError(
                f"disabled compiled step has no runtime condition: {compiled_node.id}"
            )
        inputs = ",".join(str(ref.id.segments) for ref in condition.inputs) or "-"
        logger.debug(
            "Condition: skip %s (inputs=%s; value=false)",
            ".".join(compiled_node.context.label_path),
            inputs,
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

    def run_leaf(self, node: int) -> LeafInvocation:
        compiled_node = self.compilation._native_node(node)
        if not isinstance(compiled_node, CompiledStep):
            raise GraphError(
                f"native scheduler submitted non-leaf node: {compiled_node.id}"
            )
        return self.run_leaf_callback(compiled_node)

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

    def _require_native_step(self, node: int) -> CompiledNativeStep:
        compiled_node = self.compilation._native_node(node)
        if not isinstance(compiled_node, CompiledNativeStep):
            raise GraphError(
                f"native action lifecycle references non-native node: {compiled_node.id}"
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
    if not isinstance(composite.source_graph, Graph):
        raise GraphError("native graph must not declare Python observation messages")
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
