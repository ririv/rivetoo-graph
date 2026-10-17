from __future__ import annotations

import pytest

from rivetoo_graph import (
    Capability,
    CapabilityInterface,
    ConditionalEdge,
    ExactProviderRequirement,
    Graph,
    GraphError,
    GraphExecution,
    GraphMode,
    StaticCondition,
    StaticConditions,
    Step,
    ThreadGraphRunOptions,
    UnconditionalEdge,
    compile_graph,
    render_graph,
    resolve_capability_requirements,
    runtime_condition,
)

ENABLED = StaticCondition(payload=True, description="enabled")
DISABLED = StaticCondition(payload=False, description="disabled")


def evaluate(condition: StaticCondition) -> bool:
    return condition.payload is True


def test_static_dsl_and_direct_declaration_compile_identically() -> None:
    first = Step(lambda: None, label="first")
    last = Step(lambda: None, label="last")
    static = StaticConditions(evaluator=evaluate)
    dsl = (static.when(ENABLED, first) >> last).graph()
    direct = Graph(
        steps=(first, last),
        edges=(UnconditionalEdge(first, last),),
        static=StaticConditions(evaluator=evaluate, conditions={first: ENABLED}),
    )

    assert compile_graph(dsl, root_label="root") == compile_graph(
        direct, root_label="root"
    )


@pytest.mark.parametrize("enabled", [True, False])
@pytest.mark.parametrize("mode", ["dynamic", "compiled"])
def test_static_edge_keeps_both_actions(enabled: bool, mode: GraphMode) -> None:
    calls: list[str] = []
    first = Step(lambda: calls.append("first"))
    last = Step(lambda: calls.append("last"))
    graph = (
        StaticConditions(evaluator=evaluate)
        .edge(first, last, when=ENABLED if enabled else DISABLED)
        .graph()
    )

    compiled = compile_graph(graph, root_label="root")
    assert bool(compiled.steps[1].after) is enabled
    GraphExecution(graph).run(options=ThreadGraphRunOptions(), mode=mode)
    assert sorted(calls) == ["first", "last"]


def test_static_scopes_compose_without_replacing_evaluators() -> None:
    left = Step(lambda: None, label="left")
    right = Step(lambda: None, label="right")
    yes = StaticConditions(evaluator=lambda _condition: True)
    no = StaticConditions(evaluator=lambda _condition: False)
    graph = (yes.when(ENABLED, left) | no.when(ENABLED, right)).graph()

    assert len(compile_graph(graph, root_label="root").steps) == 1
    assert graph.steps == (left, right)


def test_inactive_subgraph_does_not_evaluate_nested_static_rules() -> None:
    def unexpected(_condition: StaticCondition) -> bool:
        pytest.fail("inactive subgraph evaluator must not run")

    child = (
        StaticConditions(evaluator=unexpected)
        .when(ENABLED, Step(lambda: pytest.fail("inactive action must not run")))
        .graph()
    )
    graph = StaticConditions(evaluator=evaluate).when(DISABLED, child).graph()

    assert not compile_graph(graph, root_label="root").steps
    GraphExecution(graph).run(options=ThreadGraphRunOptions())


@pytest.mark.parametrize("mode", ["dynamic", "compiled"])
def test_static_and_runtime_rules_coexist_on_nodes_and_edges(mode: GraphMode) -> None:
    calls: list[str] = []
    first = Step(lambda: calls.append("first"))
    last = Step(
        lambda: calls.append("last"),
        when=runtime_condition(when=lambda: False),
    )
    graph = Graph(
        steps=(first, last),
        edges=(ConditionalEdge(first, last, runtime_condition(when=lambda: True)),),
        static=StaticConditions(
            evaluator=evaluate,
            conditions={last: ENABLED, (first, last): ENABLED},
        ),
    )

    compiled = compile_graph(graph, root_label="root")
    assert compiled.steps[1].when is not None
    assert compiled.steps[1].after
    GraphExecution(graph).run(options=ThreadGraphRunOptions(), mode=mode)
    assert calls == ["first"]


def test_declaration_render_keeps_conditions_without_evaluation() -> None:
    def unexpected(_condition: StaticCondition) -> bool:
        pytest.fail("declaration rendering must not evaluate conditions")

    first = Step(lambda: None, label="first")
    last = Step(lambda: None, label="last")
    conditions: dict[Step | tuple[Step, Step], StaticCondition] = {first: ENABLED}
    static = StaticConditions(evaluator=unexpected, conditions=conditions)
    conditions.clear()
    graph = Graph(
        steps=(first, last),
        edges=(UnconditionalEdge(first, last),),
        static=(
            static,
            StaticConditions(
                evaluator=unexpected, conditions={(first, last): DISABLED}
            ),
        ),
    )

    rendered = render_graph(graph, root_label="root")
    assert "[static: enabled]" in rendered
    assert "(static: disabled)" in rendered


def test_static_rules_validate_graph_membership_and_reject_overrides() -> None:
    first = Step(lambda: None)
    last = Step(lambda: None)
    static = StaticConditions(evaluator=evaluate, conditions={first: ENABLED})
    with pytest.raises(GraphError, match="outside this graph"):
        Graph(steps=(last,), static=static)
    with pytest.raises(GraphError, match="unknown graph edge"):
        Graph(
            steps=(first, last),
            static=StaticConditions(
                evaluator=evaluate, conditions={(first, last): ENABLED}
            ),
        )
    with pytest.raises(GraphError, match="already declared"):
        Graph(steps=(first,), static=(static, static))


def test_static_pruning_does_not_silently_drop_required_capability() -> None:
    ready = Capability("ready")
    produce = Step(lambda: None)
    consume = Step(lambda: None)
    provider = Graph(
        steps=(produce,), capabilities=CapabilityInterface(outputs={ready: produce})
    ).step()
    consumer = Graph(
        steps=(consume,), capabilities=CapabilityInterface(inputs={ready: consume})
    ).step()
    bindings = resolve_capability_requirements(
        (provider, consumer),
        (
            ExactProviderRequirement(
                consumer=consumer, provider=provider, capability=ready
            ),
        ),
    )
    graph = Graph(
        steps=(provider, consumer),
        edges=bindings,
        static=StaticConditions(evaluator=evaluate, conditions={provider: DISABLED}),
    )

    with pytest.raises(GraphError, match="inactive provider"):
        compile_graph(graph, root_label="root")


def test_static_pruning_does_not_leave_runtime_reads_without_a_source() -> None:
    probe = Step(lambda: True)
    consumer = Step(
        lambda: None,
        when=runtime_condition(
            inputs=(probe,), when=lambda result: bool(result.unwrap())
        ),
    )
    graph = Graph(
        steps=(probe, consumer),
        static=StaticConditions(evaluator=evaluate, conditions={probe: DISABLED}),
    )

    with pytest.raises(GraphError, match="inactive"):
        compile_graph(graph, root_label="root")
