from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from .errors import GraphError

if TYPE_CHECKING:
    from collections.abc import Iterable

    from .model import Graph, Step


@dataclass(frozen=True, slots=True)
class Capability:
    """A named contract that can be required and provided by graph components."""

    name: str

    def __post_init__(self) -> None:
        if not self.name or self.name != self.name.strip():
            raise GraphError("capability name must be non-empty and stripped")


@dataclass(frozen=True, slots=True)
class CapabilityInput:
    """The internal step at which a graph starts requiring a capability."""

    capability: Capability
    needed_by: Step


@dataclass(frozen=True, slots=True)
class CapabilityOutput:
    """The internal step after which a graph provides a capability."""

    capability: Capability
    ready_after: Step


@dataclass(frozen=True, slots=True)
class AnyProviderRequirement:
    """A consumer requirement that does not name its concrete provider."""

    consumer: Step
    input: CapabilityInput

    @property
    def capability(self) -> Capability:
        return self.input.capability


@dataclass(frozen=True, slots=True)
class ExactProviderRequirement:
    """A consumer requirement bound by declaration to one concrete provider."""

    consumer: Step
    provider: Step
    input: CapabilityInput

    @property
    def capability(self) -> Capability:
        return self.input.capability


type CapabilityRequirement = AnyProviderRequirement | ExactProviderRequirement


@dataclass(frozen=True, slots=True)
class CapabilitySelection:
    """A deterministic provider selection for an any-provider requirement."""

    requirement: AnyProviderRequirement
    provider: Step


@dataclass(frozen=True, slots=True)
class CapabilityBinding:
    """A resolved capability edge between two mounted graph components."""

    source: Step
    target: Step
    capability: Capability


def resolve_capability_requirements(
    components: Iterable[Step],
    requirements: Iterable[CapabilityRequirement],
    *,
    selections: Iterable[CapabilitySelection] = (),
) -> tuple[CapabilityBinding, ...]:
    """Resolve symbolic requirements into deterministic provider bindings."""

    mounted_components = tuple(components)
    mounted_set = set(mounted_components)
    providers = _index_capability_providers(mounted_components)
    selection_by_requirement: dict[
        AnyProviderRequirement,
        CapabilitySelection,
    ] = {}
    for selection in selections:
        requirement = selection.requirement
        if requirement in selection_by_requirement:
            raise GraphError(
                "capability requirement has multiple provider selections: "
                f"{_step_label(requirement.consumer)} requires "
                f"{requirement.capability.name}"
            )
        selection_by_requirement[requirement] = selection

    bindings: list[CapabilityBinding] = []
    requirement_kinds: dict[tuple[Step, Capability], type[CapabilityRequirement]] = {}
    seen_any_requirements: set[tuple[Step, Capability]] = set()
    seen_exact_requirements: set[tuple[Step, Capability, Step]] = set()
    used_selections: set[AnyProviderRequirement] = set()
    for requirement in requirements:
        key = (requirement.consumer, requirement.capability)
        requirement_kind = type(requirement)
        previous_kind = requirement_kinds.setdefault(key, requirement_kind)
        if previous_kind is not requirement_kind:
            raise GraphError(
                "capability input mixes any-provider and exact-provider requirements: "
                f"{_step_label(requirement.consumer)} requires "
                f"{requirement.capability.name}"
            )
        if isinstance(requirement, AnyProviderRequirement):
            if key in seen_any_requirements:
                raise GraphError(
                    "capability requirement is declared more than once: "
                    f"{_step_label(requirement.consumer)} requires "
                    f"{requirement.capability.name}"
                )
            seen_any_requirements.add(key)
        if isinstance(requirement, ExactProviderRequirement):
            exact_key = (*key, requirement.provider)
            if exact_key in seen_exact_requirements:
                raise GraphError(
                    "exact capability requirement is declared more than once: "
                    f"{_step_label(requirement.consumer)} requires "
                    f"{requirement.capability.name} from "
                    f"{_step_label(requirement.provider)}"
                )
            seen_exact_requirements.add(exact_key)
        _require_mounted_component(
            requirement.consumer,
            mounted_set,
            role="consumer",
        )
        _require_declared_capability_input(
            requirement.consumer,
            requirement.input,
        )

        match requirement:
            case ExactProviderRequirement(provider=provider):
                selected_provider = provider
            case AnyProviderRequirement():
                candidates = providers.get(requirement.capability, ())
                selection = selection_by_requirement.get(requirement)
                if selection is not None:
                    used_selections.add(requirement)
                    selected_provider = selection.provider
                    if selected_provider not in candidates:
                        raise GraphError(
                            "selected component does not provide capability: "
                            f"{_step_label(selected_provider)} does not provide "
                            f"{requirement.capability.name}"
                        )
                elif len(candidates) == 1:
                    selected_provider = candidates[0]
                elif not candidates:
                    raise GraphError(
                        "capability has no provider: "
                        f"{_step_label(requirement.consumer)} requires "
                        f"{requirement.capability.name}"
                    )
                else:
                    candidate_labels = ", ".join(
                        _step_label(candidate) for candidate in candidates
                    )
                    raise GraphError(
                        "capability provider is ambiguous: "
                        f"{_step_label(requirement.consumer)} requires "
                        f"{requirement.capability.name}; candidates: "
                        f"{candidate_labels}"
                    )

        _require_mounted_component(
            selected_provider,
            mounted_set,
            role="provider",
        )
        _require_capability_output(
            selected_provider,
            requirement.capability,
        )
        bindings.append(
            CapabilityBinding(
                source=selected_provider,
                target=requirement.consumer,
                capability=requirement.capability,
            )
        )

    unused_selections = set(selection_by_requirement) - used_selections
    if unused_selections:
        requirement = next(iter(unused_selections))
        raise GraphError(
            "provider selection has no matching any-provider requirement: "
            f"{_step_label(requirement.consumer)} requires "
            f"{requirement.capability.name}"
        )
    return tuple(bindings)


def _index_capability_providers(
    components: tuple[Step, ...],
) -> dict[Capability, tuple[Step, ...]]:
    mutable: dict[Capability, list[Step]] = {}
    for component in components:
        graph = _require_component_graph(component)
        for output in graph.capability_outputs:
            mutable.setdefault(output.capability, []).append(component)
    return {capability: tuple(providers) for capability, providers in mutable.items()}


def _require_mounted_component(
    component: Step,
    mounted_components: set[Step],
    *,
    role: str,
) -> None:
    if component not in mounted_components:
        raise GraphError(
            f"capability {role} is not mounted in this composition: "
            f"{_step_label(component)}"
        )


def _require_component_graph(component: Step) -> Graph:
    from .model import Graph

    if not isinstance(component.action, Graph):
        raise GraphError(
            "capability component must be a graph-valued step: "
            f"{_step_label(component)}"
        )
    return component.action


def _require_declared_capability_input(
    component: Step,
    required_input: CapabilityInput,
) -> CapabilityInput:
    graph = _require_component_graph(component)
    for input_port in graph.capability_inputs:
        if input_port is required_input:
            return input_port
    raise GraphError(
        f"component {_step_label(component)} does not require capability "
        f"{required_input.capability.name} through the referenced input"
    )


def _require_capability_output(
    component: Step,
    capability: Capability,
) -> CapabilityOutput:
    graph = _require_component_graph(component)
    for output_port in graph.capability_outputs:
        if output_port.capability == capability:
            return output_port
    raise GraphError(
        f"component {_step_label(component)} does not provide capability "
        f"{capability.name}"
    )


def _step_label(step: Step) -> str:
    return "<unlabeled>" if step.label is None else step.label
