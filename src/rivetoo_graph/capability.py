from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from ._native import resolve_capability_bindings
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
    component_indexes = {
        component: index for index, component in enumerate(mounted_components)
    }
    component_graphs = tuple(
        _require_component_graph(component) for component in mounted_components
    )
    declared_requirements = tuple(requirements)
    declared_selections = tuple(selections)

    capabilities: dict[Capability, int] = {}
    for graph in component_graphs:
        for output in graph.capability_outputs:
            capabilities.setdefault(output.capability, len(capabilities))
    for requirement in declared_requirements:
        capabilities.setdefault(requirement.capability, len(capabilities))

    any_requirements: list[tuple[int, int, int]] = []
    exact_requirements: list[tuple[int, int, int, int]] = []
    for index, requirement in enumerate(declared_requirements):
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
                _require_mounted_component(provider, mounted_set, role="provider")
                _require_capability_output(provider, requirement.capability)
                exact_requirements.append(
                    (
                        index,
                        component_indexes[requirement.consumer],
                        capabilities[requirement.capability],
                        component_indexes[provider],
                    )
                )
            case AnyProviderRequirement():
                any_requirements.append(
                    (
                        index,
                        component_indexes[requirement.consumer],
                        capabilities[requirement.capability],
                    )
                )

    native_selections = [
        (
            _requirement_index(declared_requirements, selection.requirement),
            component_indexes.get(selection.provider, len(mounted_components)),
        )
        for selection in declared_selections
    ]
    component_outputs = [
        [capabilities[output.capability] for output in graph.capability_outputs]
        for graph in component_graphs
    ]
    try:
        resolved = resolve_capability_bindings(
            component_outputs,
            len(declared_requirements),
            any_requirements,
            exact_requirements,
            native_selections,
        )
    except ValueError as error:
        raise GraphError(str(error)) from error

    capabilities_by_index = tuple(capabilities)
    return tuple(
        CapabilityBinding(
            source=mounted_components[provider],
            target=mounted_components[consumer],
            capability=capabilities_by_index[capability],
        )
        for provider, consumer, capability in resolved
    )


def _requirement_index(
    requirements: tuple[CapabilityRequirement, ...],
    selected: AnyProviderRequirement,
) -> int:
    for index, requirement in enumerate(requirements):
        if requirement == selected:
            return index
    # 交由 Rust 核心统一报告未知 selection；这里不复制结构校验。
    return len(requirements)


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
