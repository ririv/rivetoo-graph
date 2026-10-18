from __future__ import annotations

from ..capability import CapabilityBinding
from ..compile import GraphCompilation, compiled_prerequisite_node
from ..errors import GraphError
from ..model import (
    Graph,
    GraphMode,
    ResultEdge,
    Step,
    StepId,
    resolve_step_label,
    step_ids_for_mount,
)
from .formatting import (
    ensure_compilation,
    escape_label,
    mermaid_graph_node_id,
    mermaid_step_node_id,
    render_compiled_step_label,
    render_step_label,
    render_step_path,
    result_receiver_label,
)


def render_graph_mermaid(
    subject: Graph | GraphCompilation,
    *,
    root_label: str,
    mode: GraphMode = "dynamic",
) -> str:
    match mode:
        case "compiled":
            return render_graph_compilation_mermaid(
                ensure_compilation(subject, root_label=root_label),
                root_label=root_label,
            )
        case "dynamic":
            if isinstance(subject, GraphCompilation):
                return render_graph_structure_mermaid(
                    subject.source_graph,
                    root_label=root_label,
                )
            return render_graph_structure_mermaid(subject, root_label=root_label)
        case _:
            raise GraphError(f"unsupported graph render mode: {mode}")


def render_graph_structure_mermaid(graph: Graph, *, root_label: str) -> str:
    lines = ["flowchart TD"]
    append_graph_mermaid(
        lines,
        graph,
        graph_label=root_label,
        graph_path=(root_label,),
        parent_step_id=None,
        indent="    ",
    )
    return "\n".join(lines)


def render_graph_compilation_mermaid(
    compilation: GraphCompilation,
    *,
    root_label: str,
) -> str:
    node_ids = {
        node.id: f"compiled_{index}"
        for index, node in enumerate((*compilation.steps, *compilation.composites))
    }
    lines = [
        "flowchart TD",
        f'    subgraph compiled_graph["{escape_label(root_label)}"]',
    ]
    for step in compilation.steps:
        label = render_compiled_step_label(step, step.context.label_path)
        lines.append(f'        {node_ids[step.id]}["{escape_label(label)}"]')
    lines.append("    end")
    if compilation.composites:
        lines.append("    subgraph compiled_composites")
        for composite in compilation.composites:
            label = f"{render_step_path(composite.label_path)} [composite]"
            lines.append(f'        {node_ids[composite.id]}["{escape_label(label)}"]')
        lines.append("    end")
    for step in compilation.steps:
        target_id = node_ids[step.id]
        for dep in step.after:
            dep_id = compiled_prerequisite_node(dep).id
            lines.append(f"        {node_ids[dep_id]} --> {target_id}")
    for composite in compilation.composites:
        target_id = node_ids[composite.id]
        for dep in composite.after:
            dep_id = compiled_prerequisite_node(dep).id
            lines.append(f"        {node_ids[dep_id]} --> {target_id}")
        for child in composite.children:
            lines.append(f"        {target_id} -.-> {node_ids[child.id]}")
    return "\n".join(lines)


def append_graph_mermaid(
    lines: list[str],
    graph: Graph,
    *,
    graph_label: str,
    graph_path: tuple[str, ...],
    parent_step_id: StepId | None,
    indent: str,
) -> None:
    lines.append(
        f'{indent}subgraph {mermaid_graph_node_id(graph_path)}["{escape_label(graph_label)}"]'
    )
    node_ids: dict[Step, str] = {}
    step_ids = step_ids_for_mount(graph, parent_step_id=parent_step_id)
    for index, step in enumerate(graph.steps):
        step_id = step_ids[step]
        step_label = resolve_step_label(step, step_id)
        node_id = mermaid_step_node_id(graph_path, step_label, index)
        node_ids[step] = node_id
        action = step.action
        if isinstance(action, Graph):
            lines.append(f'{indent}    {node_id}["{escape_label(step_label)}"]')
            append_graph_mermaid(
                lines,
                action,
                graph_label=step_label,
                graph_path=(*graph_path, step_label),
                parent_step_id=step_id,
                indent=indent + "    ",
            )
        else:
            lines.append(
                f'{indent}    {node_id}["{escape_label(render_step_label(step, step_id=step_id))}"]'
            )
    for edge in graph.edges:
        if isinstance(edge, ResultEdge):
            lines.append(
                f'{indent}    {node_ids[edge.source]} -->|"{escape_label(result_receiver_label(edge))} input {edge.argument_index}"| {node_ids[edge.target]}'
            )
        elif isinstance(edge, CapabilityBinding):
            lines.append(
                f'{indent}    {node_ids[edge.source]} -->|"{escape_label(edge.capability.name)}"| {node_ids[edge.target]}'
            )
        else:
            lines.append(
                f"{indent}    {node_ids[edge.source]} --> {node_ids[edge.target]}"
            )
    lines.append(f"{indent}end")
