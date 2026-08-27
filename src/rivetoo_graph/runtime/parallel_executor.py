from __future__ import annotations

import asyncio
import threading
import traceback
from typing import TYPE_CHECKING

from ..compile import GraphCompilation
from ..errors import GraphError
from ..frozen import FrozenGraph
from ..observe import GraphObservationPhase, GraphObserver
from .action_log import (
    NO_GRAPH_ACTION_LOG,
    GraphActionLog,
)
from .compiled import NativeCompiledSchedulerHost
from .dynamic import DynamicGraphScheduling, NativeDynamicSchedulerHost
from .executor_common import (
    as_graph_result,
    await_result,
    emit_graph_observation,
    step_thread_name,
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
    from collections.abc import Mapping

    from ..model import Step, StepId, StepLabelPath
    from ..results import GraphScope, StepRunContext
    from .control import GraphExecutionControl


class ParallelGraphExecutor:
    """使用线程池并发执行图节点。

    第一个失败会停止提交新节点并请求停止本次执行；已提交的 future 仍会
    等待结束。这样返回错误时，不会遗留仍在修改构建目录的 worker。
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
        workers: int,
    ) -> GraphExecutionState:
        self.execution_control.require_running()
        self.state.begin_run()
        match graph:
            case GraphCompilation():
                self._execute_compilation(graph, workers=workers)
            case FrozenGraph():
                self._execute_graph(
                    DynamicGraphRun(graph),
                    graph_label_path=(),
                    workers=workers,
                )
        return self.state

    def _execute_compilation(
        self,
        compilation: GraphCompilation,
        *,
        workers: int,
    ) -> None:
        run = CompiledGraphRun(compilation=compilation)
        emit_graph_observation(
            observer=self.observer,
            state=self.state,
            graph=run,
            label_path=(),
            phase=GraphObservationPhase.BEFORE,
        )
        scheduler_host = NativeCompiledSchedulerHost(
            compilation,
            self.state,
            observer=self.observer,
            execution_control=self.execution_control,
            run_leaf=lambda step: self._run_step(
                run,
                step,
                step.context.label_path,
                workers,
                step.context.graph_step_ids,
                step.context.ancestor_graph_scopes,
            ),
        )
        try:
            compilation._native_graph.run_threaded(scheduler_host, workers)
        except ValueError as exc:
            raise GraphError(str(exc)) from exc
        emit_graph_observation(
            observer=self.observer,
            state=self.state,
            graph=run,
            label_path=(),
            phase=GraphObservationPhase.AFTER_SUCCESS,
        )

    def _execute_graph(
        self,
        run: DynamicGraphRun,
        *,
        graph_label_path: StepLabelPath,
        workers: int,
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
        scheduler_host = NativeDynamicSchedulerHost(
            scheduling,
            self.state,
            execution_control=self.execution_control,
            run_leaf=lambda step: self._run_step(
                run,
                step,
                scheduling.label_path(step),
                workers,
                scheduling.graph_step_ids,
                scheduling.ancestor_graph_scopes,
            ),
        )
        try:
            scheduling.native_graph.run_threaded(scheduler_host, workers)
        except ValueError as exc:
            raise GraphError(str(exc)) from exc
        emit_graph_observation(
            observer=self.observer,
            state=self.state,
            graph=run,
            label_path=graph_label_path,
            phase=GraphObservationPhase.AFTER_SUCCESS,
        )

    def _run_step(
        self,
        run: GraphRunLike,
        step: ExecutableStep,
        label_path: StepLabelPath,
        workers: int,
        graph_step_ids: Mapping[Step, StepId],
        ancestor_graph_scopes: tuple[GraphScope, ...],
    ) -> None:
        thread = threading.current_thread()
        original_thread_name = thread.name
        thread.name = step_thread_name(label_path)
        self.state.record_step(step.id, label_path)
        run_ctx: StepRunContext | None = None
        try:
            action = step.action
            if isinstance(action, FrozenGraph):
                self._execute_graph(
                    run.nest(
                        action,
                        component_log_path=(
                            self.state.get_component_log_path(step.id)
                            or run.component_log_path
                        ),
                    ),
                    graph_label_path=label_path,
                    workers=workers,
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

            def _invoke() -> object:
                result = action.invoke(run_ctx)
                return asyncio.run(await_result(result))

            if run_ctx.component_log_path is None:
                value = _invoke()
            else:
                with self.action_log.scope(run_ctx.component_log_path):
                    value = _invoke()
            nested_graph = as_graph_result(
                value,
                parent_step_id=step.id,
                default_label=label_path[-1],
            )
            if nested_graph is not None:
                nested_component_log_path = (
                    self.state.get_component_log_path(step.id)
                    or run_ctx.component_log_path
                )
                self._execute_graph(
                    run.nest(
                        nested_graph,
                        component_log_path=nested_component_log_path,
                    ),
                    graph_label_path=label_path,
                    workers=workers,
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
        except Exception:
            if run_ctx is not None and run_ctx.component_log_path is not None:
                self.action_log.append(
                    run_ctx.component_log_path,
                    "===== PYTHON EXCEPTION =====\n"
                    f"{traceback.format_exc()}"
                    "===== END PYTHON EXCEPTION =====\n",
                )
            raise
        finally:
            thread.name = original_thread_name
