from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from types import MappingProxyType
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


type CapabilityInputs = Mapping[Capability, Step | tuple[Step, ...]] | Graph
type CapabilityOutputs = Mapping[Capability, Step] | Graph
type CapabilitiesInput = CapabilityInterface | Graph


@dataclass(frozen=True, slots=True, init=False)
class CapabilityInterface:
    """An immutable public interface with explicit input and output forwarding."""

    inputs: Mapping[Capability, tuple[Step, ...]]
    outputs: Mapping[Capability, Step]

    def __init__(
        self,
        *,
        inputs: CapabilityInputs = MappingProxyType({}),
        outputs: CapabilityOutputs = MappingProxyType({}),
    ) -> None:
        from .model import Graph, Step

        input_mapping = (
            inputs.capabilities.inputs if isinstance(inputs, Graph) else inputs
        )
        output_mapping = (
            outputs.capabilities.outputs if isinstance(outputs, Graph) else outputs
        )
        normalized_inputs = {}
        # O(P + R)：P 为端口数，R 为全部输入步骤引用数；复制映射隔离调用方修改。
        for capability, value in input_mapping.items():
            steps = (value,) if isinstance(value, Step) else value
            if not steps:
                raise GraphError(
                    f"capability input must reference at least one step: {capability.name}"
                )
            if len(set(steps)) != len(steps):
                raise GraphError(
                    f"capability input contains duplicate steps: {capability.name}"
                )
            normalized_inputs[capability] = steps
        object.__setattr__(self, "inputs", MappingProxyType(normalized_inputs))
        object.__setattr__(self, "outputs", MappingProxyType(dict(output_mapping)))

    @classmethod
    def resolve(cls, value: CapabilitiesInput) -> CapabilityInterface:
        from .model import Graph

        return value.capabilities if isinstance(value, Graph) else value


@dataclass(frozen=True, slots=True)
class AnyProviderRequirement:
    """A consumer requirement that does not name its concrete provider."""

    consumer: Step
    capability: Capability


@dataclass(frozen=True, slots=True)
class ExactProviderRequirement:
    """A consumer requirement bound by declaration to one concrete provider."""

    consumer: Step
    provider: Step
    capability: Capability


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
        for capability in graph.capabilities.outputs:
            capabilities.setdefault(capability, len(capabilities))
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
            requirement.capability,
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
        [capabilities[capability] for capability in graph.capabilities.outputs]
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
    capability: Capability,
) -> None:
    graph = _require_component_graph(component)
    if capability in graph.capabilities.inputs:
        return
    raise GraphError(
        f"component {_step_label(component)} does not require capability "
        f"{capability.name}"
    )


def _require_capability_output(
    component: Step,
    capability: Capability,
) -> Step:
    graph = _require_component_graph(component)
    if capability in graph.capabilities.outputs:
        return graph.capabilities.outputs[capability]
    raise GraphError(
        f"component {_step_label(component)} does not provide capability "
        f"{capability.name}"
    )


def _step_label(step: Step) -> str:
    return "<unlabeled>" if step.label is None else step.label
