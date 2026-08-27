from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING

from ..compile import CompiledStep, GraphCompilation
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
    await_result,
    emit_graph_observation,
)
from .run import (
    CompiledGraphRun,
    DynamicGraphRun,
    ExecutableGraph,
    RuntimeStep,
)
from .state import GraphExecutionState

if TYPE_CHECKING:
    from ..model import LeafAction, StepLabelPath
    from ..results import GraphScope, StepRunContext


def _invoke_serial_step_action(
    action: LeafAction,
    run_ctx: StepRunContext,
    action_log: GraphActionLog,
) -> object:
    def _invoke() -> object:
        result = action.invoke(run_ctx)
        return asyncio.run(await_result(result))

    if run_ctx.component_log_path is None:
        return _invoke()
    with action_log.scope(run_ctx.component_log_path):
        return _invoke()


class SerialGraphExecutor:
    """按 DAG 顺序串行执行节点的参考执行器。"""

    def __init__(
        self,
        state: GraphExecutionState | None = None,
        *,
        observer: GraphObserver | None = None,
        action_log: GraphActionLog = NO_GRAPH_ACTION_LOG,
    ) -> None:
        self.state = state or GraphExecutionState()
        self.observer = observer
        self.action_log = action_log

    def execute(
        self,
        graph: ExecutableGraph,
    ) -> GraphExecutionState:
        self.state.begin_run()
        match graph:
            case GraphCompilation():
                self._execute_compilation(graph)
            case FrozenGraph():
                self._execute_graph(DynamicGraphRun(graph), graph_label_path=())
        return self.state

    def _execute_compilation(self, compilation: GraphCompilation) -> None:
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
            run_leaf=lambda step: self._execute_compiled_step(run, step),
        )
        try:
            compilation._native_graph.run_serial(scheduler_host)
        except ValueError as exc:
            raise GraphError(str(exc)) from exc
        emit_graph_observation(
            observer=self.observer,
            state=self.state,
            graph=run,
            label_path=(),
            phase=GraphObservationPhase.AFTER_SUCCESS,
        )

    def _execute_compiled_step(
        self,
        run: CompiledGraphRun,
        step: CompiledStep,
    ) -> None:
        label_path = step.context.label_path
        self.state.record_step(step.id, label_path)
        runtime_step = RuntimeStep(
            step=step,
            label_path=label_path,
            graph_step_ids=step.context.graph_step_ids,
            ancestor_graph_scopes=step.context.ancestor_graph_scopes,
        )
        run_ctx = run.make_run_context(self.state, runtime_step)
        value = _invoke_serial_step_action(
            step.action,
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
            self._execute_graph(
                run.nest(
                    nested_graph,
                    component_log_path=nested_component_log_path,
                ),
                graph_label_path=label_path,
                ancestor_graph_scopes=(
                    run.child_graph_scope(
                        step,
                        graph_step_ids=step.context.graph_step_ids,
                    ),
                    *step.context.ancestor_graph_scopes,
                ),
            )
            self.state.record_result(step.id, None)
        else:
            self.state.record_result(step.id, value)
        self.state.complete_step(step.id)

    def _execute_graph(
        self,
        run: DynamicGraphRun,
        *,
        graph_label_path: StepLabelPath,
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
            run_leaf=lambda step: self._execute_dynamic_step(scheduling, step),
        )
        try:
            scheduling.native_graph.run_serial(scheduler_host)
        except ValueError as exc:
            raise GraphError(str(exc)) from exc
        emit_graph_observation(
            observer=self.observer,
            state=self.state,
            graph=run,
            label_path=graph_label_path,
            phase=GraphObservationPhase.AFTER_SUCCESS,
        )

    def _execute_dynamic_step(
        self,
        scheduling: DynamicGraphScheduling,
        step: FrozenStep,
    ) -> None:
        run = scheduling.run
        step_label_path = scheduling.label_path(step)
        self.state.record_step(step.id, step_label_path)
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
                graph_label_path=step_label_path,
                ancestor_graph_scopes=(
                    run.child_graph_scope(
                        step,
                        graph_step_ids=scheduling.graph_step_ids,
                    ),
                    *scheduling.ancestor_graph_scopes,
                ),
            )
            self.state.complete_step(step.id)
            return

        runtime_step = RuntimeStep(
            step=step,
            label_path=step_label_path,
            graph_step_ids=scheduling.graph_step_ids,
            ancestor_graph_scopes=scheduling.ancestor_graph_scopes,
        )
        run_ctx = run.make_run_context(self.state, runtime_step)
        value = _invoke_serial_step_action(action, run_ctx, self.action_log)
        nested_graph = as_graph_result(
            value,
            parent_step_id=step.id,
            default_label=step_label_path[-1],
        )
        if nested_graph is not None:
            nested_component_log_path = (
                self.state.get_component_log_path(step.id) or run_ctx.component_log_path
            )
            self._execute_graph(
                run.nest(
                    nested_graph,
                    component_log_path=nested_component_log_path,
                ),
                graph_label_path=step_label_path,
                ancestor_graph_scopes=(
                    run.child_graph_scope(
                        step,
                        graph_step_ids=scheduling.graph_step_ids,
                    ),
                    *scheduling.ancestor_graph_scopes,
                ),
            )
            self.state.record_result(step.id, None)
        else:
            self.state.record_result(step.id, value)
        self.state.complete_step(step.id)
