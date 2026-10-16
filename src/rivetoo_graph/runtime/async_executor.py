from __future__ import annotations

import asyncio
import inspect
from typing import TYPE_CHECKING, cast

from .action_log import NO_GRAPH_ACTION_LOG, GraphActionLog
from .batch_driver import run_async_session
from .python_actions import PythonActionRuntime
from .state import GraphExecutionState

if TYPE_CHECKING:
    from collections.abc import Awaitable

    from ..model import LeafAction
    from ..observe import GraphObserver
    from ..results import StepRunContext
    from .control import GraphExecutionControl
    from .run import ExecutableGraph


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

    if run_ctx.step_log_path is None:
        return await _invoke()
    with action_log.scope(run_ctx.step_log_path):
        return await _invoke()


class AsyncGraphExecutor:
    """Execute asynchronous actions with one budget shared by all sessions."""

    def __init__(
        self,
        state: GraphExecutionState | None = None,
        *,
        shared: object | None = None,
        execution_control: GraphExecutionControl,
        observer: GraphObserver | None = None,
        action_log: GraphActionLog = NO_GRAPH_ACTION_LOG,
        record_composites_as_steps: bool = False,
    ) -> None:
        self.state = state or GraphExecutionState()
        self.execution_control = execution_control
        self._runtime = PythonActionRuntime(
            self.state,
            execution_control,
            observer,
            action_log,
            shared,
            record_composites_as_steps,
            _invoke_async_step_action,
        )

    def execute(
        self, graph: ExecutableGraph, *, concurrency: int
    ) -> GraphExecutionState:
        return asyncio.run(self.aexecute(graph, concurrency=concurrency))

    async def aexecute(
        self, graph: ExecutableGraph, *, concurrency: int
    ) -> GraphExecutionState:
        """Execute within the caller's running event loop."""

        self.execution_control.require_running()
        self.state.begin_run()
        definition = self._runtime.prepare(graph)
        await run_async_session(
            definition.graph,
            definition.host,
            concurrency=concurrency,
            native_actions=definition.native_actions,
            native_contexts=definition.native_contexts,
        )
        self.state.require_skip_requests_resolved()
        self._runtime.after_success(graph)
        return self.state
