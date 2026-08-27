from __future__ import annotations

from threading import Event

import pytest

from rivetoo_graph import (
    AnyProviderRequirement,
    Capability,
    CapabilityInput,
    CapabilityOutput,
    CapabilitySelection,
    ExactProviderRequirement,
    Graph,
    GraphError,
    GraphPlan,
    Step,
    ThreadGraphRunOptions,
    resolve_capability_requirements,
)


def _input_of(component: Step) -> CapabilityInput:
    action = component.action
    assert isinstance(action, Graph)
    return action.capability_inputs[0]


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
        capability_inputs=(
            ()
            if capability_input is None
            else (
                CapabilityInput(
                    capability=capability_input[0],
                    needed_by=capability_input[1],
                ),
            )
        ),
        capability_outputs=(
            ()
            if capability_output is None
            else (
                CapabilityOutput(
                    capability=capability_output[0],
                    ready_after=capability_output[1],
                ),
            )
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
        (AnyProviderRequirement(consumer=consumer, input=_input_of(consumer)),),
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
                    input=_input_of(consumer),
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
        capability_outputs=(CapabilityOutput(capability, ready),),
    )
    consumer_graph = Graph.from_wiring(
        wiring=(Graph.from_wiring(wiring=(needs.wire(),)).step().wire(),),
        capability_inputs=(CapabilityInput(capability, needs),),
    )
    provider = provider_graph.step(label="A")
    consumer = consumer_graph.step(label="B")
    bindings = resolve_capability_requirements(
        (provider, consumer),
        (
            ExactProviderRequirement(
                consumer,
                provider,
                consumer_graph.capability_inputs[0],
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
        input=_input_of(consumer),
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


def test_capability_binding_rejects_dynamic_execution() -> None:
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
            (AnyProviderRequirement(consumer=consumer, input=_input_of(consumer)),),
        ),
    )

    with pytest.raises(GraphError, match="require compiled execution mode"):
        GraphPlan(label="root", graph=graph).run()
