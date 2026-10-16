from __future__ import annotations

import json
import os
import subprocess
import sys

import pytest

from rivetoo_graph import (
    AsyncGraphRunOptions,
    Capability,
    CapabilityInterface,
    ExactProviderRequirement,
    Graph,
    GraphExecution,
    GraphExecutionState,
    GraphResultStore,
    Step,
    StepExecutionEvent,
    StepExecutionPhase,
    StepRunContext,
    ThreadGraphRunOptions,
    resolve_capability_requirements,
)
from rivetoo_graph._native import _native_process_graph


@pytest.mark.parametrize("asynchronous", [False, True])
def test_native_and_python_actions_share_dynamic_budget(tmp_path, asynchronous) -> None:
    pytest.importorskip("fcntl")
    state_path = tmp_path / "activity.json"
    state_path.write_text(json.dumps({"active": 0, "peak": 0, "completed": 0}))
    script = """
import fcntl, json, sys, time
path = sys.argv[1]
def change(delta):
    with open(path, 'r+') as file:
        fcntl.flock(file, fcntl.LOCK_EX)
        state = json.load(file)
        state['active'] += delta
        state['peak'] = max(state['peak'], state['active'])
        if delta < 0:
            state['completed'] += 1
        file.seek(0)
        file.truncate()
        json.dump(state, file)
change(1)
time.sleep(0.02)
change(-1)
"""
    command = [sys.executable, "-c", script, str(state_path)]

    def python_action() -> None:
        subprocess.run(command, check=True)

    def branch() -> Graph:
        native = _native_process_graph(
            "native",
            [("process", command[0], command[1:], str(tmp_path), dict(os.environ))],
            [],
        )
        return Graph(steps=(Step(native), Step(python_action), Step(python_action)))

    graph = Graph(steps=tuple(Step(branch) for _ in range(4)))
    events: list[StepExecutionEvent] = []

    class Sink:
        def emit(self, event: StepExecutionEvent) -> None:
            events.append(event)

    GraphExecution(graph, events=Sink()).run(
        AsyncGraphRunOptions(concurrency=2)
        if asynchronous
        else ThreadGraphRunOptions(workers=2)
    )
    state = json.loads(state_path.read_text())
    assert state["completed"] == 12
    assert state["active"] == 0
    assert state["peak"] <= 2
    native_events = [event for event in events if event.label_path[-1] == "process"]
    assert (
        sum(event.phase is StepExecutionPhase.STARTED for event in native_events) == 4
    )
    assert (
        sum(event.phase is StepExecutionPhase.COMPLETED for event in native_events) == 4
    )


@pytest.mark.parametrize("asynchronous", [False, True])
def test_runtime_child_preserves_capability_ports_and_ancestor_results(
    asynchronous,
) -> None:
    calls = []
    prepared = Step(lambda: "prepared", label="prepared")
    capability = Capability("ready")

    def expand(ctx: StepRunContext) -> Graph:
        source = ctx.require_value(prepared)
        ready = Step(lambda: calls.append(source), label="ready")
        provider = Graph(
            steps=(ready,),
            capabilities=CapabilityInterface(outputs={capability: ready}),
        ).step()

        def consume() -> None:
            assert calls == ["prepared"]
            calls.append("consumed")

        use = Step(consume, label="consume")
        consumer = Graph(
            steps=(use,),
            capabilities=CapabilityInterface(inputs={capability: use}),
        ).step()
        return Graph(
            steps=(consumer, provider),
            edges=resolve_capability_requirements(
                (provider, consumer),
                (ExactProviderRequirement(consumer, provider, capability),),
            ),
        )

    graph = (prepared >> Step(expand, label="expand")).graph()
    GraphExecution(
        graph, execution_state=GraphExecutionState(results=GraphResultStore())
    ).run(
        AsyncGraphRunOptions(concurrency=1)
        if asynchronous
        else ThreadGraphRunOptions(workers=1)
    )
    assert calls == ["prepared", "consumed"]
