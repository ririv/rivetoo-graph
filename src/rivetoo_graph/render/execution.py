from __future__ import annotations

from enum import StrEnum
from typing import TYPE_CHECKING

from ..model import (
    Graph,
    GraphMode,
    RuntimeCondition,
    Step,
    StepId,
    resolve_step_label,
    step_ids_for_mount,
)
from .formatting import (
    condition_name,
    render_execution_result_suffix,
    render_graph_after_suffix,
    render_step_label,
)

if TYPE_CHECKING:
    from collections.abc import Mapping

    from ..runtime import GraphExecutionSnapshot, GraphExecutionState


class ExecutionRenderStatus(StrEnum):
    """根据执行快照派生的展示状态，不作为 runtime 状态机事实来源。"""

    RUNNING = "running"
    EXECUTED = "executed"
    ENTERED = "entered"
    COMPLETED = "completed"
    SKIPPED = "skipped"
    SKIPPED_BY_ANCESTOR = "skipped_by_ancestor"
    NOT_REACHED = "not_reached"
    FAILED = "failed"
    BLOCKED = "blocked"


def render_graph_execution(
    graph: Graph,
    state: GraphExecutionState,
    *,
    root_label: str,
    mode: GraphMode = "dynamic",
) -> str:
    return _render_graph_execution_impl(
        graph,
        state,
        root_label=root_label,
        mode=mode,
        include_not_reached=True,
        title="execution",
    )


def render_live_graph_execution(
    graph: Graph,
    state: GraphExecutionState,
    *,
    root_label: str,
    mode: GraphMode = "dynamic",
) -> str:
    snapshot = state.snapshot()
    leaf_steps = collect_leaf_steps(graph)
    running_step_ids = deepest_running_step_ids(snapshot)
    running_labels = [
        snapshot.step_label_paths[step_id] for step_id in running_step_ids
    ]
    remaining_count = sum(
        1
        for step_id, _label_path in leaf_steps
        if step_id not in snapshot.completed_steps
        and step_id not in snapshot.skipped_steps
        and step_id not in snapshot.node_failures
    )
    last_step_id = snapshot.executed_steps[-1] if snapshot.executed_steps else None
    running_text = _summarize_paths(running_labels, fallback="idle")
    last_text = (
        _format_step_path(snapshot.step_label_paths[last_step_id])
        if last_step_id is not None
        else "-"
    )
    return (
        f"graph={root_label} mode={mode} "
        f"running={running_text} "
        f"last={last_text} "
        f"remaining={remaining_count}"
    )


def _render_graph_execution_impl(
    graph: Graph,
    state: GraphExecutionState,
    *,
    root_label: str,
    mode: GraphMode,
    include_not_reached: bool,
    title: str,
) -> str:
    snapshot = state.snapshot()
    lines = [f"{title}: {root_label} (mode={mode})"]
    active_step_ids = active_execution_step_ids(snapshot)
    if not active_step_ids and not include_not_reached:
        return "\n".join(lines)
    append_execution_graph(
        lines,
        graph,
        snapshot=snapshot,
        active_step_ids=active_step_ids,
        label_path=(),
        parent_step_id=None,
        ancestor_skipped=False,
        include_not_reached=include_not_reached,
        prefix="",
    )
    return "\n".join(lines)


def append_execution_graph(
    lines: list[str],
    graph: Graph,
    *,
    snapshot: GraphExecutionSnapshot,
    active_step_ids: set[StepId],
    label_path: tuple[str, ...],
    parent_step_id: StepId | None,
    ancestor_skipped: bool,
    include_not_reached: bool,
    prefix: str,
) -> None:
    graph_step_ids = step_ids_for_mount(graph, parent_step_id=parent_step_id)
    for index, step in enumerate(graph.steps):
        append_execution_step(
            lines,
            graph,
            step,
            snapshot=snapshot,
            active_step_ids=active_step_ids,
            label_path=label_path,
            graph_step_ids=graph_step_ids,
            ancestor_skipped=ancestor_skipped,
            include_not_reached=include_not_reached,
            is_last=index == len(graph.steps) - 1,
            prefix=prefix,
        )


def append_execution_step(
    lines: list[str],
    graph: Graph,
    step: Step,
    *,
    snapshot: GraphExecutionSnapshot,
    active_step_ids: set[StepId],
    label_path: tuple[str, ...],
    graph_step_ids: Mapping[Step, StepId],
    ancestor_skipped: bool,
    include_not_reached: bool,
    is_last: bool,
    prefix: str,
) -> None:
    step_id = graph_step_ids[step]
    step_label = resolve_step_label(step, step_id)
    step_label_path = (*label_path, step_label)
    status = resolve_execution_status(
        step,
        step_id=step_id,
        snapshot=snapshot,
        active_step_ids=active_step_ids,
        ancestor_skipped=ancestor_skipped,
    )
    if status is ExecutionRenderStatus.NOT_REACHED and not include_not_reached:
        return
    branch = "└─ " if is_last else "├─ "
    child_prefix = prefix + ("   " if is_last else "│  ")
    action = step.action
    base = (
        f"{step_label} [graph]"
        if isinstance(action, Graph)
        else render_step_label(step, step_id=step_id)
    )
    lines.append(
        f"{prefix}{branch}{base}"
        f"{render_execution_when_suffix(step, step_id=step_id, condition_values=snapshot.runtime_condition_values)}"
        f" [{status.value}]"
        f"{render_execution_result_suffix(step_id, snapshot.results)}"
        f"{render_graph_after_suffix(graph, step, step_ids=graph_step_ids)}"
    )
    if not isinstance(action, Graph):
        return
    append_execution_graph(
        lines,
        action,
        snapshot=snapshot,
        active_step_ids=active_step_ids,
        label_path=step_label_path,
        parent_step_id=step_id,
        ancestor_skipped=(ancestor_skipped or status is ExecutionRenderStatus.SKIPPED),
        include_not_reached=include_not_reached,
        prefix=child_prefix,
    )


def resolve_execution_status(
    step: Step,
    *,
    step_id: StepId,
    snapshot: GraphExecutionSnapshot,
    active_step_ids: set[StepId],
    ancestor_skipped: bool,
) -> ExecutionRenderStatus:
    if failure := snapshot.node_failures.get(step_id):
        return ExecutionRenderStatus(failure.value)
    if ancestor_skipped:
        return ExecutionRenderStatus.SKIPPED_BY_ANCESTOR
    if step_id in snapshot.skipped_steps:
        return ExecutionRenderStatus.SKIPPED
    if isinstance(step.action, Graph):
        if (
            step_id in snapshot.completed_composites
            or step_id in snapshot.completed_steps
        ):
            return ExecutionRenderStatus.COMPLETED
        if step_id in snapshot.entered_composites or step_id in active_step_ids:
            return ExecutionRenderStatus.ENTERED
        return ExecutionRenderStatus.NOT_REACHED
    if step_id in active_step_ids:
        if step_id in snapshot.completed_steps:
            return ExecutionRenderStatus.EXECUTED
        return ExecutionRenderStatus.RUNNING
    return ExecutionRenderStatus.NOT_REACHED


def render_execution_when_suffix(
    step: Step,
    *,
    step_id: StepId,
    condition_values: Mapping[StepId, bool],
) -> str:
    """渲染 runtime 已记录的条件值，不在渲染阶段重新执行 predicate。"""

    when = step.when
    if when is None:
        return ""
    if isinstance(when, RuntimeCondition):
        value = condition_values.get(step_id)
        if value is None:
            return " [when=pending]"
        return f" [when={str(value).lower()}]"
    return f" [when: {condition_name(when)}]"


def active_execution_step_ids(
    snapshot: GraphExecutionSnapshot,
) -> set[StepId]:
    active: set[StepId] = set()
    for step_id in [
        *snapshot.executed_steps,
        *snapshot.entered_composites,
        *snapshot.skipped_steps,
        *snapshot.node_failures,
    ]:
        current: StepId | None = step_id
        while current is not None:
            active.add(current)
            current = current.parent
    return active


def deepest_running_step_ids(
    snapshot: GraphExecutionSnapshot,
) -> tuple[StepId, ...]:
    """返回当前未完成路径中最深的 step，避免同时展示其执行中祖先。"""

    running = [
        step_id
        for step_id in snapshot.executed_steps
        if step_id not in snapshot.completed_steps
        and step_id not in snapshot.skipped_steps
        and step_id not in snapshot.node_failures
    ]
    running_set = set(running)
    ancestors_with_running_children: set[StepId] = set()
    for step_id in running:
        current = step_id.parent
        while current is not None:
            if current in running_set:
                ancestors_with_running_children.add(current)
            current = current.parent
    return tuple(
        step_id for step_id in running if step_id not in ancestors_with_running_children
    )


def collect_step_labels(
    graph: Graph,
    *,
    label_path: tuple[str, ...] = (),
    parent_step_id: StepId | None = None,
) -> tuple[tuple[StepId, tuple[str, ...]], ...]:
    out: list[tuple[StepId, tuple[str, ...]]] = []
    for step, step_id in step_ids_for_mount(
        graph,
        parent_step_id=parent_step_id,
    ).items():
        step_label_path = (*label_path, resolve_step_label(step, step_id))
        out.append((step_id, step_label_path))
        if isinstance(step.action, Graph):
            out.extend(
                collect_step_labels(
                    step.action,
                    label_path=step_label_path,
                    parent_step_id=step_id,
                )
            )
    return tuple(out)


def collect_leaf_steps(
    graph: Graph,
    *,
    label_path: tuple[str, ...] = (),
    parent_step_id: StepId | None = None,
) -> tuple[tuple[StepId, tuple[str, ...]], ...]:
    out: list[tuple[StepId, tuple[str, ...]]] = []
    for step, step_id in step_ids_for_mount(
        graph,
        parent_step_id=parent_step_id,
    ).items():
        step_label_path = (*label_path, resolve_step_label(step, step_id))
        if isinstance(step.action, Graph):
            out.extend(
                collect_leaf_steps(
                    step.action,
                    label_path=step_label_path,
                    parent_step_id=step_id,
                )
            )
            continue
        out.append((step_id, step_label_path))
    return tuple(out)


def _format_step_path(path: tuple[str, ...] | None) -> str:
    if path is None or not path:
        return "-"
    return "/".join(path)


def _summarize_paths(paths: list[tuple[str, ...]], *, fallback: str) -> str:
    if not paths:
        return fallback
    labels = [_format_step_path(path) for path in paths[:2]]
    remaining = len(paths) - len(labels)
    if remaining > 0:
        labels.append(f"+{remaining}")
    return ",".join(labels)
