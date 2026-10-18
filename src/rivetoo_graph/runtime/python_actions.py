from __future__ import annotations

import inspect
import traceback
from dataclasses import dataclass
from typing import TYPE_CHECKING

from ..compile import CompiledStep, GraphCompilation, compile_graph
from ..model import Graph
from ..observe import GraphObservationPhase
from ..option import Nothing, Option
from .executor_common import emit_graph_observation
from .run import CompiledGraphRun, RuntimeStep
from .session_tree import (
    DynamicSubgraph,
    LeafInvocation,
    LeafOutcome,
    SessionDefinition,
)

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable
    from pathlib import Path

    from ..model import LeafAction
    from ..observe import GraphObserver
    from ..results import StepRunContext
    from .action_log import GraphActionLog
    from .control import GraphExecutionControl
    from .state import GraphExecutionState


@dataclass(frozen=True, slots=True)
class PythonActionRuntime:
    """Bind Python actions and runtime subgraphs without recursive execution."""

    state: GraphExecutionState
    control: GraphExecutionControl
    observer: GraphObserver | None
    action_log: GraphActionLog
    shared: object | None
    record_composites_as_steps: bool
    invoke: Callable[
        [LeafAction, StepRunContext, GraphActionLog, tuple[Option[object], ...]], object
    ]

    def prepare(
        self, compilation: GraphCompilation, *, step_log_path: Path | None = None
    ) -> SessionDefinition:
        from .compiled import CompiledExecutionHost

        run = CompiledGraphRun(compilation, step_log_path)
        emit_graph_observation(
            observer=self.observer,
            state=self.state,
            graph=run,
            label_path=compilation.root_label_path,
            phase=GraphObservationPhase.BEFORE,
        )
        host = CompiledExecutionHost(
            compilation,
            self.state,
            observer=self.observer,
            execution_control=self.control,
            record_composites_as_steps=self.record_composites_as_steps,
            run_leaf=lambda step: self._run_step(run, step),
        )
        return SessionDefinition(
            compilation._native_graph,
            host,
            compilation._native_actions,
            run.native_action_contexts(self.state),
        )

    def after_success(
        self, compilation: GraphCompilation, step_log_path: Path | None = None
    ) -> None:
        emit_graph_observation(
            observer=self.observer,
            state=self.state,
            graph=CompiledGraphRun(compilation, step_log_path),
            label_path=compilation.root_label_path,
            phase=GraphObservationPhase.AFTER_SUCCESS,
        )

    def _run_step(self, run: CompiledGraphRun, step: CompiledStep) -> LeafInvocation:
        self.control.require_running()
        context = step.context
        self.state.record_step(step.id, context.label_path)
        runtime_step = RuntimeStep(
            step,
            context.label_path,
            context.graph_step_ids,
            context.ancestor_graph_scopes,
        )
        ctx = run.make_run_context(
            self.state,
            runtime_step,
            shared=self.shared,
            execution_control=self.control,
        )
        try:
            results = self.state.results
            if step.inputs and results is None:
                raise RuntimeError("result inputs require an initialized result store")
            inputs = (
                ()
                if results is None
                else tuple(
                    Nothing() if (result := results.get(ref.id)) is None else result
                    for ref in step.inputs
                )
            )
            value = self.invoke(step.action, ctx, self.action_log, inputs)
            if inspect.isawaitable(value):
                return self._await_step(run, step, ctx, value)
            return self._finish_step(run, step, ctx, value)
        except Exception:
            self._log_exception(ctx)
            raise

    async def _await_step(
        self,
        run: CompiledGraphRun,
        step: CompiledStep,
        ctx: StepRunContext,
        value: Awaitable[object],
    ) -> LeafOutcome:
        try:
            return self._finish_step(run, step, ctx, await value)
        except Exception:
            self._log_exception(ctx)
            raise

    def _finish_step(
        self,
        run: CompiledGraphRun,
        step: CompiledStep,
        ctx: StepRunContext,
        value: object,
    ) -> LeafOutcome:
        if isinstance(value, Graph):
            compilation = compile_graph(
                value,
                root_label=step.context.label_path[-1],
                parent_step_id=step.id,
                root_label_path=step.context.label_path,
                ancestor_graph_scopes=(
                    run.child_graph_scope(
                        step, graph_step_ids=step.context.graph_step_ids
                    ),
                    *step.context.ancestor_graph_scopes,
                ),
            )
            definition = self.prepare(compilation, step_log_path=ctx.step_log_path)

            def complete() -> None:
                self.after_success(compilation, ctx.step_log_path)
                self.state.record_result(step.id, None)
                self.state.complete_step(step.id)

            return DynamicSubgraph(definition, complete)
        self.state.record_result(step.id, value)
        self.state.complete_step(step.id)
        return None

    def _log_exception(self, ctx: StepRunContext) -> None:
        if ctx.step_log_path is not None:
            self.action_log.append(
                ctx.step_log_path,
                "===== ACTION EXCEPTION =====\n"
                f"{traceback.format_exc()}"
                "===== END ACTION EXCEPTION =====\n",
            )
