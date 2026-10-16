from __future__ import annotations

from typing import TYPE_CHECKING

from ..compile import CompiledStep, GraphCompilation, compile_graph
from ..errors import GraphError
from ..observe import GraphObservationPhase, GraphObserver
from .action_log import (
    NO_GRAPH_ACTION_LOG,
    GraphActionLog,
)
from .batch_driver import run_serial_session
from .compiled import CompiledExecutionHost
from .executor_common import (
    as_graph_result,
    emit_graph_observation,
    resolve_action_result,
)
from .run import (
    CompiledGraphRun,
    ExecutableGraph,
    RuntimeStep,
)
from .state import GraphExecutionState

if TYPE_CHECKING:
    from pathlib import Path

    from ..model import LeafAction
    from ..results import StepRunContext


def _invoke_serial_python_action(
    action: LeafAction,
    run_ctx: StepRunContext,
    action_log: GraphActionLog,
) -> object:
    def _invoke() -> object:
        result = action.invoke(run_ctx)
        return resolve_action_result(result)

    if run_ctx.step_log_path is None:
        return _invoke()
    with action_log.scope(run_ctx.step_log_path):
        return _invoke()


class SerialGraphExecutor:
    """按 DAG 顺序串行执行节点的参考执行器。"""

    def __init__(
        self,
        state: GraphExecutionState | None = None,
        *,
        shared: object | None = None,
        observer: GraphObserver | None = None,
        action_log: GraphActionLog = NO_GRAPH_ACTION_LOG,
        record_composites_as_steps: bool = False,
    ) -> None:
        self.state = state or GraphExecutionState()
        self._shared = shared
        self.observer = observer
        self.action_log = action_log
        self.record_composites_as_steps = record_composites_as_steps

    def execute(
        self,
        graph: ExecutableGraph,
    ) -> GraphExecutionState:
        self.state.begin_run()
        self._execute_compilation(graph)
        self.state.require_skip_requests_resolved()
        return self.state

    def _execute_compilation(
        self,
        compilation: GraphCompilation,
        *,
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
            record_composites_as_steps=self.record_composites_as_steps,
            run_leaf=lambda step: self._execute_compiled_step(run, step),
        )
        try:
            run_serial_session(
                compilation._native_graph,
                execution_host,
                compilation._native_actions,
                run.native_action_contexts(self.state),
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

    def _execute_compiled_step(
        self,
        run: CompiledGraphRun,
        step: CompiledStep,
    ) -> None:
        label_path = step.context.label_path
        self.state.record_step(step.id, label_path)
        step_log_path = run.step_log_path_for(self.state, step)
        action = step.action
        runtime_step = RuntimeStep(
            step=step,
            label_path=label_path,
            graph_step_ids=step.context.graph_step_ids,
            ancestor_graph_scopes=step.context.ancestor_graph_scopes,
        )
        run_ctx = run.make_run_context(self.state, runtime_step, shared=self._shared)
        value = _invoke_serial_python_action(
            action,
            run_ctx,
            self.action_log,
        )
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
                            graph_step_ids=step.context.graph_step_ids,
                        ),
                        *step.context.ancestor_graph_scopes,
                    ),
                ),
                step_log_path=step_log_path,
            )
            self.state.record_result(step.id, None)
        else:
            self.state.record_result(step.id, value)
        self.state.complete_step(step.id)
