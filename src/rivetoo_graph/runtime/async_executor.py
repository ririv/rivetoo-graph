from __future__ import annotations

import asyncio
import inspect
from typing import TYPE_CHECKING, cast

from ..compile import CompiledStep, GraphCompilation, compile_graph
from ..errors import GraphError
from ..observe import GraphObservationPhase, GraphObserver
from .action_log import (
    NO_GRAPH_ACTION_LOG,
    GraphActionLog,
)
from .batch_driver import run_async_session
from .compiled import CompiledExecutionHost
from .executor_common import (
    as_graph_result,
    emit_graph_observation,
)
from .run import (
    CompiledGraphRun,
    ExecutableGraph,
    RuntimeStep,
)
from .state import GraphExecutionState

if TYPE_CHECKING:
    from collections.abc import Awaitable, Mapping
    from pathlib import Path

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
        record_composites_as_steps: bool = False,
    ) -> None:
        self.state = state or GraphExecutionState()
        self.execution_control = execution_control
        self.observer = observer
        self.action_log = action_log
        self.record_composites_as_steps = record_composites_as_steps

    def execute(
        self,
        graph: ExecutableGraph,
        *,
        concurrency: int,
    ) -> GraphExecutionState:
        self.execution_control.require_running()
        self.state.begin_run()
        asyncio.run(
            self._execute_compilation(
                graph,
                concurrency=concurrency,
            )
        )
        self.state.require_skip_requests_resolved()
        return self.state

    async def _execute_compilation(
        self,
        compilation: GraphCompilation,
        *,
        concurrency: int,
        component_log_path: Path | None = None,
    ) -> None:
        run = CompiledGraphRun(
            compilation=compilation,
            component_log_path=component_log_path,
        )
        emit_graph_observation(
            observer=self.observer,
            state=self.state,
            graph=run,
            label_path=compilation.root_label_path,
            phase=GraphObservationPhase.BEFORE,
        )
        execution_host = CompiledExecutionHost(
            compilation,
            self.state,
            observer=self.observer,
            execution_control=self.execution_control,
            record_composites_as_steps=self.record_composites_as_steps,
            run_leaf=lambda _step: None,
        )

        async def _run_leaf(node: int) -> None:
            step = compilation._native_node(node)
            if not isinstance(step, CompiledStep):
                raise GraphError(f"native scheduler submitted non-leaf node: {step.id}")
            await self._run_step(
                run,
                step,
                step.context.label_path,
                concurrency,
                step.context.graph_step_ids,
                step.context.ancestor_graph_scopes,
            )

        try:
            await run_async_session(
                compilation._native_graph,
                execution_host,
                _run_leaf,
                concurrency=concurrency,
            )
        except ValueError as exc:
            raise GraphError(str(exc)) from exc
        emit_graph_observation(
            observer=self.observer,
            state=self.state,
            graph=run,
            label_path=compilation.root_label_path,
            phase=GraphObservationPhase.AFTER_SUCCESS,
        )

    async def _run_step(
        self,
        run: CompiledGraphRun,
        step: CompiledStep,
        label_path: StepLabelPath,
        concurrency: int,
        graph_step_ids: Mapping[Step, StepId],
        ancestor_graph_scopes: tuple[GraphScope, ...],
    ) -> None:
        self.state.record_step(step.id, label_path)
        action = step.action
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
        nested_graph = as_graph_result(value)
        if nested_graph is not None:
            nested_component_log_path = (
                self.state.get_component_log_path(step.id) or run_ctx.component_log_path
            )
            await self._execute_compilation(
                compile_graph(
                    nested_graph,
                    root_label=label_path[-1],
                    parent_step_id=step.id,
                    root_label_path=label_path,
                    ancestor_graph_scopes=(
                        run.child_graph_scope(
                            step,
                            graph_step_ids=graph_step_ids,
                        ),
                        *ancestor_graph_scopes,
                    ),
                ),
                concurrency=concurrency,
                component_log_path=nested_component_log_path,
            )
            self.state.record_result(step.id, None)
            self.state.complete_step(step.id)
            return
        self.state.record_result(step.id, value)
        self.state.complete_step(step.id)
