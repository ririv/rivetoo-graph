from __future__ import annotations

import asyncio
from dataclasses import InitVar, dataclass, field
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
    from collections.abc import Mapping
    from pathlib import Path

    from .events import StepEventSink
    from .model import Graph, GraphMode, Step, StepId
    from .option import Some


@dataclass(slots=True)
class GraphExecution:
    """DAG 图的实例化执行载体。

    `GraphExecution` 把一张 `Graph` DAG 绑定到“这一次”的运行与编译上下文上。

    它持有：
    - 原始图结构
    - 这次执行共享的 `GraphExecutionState`
    - 这次执行使用的 `GraphExecutionControl`
    - 调用方显式提供、由各 Python action 共享的业务对象

    它负责的入口包括：
    - `compile()`：在内部 lower 条件边并生成 `GraphCompilation`
    - `run()`：按 dynamic / compiled 两种模式执行这次图实例
    """

    graph: Graph
    shared: object | None = field(default=None, kw_only=True, repr=False)
    execution_state: GraphExecutionState = field(default_factory=GraphExecutionState)
    execution_control: GraphExecutionControl = field(
        default_factory=GraphExecutionControl,
    )
    action_log: GraphActionLog = NO_GRAPH_ACTION_LOG
    execution_output: GraphExecutionOutput = NO_GRAPH_EXECUTION_OUTPUT
    step_log_paths: InitVar[Mapping[Step, Path] | None] = field(
        default=None, kw_only=True
    )
    events: InitVar[StepEventSink | None] = field(default=None, kw_only=True)

    def __post_init__(
        self, step_log_paths: Mapping[Step, Path] | None, events: StepEventSink | None
    ) -> None:
        if not step_log_paths:
            if events is not None:
                self.execution_state._initialize_execution_inputs({}, events)
            return
        mounted_step_ids = self._index_mounted_step_ids()
        # 先完整校验再写入状态，避免某个引用无效时只绑定了一部分日志。
        resolved = {
            self._require_unique_mounted_step_id(
                step, mounted_step_ids=mounted_step_ids
            ): path
            for step, path in step_log_paths.items()
        }
        self.execution_state._initialize_execution_inputs(resolved, events)

    def compile(self) -> GraphCompilation:
        return compile_graph(self.graph, root_label=self.graph.display_label)

    def require_result(self, step: Step) -> Some[object]:
        """按声明 step 引用读取本次图挂载中的执行结果。"""

        results = self.execution_state.results
        if results is None:
            raise RuntimeError("graph execution did not enable graph results")
        return results.require(self._require_unique_mounted_step_id(step))

    def _require_unique_mounted_step_id(
        self,
        step: Step,
        *,
        mounted_step_ids: Mapping[Step, tuple[StepId, ...]] | None = None,
    ) -> StepId:
        if mounted_step_ids is None:
            mounted_step_ids = self._index_mounted_step_ids()
        matches = mounted_step_ids.get(step)
        if not matches:
            raise RuntimeError(f"step '{step.label}' is not mounted in graph execution")
        if len(matches) > 1:
            raise RuntimeError(
                f"step '{step.label}' is mounted more than once in graph execution"
            )
        return next(iter(matches))

    def _index_mounted_step_ids(self) -> dict[Step, tuple[StepId, ...]]:
        from .model import Graph, step_ids_for_mount

        mounted_step_ids: dict[Step, list[StepId]] = {}

        def _collect(graph: Graph, parent_step_id: StepId | None) -> None:
            for candidate, candidate_id in step_ids_for_mount(
                graph,
                parent_step_id=parent_step_id,
            ).items():
                mounted_step_ids.setdefault(candidate, []).append(candidate_id)
                if isinstance(candidate.action, Graph):
                    _collect(candidate.action, candidate_id)

        # V 为静态挂载中的 step 出现次数。一次批量绑定以 O(V) 时间和空间建立索引，
        # 随后的 K 次查找均为 O(1)，总复杂度为 O(V + K)，避免退化为 O(K * V)。
        _collect(self.graph, None)
        return {
            candidate: tuple(step_ids)
            for candidate, step_ids in mounted_step_ids.items()
        }

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
                root_label=self.graph.display_label,
                mode=mode,
            )
        except Exception as output_error:
            if execution_error is None:
                raise
            execution_error.add_note(
                f"failed to write graph execution output: {output_error}"
            )

    def _compile_for_run(self, mode: GraphMode) -> GraphCompilation:
        self.execution_control.require_running()
        if mode not in ("dynamic", "compiled"):
            raise GraphError(f"unsupported graph mode: {mode}")
        return self.compile()

    def run(
        self,
        options: GraphRunOptions | None = None,
        *,
        mode: GraphMode = "dynamic",
    ) -> GraphExecutionState:
        from .runtime import ParallelGraphExecutor

        run_options = options or ThreadGraphRunOptions()
        if isinstance(run_options, AsyncGraphRunOptions):
            return asyncio.run(self.arun(run_options, mode=mode))
        if not isinstance(run_options, ThreadGraphRunOptions):
            raise GraphError(
                f"unsupported graph run options: {type(run_options).__name__}"
            )
        target_graph = self._compile_for_run(mode)
        try:
            result = ParallelGraphExecutor(
                self.execution_state,
                shared=self.shared,
                execution_control=self.execution_control,
                observer=run_options.observer,
                action_log=self.action_log,
                record_composites_as_steps=mode == "dynamic",
            ).execute(target_graph, workers=run_options.workers)
        except BaseException as execution_error:
            self._write_execution_output(mode=mode, execution_error=execution_error)
            raise
        self._write_execution_output(mode=mode)
        return result

    async def arun(
        self,
        options: AsyncGraphRunOptions | None = None,
        *,
        mode: GraphMode = "dynamic",
    ) -> GraphExecutionState:
        """Execute asynchronous actions in the caller's event loop."""

        from .runtime import AsyncGraphExecutor

        run_options = options or AsyncGraphRunOptions()
        target_graph = self._compile_for_run(mode)
        try:
            result = await AsyncGraphExecutor(
                self.execution_state,
                shared=self.shared,
                execution_control=self.execution_control,
                observer=run_options.observer,
                action_log=self.action_log,
                record_composites_as_steps=mode == "dynamic",
            ).aexecute(target_graph, concurrency=run_options.concurrency)
        except BaseException as execution_error:
            self._write_execution_output(mode=mode, execution_error=execution_error)
            raise
        self._write_execution_output(mode=mode)
        return result
