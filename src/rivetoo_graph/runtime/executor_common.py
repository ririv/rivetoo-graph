from __future__ import annotations

import graphlib
import inspect
import logging
import threading
from collections import deque
from typing import TYPE_CHECKING

from ..compile import CompiledStep
from ..errors import GraphError
from ..frozen import FrozenGraph, RuntimeConditionalEdge, freeze_graph
from ..model import Graph, StepId, StepLabelPath
from ..observe import GraphObservationEvent, GraphObservationPhase, GraphObserver

if TYPE_CHECKING:
    from collections.abc import Mapping

    from ..model import Step
    from ..results import GraphResultStore
    from .run import ExecutableStep, GraphRunLike
    from .state import GraphExecutionState

logger = logging.getLogger(__name__)


async def await_result(result: object) -> object:
    # LeafAction 既可能返回普通值，也可能返回 awaitable；
    # 这里把两种返回形态统一收口，供三个执行器共用。
    if inspect.isawaitable(result):
        return await result
    return result


def as_graph_result(
    value: object,
    *,
    parent_step_id: StepId,
    default_label: str,
) -> FrozenGraph | None:
    """把 action 在运行期返回的声明图冻结成动态子图。"""

    if isinstance(value, Graph):
        return freeze_graph(
            value,
            root_label=default_label,
            parent_step_id=parent_step_id,
        )
    return None


def step_thread_name(label_path: StepLabelPath) -> str:
    current = threading.current_thread().name
    if not label_path:
        return current
    label = label_path[0]
    if current.startswith("GraphWorker_"):
        return f"{current}/{label}"
    return label


def has_runtime_conditions(run: GraphRunLike) -> bool:
    for step in run.steps:
        if isinstance(step, CompiledStep):
            raise GraphError("compiled graph must use the compiled scheduler")
        when = step.when
        if when is not None:
            return True
        for dep in step.after:
            if isinstance(dep, RuntimeConditionalEdge):
                return True
    return False


def static_prerequisite_ids(step: ExecutableStep) -> set[StepId]:
    if isinstance(step, CompiledStep):
        raise GraphError("compiled graph must use the compiled scheduler")
    return {
        dep.step_id if isinstance(dep, RuntimeConditionalEdge) else dep
        for dep in step.after
    }


def make_graph_observation_results(
    state: GraphExecutionState,
) -> GraphResultStore | None:
    if state.results is None:
        return None
    return state.results


def emit_graph_observation(
    *,
    observer: GraphObserver | None,
    state: GraphExecutionState,
    graph: GraphRunLike,
    label_path: StepLabelPath,
    phase: GraphObservationPhase,
) -> None:
    if observer is None:
        return
    message = (
        graph.observe.before
        if phase == GraphObservationPhase.BEFORE
        else graph.observe.after_success
    )
    if message is None:
        return
    observer.emit(
        GraphObservationEvent(
            phase=phase,
            message=message,
            source_graph=graph.source_graph,
            graph_label=graph.label,
            graph_label_path=label_path,
            graph_step_ids=graph.observation_step_ids,
            results=make_graph_observation_results(state),
        )
    )


def prepare_ready_graph_run(
    run: GraphRunLike,
) -> tuple[
    dict[StepId, ExecutableStep],
    Mapping[Step, StepId],
    graphlib.TopologicalSorter,
    deque[StepId],
]:
    """为不含运行期条件的图建立拓扑调度器和初始 ready 队列。"""

    steps_by_id = {step.id: step for step in run.steps}
    graph_step_ids = run.observation_step_ids
    sorter = graphlib.TopologicalSorter(
        {step.id: static_prerequisite_ids(step) for step in run.steps}
    )
    sorter.prepare()
    ready: deque[StepId] = deque(sorter.get_ready())
    return steps_by_id, graph_step_ids, sorter, ready
