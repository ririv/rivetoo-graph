from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable, Generator
from contextvars import ContextVar
from typing import Literal, Never

import pytest

from rivetoo_graph import (
    Graph,
    GraphExecution,
    GraphExecutionControl,
    GraphExecutionState,
    GraphResultStore,
    SerialGraphExecutor,
    Step,
    StepId,
)
from rivetoo_graph.runtime import ParallelGraphExecutor


def _execute(graph: Graph, workers: int) -> GraphResultStore:
    compilation = GraphExecution(graph=graph).compile()
    results = GraphResultStore()
    state = GraphExecutionState(results=results)
    if workers == 0:
        SerialGraphExecutor(state).execute(compilation)
    else:
        ParallelGraphExecutor(state, execution_control=GraphExecutionControl()).execute(
            compilation, workers=workers
        )
    return results


@pytest.mark.parametrize("workers", [0, 1, 4])
def test_sync_actions_and_returned_graph_do_not_create_event_loop(
    monkeypatch: pytest.MonkeyPatch, workers: int
) -> None:
    def unexpected_loop(*args: object, **kwargs: object) -> Never:
        pytest.fail("synchronous action must not create an asyncio event loop")

    monkeypatch.setattr(asyncio, "run", unexpected_loop)
    calls: list[str] = []

    def make_graph() -> Graph:
        calls.append("declare")
        return Graph(steps=(Step(lambda: calls.append("nested")),))

    steps = (Step(lambda: 42), Step(lambda: None), Step(make_graph))
    results = _execute(Graph(steps=steps), workers)
    assert calls == ["declare", "nested"]
    assert results.require(StepId((0,))).value == 42
    assert results.require(StepId((1,))).value is None
    assert results.require(StepId((2,))).value is None


@pytest.mark.parametrize("workers", [0, 1, 4])
@pytest.mark.parametrize("kind", ["coroutine", "sync-returned-coroutine", "awaitable"])
def test_awaitable_results_remain_supported(
    workers: int, kind: Literal["coroutine", "sync-returned-coroutine", "awaitable"]
) -> None:
    calls: list[str] = []

    async def asynchronous() -> int:
        calls.append("started")
        await asyncio.sleep(0)
        calls.append("finished")
        return 42

    class Result:
        def __await__(self) -> Generator[object, None, int]:
            return asynchronous().__await__()

    def sync_returned_coroutine() -> Awaitable[int]:
        return asynchronous()

    action: Callable[[], Awaitable[int]]
    match kind:
        case "coroutine":
            action = asynchronous
        case "sync-returned-coroutine":
            action = sync_returned_coroutine
        case "awaitable":
            action = Result

    results = _execute(Graph(steps=(Step(action),)), workers)
    assert calls == ["started", "finished"]
    assert results.require(StepId((0,))).value == 42


@pytest.mark.parametrize("workers", [0, 1, 4])
def test_awaitable_keeps_action_context_and_propagates_error(workers: int) -> None:
    marker = ContextVar("marker", default="outside")

    async def fail() -> None:
        await asyncio.sleep(0)
        assert marker.get() == "action"
        raise RuntimeError("action failed")

    def action() -> Awaitable[None]:
        marker.set("action")
        return fail()

    token = marker.set("outside")
    try:
        with pytest.raises(RuntimeError, match="action failed"):
            _execute(Graph(steps=(Step(action),)), workers)
    finally:
        marker.reset(token)
