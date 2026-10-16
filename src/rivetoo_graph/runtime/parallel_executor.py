from __future__ import annotations

import threading
import traceback
from typing import TYPE_CHECKING

from ..compile import CompiledStep, GraphCompilation, compile_graph
from ..errors import GraphError
from ..observe import GraphObservationPhase, GraphObserver
from .action_log import (
    NO_GRAPH_ACTION_LOG,
    GraphActionLog,
)
from .batch_driver import run_threaded_session
from .compiled import CompiledExecutionHost
from .executor_common import (
    as_graph_result,
    emit_graph_observation,
    resolve_action_result,
    step_thread_name,
)
from .run import (
    CompiledGraphRun,
    ExecutableGraph,
    RuntimeStep,
)
from .state import GraphExecutionState

if TYPE_CHECKING:
    from collections.abc import Mapping
    from pathlib import Path

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
        shared: object | None = None,
        execution_control: GraphExecutionControl,
        observer: GraphObserver | None = None,
        action_log: GraphActionLog = NO_GRAPH_ACTION_LOG,
        record_composites_as_steps: bool = False,
    ) -> None:
        self.state = state or GraphExecutionState()
        self._shared = shared
        self.execution_control = execution_control
        self.observer = observer
        self.action_log = action_log
        self.record_composites_as_steps = record_composites_as_steps

    def execute(
        self,
        graph: ExecutableGraph,
        *,
        workers: int,
    ) -> GraphExecutionState:
        self.execution_control.require_running()
        self.state.begin_run()
        self._execute_compilation(graph, workers=workers)
        self.state.require_skip_requests_resolved()
        return self.state

    def _execute_compilation(
        self,
        compilation: GraphCompilation,
        *,
        workers: int,
        step_log_path: Path | None = None,
    ) -> None:
        run = CompiledGraphRun(
            compilation=compilation,
            step_log_path=step_log_path,
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
            run_threaded_session(
                compilation._native_graph,
                execution_host,
                workers=workers,
                native_actions=compilation._native_actions,
                native_contexts=run.native_action_contexts(self.state),
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

    def _run_step(
        self,
        run: CompiledGraphRun,
        step: CompiledStep,
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
        step_log_path = run.step_log_path_for(self.state, step)
        try:
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
                shared=self._shared,
            )

            def _invoke() -> object:
                result = action.invoke(run_ctx)
                return resolve_action_result(result)

            if run_ctx.step_log_path is None:
                value = _invoke()
            else:
                with self.action_log.scope(run_ctx.step_log_path):
                    value = _invoke()
            nested_graph = as_graph_result(value)
            if nested_graph is not None:
                self._execute_compilation(
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
                    workers=workers,
                    step_log_path=run_ctx.step_log_path,
                )
                self.state.record_result(step.id, None)
                self.state.complete_step(step.id)
                return
            self.state.record_result(step.id, value)
            self.state.complete_step(step.id)
        except Exception:
            if step_log_path is not None:
                self.action_log.append(
                    step_log_path,
                    "===== ACTION EXCEPTION =====\n"
                    f"{traceback.format_exc()}"
                    "===== END ACTION EXCEPTION =====\n",
                )
            raise
        finally:
            thread.name = original_thread_name
