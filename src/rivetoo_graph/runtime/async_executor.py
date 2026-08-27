from __future__ import annotations

import asyncio
import inspect
from typing import TYPE_CHECKING, cast

from ..compile import GraphCompilation
from ..errors import GraphError
from ..frozen import FrozenGraph, FrozenStep
from ..observe import GraphObservationPhase, GraphObserver
from .action_log import (
    NO_GRAPH_ACTION_LOG,
    GraphActionLog,
)
from .compiled import NativeCompiledSchedulerHost
from .dynamic import DynamicGraphScheduling, NativeDynamicSchedulerHost
from .executor_common import (
    as_graph_result,
    emit_graph_observation,
)
from .run import (
    CompiledGraphRun,
    DynamicGraphRun,
    ExecutableGraph,
    ExecutableStep,
    GraphRunLike,
    RuntimeStep,
)
from .state import GraphExecutionState

if TYPE_CHECKING:
    from collections.abc import Awaitable, Mapping

    from ..compile import CompiledStep
    from ..model import LeafAction, Step, StepId, StepLabelPath
    from ..results import GraphScope, StepRunContext
    from .control import GraphExecutionControl


async def _invoke_async_step_action(
    action: LeafAction,
    run_ctx: StepRunContext,
    action_log: GraphActionLog,
) -> object:
    """在 async runtime 中执行 action，同时避免同步函数阻塞事件循环。

    coroutine function 直接在当前 event loop 中运行；普通函数通过
    `asyncio.to_thread()` 执行。普通函数仍可能返回 awaitable，因此返回值
    还需在 event loop 中完成最终等待。`ContextVar` 日志上下文会随
    `to_thread()` 自动传播。
    """

    async def _invoke() -> object:
        if inspect.iscoroutinefunction(action.fn):
            result = action.invoke(run_ctx)
        else:
            result = await asyncio.to_thread(action.invoke, run_ctx)
        if inspect.isawaitable(result):
            return await cast("Awaitable[object]", result)
        return result

    if run_ctx.component_log_path is None:
        return await _invoke()
    with action_log.scope(run_ctx.component_log_path):
        return await _invoke()


class AsyncGraphExecutor:
    """使用 asyncio 调度图节点的执行器。

    第一个失败会停止提交新节点，但已经开始的 task 会继续等待到结束。
    构建 action 通常会修改工作目录或产物，直接取消无法保证外部进程已经
    停止，反而可能在调用方处理失败时留下仍在写入的后台任务。
    """

    def __init__(
        self,
        state: GraphExecutionState | None = None,
        *,
        execution_control: GraphExecutionControl,
        observer: GraphObserver | None = None,
        action_log: GraphActionLog = NO_GRAPH_ACTION_LOG,
    ) -> None:
        self.state = state or GraphExecutionState()
        self.execution_control = execution_control
        self.observer = observer
        self.action_log = action_log

    def execute(
        self,
        graph: ExecutableGraph,
        *,
        concurrency: int,
    ) -> GraphExecutionState:
        self.execution_control.require_running()
        self.state.begin_run()
        match graph:
            case GraphCompilation():
                asyncio.run(
                    self._execute_compilation(
                        graph,
                        concurrency=concurrency,
                    )
                )
            case FrozenGraph():
                asyncio.run(
                    self._execute_graph(
                        DynamicGraphRun(graph),
                        graph_label_path=(),
                        concurrency=concurrency,
                    )
                )
        return self.state

    async def _execute_compilation(
        self,
        compilation: GraphCompilation,
        *,
        concurrency: int,
    ) -> None:
        run = CompiledGraphRun(compilation=compilation)
        emit_graph_observation(
            observer=self.observer,
            state=self.state,
            graph=run,
            label_path=(),
            phase=GraphObservationPhase.BEFORE,
        )
        loop = asyncio.get_running_loop()

        def _run_leaf(step: CompiledStep) -> None:
            future = asyncio.run_coroutine_threadsafe(
                self._run_step(
                    run,
                    step,
                    step.context.label_path,
                    concurrency,
                    step.context.graph_step_ids,
                    step.context.ancestor_graph_scopes,
                ),
                loop,
            )
            future.result()

        scheduler_host = NativeCompiledSchedulerHost(
            compilation,
            self.state,
            observer=self.observer,
            execution_control=self.execution_control,
            run_leaf=_run_leaf,
        )
        try:
            await asyncio.to_thread(
                compilation._native_graph.run_threaded,
                scheduler_host,
                concurrency,
            )
        except ValueError as exc:
            raise GraphError(str(exc)) from exc
        emit_graph_observation(
            observer=self.observer,
            state=self.state,
            graph=run,
            label_path=(),
            phase=GraphObservationPhase.AFTER_SUCCESS,
        )

    async def _execute_graph(
        self,
        run: DynamicGraphRun,
        *,
        graph_label_path: StepLabelPath,
        concurrency: int,
        ancestor_graph_scopes: tuple[GraphScope, ...] = (),
    ) -> None:
        emit_graph_observation(
            observer=self.observer,
            state=self.state,
            graph=run,
            label_path=graph_label_path,
            phase=GraphObservationPhase.BEFORE,
        )
        scheduling = DynamicGraphScheduling.materialize(
            run,
            graph_label_path=graph_label_path,
            ancestor_graph_scopes=ancestor_graph_scopes,
        )
        loop = asyncio.get_running_loop()

        def _run_leaf(step: FrozenStep) -> None:
            future = asyncio.run_coroutine_threadsafe(
                self._run_step(
                    run,
                    step,
                    scheduling.label_path(step),
                    concurrency,
                    scheduling.graph_step_ids,
                    scheduling.ancestor_graph_scopes,
                ),
                loop,
            )
            future.result()

        scheduler_host = NativeDynamicSchedulerHost(
            scheduling,
            self.state,
            execution_control=self.execution_control,
            run_leaf=_run_leaf,
        )
        try:
            await asyncio.to_thread(
                scheduling.native_graph.run_threaded,
                scheduler_host,
                concurrency,
            )
        except ValueError as exc:
            raise GraphError(str(exc)) from exc
        emit_graph_observation(
            observer=self.observer,
            state=self.state,
            graph=run,
            label_path=graph_label_path,
            phase=GraphObservationPhase.AFTER_SUCCESS,
        )

    async def _run_step(
        self,
        run: GraphRunLike,
        step: ExecutableStep,
        label_path: StepLabelPath,
        concurrency: int,
        graph_step_ids: Mapping[Step, StepId],
        ancestor_graph_scopes: tuple[GraphScope, ...],
    ) -> None:
        self.state.record_step(step.id, label_path)
        action = step.action
        if isinstance(action, FrozenGraph):
            await self._execute_graph(
                run.nest(
                    action,
                    component_log_path=(
                        self.state.get_component_log_path(step.id)
                        or run.component_log_path
                    ),
                ),
                graph_label_path=label_path,
                concurrency=concurrency,
                ancestor_graph_scopes=(
                    run.child_graph_scope(
                        step,
                        graph_step_ids=graph_step_ids,
                    ),
                    *ancestor_graph_scopes,
                ),
            )
            self.state.complete_step(step.id)
            return
        runtime_step = RuntimeStep(
            step=step,
            label_path=label_path,
            graph_step_ids=graph_step_ids,
            ancestor_graph_scopes=ancestor_graph_scopes,
        )
        run_ctx = run.make_run_context(
            self.state,
            runtime_step,
        )
        value = await _invoke_async_step_action(
            action,
            run_ctx,
            self.action_log,
        )
        nested_graph = as_graph_result(
            value,
            parent_step_id=step.id,
            default_label=label_path[-1],
        )
        if nested_graph is not None:
            nested_component_log_path = (
                self.state.get_component_log_path(step.id) or run_ctx.component_log_path
            )
            await self._execute_graph(
                run.nest(
                    nested_graph,
                    component_log_path=nested_component_log_path,
                ),
                graph_label_path=label_path,
                concurrency=concurrency,
                ancestor_graph_scopes=(
                    run.child_graph_scope(
                        step,
                        graph_step_ids=graph_step_ids,
                    ),
                    *ancestor_graph_scopes,
                ),
            )
            self.state.record_result(step.id, None)
            self.state.complete_step(step.id)
            return
        self.state.record_result(step.id, value)
        self.state.complete_step(step.id)
