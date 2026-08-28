from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from ..compile import CompiledStep, GraphCompilation
from ..errors import GraphError
from ..results import GraphScope, StepControl, StepReadContext, StepRunContext

if TYPE_CHECKING:
    from collections.abc import Mapping
    from pathlib import Path

    from ..model import Graph, GraphObserve, Step, StepId, StepLabelPath
    from ..observe import GraphObserver
    from .state import GraphExecutionState


@dataclass(frozen=True, slots=True)
class ThreadGraphRunOptions:
    """使用线程池执行同步或可等待 action 的运行参数。"""

    workers: int = 1
    observer: GraphObserver | None = None

    def __post_init__(self) -> None:
        if self.workers < 1:
            raise GraphError("graph run workers must be >= 1")


@dataclass(frozen=True, slots=True)
class AsyncGraphRunOptions:
    """使用 asyncio 调度 action 的运行参数。"""

    concurrency: int = 1
    observer: GraphObserver | None = None

    def __post_init__(self) -> None:
        if self.concurrency < 1:
            raise GraphError("graph run concurrency must be >= 1")


type GraphRunOptions = ThreadGraphRunOptions | AsyncGraphRunOptions
type ExecutableGraph = GraphCompilation


@dataclass(frozen=True, slots=True)
class RuntimeStep:
    """当前可执行 step 及其声明图结果读取作用域。"""

    step: CompiledStep
    label_path: StepLabelPath
    graph_step_ids: Mapping[Step, StepId]
    ancestor_graph_scopes: tuple[GraphScope, ...]


@dataclass(frozen=True, slots=True)
class CompiledGraphRun:
    """使用编译路径和原始声明作用域执行 `GraphCompilation` 的视图。

    编译节点使用全局编码 id 调度，但 action 读取结果时仍应看到声明图
    中的局部 step id。该适配器通过 `CompiledStepContext` 恢复两者映射。
    """

    compilation: GraphCompilation
    component_log_path: Path | None = None

    @property
    def label(self) -> str:
        return self.compilation.label

    @property
    def observe(self) -> GraphObserve:
        return self.compilation.observe

    @property
    def source_graph(self) -> Graph:
        return self.compilation.source_graph

    @property
    def steps(self) -> tuple[CompiledStep, ...]:
        return self.compilation.steps

    @property
    def observation_step_ids(self) -> Mapping[Step, StepId]:
        return self.compilation.root_graph_step_ids

    def make_run_context(
        self,
        state: GraphExecutionState,
        runtime_step: RuntimeStep,
    ) -> StepRunContext:
        step = runtime_step.step
        context = step.context
        component_log_path = self.component_log_path
        for composite_id in context.ancestor_composite_ids:
            bound_path = state.get_component_log_path(composite_id)
            if bound_path is not None:
                component_log_path = bound_path
                break
        return StepRunContext(
            view=StepReadContext(
                step_id=step.id,
                label_path=context.label_path,
                graph_step_ids=context.graph_step_ids,
                ancestor_graph_scopes=context.ancestor_graph_scopes,
                results=state.results,
            ),
            control=StepControl(
                step_id=step.id,
                _state=state,
            ),
            component_log_path=component_log_path,
        )

    def child_graph_scope(
        self,
        step: CompiledStep,
        *,
        graph_step_ids: Mapping[Step, StepId],
    ) -> GraphScope:
        del graph_step_ids
        return GraphScope(
            step_ids=step.context.graph_step_ids,
        )
