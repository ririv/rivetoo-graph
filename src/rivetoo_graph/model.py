from __future__ import annotations

import inspect
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Literal, TypeGuard

from ._native import dag_cycle
from .capability import CapabilityBinding, CapabilityInput, CapabilityOutput
from .errors import GraphError
from .static_condition import StaticCondition, StaticConditionEval
from .topology import Arc


@dataclass(frozen=True, order=True, slots=True)
class StepId:
    """step 在一次具体图挂载中的内部身份。"""

    segments: tuple[int, ...]

    def __post_init__(self) -> None:
        if not self.segments:
            raise GraphError("step id must contain at least one segment")
        if any(segment < 0 for segment in self.segments):
            raise GraphError(f"step id segments must be non-negative: {self.segments}")

    def child(self, index: int) -> StepId:
        if index < 0:
            raise GraphError(f"step child index must be non-negative: {index}")
        return StepId((*self.segments, index))

    def __str__(self) -> str:
        return ".".join(str(segment) for segment in self.segments)

    @property
    def parent(self) -> StepId | None:
        if len(self.segments) == 1:
            return None
        return StepId(self.segments[:-1])


def step_ids_for_mount(
    graph: Graph,
    *,
    parent_step_id: StepId | None = None,
) -> dict[Step, StepId]:
    """为一张图在当前挂载位置分配内部 step 身份。"""

    return {
        step: (
            StepId((index,)) if parent_step_id is None else parent_step_id.child(index)
        )
        for index, step in enumerate(graph.steps)
    }


type StepLabelPath = tuple[str, ...]
type GraphMode = Literal["dynamic", "compiled"]

if TYPE_CHECKING:
    from .results import StepReadContext, StepRunContext


@dataclass(frozen=True, slots=True)
class GraphObservationMessages:
    before: str | None = None
    after_success: str | None = None


type LeafActionCallable = Callable[[], object] | Callable[["StepRunContext"], object]


@dataclass(frozen=True, slots=True)
class _NoArgsCall:
    fn: Callable[[], object]


@dataclass(frozen=True, slots=True)
class _StepContextCall:
    fn: Callable[[StepRunContext], object]


type _LeafActionCall = _NoArgsCall | _StepContextCall


def _accepts_no_args(
    _fn: LeafActionCallable,
    signature: inspect.Signature,
) -> TypeGuard[Callable[[], object]]:
    try:
        signature.bind()
    except TypeError:
        return False
    return True


def _accepts_step_context(
    _fn: LeafActionCallable,
    signature: inspect.Signature,
) -> TypeGuard[Callable[[StepRunContext], object]]:
    try:
        signature.bind(object())
    except TypeError:
        return False
    return True


@dataclass(frozen=True, slots=True, init=False)
class LeafAction:
    """封装叶子 action，并在构造时确定 runtime 的调用方式。

    runtime 只使用以下两种形式之一调用 action：

    - `fn()`：action 不需要运行上下文
    - `fn(ctx)`：action 接收当前 step 的 `StepRunContext`

    构造函数通过签名绑定提前选择调用形式。若 callable 同时支持零参数和
    单参数调用，则固定使用 `fn()`；这样不会把运行上下文错误传给
    `lambda value=value: ...` 这类通过默认参数捕获业务值的闭包。

    runtime 不会把其它业务参数传给 action。需要业务值时由闭包捕获；
    同时需要运行上下文时，则把唯一的 runtime 实参位置留给 `ctx`。
    """

    _call: _LeafActionCall

    def __init__(self, fn: LeafActionCallable) -> None:
        try:
            signature = inspect.signature(fn)
        except (TypeError, ValueError) as exc:
            raise GraphError(
                f"step action has no inspectable signature: {fn!r}: {exc}"
            ) from exc

        if _accepts_no_args(fn, signature):
            # 同时支持零参和单参的 callable 固定按零参调用，避免把
            # `lambda value=value: ...` 的闭包默认值误覆盖成运行上下文。
            call: _LeafActionCall = _NoArgsCall(fn=fn)
        elif _accepts_step_context(fn, signature):
            call = _StepContextCall(fn=fn)
        else:
            raise GraphError(
                "step action must accept either no args or one StepRunContext"
            )

        object.__setattr__(self, "_call", call)

    @property
    def fn(self) -> LeafActionCallable:
        return self._call.fn

    def invoke(self, run_ctx: StepRunContext) -> object:
        match self._call:
            case _NoArgsCall(fn=fn):
                return fn()
            case _StepContextCall(fn=fn):
                return fn(run_ctx)


type StepAction = LeafAction | Graph
type StepActionInput = StepAction | LeafActionCallable
type ConditionalEdgePredicate = Callable[["StepReadContext"], bool]


@dataclass(frozen=True, slots=True)
class RuntimeCondition:
    """必须在执行期求值的条件及其显式结果依赖。

    `reads` 不属于普通 `after` 边，但条件只有在这些 step 完成后才可求值，
    因而它们同样参与 DAG 环检测和 runtime ready 判定。
    """

    when: ConditionalEdgePredicate
    reads: tuple[Step, ...] = ()


def runtime_condition(
    *,
    when: ConditionalEdgePredicate,
    reads: tuple[Step, ...] = (),
) -> RuntimeCondition:
    return RuntimeCondition(when=when, reads=reads)


type Condition = StaticCondition | RuntimeCondition


@dataclass(frozen=True, slots=True)
class ConditionalPrerequisite:
    step: Step
    condition: Condition = field(kw_only=True)


type StepPrerequisite = Step | ConditionalPrerequisite


def prerequisite_step(dep: StepPrerequisite) -> Step:
    if isinstance(dep, ConditionalPrerequisite):
        return dep.step
    return dep


def _validated_blocking_arcs(
    steps: tuple[Step, ...],
    edges: tuple[GraphEdge, ...],
    *,
    known_steps: set[Step],
    graph_label: str,
) -> Iterator[Arc[Step]]:
    """Validate domain edges while projecting every blocking relationship."""

    # 单次投影全部阻塞关系：V 为 step 数，E 为 graph edge 数，B 为所有
    # RuntimeCondition.reads 的引用总数，时间复杂度为 O(V + E + B)。
    seen: set[Arc[Step]] = set()

    def unique_arc(source: Step, target: Step) -> Arc[Step] | None:
        arc = Arc(source=source, target=target)
        if arc in seen:
            return None
        seen.add(arc)
        return arc

    for edge in edges:
        if isinstance(edge, CapabilityBinding):
            if edge.source not in known_steps:
                raise GraphError(
                    f"graph '{graph_label}' has edge from unknown step: {edge.source.label}"
                )
            if edge.target not in known_steps:
                raise GraphError(
                    f"graph '{graph_label}' has edge to unknown step: {edge.target.label}"
                )
            from .capability import (
                _require_capability_output,
            )

            _require_capability_output(edge.source, edge.capability)
            consumer_graph = edge.target.action
            if not isinstance(consumer_graph, Graph):
                raise GraphError(
                    "capability consumer must be a graph-valued step: "
                    f"{edge.target.label}"
                )
            if not any(
                input_port.capability == edge.capability
                for input_port in consumer_graph.capability_inputs
            ):
                raise GraphError(
                    f"component {edge.target.label} does not require capability "
                    f"{edge.capability.name}"
                )
            if isinstance(edge.source.when, RuntimeCondition):
                raise GraphError(
                    f"graph '{graph_label}' capability provider component must not "
                    f"have a runtime condition: {edge.source.label}"
                )
            provider_graph = edge.source.action
            if isinstance(provider_graph, Graph):
                output = next(
                    output
                    for output in provider_graph.capability_outputs
                    if output.capability == edge.capability
                )
                if isinstance(output.ready_after.when, RuntimeCondition):
                    raise GraphError(
                        f"graph '{graph_label}' capability output step must not have "
                        f"a runtime condition: {edge.capability.name}"
                    )
            continue

        arc = unique_arc(edge.source, edge.target)
        if arc is not None:
            yield arc
        if isinstance(edge, ConditionalEdge) and isinstance(
            edge.condition,
            RuntimeCondition,
        ):
            for read_step in edge.condition.reads:
                arc = unique_arc(read_step, edge.target)
                if arc is not None:
                    yield arc

    for step in steps:
        if isinstance(step.when, RuntimeCondition):
            for read_step in step.when.reads:
                arc = unique_arc(read_step, step)
                if arc is not None:
                    yield arc


@dataclass(eq=False, frozen=True, slots=True, init=False)
class Step:
    label: str | None
    action: StepAction
    # `when` 控制的是 step 本身是否参与这次图执行，而不是依赖边是否成立。
    # 约定：
    # - StaticCondition: freeze 阶段直接裁掉该 step
    # - RuntimeCondition: 运行时求值；为 False 时该 step 视为不存在
    # 这条语义对 leaf step 和 graph-action step 完全一致。
    when: Condition | None = None

    def __init__(
        self,
        action: StepActionInput,
        *,
        label: str | None = None,
        when: Condition | None = None,
    ) -> None:
        if label == "":
            raise GraphError("step label must not be empty")

        normalized_action: StepAction
        if isinstance(action, (LeafAction, Graph)):
            normalized_action = action
        elif callable(action):
            normalized_action = LeafAction(fn=action)
        else:
            raise GraphError(
                f"step '{label}' has unsupported action type: {type(action)!r}"
            )

        object.__setattr__(self, "label", label)
        object.__setattr__(self, "action", normalized_action)
        object.__setattr__(self, "when", when)

    def wire(
        self,
        after: tuple[StepPrerequisite, ...] = (),
    ) -> StepWiring:
        """为当前 step 创建一份新的图接线，不修改 step 本身。"""

        return StepWiring(step=self, after=after)

    def __rshift__(self, other: GraphWiringOperand) -> GraphWiring:
        return _sequence_wiring(self, other)

    def __or__(self, other: GraphWiringOperand) -> GraphWiring:
        return _parallel_wiring(self, other)


def resolve_step_label(step: Step, step_id: StepId) -> str:
    """解析 step 在一次具体挂载中的展示名称。"""

    return str(step_id) if step.label is None else step.label


def step(action: StepActionInput) -> Step:
    """把 action 声明为可组合 step；可用时以 action 自身名称作为 label。"""

    match action:
        case Graph():
            label = action.label
        case LeafAction():
            label = _callable_step_label(action.fn)
        case _:
            label = _callable_step_label(action)
    return Step(label=label, action=action)


def _callable_step_label(action: Callable[..., object]) -> str | None:
    label = getattr(action, "__name__", None)
    if not isinstance(label, str) or not label or label == "<lambda>":
        return None
    return label


@dataclass(frozen=True, slots=True)
class StepWiring:
    """把 step 接入当前图时的接线输入。

    `after` 只属于这次图声明；`Graph` 构造时会把它一次解析为
    引用真实 `Step` 对象的边，运行期不保留该包装器。
    """

    step: Step
    after: tuple[StepPrerequisite, ...] = ()


@dataclass(frozen=True, slots=True)
class UnconditionalEdge:
    source: Step
    target: Step


@dataclass(frozen=True, slots=True)
class ConditionalEdge:
    source: Step
    target: Step
    condition: Condition


type GraphEdge = UnconditionalEdge | ConditionalEdge | CapabilityBinding


@dataclass(frozen=True, slots=True)
class GraphWiring:
    """由组合 DSL 产生、尚未物化为 Graph 的规范化接线。"""

    steps: tuple[Step, ...]
    edges: tuple[GraphEdge, ...] = ()

    def _boundary_nodes(self) -> tuple[set[Step], set[Step]]:
        known_steps = set(self.steps)
        sources: set[Step] = set()
        targets: set[Step] = set()

        # `reads` 是条件求值依赖，因此也会改变物化图的真实入口和出口。
        # DSL 中间态可能暂时引用尚未组合进来的 step；只有引用两端均已进入
        # 当前 wiring 时，才把这条关系计入当前组合边界。
        for edge in self.edges:
            sources.add(edge.source)
            targets.add(edge.target)
            if isinstance(edge, ConditionalEdge) and isinstance(
                edge.condition,
                RuntimeCondition,
            ):
                for read_step in edge.condition.reads:
                    if read_step in known_steps and edge.target in known_steps:
                        sources.add(read_step)
                        targets.add(edge.target)
        for step in self.steps:
            if isinstance(step.when, RuntimeCondition):
                for read_step in step.when.reads:
                    if read_step in known_steps:
                        sources.add(read_step)
                        targets.add(step)
        return sources, targets

    @property
    def entries(self) -> tuple[Step, ...]:
        _, targets = self._boundary_nodes()
        return tuple(step for step in self.steps if step not in targets)

    @property
    def exits(self) -> tuple[Step, ...]:
        sources, _ = self._boundary_nodes()
        return tuple(step for step in self.steps if step not in sources)

    def __rshift__(self, other: GraphWiringOperand) -> GraphWiring:
        return _sequence_wiring(self, other)

    def __or__(self, other: GraphWiringOperand) -> GraphWiring:
        return _parallel_wiring(self, other)

    def graph(
        self,
        *,
        label: str | None = None,
        capability_inputs: tuple[CapabilityInput, ...] = (),
        capability_outputs: tuple[CapabilityOutput, ...] = (),
        observation: GraphObservationMessages | None = None,
        static_condition_eval: StaticConditionEval | None = None,
    ) -> Graph:
        """将当前接线显式物化为一张 Graph。"""

        return Graph.from_wiring(
            self,
            label=label,
            capability_inputs=capability_inputs,
            capability_outputs=capability_outputs,
            observation=observation,
            static_condition_eval=static_condition_eval,
        )


type GraphWiringOperand = Step | Graph | GraphWiring
type GraphWiringInput = tuple[StepWiring, ...] | GraphWiring


@dataclass(frozen=True, slots=True)
class Graph:
    """DAG 图结构本体。

    `Graph` 表达的是一张有向无环图（DAG），描述图结构本体，
    以及这张图自己的静态条件求值入口。

    它表达的是：
    - 可选的可读 label
    - 这张图有哪些 step
    - step 之间有哪些依赖边
    - 哪些 step 挂子图
    - 图级 observation
    - 这张图里的 `StaticCondition` 该如何求值

    它不表达的是：
    - 图挂载后的结构身份
    - 这次执行的 results / skip 状态
    - GraphPlan 的展示与 composite 记录模式
    """

    label: str | None = None
    steps: tuple[Step, ...] = field(default_factory=tuple)
    edges: tuple[GraphEdge, ...] = field(default_factory=tuple)
    capability_inputs: tuple[CapabilityInput, ...] = field(default_factory=tuple)
    capability_outputs: tuple[CapabilityOutput, ...] = field(default_factory=tuple)
    observation: GraphObservationMessages = field(
        default_factory=GraphObservationMessages
    )
    static_condition_eval: StaticConditionEval | None = None

    @classmethod
    def from_wiring(
        cls,
        wiring: GraphWiringInput,
        *,
        label: str | None = None,
        capability_inputs: tuple[CapabilityInput, ...] = (),
        capability_outputs: tuple[CapabilityOutput, ...] = (),
        observation: GraphObservationMessages | None = None,
        static_condition_eval: StaticConditionEval | None = None,
    ) -> Graph:
        normalized = (
            wiring
            if isinstance(wiring, GraphWiring)
            else _graph_wiring_from_step_wirings(wiring, label=label)
        )
        return cls(
            label=label,
            steps=normalized.steps,
            edges=normalized.edges,
            capability_inputs=capability_inputs,
            capability_outputs=capability_outputs,
            observation=(
                GraphObservationMessages() if observation is None else observation
            ),
            static_condition_eval=static_condition_eval,
        )

    def __rshift__(self, other: GraphWiringOperand) -> GraphWiring:
        return _sequence_wiring(self, other)

    def __or__(self, other: GraphWiringOperand) -> GraphWiring:
        return _parallel_wiring(self, other)

    def step(
        self,
        *,
        label: str | None = None,
        when: Condition | None = None,
    ) -> Step:
        """把当前图包装成一个可挂载 step。"""

        return Step(
            self,
            label=self.label if label is None else label,
            when=when,
        )

    def incoming_edges(self, step: Step) -> tuple[GraphEdge, ...]:
        return tuple(
            edge
            for edge in self.edges
            if edge.target is step and not isinstance(edge, CapabilityBinding)
        )

    def __post_init__(self) -> None:
        if self.label == "":
            raise GraphError("graph label must not be empty")

        graph_label = "<unlabeled>" if self.label is None else self.label
        _validate_graph_topology(
            steps=self.steps,
            edges=self.edges,
            graph_label=graph_label,
        )
        _validate_capability_interface(
            steps=self.steps,
            capability_inputs=self.capability_inputs,
            capability_outputs=self.capability_outputs,
            graph_label=graph_label,
        )


def _as_graph_wiring(value: GraphWiringOperand) -> GraphWiring:
    match value:
        case GraphWiring():
            return value
        case Step():
            return GraphWiring(steps=(value,))
        case Graph():
            return GraphWiring(steps=(value.step(),))


def _sequence_wiring(
    left: GraphWiringOperand,
    right: GraphWiringOperand,
) -> GraphWiring:
    left_wiring = _as_graph_wiring(left)
    right_wiring = _as_graph_wiring(right)
    connecting_edges = tuple(
        UnconditionalEdge(source=source, target=target)
        for source in left_wiring.exits
        for target in right_wiring.entries
    )
    return GraphWiring(
        steps=(*left_wiring.steps, *right_wiring.steps),
        edges=(*left_wiring.edges, *right_wiring.edges, *connecting_edges),
    )


def _parallel_wiring(
    left: GraphWiringOperand,
    right: GraphWiringOperand,
) -> GraphWiring:
    left_wiring = _as_graph_wiring(left)
    right_wiring = _as_graph_wiring(right)
    return GraphWiring(
        steps=(*left_wiring.steps, *right_wiring.steps),
        edges=(*left_wiring.edges, *right_wiring.edges),
    )


def _graph_wiring_from_step_wirings(
    wiring: tuple[StepWiring, ...],
    *,
    label: str | None,
) -> GraphWiring:
    for item in wiring:
        if not isinstance(item, StepWiring):
            raise GraphError(
                "Graph.from_wiring() accepts only StepWiring or GraphWiring"
            )
    steps = tuple(item.step for item in wiring)
    graph_label = "<unlabeled>" if label is None else label
    known_steps = _validated_steps(steps, graph_label=graph_label)
    declared_edges: list[GraphEdge] = []
    for item in wiring:
        target = item.step
        for prerequisite in item.after:
            source = prerequisite_step(prerequisite)
            if source not in known_steps:
                raise GraphError(
                    f"graph '{graph_label}' step '{target.label}' has prerequisite outside this graph: {source.label}"
                )
            declared_edges.append(
                ConditionalEdge(
                    source=source,
                    target=target,
                    condition=prerequisite.condition,
                )
                if isinstance(prerequisite, ConditionalPrerequisite)
                else UnconditionalEdge(source=source, target=target)
            )
    return GraphWiring(steps=steps, edges=tuple(declared_edges))


def _validate_graph_topology(
    *,
    steps: tuple[Step, ...],
    edges: tuple[GraphEdge, ...],
    graph_label: str,
) -> None:
    known_steps = _validated_steps(steps, graph_label=graph_label)
    step_indexes = {step: index for index, step in enumerate(steps)}
    blocking_arcs = tuple(
        _validated_blocking_arcs(
            steps,
            edges,
            known_steps=known_steps,
            graph_label=graph_label,
        )
    )
    try:
        cycle_indexes = dag_cycle(
            len(steps),
            [
                (
                    step_indexes.get(arc.source, len(steps)),
                    step_indexes.get(arc.target, len(steps)),
                )
                for arc in blocking_arcs
            ],
        )
    except ValueError as exc:
        raise GraphError(str(exc)) from exc
    if cycle_indexes is not None:
        cycle = " -> ".join(
            resolve_step_label(steps[index], StepId((index,)))
            for index in cycle_indexes
        )
        raise GraphError(f"graph '{graph_label}' has dependency cycle: {cycle}")


def _validate_capability_interface(
    *,
    steps: tuple[Step, ...],
    capability_inputs: tuple[CapabilityInput, ...],
    capability_outputs: tuple[CapabilityOutput, ...],
    graph_label: str,
) -> None:
    seen_inputs = set()
    for input_port in capability_inputs:
        mount_count = _descendant_step_mount_count(steps, input_port.needed_by)
        if mount_count == 0:
            raise GraphError(
                f"graph '{graph_label}' capability input "
                f"'{input_port.capability.name}' references a step outside this graph"
            )
        if mount_count > 1:
            raise GraphError(
                f"graph '{graph_label}' capability input "
                f"'{input_port.capability.name}' references a step mounted more than once"
            )
        if input_port.capability in seen_inputs:
            raise GraphError(
                f"graph '{graph_label}' declares duplicate capability input: "
                f"{input_port.capability.name}"
            )
        seen_inputs.add(input_port.capability)

    seen_outputs = set()
    for output_port in capability_outputs:
        mount_count = _descendant_step_mount_count(steps, output_port.ready_after)
        if mount_count == 0:
            raise GraphError(
                f"graph '{graph_label}' capability output "
                f"'{output_port.capability.name}' references a step outside this graph"
            )
        if mount_count > 1:
            raise GraphError(
                f"graph '{graph_label}' capability output "
                f"'{output_port.capability.name}' references a step mounted more than once"
            )
        if output_port.capability in seen_outputs:
            raise GraphError(
                f"graph '{graph_label}' declares duplicate capability output: "
                f"{output_port.capability.name}"
            )
        seen_outputs.add(output_port.capability)


def _descendant_step_mount_count(steps: tuple[Step, ...], target: Step) -> int:
    count = 0
    for step in steps:
        if step is target:
            count += 1
        if isinstance(step.action, Graph):
            count += _descendant_step_mount_count(step.action.steps, target)
    return count


def _validated_steps(
    steps: tuple[Step, ...],
    *,
    graph_label: str,
) -> set[Step]:
    known_steps: set[Step] = set()
    for index, step in enumerate(steps):
        if not isinstance(step, Step):
            raise GraphError(
                "Graph.steps accepts only Step; use Graph.from_wiring() for StepWiring"
            )
        if step in known_steps:
            raise GraphError(
                f"graph '{graph_label}' contains the same step more than once: "
                f"{resolve_step_label(step, StepId((index,)))}"
            )
        known_steps.add(step)
    return known_steps
