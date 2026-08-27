from __future__ import annotations

import asyncio
import concurrent.futures
import threading
import traceback
from typing import TYPE_CHECKING

from ..compile import CompiledStep, GraphCompilation
from ..errors import GraphError
from ..frozen import FrozenGraph
from ..observe import GraphObservationPhase, GraphObserver
from .action_log import (
    NO_GRAPH_ACTION_LOG,
    GraphActionLog,
)
from .compiled import CompiledScheduler
from .dynamic import (
    DynamicConditionCache,
    DynamicStepState,
    initial_ready_dynamic_steps,
    log_disabled_step_condition,
    refresh_ready_dynamic_steps,
)
from .executor_common import (
    as_graph_result,
    await_result,
    emit_graph_observation,
    has_runtime_conditions,
    logger,
    prepare_ready_graph_run,
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
        scheduler = CompiledScheduler(
            compilation,
            self.state,
            observer=self.observer,
        )
        scheduler.start()
        failure: BaseException | None = None
        with concurrent.futures.ThreadPoolExecutor(
            max_workers=workers,
            thread_name_prefix="GraphWorker",
        ) as executor:
            futures: dict[
                concurrent.futures.Future[None],
                CompiledStep,
            ] = {}
            while not scheduler.is_finished or futures:
                while (
                    failure is None
                    and not self.execution_control.stop_requested()
                    and len(futures) < workers
                ):
                    step = scheduler.take_ready_step()
                    if step is None:
                        break
                    logger.debug(
                        "Graph submit: step=%s inflight=%d workers=%d",
                        ".".join(step.context.label_path),
                        len(futures),
                        workers,
                    )
                    futures[
                        executor.submit(
                            self._run_step,
                            run,
                            step,
                            step.context.label_path,
                            workers,
                            step.context.graph_step_ids,
                            step.context.ancestor_graph_scopes,
                        )
                    ] = step

                if not futures:
                    if failure is not None or self.execution_control.stop_requested():
                        break
                    scheduler.require_finished()
                    break

                done, _ = concurrent.futures.wait(
                    futures,
                    return_when=concurrent.futures.FIRST_COMPLETED,
                )
                for future in done:
                    step = futures.pop(future)
                    try:
                        future.result()
                    except BaseException as exc:
                        if failure is None:
                            failure = exc
                            self.execution_control.request_stop()
                    else:
                        if failure is None:
                            scheduler.complete_step(step)

        if failure is not None:
            raise failure
        self.execution_control.require_running()
        scheduler.require_finished()
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
        if has_runtime_conditions(run):
            self._execute_graph_dynamic(
                run,
                graph_label_path=graph_label_path,
                workers=workers,
                ancestor_graph_scopes=ancestor_graph_scopes,
            )
            return
        steps_by_id, graph_step_ids, sorter, ready = prepare_ready_graph_run(run)
        failure: BaseException | None = None
        emit_graph_observation(
            observer=self.observer,
            state=self.state,
            graph=run,
            label_path=graph_label_path,
            phase=GraphObservationPhase.BEFORE,
        )

        with concurrent.futures.ThreadPoolExecutor(
            max_workers=workers,
            thread_name_prefix="GraphWorker",
        ) as executor:
            futures: dict[concurrent.futures.Future[None], StepId] = {}

            while ready or futures:
                while (
                    failure is None
                    and not self.execution_control.stop_requested()
                    and ready
                    and len(futures) < workers
                ):
                    step_id = ready.popleft()
                    step = steps_by_id[step_id]
                    step_label_path = run.resolve_step_label_path(
                        step,
                        graph_label_path=graph_label_path,
                    )
                    if self.state.is_skipped(step.id):
                        sorter.done(step_id)
                        ready.extend(sorter.get_ready())
                        continue
                    logger.debug(
                        "Graph submit: step=%s ready_size=%d inflight=%d workers=%d",
                        ".".join(step_label_path),
                        len(ready),
                        len(futures),
                        workers,
                    )
                    futures[
                        executor.submit(
                            self._run_step,
                            run,
                            step,
                            step_label_path,
                            workers,
                            graph_step_ids,
                            ancestor_graph_scopes,
                        )
                    ] = step_id

                if not futures:
                    if failure is not None or self.execution_control.stop_requested():
                        break
                    continue

                done, _ = concurrent.futures.wait(
                    futures,
                    return_when=concurrent.futures.FIRST_COMPLETED,
                )
                for future in done:
                    step_id = futures.pop(future)
                    try:
                        future.result()
                    except BaseException as exc:
                        if failure is None:
                            failure = exc
                            self.execution_control.request_stop()
                    else:
                        sorter.done(step_id)
                        if failure is None:
                            ready.extend(sorter.get_ready())

        if failure is not None:
            raise failure
        self.execution_control.require_running()
        emit_graph_observation(
            observer=self.observer,
            state=self.state,
            graph=run,
            label_path=graph_label_path,
            phase=GraphObservationPhase.AFTER_SUCCESS,
        )

    def _execute_graph_dynamic(
        self,
        run: DynamicGraphRun,
        *,
        graph_label_path: StepLabelPath,
        workers: int,
        ancestor_graph_scopes: tuple[GraphScope, ...] = (),
    ) -> None:
        condition_cache = DynamicConditionCache()
        (
            steps_by_id,
            graph_step_ids,
            ready,
            remaining_step_ids,
        ) = initial_ready_dynamic_steps(
            run,
            self.state,
            graph_label_path=graph_label_path,
            ancestor_graph_scopes=ancestor_graph_scopes,
            condition_cache=condition_cache,
        )
        finished_step_ids: set[StepId] = set()
        failure: BaseException | None = None
        emit_graph_observation(
            observer=self.observer,
            state=self.state,
            graph=run,
            label_path=graph_label_path,
            phase=GraphObservationPhase.BEFORE,
        )

        with concurrent.futures.ThreadPoolExecutor(
            max_workers=workers,
            thread_name_prefix="GraphWorker",
        ) as executor:
            futures: dict[concurrent.futures.Future[None], StepId] = {}

            while ready or futures:
                while (
                    failure is None
                    and not self.execution_control.stop_requested()
                    and ready
                    and len(futures) < workers
                ):
                    step_id, step_state = ready.popleft()
                    step = steps_by_id[step_id]
                    step_label_path = run.resolve_step_label_path(
                        step,
                        graph_label_path=graph_label_path,
                    )
                    if self.state.is_skipped(step.id):
                        finished_step_ids.add(step_id)
                        refresh_ready_dynamic_steps(
                            run,
                            self.state,
                            graph_label_path=graph_label_path,
                            remaining_step_ids=remaining_step_ids,
                            finished_step_ids=finished_step_ids,
                            graph_step_ids=graph_step_ids,
                            ancestor_graph_scopes=ancestor_graph_scopes,
                            steps_by_id=steps_by_id,
                            ready=ready,
                            condition_cache=condition_cache,
                        )
                        continue
                    match step_state:
                        case DynamicStepState.DISABLED:
                            log_disabled_step_condition(
                                step,
                                label_path=step_label_path,
                            )
                            self.state.skip_step(step.id)
                            finished_step_ids.add(step_id)
                            refresh_ready_dynamic_steps(
                                run,
                                self.state,
                                graph_label_path=graph_label_path,
                                remaining_step_ids=remaining_step_ids,
                                finished_step_ids=finished_step_ids,
                                graph_step_ids=graph_step_ids,
                                ancestor_graph_scopes=ancestor_graph_scopes,
                                steps_by_id=steps_by_id,
                                ready=ready,
                                condition_cache=condition_cache,
                            )
                            continue
                        case DynamicStepState.READY:
                            pass
                        case _:
                            raise GraphError(
                                f"ready queue must not contain pending step: {step.id}"
                            )
                    logger.debug(
                        "Graph submit: step=%s ready_size=%d inflight=%d workers=%d",
                        ".".join(step_label_path),
                        len(ready),
                        len(futures),
                        workers,
                    )
                    futures[
                        executor.submit(
                            self._run_step,
                            run,
                            step,
                            step_label_path,
                            workers,
                            graph_step_ids,
                            ancestor_graph_scopes,
                        )
                    ] = step_id

                if not futures:
                    if failure is not None or self.execution_control.stop_requested():
                        break
                    continue

                done, _ = concurrent.futures.wait(
                    futures,
                    return_when=concurrent.futures.FIRST_COMPLETED,
                )
                for future in done:
                    step_id = futures.pop(future)
                    try:
                        future.result()
                    except BaseException as exc:
                        if failure is None:
                            failure = exc
                            self.execution_control.request_stop()
                    else:
                        finished_step_ids.add(step_id)
                        if failure is None:
                            refresh_ready_dynamic_steps(
                                run,
                                self.state,
                                graph_label_path=graph_label_path,
                                remaining_step_ids=remaining_step_ids,
                                finished_step_ids=finished_step_ids,
                                graph_step_ids=graph_step_ids,
                                ancestor_graph_scopes=ancestor_graph_scopes,
                                steps_by_id=steps_by_id,
                                ready=ready,
                                condition_cache=condition_cache,
                            )

        if failure is not None:
            raise failure
        self.execution_control.require_running()
        if remaining_step_ids:
            raise GraphError(
                "dynamic graph has unresolved conditional dependencies: "
                + ", ".join(str(step_id) for step_id in sorted(remaining_step_ids))
            )
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
