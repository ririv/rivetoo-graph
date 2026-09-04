from __future__ import annotations

from threading import Event

import pytest

from rivetoo_graph import (
    AnyProviderRequirement,
    Capability,
    CapabilityInterface,
    CapabilitySelection,
    ExactProviderRequirement,
    Graph,
    GraphError,
    GraphMode,
    GraphPlan,
    Step,
    ThreadGraphRunOptions,
    resolve_capability_requirements,
)


def _input_of(component: Step) -> Capability:
    action = component.action
    assert isinstance(action, Graph)
    return next(iter(action.capabilities.inputs))


def test_capability_interface_copies_mappings_and_reuses_normalized_inputs() -> None:
    sdk = Capability("sdk")
    library = Capability("library")
    configure = Step(lambda: None)
    install = Step(lambda: None)
    inputs = {sdk: configure}
    outputs = {library: install}
    shared = CapabilityInterface(inputs=inputs, outputs=outputs)
    inputs.clear()
    outputs.clear()
    assert dict(shared.inputs) == {sdk: (configure,)}
    assert dict(shared.outputs) == {library: install}
    reused = CapabilityInterface(inputs=shared.inputs)
    assert reused.inputs == shared.inputs
    assert not reused.outputs


def test_graph_forwards_complete_or_selected_interface_without_flattening() -> None:
    sdk = Capability("sdk")
    library = Capability("library")
    final = Capability("final")
    configure = Step(lambda: None)
    install = Step(lambda: None)
    finish = Step(lambda: None)
    child = (configure >> install).graph(
        capabilities=CapabilityInterface(
            inputs={sdk: configure}, outputs={library: install}
        )
    )
    forwarded = (child >> finish).graph(capabilities=child)
    assert forwarded.capabilities is child.capabilities
    assert forwarded.steps[0].action is child
    custom = (child >> finish).graph(
        capabilities=CapabilityInterface(inputs=child, outputs={final: finish})
    )
    assert custom.capabilities.inputs == child.capabilities.inputs
    assert dict(custom.capabilities.outputs) == {final: finish}
    outputs_only = Graph(
        steps=(child.step(),), capabilities=CapabilityInterface(outputs=child)
    )
    assert not outputs_only.capabilities.inputs
    assert outputs_only.capabilities.outputs == child.capabilities.outputs
    # 不声明接口时，不自动暴露内部图的端口。
    assert not (child >> finish).graph().capabilities.inputs
    assert not (child >> finish).graph().capabilities.outputs


@pytest.mark.parametrize("mode", ("compiled", "dynamic"))
def test_one_input_unlocks_multiple_independent_steps(mode: GraphMode) -> None:
    sdk = Capability("sdk")
    a_prepared, b_prepared, a_started, b_started, sdk_ready = (
        Event() for _ in range(5)
    )

    def provide() -> None:
        _require_event(a_prepared, "a preparation did not run before SDK")
        _require_event(b_prepared, "b preparation did not run before SDK")
        sdk_ready.set()

    def configure_a() -> None:
        assert sdk_ready.is_set()
        a_started.set()
        _require_event(b_started, "b input was serialized after a")

    def configure_b() -> None:
        assert sdk_ready.is_set()
        b_started.set()
        _require_event(a_started, "a input was serialized after b")

    ready = Step(provide)
    provider = Graph(
        steps=(ready,), capabilities=CapabilityInterface(outputs={sdk: ready})
    ).step()
    a, b = Step(configure_a), Step(configure_b)
    consumer = (
        ((Step(a_prepared.set) >> a) | (Step(b_prepared.set) >> b))
        .graph(capabilities=CapabilityInterface(inputs={sdk: (a, b)}))
        .step()
    )
    bindings = resolve_capability_requirements(
        (provider, consumer),
        (
            ExactProviderRequirement(
                consumer=consumer, provider=provider, capability=sdk
            ),
        ),
    )
    GraphPlan(
        label="root", graph=Graph(steps=(provider, consumer), edges=bindings)
    ).run(ThreadGraphRunOptions(workers=4), mode=mode)
    assert a_started.is_set() and b_started.is_set()


def test_interface_rejects_empty_input_and_unmounted_or_repeated_steps() -> None:
    sdk = Capability("sdk")
    step = Step(lambda: None)
    with pytest.raises(GraphError, match="at least one"):
        CapabilityInterface(inputs={sdk: ()})
    with pytest.raises(GraphError, match="duplicate steps"):
        CapabilityInterface(inputs={sdk: (step, step)})
    with pytest.raises(GraphError, match="outside this graph"):
        Graph(capabilities=CapabilityInterface(inputs={sdk: step}))
    child = Graph(steps=(step,))
    with pytest.raises(GraphError, match="mounted more than once"):
        Graph(
            steps=(child.step(), child.step()),
            capabilities=CapabilityInterface(inputs={sdk: step}),
        )


def _require_event(event: Event, message: str) -> None:
    if not event.wait(timeout=2.0):
        raise AssertionError(message)


def _component(
    label: str,
    *,
    capability_input: tuple[Capability, Step] | None = None,
    capability_output: tuple[Capability, Step] | None = None,
    steps: tuple[Step, ...],
) -> Step:
    graph = Graph.from_wiring(
        tuple(
            step.wire(after=() if index == 0 else (steps[index - 1],))
            for index, step in enumerate(steps)
        ),
        label=label,
        capabilities=CapabilityInterface(
            inputs=dict(
                ()
                if capability_input is None
                else ((capability_input[0], capability_input[1]),)
            ),
            outputs=dict(
                ()
                if capability_output is None
                else ((capability_output[0], capability_output[1]),)
            ),
        ),
    )
    return graph.step(label=label)


def test_capability_binding_unlocks_only_the_consumer_input() -> None:
    capability = Capability("123")
    consumer_prepared = Event()
    consumer_started = Event()

    provider_steps = (
        Step(lambda: None, label="1"),
        Step(lambda: None, label="2"),
        Step(
            lambda: _require_event(consumer_prepared, "consumer did not prepare"),
            label="3",
        ),
        Step(
            lambda: _require_event(consumer_started, "consumer did not start"),
            label="4",
        ),
        Step(lambda: None, label="5"),
    )
    consumer_steps = (
        Step(consumer_prepared.set, label="a"),
        Step(consumer_started.set, label="b"),
        Step(lambda: None, label="c"),
        Step(lambda: None, label="d"),
        Step(lambda: None, label="e"),
    )
    provider = _component(
        "A",
        capability_output=(capability, provider_steps[2]),
        steps=provider_steps,
    )
    consumer = _component(
        "B",
        capability_input=(capability, consumer_steps[1]),
        steps=consumer_steps,
    )
    bindings = resolve_capability_requirements(
        (provider, consumer),
        (AnyProviderRequirement(consumer=consumer, capability=_input_of(consumer)),),
    )
    graph = Graph(
        label="capability",
        steps=(provider, consumer),
        edges=bindings,
    )

    GraphPlan(label="root", graph=graph).run(
        ThreadGraphRunOptions(workers=2),
        mode="compiled",
    )

    assert consumer_prepared.is_set()
    assert consumer_started.is_set()


def test_exact_provider_requirement_selects_the_named_provider() -> None:
    capability = Capability("123")
    a_ready = Step(lambda: None, label="ready")
    c_ready = Step(lambda: None, label="ready")
    b_needs = Step(lambda: None, label="needs")
    provider_a = _component(
        "A",
        capability_output=(capability, a_ready),
        steps=(a_ready,),
    )
    provider_c = _component(
        "C",
        capability_output=(capability, c_ready),
        steps=(c_ready,),
    )
    consumer = _component(
        "B",
        capability_input=(capability, b_needs),
        steps=(b_needs,),
    )

    assert (
        resolve_capability_requirements(
            (provider_a, provider_c, consumer),
            (
                ExactProviderRequirement(
                    consumer=consumer,
                    provider=provider_a,
                    capability=_input_of(consumer),
                ),
            ),
        )[0].source
        is provider_a
    )


def test_exact_requirements_allow_multiple_providers_for_one_input() -> None:
    capability = Capability("123")
    a_ready = Step(lambda: None, label="ready")
    c_ready = Step(lambda: None, label="ready")
    b_needs = Step(lambda: None, label="needs")
    provider_a = _component(
        "A",
        capability_output=(capability, a_ready),
        steps=(a_ready,),
    )
    provider_c = _component(
        "C",
        capability_output=(capability, c_ready),
        steps=(c_ready,),
    )
    consumer = _component(
        "B",
        capability_input=(capability, b_needs),
        steps=(b_needs,),
    )
    input_port = _input_of(consumer)

    bindings = resolve_capability_requirements(
        (provider_a, provider_c, consumer),
        (
            ExactProviderRequirement(consumer, provider_a, input_port),
            ExactProviderRequirement(consumer, provider_c, input_port),
        ),
    )

    assert tuple(binding.source for binding in bindings) == (
        provider_a,
        provider_c,
    )


def test_capability_port_can_explicitly_export_descendant_step() -> None:
    capability = Capability("123")
    ready = Step(lambda: None, label="ready")
    needs = Step(lambda: None, label="needs")
    provider_graph = Graph.from_wiring(
        wiring=(Graph.from_wiring(wiring=(ready.wire(),)).step().wire(),),
        capabilities=CapabilityInterface(outputs={capability: ready}),
    )
    consumer_graph = Graph.from_wiring(
        wiring=(Graph.from_wiring(wiring=(needs.wire(),)).step().wire(),),
        capabilities=CapabilityInterface(inputs={capability: needs}),
    )
    provider = provider_graph.step(label="A")
    consumer = consumer_graph.step(label="B")
    bindings = resolve_capability_requirements(
        (provider, consumer),
        (
            ExactProviderRequirement(
                consumer,
                provider,
                capability,
            ),
        ),
    )

    GraphPlan(
        label="root",
        graph=Graph(steps=(provider, consumer), edges=bindings),
    ).run(mode="compiled")


def test_any_provider_requirement_uses_explicit_selection_when_ambiguous() -> None:
    capability = Capability("123")
    a_ready = Step(lambda: None)
    c_ready = Step(lambda: None)
    b_needs = Step(lambda: None)
    provider_a = _component(
        "A",
        capability_output=(capability, a_ready),
        steps=(a_ready,),
    )
    provider_c = _component(
        "C",
        capability_output=(capability, c_ready),
        steps=(c_ready,),
    )
    consumer = _component(
        "B",
        capability_input=(capability, b_needs),
        steps=(b_needs,),
    )
    requirement = AnyProviderRequirement(
        consumer=consumer,
        capability=_input_of(consumer),
    )

    with pytest.raises(GraphError, match="provider is ambiguous"):
        resolve_capability_requirements(
            (provider_a, provider_c, consumer),
            (requirement,),
        )

    assert (
        resolve_capability_requirements(
            (provider_a, provider_c, consumer),
            (requirement,),
            selections=(
                CapabilitySelection(
                    requirement=requirement,
                    provider=provider_c,
                ),
            ),
        )[0].source
        is provider_c
    )


def test_capability_binding_uses_unified_execution_path() -> None:
    capability = Capability("123")
    ready = Step(lambda: None)
    needs = Step(lambda: None)
    provider = _component(
        "A",
        capability_output=(capability, ready),
        steps=(ready,),
    )
    consumer = _component(
        "B",
        capability_input=(capability, needs),
        steps=(needs,),
    )
    graph = Graph(
        steps=(provider, consumer),
        edges=resolve_capability_requirements(
            (provider, consumer),
            (
                AnyProviderRequirement(
                    consumer=consumer, capability=_input_of(consumer)
                ),
            ),
        ),
    )

    GraphPlan(label="root", graph=graph).run()
