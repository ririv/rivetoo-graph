from __future__ import annotations

from typing import Literal

import pytest

from rivetoo_graph import (
    AsyncGraphRunOptions,
    ConditionalPrerequisite,
    Graph,
    GraphExecution,
    GraphExecutionState,
    Nothing,
    Option,
    SerialGraphExecutor,
    Some,
    Step,
    StepRunContext,
    ThreadGraphRunOptions,
    compile_graph,
    runtime_condition,
)

type Backend = Literal["serial", "thread", "async"]


def execute(graph: Graph, backend: Backend) -> None:
    if backend == "serial":
        SerialGraphExecutor(GraphExecutionState()).execute(
            compile_graph(graph, root_label="root")
        )
    else:
        options = (
            ThreadGraphRunOptions(workers=2)
            if backend == "thread"
            else AsyncGraphRunOptions(concurrency=2)
        )
        GraphExecution(graph).run(options)


def test_option_preserves_none_and_false_values() -> None:
    assert Some(None).is_some()
    assert not Some(None).is_none()
    assert Some(None).unwrap() is None
    assert Some(None).unwrap_or("default") is None
    assert Some(False).unwrap_or(True) is False
    assert Nothing().is_none()
    assert not Nothing().is_some()
    assert Nothing().unwrap_or("default") == "default"
    with pytest.raises(ValueError, match="absent result"):
        Nothing().unwrap()
    for option in (Some(False), Nothing()):
        with pytest.raises(TypeError, match="explicitly"):
            bool(option)


@pytest.mark.parametrize("backend", ["serial", "thread", "async"])
@pytest.mark.parametrize("edge", [False, True])
def test_inputs_distinguish_skipped_from_none_in_declared_order(
    backend: Backend, edge: bool
) -> None:
    seen: list[tuple[Option[object], ...]] = []
    calls: list[str] = []
    skipped = Step(lambda: "unreachable", when=runtime_condition(when=lambda: False))
    returned_none = Step(lambda: None)
    returned_false = Step(lambda: False)

    def condition(
        missing: Option[object], present: Option[object], false: Option[object]
    ) -> bool:
        seen.append((missing, present, false))
        return (
            missing.is_none() and present.unwrap() is None and false.unwrap() is False
        )

    predicate = runtime_condition(
        inputs=(skipped, returned_none, returned_false), when=condition
    )
    prerequisite = Step(lambda: calls.append("prerequisite"))
    consumer = Step(lambda: calls.append("consumer"), when=None if edge else predicate)
    graph = Graph.from_wiring(
        (
            consumer.wire(
                after=(ConditionalPrerequisite(prerequisite, condition=predicate),)
                if edge
                else ()
            ),
            prerequisite.wire(),
            returned_false.wire(),
            returned_none.wire(),
            skipped.wire(),
        )
    )
    execute(graph, backend)
    assert seen == [(Nothing(), Some(None), Some(False))]
    assert "consumer" in calls
    if edge:
        assert calls == ["prerequisite", "consumer"]


@pytest.mark.parametrize("backend", ["serial", "thread", "async"])
def test_repeated_nested_graph_mounts_supply_their_own_results(
    backend: Backend,
) -> None:
    seen: list[object] = []

    def produce(ctx: StepRunContext) -> object:
        return ctx.step_id

    probe = Step(produce, label="probe")

    def condition(result: Option[object]) -> bool:
        seen.append(result.unwrap())
        return True

    consumer = Step(
        lambda: None, when=runtime_condition(inputs=(probe,), when=condition)
    )
    child = Graph(steps=(consumer, probe))
    nested = Graph(steps=(Step(child),))
    execute(Graph(steps=(Step(nested), Step(nested))), backend)
    assert len(seen) == 2
    assert seen[0] != seen[1]


def test_repeated_input_preserves_arguments_without_rewrapping() -> None:
    probe = Step(lambda: None)
    seen: list[tuple[Option[object], Option[object]]] = []

    def condition(first: Option[object], second: Option[object]) -> bool:
        seen.append((first, second))
        return True

    consumer = Step(
        lambda: None,
        when=runtime_condition(inputs=(probe, probe), when=condition),
    )
    execution = GraphExecution(Graph(steps=(consumer, probe)))
    execution.run()
    assert len(seen) == 1
    first, second = seen[0]
    assert first is second
    assert first is execution.require_result(probe)
    assert first.unwrap() is None
