from __future__ import annotations

import logging
from collections import deque
from dataclasses import dataclass, field
from enum import StrEnum
from typing import TYPE_CHECKING

from ..errors import GraphError
from ..frozen import FrozenRuntimeCondition, FrozenStep, RuntimeConditionalEdge
from ..results import GraphResultStore, GraphScope, StepReadContext

if TYPE_CHECKING:
    from collections.abc import Mapping

    from ..model import Step, StepId, StepLabelPath
    from .run import DynamicGraphRun
    from .state import GraphExecutionState

logger = logging.getLogger(__name__)


class DynamicStepState(StrEnum):
    """运行期条件和依赖共同决定的 step 调度状态。"""

    PENDING = "pending"
    DISABLED = "disabled"
    READY = "ready"


@dataclass(slots=True)
class DynamicConditionCache:
    """保存单层图执行中已经确定的运行时条件结果。

    ready 队列会在 prerequisite 完成后反复检查候选 step。缓存保证每个
    step condition 和 conditional edge predicate 在该层图中最多求值一次，
    避免带副作用或读取易变状态的 predicate 得到不一致结果。
    """

    step_values: dict[StepId, bool] = field(default_factory=dict)
    edge_values: dict[tuple[StepId, int], bool] = field(default_factory=dict)


def make_pending_edge_context(
    *,
    step_id: StepId,
    label_path: StepLabelPath,
    graph_step_ids: Mapping[Step, StepId],
    ancestor_graph_scopes: tuple[GraphScope, ...],
    results: GraphResultStore | None,
) -> StepReadContext:
    return StepReadContext(
        step_id=step_id,
        label_path=label_path,
        graph_step_ids=graph_step_ids,
        ancestor_graph_scopes=ancestor_graph_scopes,
        results=results,
    )


type DynamicExecutableStep = FrozenStep


def conditional_dep_satisfied(
    dep: RuntimeConditionalEdge,
    *,
    target_step_id: StepId,
    prerequisite_index: int,
    edge_ctx: StepReadContext,
    finished_step_ids: set[StepId],
    condition_cache: DynamicConditionCache,
) -> bool:
    """判断一条运行期条件边是否已经满足调度要求。

    条件为假表示该依赖边不存在，因此无需等待来源 step；条件为真时，
    来源 step 必须已经结束。条件自身的 `reads` 未完成时仍不可求值。
    """

    condition = dep.condition
    if any(step_id not in finished_step_ids for step_id in condition.reads):
        return False
    cache_key = (target_step_id, prerequisite_index)
    if cache_key in condition_cache.edge_values:
        active = condition_cache.edge_values[cache_key]
    else:
        active = condition.when(edge_ctx)
        if not isinstance(active, bool):
            raise GraphError(
                "conditional edge predicate must return bool: "
                f"{edge_ctx.label_path[-1]} <- {dep.step_id}"
            )
        condition_cache.edge_values[cache_key] = active
    if not active:
        return True
    return dep.step_id in finished_step_ids


def runtime_step_condition_value(
    step: DynamicExecutableStep,
    *,
    edge_ctx: StepReadContext,
    finished_step_ids: set[StepId],
    condition_cache: DynamicConditionCache,
    state: GraphExecutionState,
) -> bool | None:
    """返回 step 条件值；依赖结果尚未就绪时返回 `None`。

    首次求值结果同时写入条件缓存和执行状态。后续调度检查复用缓存，
    render 则读取执行状态，不会再次调用 predicate。
    """

    condition = step.when
    if condition is None:
        return True
    if any(step_id not in finished_step_ids for step_id in condition.reads):
        return None
    if step.id in condition_cache.step_values:
        return condition_cache.step_values[step.id]
    active = condition.when(edge_ctx)
    if not isinstance(active, bool):
        raise GraphError(f"step runtime condition must return bool: {step.id}")
    condition_cache.step_values[step.id] = active
    state.record_runtime_condition(step.id, active)
    return active


def dynamic_step_state(
    step: DynamicExecutableStep,
    *,
    label_path: StepLabelPath,
    graph_step_ids: Mapping[Step, StepId],
    ancestor_graph_scopes: tuple[GraphScope, ...],
    state: GraphExecutionState,
    finished_step_ids: set[StepId],
    condition_cache: DynamicConditionCache,
) -> DynamicStepState:
    # dynamic 路径下统一把 step 收成三态：
    # - PENDING：条件还不能判，或静态/条件依赖还没满足
    # - DISABLED：`when=False`，这次执行里该 step 视为不存在
    # - READY：可以真正进入 action / 子图执行
    edge_ctx = make_pending_edge_context(
        step_id=step.id,
        label_path=label_path,
        graph_step_ids=graph_step_ids,
        ancestor_graph_scopes=ancestor_graph_scopes,
        results=state.results,
    )
    step_active = runtime_step_condition_value(
        step,
        edge_ctx=edge_ctx,
        finished_step_ids=finished_step_ids,
        condition_cache=condition_cache,
        state=state,
    )
    if step_active is None:
        return DynamicStepState.PENDING
    if not step_active:
        return DynamicStepState.DISABLED
    for prerequisite_index, dep in enumerate(step.after):
        if isinstance(dep, RuntimeConditionalEdge):
            if not conditional_dep_satisfied(
                dep,
                target_step_id=step.id,
                prerequisite_index=prerequisite_index,
                edge_ctx=edge_ctx,
                finished_step_ids=finished_step_ids,
                condition_cache=condition_cache,
            ):
                return DynamicStepState.PENDING
            continue
        if dep not in finished_step_ids:
            return DynamicStepState.PENDING
    return DynamicStepState.READY


def log_disabled_step_condition(
    step: DynamicExecutableStep,
    *,
    label_path: StepLabelPath,
) -> None:
    condition = step.when
    if not isinstance(condition, FrozenRuntimeCondition):
        raise GraphError(
            f"disabled dynamic step must carry runtime condition: {step.id}"
        )
    reads = ",".join(str(step_id.segments) for step_id in condition.reads) or "-"
    logger.debug(
        "Condition: skip %s (reads=%s; value=false)",
        ".".join(label_path),
        reads,
    )


def initial_ready_dynamic_steps(
    run: DynamicGraphRun,
    state: GraphExecutionState,
    *,
    graph_label_path: StepLabelPath,
    ancestor_graph_scopes: tuple[GraphScope, ...],
    condition_cache: DynamicConditionCache,
) -> tuple[
    dict[StepId, DynamicExecutableStep],
    Mapping[Step, StepId],
    deque[tuple[StepId, DynamicStepState]],
    set[StepId],
]:
    """建立动态调度的初始 ready 队列和待判定集合。"""

    steps_by_id = {step.id: step for step in run.steps}
    graph_step_ids = run.observation_step_ids
    remaining = set(steps_by_id)
    ready: deque[tuple[StepId, DynamicStepState]] = deque()
    for step in run.steps:
        step_label_path = run.resolve_step_label_path(
            step,
            graph_label_path=graph_label_path,
        )
        step_state = dynamic_step_state(
            step,
            label_path=step_label_path,
            graph_step_ids=graph_step_ids,
            ancestor_graph_scopes=ancestor_graph_scopes,
            state=state,
            finished_step_ids=set(),
            condition_cache=condition_cache,
        )
        if step_state is not DynamicStepState.PENDING:
            ready.append((step.id, step_state))
            remaining.remove(step.id)
    return steps_by_id, graph_step_ids, ready, remaining


def refresh_ready_dynamic_steps(
    run: DynamicGraphRun,
    state: GraphExecutionState,
    *,
    graph_label_path: StepLabelPath,
    remaining_step_ids: set[StepId],
    finished_step_ids: set[StepId],
    graph_step_ids: Mapping[Step, StepId],
    ancestor_graph_scopes: tuple[GraphScope, ...],
    steps_by_id: dict[StepId, DynamicExecutableStep],
    ready: deque[tuple[StepId, DynamicStepState]],
    condition_cache: DynamicConditionCache,
) -> None:
    """在 step 结束后重新判定仍处于 pending 的候选节点。

    这里按剩余集合扫描是当前分层 runtime 的简单实现。Graph step 是构建级
    粗粒度任务；若未来出现大量微型节点，应以反向依赖索引替代全量扫描。
    """

    newly_ready: list[tuple[StepId, DynamicStepState]] = []
    for step_id in tuple(remaining_step_ids):
        step = steps_by_id[step_id]
        step_label_path = run.resolve_step_label_path(
            step,
            graph_label_path=graph_label_path,
        )
        step_state = dynamic_step_state(
            step,
            label_path=step_label_path,
            graph_step_ids=graph_step_ids,
            ancestor_graph_scopes=ancestor_graph_scopes,
            state=state,
            finished_step_ids=finished_step_ids,
            condition_cache=condition_cache,
        )
        if step_state is not DynamicStepState.PENDING:
            newly_ready.append((step_id, step_state))
            remaining_step_ids.remove(step_id)
    ready.extend(newly_ready)
