from __future__ import annotations

import logging
from collections import defaultdict, deque
from enum import StrEnum
from typing import TYPE_CHECKING

from ..compile import (
    CompiledComposite,
    CompiledConditionalDependency,
    CompiledNode,
    CompiledNodeRef,
    CompiledPrerequisite,
    CompiledRuntimeCondition,
    CompiledStep,
    GraphCompilation,
    compiled_node_id,
    compiled_prerequisite_node,
)
from ..errors import GraphError
from ..observe import GraphObservationEvent, GraphObservationPhase
from ..results import StepReadContext

if TYPE_CHECKING:
    from ..model import StepId
    from ..observe import GraphObserver
    from .state import GraphExecutionState

logger = logging.getLogger(__name__)


class _CompiledNodeState(StrEnum):
    """编译节点在单次调度中的互斥生命周期状态。"""

    PENDING = "pending"
    ACTIVE = "active"
    READY = "ready"
    RUNNING = "running"
    FINISHED = "finished"
    SKIPPED = "skipped"


_TERMINAL_STATES = frozenset({_CompiledNodeState.FINISHED, _CompiledNodeState.SKIPPED})


class CompiledScheduler:
    """调度扁平叶子，并在协调线程内维护组合生命周期。

    composite 从不进入 ready leaf 队列。它满足依赖后只会激活直接子节点，
    子节点全部终态后再完成自身。反向依赖索引保证每次状态变化只重新检查
    可能受影响的节点，而不是反复扫描整张编译图。
    """

    def __init__(
        self,
        compilation: GraphCompilation,
        state: GraphExecutionState,
        *,
        observer: GraphObserver | None,
    ) -> None:
        self.compilation = compilation
        self.state = state
        self.observer = observer
        self._nodes: dict[StepId, CompiledNode] = {
            node.id: node for node in (*compilation.steps, *compilation.composites)
        }
        self._states = dict.fromkeys(
            self._nodes,
            _CompiledNodeState.PENDING,
        )
        self._ready: deque[CompiledStep] = deque()
        self._evaluation_queue: deque[CompiledNodeRef] = deque()
        self._queued_ids: set[StepId] = set()
        # 同一节点的 when 使用 -1，条件 prerequisite 使用其 after 下标。
        # 条件一旦求值便固定，避免多个依赖完成通知导致重复调用 predicate。
        self._condition_values: dict[tuple[StepId, int], bool] = {}
        # 节点终态时只唤醒直接受影响者，避免每次状态变化扫描整张图。
        self._dependents: dict[StepId, set[CompiledNodeRef]] = defaultdict(set)
        for node in self._nodes.values():
            for prerequisite in node.after:
                self._dependents[compiled_prerequisite_node(prerequisite).id].add(
                    node.ref
                )
                if isinstance(prerequisite, CompiledConditionalDependency):
                    for read in prerequisite.condition.reads:
                        self._dependents[read.id].add(node.ref)
            if node.when is not None:
                for read in node.when.reads:
                    self._dependents[read.id].add(node.ref)

    def start(self) -> None:
        """激活根级节点的首次求值，不直接执行任何 leaf action。"""

        for root in self.compilation.roots:
            self._queue_evaluation(root)
        self._drain_evaluations()

    @property
    def is_finished(self) -> bool:
        return all(self._is_terminal(root) for root in self.compilation.roots)

    def take_ready_step(self) -> CompiledStep | None:
        """取出一个叶子并锁定为运行中；被并发 skip 的叶子会就地终结。"""

        while self._ready:
            step = self._ready.popleft()
            if self._states[step.id] is not _CompiledNodeState.READY:
                continue
            if self.state.is_skipped(step.id):
                self._skip_node(step, record_path=False)
                self._drain_evaluations()
                continue
            self._states[step.id] = _CompiledNodeState.RUNNING
            return step
        return None

    def complete_step(self, step: CompiledStep) -> None:
        """接收执行后端的成功通知，并推进受影响的依赖与 owner。"""

        if self._states[step.id] is not _CompiledNodeState.RUNNING:
            raise GraphError(f"compiled step is not running: {step.id}")
        self._states[step.id] = _CompiledNodeState.FINISHED
        self._notify_terminal(step)
        self._drain_evaluations()

    def require_finished(self) -> None:
        """断言调度已收敛；未收敛表示编译或调度状态存在错误。"""

        if self.is_finished:
            return
        unresolved = sorted(
            node_id
            for node_id, node_state in self._states.items()
            if node_state not in _TERMINAL_STATES
        )
        raise GraphError(
            "compiled graph has unresolved nodes: "
            + ", ".join(str(step_id) for step_id in unresolved)
        )

    def _queue_evaluation(self, ref: CompiledNodeRef) -> None:
        if ref.id in self._queued_ids:
            return
        if self._states[ref.id] is not _CompiledNodeState.PENDING:
            return
        self._queued_ids.add(ref.id)
        self._evaluation_queue.append(ref)

    def _drain_evaluations(self) -> None:
        while self._evaluation_queue:
            ref = self._evaluation_queue.popleft()
            self._queued_ids.remove(ref.id)
            if self._states[ref.id] is not _CompiledNodeState.PENDING:
                continue
            self._evaluate_node(self._nodes[ref.id])

    def _evaluate_node(self, node: CompiledNode) -> None:
        if node.owner is not None:
            owner_state = self._states[node.owner.id]
            if owner_state is not _CompiledNodeState.ACTIVE:
                return

        explicitly_skipped = self.state.is_skipped(node.id)
        if node.when is not None and not explicitly_skipped:
            condition_value = self._condition_value(
                node,
                node.when,
                key_index=-1,
            )
            if condition_value is None:
                return
            self.state.record_runtime_condition(
                node.id,
                condition_value,
            )
            if not condition_value:
                logger.debug(
                    "Condition: skip %s (value=false)",
                    ".".join(node.context.label_path),
                )
                self._skip_node(node, record_path=True)
                return

        for index, prerequisite in enumerate(node.after):
            prerequisite_state = self._prerequisite_satisfied(
                node,
                prerequisite,
                prerequisite_index=index,
            )
            if prerequisite_state is None or not prerequisite_state:
                return

        if explicitly_skipped:
            self._skip_node(node, record_path=False)
            return
        if isinstance(node, CompiledStep):
            self._states[node.id] = _CompiledNodeState.READY
            self._ready.append(node)
            return
        self._activate_composite(node)

    def _prerequisite_satisfied(
        self,
        node: CompiledNode,
        prerequisite: CompiledPrerequisite,
        *,
        prerequisite_index: int,
    ) -> bool | None:
        if not isinstance(prerequisite, CompiledConditionalDependency):
            return self._is_terminal(prerequisite)
        active = self._condition_value(
            node,
            prerequisite.condition,
            key_index=prerequisite_index,
        )
        if active is None:
            return None
        if not active:
            return True
        return self._is_terminal(prerequisite.node)

    def _condition_value(
        self,
        node: CompiledNode,
        condition: CompiledRuntimeCondition,
        *,
        key_index: int,
    ) -> bool | None:
        if any(not self._is_terminal(read) for read in condition.reads):
            return None
        key = (node.id, key_index)
        cached = self._condition_values.get(key)
        if cached is not None:
            return cached
        context = node.context
        value = condition.when(
            StepReadContext(
                step_id=node.id,
                label_path=context.label_path,
                graph_step_ids=context.graph_step_ids,
                ancestor_graph_scopes=context.ancestor_graph_scopes,
                results=self.state.results,
            )
        )
        if not isinstance(value, bool):
            raise GraphError(f"compiled runtime condition must return bool: {node.id}")
        self._condition_values[key] = value
        return value

    def _activate_composite(self, composite: CompiledComposite) -> None:
        self._states[composite.id] = _CompiledNodeState.ACTIVE
        self.state.enter_composite(composite.id)
        self._emit_composite_observation(
            composite,
            phase=GraphObservationPhase.BEFORE,
        )
        if not composite.children:
            self._complete_composite(composite)
            return
        for child in composite.children:
            self._queue_evaluation(child)

    def _complete_composite(self, composite: CompiledComposite) -> None:
        if self._states[composite.id] is not _CompiledNodeState.ACTIVE:
            raise GraphError(f"compiled composite is not active: {composite.id}")
        self.state.complete_composite(composite.id)
        self._emit_composite_observation(
            composite,
            phase=GraphObservationPhase.AFTER_SUCCESS,
        )
        self._states[composite.id] = _CompiledNodeState.FINISHED
        self._notify_terminal(composite)

    def _skip_node(self, node: CompiledNode, *, record_path: bool) -> None:
        if record_path:
            self.state.skip_step(node.id)
        if isinstance(node, CompiledComposite):
            self._mark_descendants_skipped(node)
        self._states[node.id] = _CompiledNodeState.SKIPPED
        self._notify_terminal(node)

    def _mark_descendants_skipped(self, composite: CompiledComposite) -> None:
        for child_ref in composite.children:
            child = self._nodes[child_ref.id]
            if isinstance(child, CompiledComposite):
                self._mark_descendants_skipped(child)
            self._states[child.id] = _CompiledNodeState.SKIPPED

    def _notify_terminal(self, node: CompiledNode) -> None:
        for dependent in self._dependents.get(node.id, ()):
            self._queue_evaluation(dependent)
        owner_ref = node.owner
        if owner_ref is None:
            return
        owner = self._nodes[owner_ref.id]
        if not isinstance(owner, CompiledComposite):
            raise GraphError(f"compiled node owner is not a composite: {node.id}")
        if self._states[owner.id] is _CompiledNodeState.ACTIVE and all(
            self._is_terminal(child) for child in owner.children
        ):
            self._complete_composite(owner)

    def _is_terminal(self, ref: CompiledNodeRef) -> bool:
        return self._states[compiled_node_id(ref)] in _TERMINAL_STATES

    def _emit_composite_observation(
        self,
        composite: CompiledComposite,
        *,
        phase: GraphObservationPhase,
    ) -> None:
        if self.observer is None:
            return
        message = (
            composite.observe.before
            if phase is GraphObservationPhase.BEFORE
            else composite.observe.after_success
        )
        if message is None:
            return
        self.observer.emit(
            GraphObservationEvent(
                phase=phase,
                message=message,
                source_graph=composite.source_graph,
                graph_label=composite.graph_label,
                graph_label_path=composite.label_path,
                graph_step_ids=composite.graph_step_ids,
                results=None if self.state.results is None else self.state.results,
            )
        )
