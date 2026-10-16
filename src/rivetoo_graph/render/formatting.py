from __future__ import annotations

import json
from typing import TYPE_CHECKING

from ..capability import CapabilityBinding
from ..compile import (
    CompiledComposite,
    CompiledCompositeRef,
    CompiledConditionalDependency,
    CompiledLeaf,
    CompiledPrerequisite,
    CompiledRuntimeCondition,
    CompiledStepRef,
    GraphCompilation,
    compile_graph,
)
from ..model import (
    ConditionalEdge,
    ConditionalPrerequisite,
    Graph,
    GraphEdge,
    RuntimeCondition,
    Step,
    StepId,
    StepPrerequisite,
    resolve_step_label,
)
from ..static_condition import StaticCondition

if TYPE_CHECKING:
    from collections.abc import Mapping

    from ..results import GraphResultView


def ensure_compilation(
    subject: Graph | GraphCompilation,
    *,
    root_label: str,
) -> GraphCompilation:
    if isinstance(subject, GraphCompilation):
        return subject
    return compile_graph(subject, root_label=root_label)


def render_step_label(
    step: Step | CompiledLeaf,
    *,
    step_id: StepId | None = None,
) -> str:
    if isinstance(step, Step):
        if step_id is None:
            raise ValueError("declaration step label requires its mounted step id")
        return resolve_step_label(step, step_id)
    return step.context.label_path[-1]


def render_compiled_step_label(step: CompiledLeaf, path: tuple[str, ...]) -> str:
    del step
    return render_step_path(path)


def render_when_suffix(
    step: Step | CompiledLeaf | CompiledComposite,
) -> str:
    when = step.when
    if when is None:
        return ""
    return f" [when: {condition_name(when)}]"


def render_after_suffix(
    step: CompiledLeaf | CompiledComposite,
    *,
    step_labels: Mapping[StepId, str] | None = None,
) -> str:
    if not step.after:
        return ""
    deps = ", ".join(
        format_prerequisite(dep, step_labels=step_labels) for dep in step.after
    )
    return f" [after: {deps}]"


def render_graph_after_suffix(
    graph: Graph,
    step: Step,
    *,
    step_ids: Mapping[Step, StepId],
) -> str:
    incoming = graph.incoming_edges(step)
    if not incoming:
        return ""
    deps = ", ".join(
        format_graph_edge(edge, graph=graph, step_ids=step_ids) for edge in incoming
    )
    return f" [after: {deps}]"


def render_static_suffix(graph: Graph, step: Step) -> str:
    binding = graph._static_bindings.get(step)
    if binding is None:
        return ""
    return f" [static: {format_static_condition(binding.condition)}]"


def render_graph_capability_suffix(graph: Graph, step: Step) -> str:
    bindings = tuple(
        edge
        for edge in graph.edges
        if isinstance(edge, CapabilityBinding) and edge.target is step
    )
    if not bindings:
        return ""
    requirements = ", ".join(
        f"{edge.capability.name} <- "
        f"{'<unlabeled>' if edge.source.label is None else edge.source.label}"
        for edge in bindings
    )
    return f" [requires: {requirements}]"


def format_graph_edge(
    edge: GraphEdge,
    *,
    graph: Graph,
    step_ids: Mapping[Step, StepId],
) -> str:
    dep_id = resolve_step_label(edge.source, step_ids[edge.source])
    binding = graph._static_bindings.get((edge.source, edge.target))
    if binding is not None:
        dep_id += f" (static: {format_static_condition(binding.condition)})"
    if isinstance(edge, ConditionalEdge):
        return f"{dep_id} (条件边: {condition_name(edge.condition)})"
    return dep_id


def render_step_path(path: tuple[str, ...]) -> str:
    return ".".join(path)


def format_prerequisite(
    dep: StepPrerequisite | CompiledPrerequisite,
    *,
    step_labels: Mapping[StepId, str] | None = None,
) -> str:
    def _label(step_id: StepId) -> str:
        if step_labels is None:
            return str(step_id)
        return step_labels[step_id]

    match dep:
        case ConditionalPrerequisite(step=source, condition=condition):
            if source.label is None:
                raise ValueError(
                    "declaration prerequisite label requires its mounted step id"
                )
            return f"{source.label} (条件边: {condition_name(condition)})"
        case CompiledConditionalDependency(node=node, condition=condition):
            dep_id = _label(node.id)
            return f"{dep_id} (条件边: {condition_name(condition)})"
        case CompiledStepRef(id=dep_id) | CompiledCompositeRef(id=dep_id):
            return _label(dep_id)
        case Step() as source:
            if source.label is None:
                raise ValueError("declaration step label requires its mounted step id")
            return source.label
        case StepId() as step_id:
            return _label(step_id)


def prerequisite_step_label(dep: StepPrerequisite) -> str | None:
    if isinstance(dep, ConditionalPrerequisite):
        return dep.step.label
    return dep.label


def condition_name(
    condition: StaticCondition | RuntimeCondition | CompiledRuntimeCondition,
) -> str:
    if isinstance(
        condition,
        (RuntimeCondition, CompiledRuntimeCondition),
    ):
        qualname = getattr(condition.when, "__qualname__", None)
        if isinstance(qualname, str) and qualname:
            return f"runtime:{qualname}"
        name = getattr(condition.when, "__name__", None)
        if isinstance(name, str) and name:
            return f"runtime:{name}"
        return f"runtime:{condition!r}"
    if isinstance(condition, StaticCondition):
        return f"static:{format_static_condition(condition)}"
    return repr(condition)


def format_static_condition(condition: StaticCondition) -> str:
    return condition.description


def escape_label(value: str) -> str:
    return value.replace("\\", "\\\\").replace('"', '\\"')


def mermaid_graph_node_id(graph_path: tuple[str, ...]) -> str:
    return "graph_" + "_".join(sanitize_mermaid_token(part) for part in graph_path)


def mermaid_step_node_id(
    graph_path: tuple[str, ...],
    step_label: str,
    index: int,
) -> str:
    path_token = "_".join(sanitize_mermaid_token(part) for part in graph_path)
    return f"step_{path_token}_{index}_{sanitize_mermaid_token(step_label)}"


def sanitize_mermaid_token(value: str) -> str:
    sanitized = "".join(char if char.isalnum() else "_" for char in value)
    sanitized = sanitized.strip("_")
    if not sanitized:
        return "node"
    if sanitized[0].isdigit():
        return f"n_{sanitized}"
    return sanitized


def render_execution_result_suffix(
    step_id: StepId,
    results: GraphResultView | None,
) -> str:
    if results is None:
        return ""
    result = results.get(step_id)
    if result is None:
        return ""
    value = result.value
    if not isinstance(value, str | int | float | bool | type(None)):
        return ""
    return f" [result={json.dumps(value, ensure_ascii=False)}]"
