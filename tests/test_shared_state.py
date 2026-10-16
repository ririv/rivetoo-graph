from __future__ import annotations

import threading
from dataclasses import dataclass, field
from typing import Literal, assert_type

import pytest

from rivetoo_graph import (
    AsyncGraphRunOptions,
    Graph,
    GraphExecution,
    GraphMode,
    SerialGraphExecutor,
    Step,
    StepControl,
    StepId,
    StepReadContext,
    StepRunContext,
    ThreadGraphRunOptions,
)
from rivetoo_graph.runtime import GraphExecutionState


@dataclass
class Progress:
    completed: set[str] = field(default_factory=set)
    lock: threading.Lock = field(default_factory=threading.Lock)

    def complete(self, name: str) -> None:
        with self.lock:
            self.completed.add(name)


@pytest.mark.parametrize("mode", ["dynamic", "compiled"])
@pytest.mark.parametrize("executor", ["serial", "threads", "async"])
def test_shared_object_reaches_parallel_and_nested_actions(
    mode: GraphMode, executor: Literal["serial", "threads", "async"]
) -> None:
    def record(ctx: StepRunContext) -> None:
        shared = ctx.shared(Progress)
        assert_type(shared, Progress)
        shared.complete(str(ctx.step_id))

    async def record_async(ctx: StepRunContext) -> None:
        record(ctx)

    def expand(ctx: StepRunContext) -> Graph:
        record(ctx)
        return Graph(steps=(Step(record_async),))

    graph = Graph(
        steps=(
            *(Step(record) for _ in range(12)),
            Step(Graph(steps=(Step(record),))),
            Step(expand),
        ),
    )
    # 同一份声明执行两次，各次显式提供的对象互不污染。
    for _ in range(2):
        progress = Progress()
        execution = GraphExecution(graph=graph, shared=progress)
        match executor:
            case "serial":
                SerialGraphExecutor(shared=progress).execute(execution.compile())
            case "threads":
                execution.run(ThreadGraphRunOptions(workers=4), mode=mode)
            case "async":
                execution.run(AsyncGraphRunOptions(concurrency=4), mode=mode)
        assert progress.completed == {
            *(str(index) for index in range(12)),
            "12.0",
            "13",
            "13.0",
        }
        assert execution.execution_state.results is None


def test_shared_type_check_is_an_explicit_boundary() -> None:
    context = StepRunContext(
        view=StepReadContext(step_id=StepId((0,)), label_path=(), graph_step_ids={}),
        control=StepControl(StepId((0,)), GraphExecutionState()),
        step_log_path=None,
        _shared=Progress(),
    )
    assert_type(context.shared(Progress), Progress)
    with pytest.raises(TypeError, match="expected str, got Progress"):
        context.shared(str)


def test_missing_shared_state_is_not_created_implicitly() -> None:
    context = StepRunContext(
        view=StepReadContext(step_id=StepId((0,)), label_path=(), graph_step_ids={}),
        control=StepControl(StepId((0,)), GraphExecutionState()),
        step_log_path=None,
    )
    with pytest.raises(RuntimeError, match="no shared state was provided"):
        context.shared(Progress)
