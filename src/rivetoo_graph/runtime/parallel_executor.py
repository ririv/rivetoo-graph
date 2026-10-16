from __future__ import annotations

import threading
from typing import TYPE_CHECKING

from .action_log import NO_GRAPH_ACTION_LOG, GraphActionLog
from .batch_driver import run_threaded_session
from .executor_common import invoke_sync_action, step_thread_name
from .python_actions import PythonActionRuntime
from .state import GraphExecutionState

if TYPE_CHECKING:
    from ..model import LeafAction
    from ..observe import GraphObserver
    from ..results import StepRunContext
    from .control import GraphExecutionControl
    from .run import ExecutableGraph


def _invoke_thread_action(
    action: LeafAction, ctx: StepRunContext, action_log: GraphActionLog
) -> object:
    thread = threading.current_thread()
    original_name = thread.name
    thread.name = step_thread_name(ctx.label_path)
    try:
        return invoke_sync_action(action, ctx, action_log)
    finally:
        thread.name = original_name


class ParallelGraphExecutor:
    """Execute all Python sessions through one bounded thread pool."""

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
            _invoke_thread_action,
        )

    def execute(self, graph: ExecutableGraph, *, workers: int) -> GraphExecutionState:
        self.execution_control.require_running()
        self.state.begin_run()
        definition = self._runtime.prepare(graph)
        run_threaded_session(
            definition.graph,
            definition.host,
            workers=workers,
            native_actions=definition.native_actions,
            native_contexts=definition.native_contexts,
        )
        self.state.require_skip_requests_resolved()
        self._runtime.after_success(graph)
        return self.state
