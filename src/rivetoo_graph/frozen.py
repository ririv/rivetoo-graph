from __future__ import annotations

from dataclasses import dataclass, field

from .capability import Capability, CapabilityBinding
from .errors import GraphError
from .model import (
    ConditionalEdge,
    ConditionalEdgePredicate,
    Graph,
    GraphObserve,
    LeafAction,
    RuntimeCondition,
    Step,
    StepId,
    resolve_step_label,
    step_ids_for_mount,
)
from .static_condition import StaticCondition, evaluate_static_condition
from .topology import Arc, DAGTopology, DirectedTopology, TopologyCycleError


@dataclass(frozen=True, slots=True)
class FrozenRuntimeCondition:
    """已经把声明 Step 引用解析为内部 StepId 的运行时条件。"""

    when: ConditionalEdgePredicate
    reads: tuple[StepId, ...]


@dataclass(frozen=True, slots=True)
class RuntimeConditionalEdge:
    """freeze 后仍需由 runtime 判断是否成立的依赖边。"""

    step_id: StepId
    condition: FrozenRuntimeCondition


type FrozenStepPrerequisite = StepId | RuntimeConditionalEdge


@dataclass(frozen=True, slots=True)
class FrozenCapabilityBinding:
    """已经解析到两个子图内部 step 的跨边界能力绑定。"""

    capability: Capability
    source: StepId
    target: StepId


@dataclass(frozen=True, slots=True)
class FrozenStep:
    """针对本次静态输入实例化后的执行节点。

    静态条件在构造该类型前已经被求值，因此该类型只能表达
    运行时条件和已冻结的子图。
    """

    id: StepId
    source_step: Step
    label: str
    action: LeafAction | FrozenGraph
    after: tuple[FrozenStepPrerequisite, ...] = ()
    when: FrozenRuntimeCondition | None = None


@dataclass(frozen=True, slots=True)
class FrozenGraph:
    """静态条件已裁剪的层级 DAG。

    不含跨子图能力绑定时可以直接交给 dynamic runtime；能力绑定需要
    compiler 先把端口引用 lower 为全局节点依赖。
    """

    source_graph: Graph
    label: str
    steps: tuple[FrozenStep, ...] = field(default_factory=tuple)
    capability_bindings: tuple[FrozenCapabilityBinding, ...] = field(
        default_factory=tuple
    )
    observe: GraphObserve = field(default_factory=GraphObserve)

    def __post_init__(self) -> None:
        if not self.label:
            raise GraphError("frozen graph label must not be empty")

        known_ids: set[StepId] = set()
        for step in self.steps:
            if step.id in known_ids:
                raise GraphError(
                    f"frozen graph '{self.label}' has duplicate step id: {step.id}"
                )
            known_ids.add(step.id)

        for step in self.steps:
            for prerequisite in step.after:
                prerequisite_id = (
                    prerequisite.step_id
                    if isinstance(prerequisite, RuntimeConditionalEdge)
                    else prerequisite
                )
                if prerequisite_id not in known_ids:
                    raise GraphError(
                        f"frozen graph '{self.label}' step '{step.id}' has unknown "
                        f"prerequisite step: {prerequisite_id}"
                    )
                if isinstance(prerequisite, RuntimeConditionalEdge):
                    _validate_runtime_reads(
                        graph_label=self.label,
                        step_id=step.id,
                        reads=prerequisite.condition.reads,
                        known_ids=known_ids,
                    )
            if step.when is not None:
                _validate_runtime_reads(
                    graph_label=self.label,
                    step_id=step.id,
                    reads=step.when.reads,
                    known_ids=known_ids,
                )

        descendant_ids = _all_frozen_step_ids(self.steps)
        for binding in self.capability_bindings:
            if binding.source not in descendant_ids:
                raise GraphError(
                    f"frozen graph '{self.label}' capability "
                    f"'{binding.capability.name}' has unknown provider step: "
                    f"{binding.source}"
                )
            if binding.target not in descendant_ids:
                raise GraphError(
                    f"frozen graph '{self.label}' capability "
                    f"'{binding.capability.name}' has unknown consumer step: "
                    f"{binding.target}"
                )

        topology = DirectedTopology(
            nodes=tuple(step.id for step in self.steps),
            arcs=tuple(
                Arc(source=dependency, target=step.id)
                for step in self.steps
                for dependency in _frozen_step_dependency_ids(step)
            ),
        )
        try:
            DAGTopology(topology)
        except TopologyCycleError as exc:
            cycle = " -> ".join(str(item) for item in exc.cycle)
            raise GraphError(
                f"frozen graph '{self.label}' has dependency cycle: {cycle}"
            ) from exc

    @property
    def requires_compiled_execution(self) -> bool:
        if self.capability_bindings:
            return True
        return any(
            isinstance(step.action, FrozenGraph)
            and step.action.requires_compiled_execution
            for step in self.steps
        )


def _all_frozen_step_ids(steps: tuple[FrozenStep, ...]) -> set[StepId]:
    result = {step.id for step in steps}
    for step in steps:
        if isinstance(step.action, FrozenGraph):
            result.update(_all_frozen_step_ids(step.action.steps))
    return result


def _frozen_step_dependency_ids(step: FrozenStep) -> set[StepId]:
    """收集冻结节点用于环检测的 prerequisite 与条件读取依赖。"""

    dependency_ids: set[StepId] = set()
    for prerequisite in step.after:
        if isinstance(prerequisite, RuntimeConditionalEdge):
            dependency_ids.add(prerequisite.step_id)
            dependency_ids.update(prerequisite.condition.reads)
        else:
            dependency_ids.add(prerequisite)
    if step.when is not None:
        dependency_ids.update(step.when.reads)
    return dependency_ids


def _validate_runtime_reads(
    *,
    graph_label: str,
    step_id: StepId,
    reads: tuple[StepId, ...],
    known_ids: set[StepId],
) -> None:
    for read_step_id in reads:
        if read_step_id not in known_ids:
            raise GraphError(
                f"frozen graph '{graph_label}' step '{step_id}' has unknown "
                f"runtime condition read step: {read_step_id}"
            )


def freeze_graph(
    graph: Graph,
    *,
    root_label: str,
    parent_step_id: StepId | None = None,
) -> FrozenGraph:
    """针对当前静态输入实例化声明图。

    该阶段递归裁掉静态条件为假的 step 和边，并把保留下来的嵌套
    `Graph` 冻结为 `FrozenGraph`。运行期条件不会在这里求值，而是以
    `RuntimeCondition` / `RuntimeConditionalEdge` 的形式进入 runtime
    或后续 compile 阶段。未显式声明的图 label 在这里按挂载位置解析：
    根图使用 `root_label`，嵌套图使用承载它的已解析 step label；未显式
    声明的 step label 使用该挂载位置的 `StepId`。
    """

    return _freeze_graph(
        graph,
        default_label=root_label,
        id_prefix=() if parent_step_id is None else parent_step_id.segments,
    )


def _freeze_graph(
    graph: Graph,
    *,
    default_label: str,
    id_prefix: tuple[int, ...],
) -> FrozenGraph:
    label = graph.label if graph.label is not None else default_label
    frozen_steps: list[FrozenStep] = []
    static_condition_eval = graph.static_condition_eval
    parent_step_id = StepId(id_prefix) if id_prefix else None
    step_ids = step_ids_for_mount(graph, parent_step_id=parent_step_id)
    step_labels = {
        step: resolve_step_label(step, step_id) for step, step_id in step_ids.items()
    }
    active_steps: set[Step] = set()
    frozen_actions: dict[Step, LeafAction | FrozenGraph] = {}

    for step in graph.steps:
        step_label = step_labels[step]
        when = step.when
        if isinstance(when, StaticCondition):
            if static_condition_eval is None:
                raise GraphError(
                    "graph contains static step condition but "
                    f"Graph.static_condition_eval is None: {label}.{step_label}"
                )
            if not evaluate_static_condition(
                when,
                static_condition_eval=static_condition_eval,
            ):
                continue
        action = step.action
        step_id = step_ids[step]
        frozen_action = (
            _freeze_graph(
                action,
                default_label=step_label,
                id_prefix=step_id.segments,
            )
            if isinstance(action, Graph)
            else action
        )
        active_steps.add(step)
        frozen_actions[step] = frozen_action

    for step in graph.steps:
        if step not in active_steps:
            continue
        step_label = step_labels[step]
        frozen_after: list[FrozenStepPrerequisite] = []
        for edge in graph.incoming_edges(step):
            prerequisite = edge.source
            prerequisite_id = step_ids[prerequisite]
            if not isinstance(edge, ConditionalEdge):
                if prerequisite in active_steps:
                    frozen_after.append(prerequisite_id)
                continue
            if prerequisite not in active_steps:
                continue
            condition = edge.condition
            if isinstance(condition, RuntimeCondition):
                frozen_after.append(
                    RuntimeConditionalEdge(
                        step_id=prerequisite_id,
                        condition=_freeze_runtime_condition(
                            condition,
                            step_ids=step_ids,
                            graph_label=label,
                            target_label=step_label,
                        ),
                    )
                )
                continue
            if static_condition_eval is None:
                raise GraphError(
                    "graph contains static condition edge but "
                    "Graph.static_condition_eval is None: "
                    f"{label}.{step_label} <- {step_labels[prerequisite]}"
                )
            if evaluate_static_condition(
                condition,
                static_condition_eval=static_condition_eval,
            ):
                frozen_after.append(prerequisite_id)

        when = step.when
        frozen_steps.append(
            FrozenStep(
                id=step_ids[step],
                source_step=step,
                label=step_label,
                action=frozen_actions[step],
                after=tuple(frozen_after),
                when=(
                    _freeze_runtime_condition(
                        when,
                        step_ids=step_ids,
                        graph_label=label,
                        target_label=step_label,
                    )
                    if isinstance(when, RuntimeCondition)
                    else None
                ),
            )
        )

    frozen_bindings: list[FrozenCapabilityBinding] = []
    for edge in graph.edges:
        if not isinstance(edge, CapabilityBinding):
            continue
        if edge.target not in active_steps:
            continue
        consumer_graph = frozen_actions[edge.target]
        consumer_source_graph = edge.target.action
        if not isinstance(
            consumer_graph,
            FrozenGraph,
        ) or not isinstance(
            consumer_source_graph,
            Graph,
        ):
            raise GraphError(
                f"graph '{label}' capability binding endpoints must be graph-valued"
            )
        input_port = next(
            input_port
            for input_port in consumer_source_graph.capability_inputs
            if input_port.capability == edge.capability
        )
        target_id = _find_frozen_step_id(
            consumer_graph,
            input_port.needed_by,
        )
        if target_id is None:
            continue
        if edge.source not in active_steps:
            raise GraphError(
                f"graph '{label}' active consumer '{step_labels[edge.target]}' "
                f"requires capability '{edge.capability.name}' from an inactive "
                f"provider '{step_labels[edge.source]}'"
            )
        provider_graph = frozen_actions[edge.source]
        provider_source_graph = edge.source.action
        if not isinstance(provider_graph, FrozenGraph) or not isinstance(
            provider_source_graph,
            Graph,
        ):
            raise GraphError(
                f"graph '{label}' capability binding endpoints must be graph-valued"
            )
        output = next(
            output
            for output in provider_source_graph.capability_outputs
            if output.capability == edge.capability
        )
        source_id = _find_frozen_step_id(
            provider_graph,
            output.ready_after,
        )
        if source_id is None:
            raise GraphError(
                f"graph '{label}' capability '{edge.capability.name}' provider "
                f"port is inactive while its consumer port is active"
            )
        frozen_bindings.append(
            FrozenCapabilityBinding(
                capability=edge.capability,
                source=source_id,
                target=target_id,
            )
        )

    return FrozenGraph(
        source_graph=graph,
        label=label,
        steps=tuple(frozen_steps),
        capability_bindings=tuple(frozen_bindings),
        observe=graph.observe,
    )


def _find_frozen_step_id(
    graph: FrozenGraph,
    source_step: Step,
) -> StepId | None:
    for step in graph.steps:
        if step.source_step is source_step:
            return step.id
        if isinstance(step.action, FrozenGraph):
            nested = _find_frozen_step_id(step.action, source_step)
            if nested is not None:
                return nested
    return None


def _freeze_runtime_condition(
    condition: RuntimeCondition,
    *,
    step_ids: dict[Step, StepId],
    graph_label: str,
    target_label: str,
) -> FrozenRuntimeCondition:
    reads: list[StepId] = []
    for read_step in condition.reads:
        read_step_id = step_ids.get(read_step)
        if read_step_id is None:
            raise GraphError(
                f"graph '{graph_label}' step '{target_label}' has runtime condition read outside this graph: {read_step.label}"
            )
        reads.append(read_step_id)
    return FrozenRuntimeCondition(when=condition.when, reads=tuple(reads))
