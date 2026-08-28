from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from .compile import (
    GraphCompilation,
    compile_graph,
)
from .errors import GraphError
from .execution_output import (
    NO_GRAPH_EXECUTION_OUTPUT,
    GraphExecutionOutput,
)
from .runtime import (
    NO_GRAPH_ACTION_LOG,
    AsyncGraphRunOptions,
    GraphActionLog,
    GraphExecutionControl,
    GraphExecutionState,
    GraphRunOptions,
    ThreadGraphRunOptions,
)

if TYPE_CHECKING:
    from pathlib import Path

    from .model import Graph, GraphMode, Step, StepId
    from .results import StepResult


@dataclass(slots=True)
class GraphPlan:
    """DAG 图的实例化执行载体。

    `GraphPlan` 把一张 `Graph` DAG 绑定到“这一次”的运行与编译上下文上。

    它持有：
    - 本次执行的根图 label
    - 原始图结构
    - 这次执行共享的 `GraphExecutionState`
    - 这次执行使用的 `GraphExecutionControl`

    它负责的入口包括：
    - `compile()`：在内部 lower 条件边并生成 `GraphCompilation`
    - `run()`：按 dynamic / compiled 两种模式执行这次图实例
    """

    label: str
    graph: Graph
    execution_state: GraphExecutionState = field(default_factory=GraphExecutionState)
    execution_control: GraphExecutionControl = field(
        default_factory=GraphExecutionControl,
    )
    action_log: GraphActionLog = NO_GRAPH_ACTION_LOG
    execution_output: GraphExecutionOutput = NO_GRAPH_EXECUTION_OUTPUT

    def __post_init__(self) -> None:
        if not self.label:
            raise GraphError("graph plan label must not be empty")

    def compile(self) -> GraphCompilation:
        return compile_graph(self.graph, root_label=self.label)

    def require_result(self, step: Step) -> StepResult:
        """按声明 step 引用读取本次图挂载中的执行结果。"""

        results = self.execution_state.results
        if results is None:
            raise RuntimeError("graph plan did not enable graph results")
        return results.require(self._require_unique_mounted_step_id(step))

    def bind_component_log_path(self, step: Step, path: Path) -> None:
        """把一次挂载中的 component step 绑定到独立日志文件。"""

        step_id = self._require_unique_mounted_step_id(step)
        self.execution_state.set_component_log_path(step_id, path)

    def _require_unique_mounted_step_id(self, step: Step) -> StepId:
        from .model import Graph, step_ids_for_mount

        matches: list[StepId] = []

        def _collect(graph: Graph, parent_step_id: StepId | None) -> None:
            for candidate, candidate_id in step_ids_for_mount(
                graph,
                parent_step_id=parent_step_id,
            ).items():
                if candidate is step:
                    matches.append(candidate_id)
                if isinstance(candidate.action, Graph):
                    _collect(candidate.action, candidate_id)

        _collect(self.graph, None)
        if not matches:
            raise RuntimeError(f"step '{step.label}' is not mounted in graph plan")
        if len(matches) > 1:
            raise RuntimeError(
                f"step '{step.label}' is mounted more than once in graph plan"
            )
        return matches[0]

    def _write_execution_output(
        self,
        *,
        mode: GraphMode,
        execution_error: BaseException | None = None,
    ) -> None:
        """写入最终执行输出，同时保证输出错误不覆盖执行错误。"""

        try:
            self.execution_output.write(
                self.graph,
                self.execution_state,
                root_label=self.label,
                mode=mode,
            )
        except Exception as output_error:
            if execution_error is None:
                raise
            execution_error.add_note(
                f"failed to write graph execution output: {output_error}"
            )

    def run(
        self,
        options: GraphRunOptions | None = None,
        *,
        mode: GraphMode = "dynamic",
    ) -> GraphExecutionState:
        from .runtime import AsyncGraphExecutor, ParallelGraphExecutor

        self.execution_control.require_running()
        target_graph = self.compile()
        match mode:
            case "dynamic":
                pass
            case "compiled":
                pass
            case _:
                raise GraphError(f"unsupported graph mode: {mode}")

        run_options = options or ThreadGraphRunOptions()
        if not isinstance(
            run_options,
            (AsyncGraphRunOptions, ThreadGraphRunOptions),
        ):
            raise GraphError(
                f"unsupported graph run options: {type(run_options).__name__}"
            )
        try:
            if isinstance(run_options, AsyncGraphRunOptions):
                result = AsyncGraphExecutor(
                    self.execution_state,
                    execution_control=self.execution_control,
                    observer=run_options.observer,
                    action_log=self.action_log,
                    record_composites_as_steps=mode == "dynamic",
                ).execute(
                    target_graph,
                    concurrency=run_options.concurrency,
                )
            elif isinstance(run_options, ThreadGraphRunOptions):
                result = ParallelGraphExecutor(
                    self.execution_state,
                    execution_control=self.execution_control,
                    observer=run_options.observer,
                    action_log=self.action_log,
                    record_composites_as_steps=mode == "dynamic",
                ).execute(
                    target_graph,
                    workers=run_options.workers,
                )
            else:
                raise GraphError(
                    f"unsupported graph run options: {type(run_options).__name__}"
                )
        except BaseException as execution_error:
            self._write_execution_output(
                mode=mode,
                execution_error=execution_error,
            )
            raise
        self._write_execution_output(mode=mode)
        return result
