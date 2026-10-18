from __future__ import annotations

import asyncio
import inspect
import logging
import threading
from typing import TYPE_CHECKING

from ..model import Graph, StepLabelPath
from ..observe import GraphObservationEvent, GraphObservationPhase, GraphObserver

if TYPE_CHECKING:
    from collections.abc import Awaitable

    from ..model import LeafAction
    from ..option import Option
    from ..results import GraphResultView, StepRunContext
    from .action_log import GraphActionLog
    from .run import CompiledGraphRun
    from .state import GraphExecutionState

logger = logging.getLogger(__name__)


def resolve_action_result(result: object) -> object:
    """在线程/串行执行器中完成 action 返回值，普通值不启动事件循环。"""

    # 按实际返回值分发，而不是按函数声明；同步函数也可能返回 awaitable。
    # 每个同步 action 只增加 O(1) 类型检查，不再创建/关闭一个事件循环。
    if inspect.isawaitable(result):
        return asyncio.run(_await_result(result))
    return result


async def _await_result(result: Awaitable[object]) -> object:
    # Python 3.13 的 asyncio.run 接受 coroutine；包装后也能支持自定义 awaitable。
    return await result


def invoke_sync_action(
    action: LeafAction,
    ctx: StepRunContext,
    action_log: GraphActionLog,
    inputs: tuple[Option[object], ...] = (),
) -> object:
    if ctx.step_log_path is None:
        return resolve_action_result(action.invoke(ctx, inputs))
    with action_log.scope(ctx.step_log_path):
        return resolve_action_result(action.invoke(ctx, inputs))


def as_graph_result(
    value: object,
) -> Graph | None:
    """识别 action 在运行期返回的声明图。"""

    if isinstance(value, Graph):
        return value
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
) -> GraphResultView | None:
    if state.results is None:
        return None
    return state.results


def emit_graph_observation(
    *,
    observer: GraphObserver | None,
    state: GraphExecutionState,
    graph: CompiledGraphRun,
    label_path: StepLabelPath,
    phase: GraphObservationPhase,
) -> None:
    if observer is None:
        return
    message = (
        graph.observation.before
        if phase == GraphObservationPhase.BEFORE
        else graph.observation.after_success
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
