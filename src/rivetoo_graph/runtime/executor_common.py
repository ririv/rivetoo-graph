from __future__ import annotations

import inspect
import logging
import threading
from typing import TYPE_CHECKING

from ..frozen import FrozenGraph, freeze_graph
from ..model import Graph, StepId, StepLabelPath
from ..observe import GraphObservationEvent, GraphObservationPhase, GraphObserver

if TYPE_CHECKING:
    from ..results import GraphResultStore
    from .run import GraphRunLike
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
