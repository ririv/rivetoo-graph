from __future__ import annotations

import asyncio
from contextvars import ContextVar

import pytest

from rivetoo_graph import (
    Graph,
    GraphExecutionControl,
    GraphExecutionState,
    GraphPlan,
    GraphResultStore,
    SerialGraphExecutor,
    Step,
    StepId,
)
from rivetoo_graph.runtime import ParallelGraphExecutor


def _execute(graph, workers):
    compilation = GraphPlan(graph=graph).compile()
    state = GraphExecutionState(results=GraphResultStore())
    if workers == 0:
        return SerialGraphExecutor(state).execute(compilation)
    return ParallelGraphExecutor(
        state, execution_control=GraphExecutionControl()
    ).execute(compilation, workers=workers)


@pytest.mark.parametrize("workers", [0, 1, 4])
def test_sync_actions_and_returned_graph_do_not_create_event_loop(monkeypatch, workers):
    def unexpected_loop(*args, **kwargs):
        pytest.fail("synchronous action must not create an asyncio event loop")

    monkeypatch.setattr(asyncio, "run", unexpected_loop)
    calls = []

    def make_graph():
        calls.append("declare")
        return Graph(steps=(Step(lambda: calls.append("nested")),))

    steps = (Step(lambda: 42), Step(lambda: None), Step(make_graph))
    state = _execute(Graph(steps=steps), workers)
    assert calls == ["declare", "nested"]
    assert state.results.require(StepId((0,))).value == 42
    assert state.results.require(StepId((1,))).value is None
    assert state.results.require(StepId((2,))).value is None


@pytest.mark.parametrize("workers", [0, 1, 4])
@pytest.mark.parametrize("kind", ["coroutine", "sync-returned-coroutine", "awaitable"])
def test_awaitable_results_remain_supported(workers, kind):
    calls = []

    async def asynchronous():
        calls.append("started")
        await asyncio.sleep(0)
        calls.append("finished")
        return 42

    class Result:
        def __await__(self):
            return asynchronous().__await__()

    match kind:
        case "coroutine":
            action = asynchronous
        case "sync-returned-coroutine":

            def action():
                return asynchronous()
        case "awaitable":

            def action():
                return Result()

    state = _execute(Graph(steps=(Step(action),)), workers)
    assert calls == ["started", "finished"]
    assert state.results.require(StepId((0,))).value == 42


@pytest.mark.parametrize("workers", [0, 1, 4])
def test_awaitable_keeps_action_context_and_propagates_error(workers):
    marker = ContextVar("marker", default="outside")

    async def fail():
        await asyncio.sleep(0)
        assert marker.get() == "action"
        raise RuntimeError("action failed")

    def action():
        marker.set("action")
        return fail()

    token = marker.set("outside")
    try:
        with pytest.raises(RuntimeError, match="action failed"):
            _execute(Graph(steps=(Step(action),)), workers)
    finally:
        marker.reset(token)
