from __future__ import annotations

import inspect
from collections.abc import Callable, Iterator, Mapping
from dataclasses import dataclass, field
from types import MappingProxyType
from typing import TYPE_CHECKING, Literal, TypeGuard

from ._native import NativeGraph, dag_cycle
from .capability import CapabilitiesInput, CapabilityBinding, CapabilityInterface
from .errors import GraphError
from .static_conditions import (
    BoundStaticCondition,
    StaticConditions,
    StaticConditionsInput,
    StaticTarget,
    resolve_static_bindings,
    static_groups,
)
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
    from .option import Option
    from .results import StepRunContext


@dataclass(frozen=True, slots=True)
class GraphObservationMessages:
    before: str | None = None
    after_success: str | None = None


type LeafActionCallable = Callable[..., object]


@dataclass(frozen=True, slots=True)
class _NoArgsCall:
    fn: Callable[[], object]


@dataclass(frozen=True, slots=True)
class _StepContextCall:
    fn: Callable[[StepRunContext], object]


@dataclass(frozen=True, slots=True)
class _ResultCall:
    fn: LeafActionCallable


type _LeafActionCall = _NoArgsCall | _StepContextCall | _ResultCall


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


@dataclass(frozen=True, slots=True, init=False, eq=False)
class LeafAction:
    """封装 action；数据参数由图的结果边显式绑定。

    无数据输入时调用 fn() 或 fn(ctx)，两者都可调用时固定选择零参，
    保留闭包默认参数。有输入时固定调用 fn(ctx, *inputs)，输入均为 Option。
    完整签名在图接线后校验，不按参数名或类型标注猜测调用方式。
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
            # 输入数量属于图的接线，在构图时校验完整调用签名。
            call = _ResultCall(fn)

        object.__setattr__(self, "_call", call)

    @property
    def fn(self) -> LeafActionCallable:
        return self._call.fn

    def validate_inputs(self, count: int) -> None:
        try:
            if count:
                inspect.signature(self.fn).bind(object(), *([object()] * count))
            elif isinstance(self._call, _ResultCall):
                raise TypeError("action requires result inputs")
        except TypeError as exc:
            raise GraphError(
                f"action signature does not match {count} result inputs: {exc}"
            ) from exc

    def invoke(
        self, run_ctx: StepRunContext, inputs: tuple[Option[object], ...] = ()
    ) -> object:
        if inputs:
            return self.fn(run_ctx, *inputs)
        match self._call:
            case _NoArgsCall(fn=fn):
                return fn()
            case _StepContextCall(fn=fn):
                return fn(run_ctx)
            case _ResultCall():
                raise GraphError("action requires result inputs")


# Python 只持有原生子图的整体边界，不暴露其中的 Rust action。
type StepAction = LeafAction | Graph | NativeGraph
type StepActionInput = StepAction | LeafActionCallable
# Step 尚不携带返回值类型；参数按 inputs 顺序传入 Option[object]。
# 不读取 callback 的类型标注来推断调用约定。
type ConditionalEdgePredicate = Callable[..., bool]


@dataclass(frozen=True, slots=True, eq=False)
class RuntimeCondition:
    """执行期判断声明；按本对象绑定的输入顺序接收 Option 结果。"""

    when: ConditionalEdgePredicate


type ResultReceiver = StepAction | RuntimeCondition
type ResultBindings = Mapping[ResultReceiver, tuple[Step, ...]]


def runtime_condition(
    *,
    when: ConditionalEdgePredicate,
) -> RuntimeCondition:
    return RuntimeCondition(when=when)


@dataclass(frozen=True, slots=True)
class ConditionalPrerequisite:
    step: Step
    condition: RuntimeCondition = field(kw_only=True)


type StepPrerequisite = Step | ConditionalPrerequisite


def prerequisite_step(dep: StepPrerequisite) -> Step:
    if isinstance(dep, ConditionalPrerequisite):
        return dep.step
    return dep


def _validated_blocking_arcs(
    edges: tuple[GraphEdge, ...],
    *,
    known_steps: set[Step],
    graph_label: str,
) -> Iterator[Arc[Step]]:
    """Validate domain edges while projecting every blocking relationship."""

    # 普通边、条件边和结果边均显式保存在 edges。投影 O(E)，连同
    # 节点校验与环检测，总时间 O(V + E)，辅助空间 O(V + E)。
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
            if edge.capability not in consumer_graph.capabilities.inputs:
                raise GraphError(
                    f"component {edge.target.label} does not require capability "
                    f"{edge.capability.name}"
                )
            if isinstance(edge.source.condition, RuntimeCondition):
                raise GraphError(
                    f"graph '{graph_label}' capability provider component must not "
                    f"have a runtime condition: {edge.source.label}"
                )
            provider_graph = edge.source.action
            if isinstance(provider_graph, Graph):
                output = provider_graph.capabilities.outputs[edge.capability]
                if isinstance(output.condition, RuntimeCondition):
                    raise GraphError(
                        f"graph '{graph_label}' capability output step must not have "
                        f"a runtime condition: {edge.capability.name}"
                    )
            continue

        arc = unique_arc(edge.source, edge.target)
        if arc is not None:
            yield arc


@dataclass(eq=False, frozen=True, slots=True, init=False)
class Step:
    label: str | None
    action: StepAction
    # `condition` 控制的是 step 本身是否参与这次图执行，而不是依赖边是否成立。
    # 约定：
    # - RuntimeCondition: 运行时求值；为 False 时该 step 进入 Skipped 终态
    # 这条语义对 leaf step 和 graph-action step 完全一致。
    condition: RuntimeCondition | None = None

    def __init__(
        self,
        action: StepActionInput,
        *,
        label: str | None = None,
        condition: RuntimeCondition | None = None,
    ) -> None:
        if label == "":
            raise GraphError("step label must not be empty")

        normalized_action: StepAction
        if isinstance(action, (LeafAction, Graph, NativeGraph)):
            normalized_action = action
        elif callable(action):
            normalized_action = LeafAction(fn=action)
        else:
            raise GraphError(
                f"step '{label}' has unsupported action type: {type(action)!r}"
            )

        object.__setattr__(self, "label", label)
        object.__setattr__(self, "action", normalized_action)
        object.__setattr__(self, "condition", condition)

    def wire(
        self,
        after: tuple[StepPrerequisite, ...] = (),
        *,
        inputs: ResultBindings = MappingProxyType({}),
    ) -> StepWiring:
        """为当前 step 创建一份新的图接线，不修改 step 本身。"""

        return StepWiring(step=self, after=after, inputs=inputs)

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
        case NativeGraph():
            label = action.label
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

    `after` 与 `inputs` 只属于这次图声明；`Graph` 构造时会把它一次解析为
    引用真实 `Step` 对象的边，运行期不保留该包装器。
    """

    step: Step
    after: tuple[StepPrerequisite, ...] = ()
    inputs: ResultBindings = field(default_factory=lambda: MappingProxyType({}))

    def __post_init__(self) -> None:
        object.__setattr__(self, "inputs", MappingProxyType(dict(self.inputs)))


@dataclass(frozen=True, slots=True)
class UnconditionalEdge:
    source: Step
    target: Step


@dataclass(frozen=True, slots=True)
class ConditionalEdge:
    source: Step
    target: Step
    condition: RuntimeCondition


@dataclass(frozen=True, slots=True)
class ResultEdge:
    """将来源结果绑定到目标步骤中指定声明对象的位置参数。"""

    source: Step
    target: Step
    argument_index: int
    receiver: ResultReceiver


type GraphEdge = UnconditionalEdge | ConditionalEdge | CapabilityBinding | ResultEdge


@dataclass(frozen=True, slots=True)
class GraphWiring:
    """由组合 DSL 产生、尚未物化为 Graph 的规范化接线。"""

    steps: tuple[Step, ...]
    edges: tuple[GraphEdge, ...] = ()
    static: tuple[StaticConditions, ...] = ()

    def _boundary_nodes(self) -> tuple[set[Step], set[Step]]:
        sources: set[Step] = set()
        targets: set[Step] = set()

        # 所有执行和数据关系均来自显式边，O(E) 推导组合边界。
        for edge in self.edges:
            sources.add(edge.source)
            targets.add(edge.target)
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
        capabilities: CapabilitiesInput | None = None,
        observation: GraphObservationMessages | None = None,
        static: StaticConditionsInput = (),
    ) -> Graph:
        """将当前接线显式物化为一张 Graph。"""

        return Graph.from_wiring(
            self,
            label=label,
            capabilities=capabilities,
            observation=observation,
            static=static,
        )


type GraphWiringOperand = Step | Graph | GraphWiring
type GraphWiringInput = tuple[StepWiring, ...] | GraphWiring


@dataclass(frozen=True, slots=True, init=False)
class Graph:
    """DAG 图结构本体。

    `Graph` 表达的是一张有向无环图（DAG），描述图结构本体，
    以及这张图绑定了求值器的静态选择。

    它表达的是：
    - 可选的可读 label
    - 这张图有哪些 step
    - step 之间有哪些依赖边
    - 哪些 step 挂子图
    - 图级 observation
    - 这张图的节点和边在哪些静态条件下参与

    它不表达的是：
    - 图挂载后的结构身份
    - 这次执行的 results / skip 状态
    - GraphExecution 的展示与 composite 记录模式
    """

    label: str | None = None
    steps: tuple[Step, ...] = field(default_factory=tuple)
    edges: tuple[GraphEdge, ...] = field(default_factory=tuple)
    capabilities: CapabilityInterface = field(default_factory=CapabilityInterface)
    observation: GraphObservationMessages = field(
        default_factory=GraphObservationMessages
    )
    static: tuple[StaticConditions, ...] = ()
    _static_bindings: Mapping[StaticTarget, BoundStaticCondition] = field(
        init=False, repr=False, compare=False
    )

    def __init__(
        self,
        label: str | None = None,
        steps: tuple[Step, ...] = (),
        edges: tuple[GraphEdge, ...] = (),
        capabilities: CapabilitiesInput | None = None,
        observation: GraphObservationMessages | None = None,
        static: StaticConditionsInput = (),
    ) -> None:
        object.__setattr__(self, "label", label)
        object.__setattr__(self, "steps", steps)
        object.__setattr__(self, "edges", edges)
        object.__setattr__(
            self,
            "capabilities",
            CapabilityInterface()
            if capabilities is None
            else CapabilityInterface.resolve(capabilities),
        )
        object.__setattr__(
            self,
            "observation",
            GraphObservationMessages() if observation is None else observation,
        )
        object.__setattr__(self, "static", static_groups(static))
        self._validate()
        object.__setattr__(
            self,
            "_static_bindings",
            resolve_static_bindings(
                self.static,
                steps=self.steps,
                edge_pairs={
                    (edge.source, edge.target)
                    for edge in self.edges
                    if not isinstance(edge, CapabilityBinding)
                }
                if self.static
                else set(),
            ),
        )

    @classmethod
    def from_wiring(
        cls,
        wiring: GraphWiringInput,
        *,
        label: str | None = None,
        capabilities: CapabilitiesInput | None = None,
        observation: GraphObservationMessages | None = None,
        static: StaticConditionsInput = (),
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
            capabilities=capabilities,
            observation=(
                GraphObservationMessages() if observation is None else observation
            ),
            static=(*normalized.static, *static_groups(static)),
        )

    def __rshift__(self, other: GraphWiringOperand) -> GraphWiring:
        return _sequence_wiring(self, other)

    def __or__(self, other: GraphWiringOperand) -> GraphWiring:
        return _parallel_wiring(self, other)

    def step(
        self,
        *,
        label: str | None = None,
        condition: RuntimeCondition | None = None,
    ) -> Step:
        """把当前图包装成一个可挂载 step。"""

        return Step(
            self,
            label=self.label if label is None else label,
            condition=condition,
        )

    def incoming_edges(self, step: Step) -> tuple[GraphEdge, ...]:
        return tuple(
            edge
            for edge in self.edges
            if edge.target is step and not isinstance(edge, CapabilityBinding)
        )

    @property
    def display_label(self) -> str:
        """返回图名；匿名图使用明确的未命名展示标识，不生成身份。"""

        return "<unlabeled>" if self.label is None else self.label

    def _validate(self) -> None:
        if self.label == "":
            raise GraphError("graph label must not be empty")

        graph_label = self.display_label
        _validate_graph_topology(
            steps=self.steps,
            edges=self.edges,
            graph_label=graph_label,
        )
        _validate_result_bindings(self.steps, self.edges)
        _validate_capability_interface(
            steps=self.steps,
            capabilities=self.capabilities,
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
        static=(*left_wiring.static, *right_wiring.static),
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
        static=(*left_wiring.static, *right_wiring.static),
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
        receivers = {id(target.action), id(target.condition)}
        receivers.update(
            id(dep.condition)
            for dep in item.after
            if isinstance(dep, ConditionalPrerequisite)
        )
        for receiver in item.inputs:
            if id(receiver) not in receivers or not isinstance(
                receiver, (LeafAction, RuntimeCondition)
            ):
                raise GraphError(
                    "result receiver does not belong to this step or is not callable"
                )
        declared_edges.extend(
            ResultEdge(
                source=source, target=target, argument_index=index, receiver=receiver
            )
            for receiver, sources in item.inputs.items()
            for index, source in enumerate(sources)
        )
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


def _validate_result_bindings(
    steps: tuple[Step, ...], edges: tuple[GraphEdge, ...]
) -> None:
    # O(V + E)，不为每个消费者重新扫描所有边。
    slots: dict[tuple[Step, ResultReceiver], set[int]] = {}
    conditions: dict[Step, set[RuntimeCondition]] = {
        step: {step.condition} if step.condition is not None else set()
        for step in steps
    }
    for edge in edges:
        if isinstance(edge, ConditionalEdge):
            conditions[edge.target].add(edge.condition)
    for edge in edges:
        if isinstance(edge, ResultEdge):
            if not (
                isinstance(edge.receiver, LeafAction)
                and edge.receiver is edge.target.action
                or isinstance(edge.receiver, RuntimeCondition)
                and edge.receiver in conditions[edge.target]
            ):
                raise GraphError(
                    "result receiver does not belong to this step or is not callable"
                )
            indexes = slots.setdefault((edge.target, edge.receiver), set())
            if edge.argument_index < 0 or edge.argument_index in indexes:
                raise GraphError(
                    "result argument indexes must be unique and non-negative"
                )
            indexes.add(edge.argument_index)
    for indexes in slots.values():
        if indexes != set(range(len(indexes))):
            raise GraphError("result argument indexes must be contiguous from zero")
    for step in steps:
        if isinstance(step.action, LeafAction):
            step.action.validate_inputs(len(slots.get((step, step.action), ())))
        for condition in conditions[step]:
            count = len(slots.get((step, condition), ()))
            try:
                inspect.signature(condition.when).bind(*([object()] * count))
            except (TypeError, ValueError) as exc:
                raise GraphError(
                    f"condition signature does not match {count} result inputs: {exc}"
                ) from exc


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
    capabilities: CapabilityInterface,
    graph_label: str,
) -> None:
    # 一次遍历挂载结构建立计数，避免为每个端口重新扫描全部子图。
    # V 为所有挂载的步骤数，R 为端口引用数：时间 O(V + R)，空间 O(V)。
    if not capabilities.inputs and not capabilities.outputs:
        return
    mount_counts: dict[Step, int] = {}
    pending = list(steps)
    while pending:
        step = pending.pop()
        mount_counts[step] = mount_counts.get(step, 0) + 1
        if isinstance(step.action, Graph):
            pending.extend(step.action.steps)

    for capability, input_steps in capabilities.inputs.items():
        for step in input_steps:
            mount_count = mount_counts.get(step, 0)
            if mount_count == 0:
                raise GraphError(
                    f"graph '{graph_label}' capability input "
                    f"'{capability.name}' references a step outside this graph"
                )
            if mount_count > 1:
                raise GraphError(
                    f"graph '{graph_label}' capability input "
                    f"'{capability.name}' references a step mounted more than once"
                )
    for capability, step in capabilities.outputs.items():
        mount_count = mount_counts.get(step, 0)
        if mount_count == 0:
            raise GraphError(
                f"graph '{graph_label}' capability output "
                f"'{capability.name}' references a step outside this graph"
            )
        if mount_count > 1:
            raise GraphError(
                f"graph '{graph_label}' capability output "
                f"'{capability.name}' references a step mounted more than once"
            )


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
