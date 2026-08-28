from __future__ import annotations

from dataclasses import dataclass, replace
from typing import TYPE_CHECKING

from .errors import GraphError
from .frozen import (
    FrozenCapabilityBinding,
    FrozenGraph,
    FrozenRuntimeCondition,
    RuntimeConditionalEdge,
    freeze_graph,
)
from .results import GraphScope
from .topology import Arc, DAGTopology, DirectedTopology, TopologyCycleError

if TYPE_CHECKING:
    from collections.abc import Mapping

    from .model import (
        ConditionalEdgePredicate,
        Graph,
        GraphObserve,
        LeafAction,
        Step,
        StepId,
        StepLabelPath,
    )


@dataclass(frozen=True, slots=True)
class CompiledStepRef:
    """引用一个会实际执行 action 的编译叶子。"""

    id: StepId


@dataclass(frozen=True, slots=True)
class CompiledCompositeRef:
    """引用一个只承担组合生命周期的编译边界。"""

    id: StepId


type CompiledNodeRef = CompiledStepRef | CompiledCompositeRef


def compiled_node_id(ref: CompiledNodeRef) -> StepId:
    """取得编译节点引用指向的全局唯一 id。"""

    return ref.id


@dataclass(frozen=True, slots=True)
class CompiledRuntimeCondition:
    """运行期 predicate 及其已经映射到编译节点的结果依赖。

    `reads` 中的节点全部终态后，runtime 才能调用 `when`。predicate
    使用原声明作用域读取结果，编译过程不会提前求值。
    """

    when: ConditionalEdgePredicate
    reads: tuple[CompiledNodeRef, ...]


@dataclass(frozen=True, slots=True)
class CompiledConditionalDependency:
    """只有条件成立时才要求来源节点完成的编译依赖。"""

    node: CompiledNodeRef
    condition: CompiledRuntimeCondition


type CompiledPrerequisite = CompiledNodeRef | CompiledConditionalDependency


def compiled_prerequisite_node(
    prerequisite: CompiledPrerequisite,
) -> CompiledNodeRef:
    """取得 prerequisite 最终约束的节点，忽略条件是否启用该约束。"""

    if isinstance(prerequisite, CompiledConditionalDependency):
        return prerequisite.node
    return prerequisite


@dataclass(frozen=True, slots=True)
class CompiledStepContext:
    """编译节点映射回声明图作用域所需的上下文。

    编译会把嵌套节点编码成全局唯一 id，但运行期条件、结果引用和日志
    仍使用声明时的路径与图作用域。该对象保存这种映射，供 leaf 和
    composite 共用，不携带调度状态。
    """

    label_path: StepLabelPath
    graph_label_path: StepLabelPath
    graph_step_ids: Mapping[Step, StepId]
    ancestor_graph_scopes: tuple[GraphScope, ...]
    ancestor_composite_ids: tuple[StepId, ...]


@dataclass(frozen=True, slots=True)
class CompiledStep:
    """完全扁平化后会被执行器提交给 worker 的叶子节点。"""

    ref: CompiledStepRef
    action: LeafAction
    after: tuple[CompiledPrerequisite, ...]
    when: CompiledRuntimeCondition | None
    owner: CompiledCompositeRef | None
    context: CompiledStepContext

    @property
    def id(self) -> StepId:
        return self.ref.id


@dataclass(frozen=True, slots=True)
class CompiledComposite:
    """原始子图在编译产物中的非执行生命周期投影。

    子图 action 已经展开为 `CompiledStep`；该类型只保留直接子节点、依赖、
    条件和 observation。runtime 会追踪它的进入与完成，但不会把它提交
    给 worker，也不会为它生成伪造的 no-op step。
    """

    ref: CompiledCompositeRef
    source_graph: Graph
    graph_label: str
    children: tuple[CompiledNodeRef, ...]
    after: tuple[CompiledPrerequisite, ...]
    when: CompiledRuntimeCondition | None
    owner: CompiledCompositeRef | None
    observe: GraphObserve
    context: CompiledStepContext
    graph_step_ids: Mapping[Step, StepId]

    @property
    def id(self) -> StepId:
        return self.ref.id

    @property
    def label_path(self) -> StepLabelPath:
        return self.context.label_path


type CompiledNode = CompiledStep | CompiledComposite


@dataclass(frozen=True, slots=True)
class GraphCompilation:
    """声明图的扁平执行计划和组合生命周期记录。

    `steps` 是唯一的 action 执行事实来源。`composites` 不保存可执行子图，
    只描述原始子图边界；`roots` 则明确给出根图直接拥有的节点。
    """

    source_graph: Graph
    source_frozen_graph: FrozenGraph
    label: str
    steps: tuple[CompiledStep, ...]
    composites: tuple[CompiledComposite, ...]
    roots: tuple[CompiledNodeRef, ...]

    @property
    def observe(self) -> GraphObserve:
        return self.source_graph.observe

    def __post_init__(self) -> None:
        # 编译产物会被 runtime 直接消费，构造时必须一次性验证引用图，
        # 避免把结构错误延迟到部分 step 已经执行之后。
        if not self.label:
            raise GraphError("compiled graph label must not be empty")

        # 单次建立节点、owner 和声明作用域索引。设 V 为节点数、S 为所有不同
        # 声明作用域中的 step 总数：时间 O(V + S)，额外空间 O(V + S)。后续
        # composite 校验必须查询这些索引，不能为每个 composite 重扫全部节点。
        nodes: dict[StepId, CompiledNode] = {}
        child_ids_by_owner: dict[CompiledCompositeRef, set[StepId]] = {}
        ownerless_node_ids: set[StepId] = set()
        scope_step_ids: dict[int, frozenset[StepId]] = {}
        for node in (*self.steps, *self.composites):
            if node.id in nodes:
                raise GraphError(f"compiled graph has duplicate node id: {node.id}")
            nodes[node.id] = node
            if node.owner is None:
                ownerless_node_ids.add(node.id)
            else:
                child_ids_by_owner.setdefault(node.owner, set()).add(node.id)
            scope = node.context.graph_step_ids
            scope_key = id(scope)
            if scope_key not in scope_step_ids:
                scope_step_ids[scope_key] = frozenset(scope.values())

        root_ids = tuple(root.id for root in self.roots)
        if len(root_ids) != len(set(root_ids)):
            raise GraphError("compiled graph has duplicate root references")
        for root in self.roots:
            node = _require_compiled_node(nodes, root)
            if node.owner is not None:
                raise GraphError(f"compiled root has an owner: {node.id}")
        if set(root_ids) != ownerless_node_ids:
            raise GraphError("compiled graph roots do not match ownerless nodes")

        # 节点、直接依赖和 children 只遍历一次：时间 O(V + A)，其中 A 为
        # prerequisite 与 composite-child 引用总数；不再含 O(C * V) 扫描。
        for step in self.steps:
            _validate_compiled_node(
                nodes,
                step,
                scope_step_ids=scope_step_ids[id(step.context.graph_step_ids)],
            )
        for composite in self.composites:
            _validate_compiled_node(
                nodes,
                composite,
                scope_step_ids=scope_step_ids[id(composite.context.graph_step_ids)],
            )
            child_ids = tuple(child.id for child in composite.children)
            if len(child_ids) != len(set(child_ids)):
                raise GraphError(
                    f"compiled composite has duplicate children: {composite.id}"
                )
            for child in composite.children:
                child_node = _require_compiled_node(nodes, child)
                if child_node.owner != composite.ref:
                    raise GraphError(
                        "compiled composite child has a different owner: "
                        f"{child_node.id}"
                    )
            expected_child_ids = child_ids_by_owner.get(composite.ref, set())
            if set(child_ids) != expected_child_ids:
                raise GraphError(
                    "compiled composite children do not match owned nodes: "
                    f"{composite.id}"
                )

        topology = DirectedTopology(
            nodes=tuple(nodes),
            arcs=tuple(
                Arc(source=dependency, target=node.id)
                for node in nodes.values()
                for dependency in _compiled_node_dependency_ids(node)
            ),
        )
        try:
            DAGTopology(topology)
        except TopologyCycleError as exc:
            cycle = " -> ".join(str(item) for item in exc.cycle)
            raise GraphError(f"compiled graph has dependency cycle: {cycle}") from exc


def _require_compiled_node(
    nodes: dict[StepId, CompiledNode],
    ref: CompiledNodeRef,
) -> CompiledNode:
    """解析 typed ref，并验证引用类别与实际节点类别一致。

    `StepId` 在扁平图中全局唯一，但只有 id 存在还不够：
    `CompiledStepRef` 不能指向 `CompiledComposite`，反之亦然。
    """

    node = nodes.get(ref.id)
    if node is None:
        raise GraphError(f"compiled graph references unknown node: {ref.id}")
    if isinstance(ref, CompiledStepRef) != isinstance(node, CompiledStep):
        raise GraphError(f"compiled graph reference kind does not match node: {ref.id}")
    return node


def _validate_compiled_node(
    nodes: dict[StepId, CompiledNode],
    node: CompiledNode,
    *,
    scope_step_ids: frozenset[StepId],
) -> None:
    """验证单个节点的声明作用域、所有权和直接依赖引用。"""

    context = node.context
    if not context.label_path or context.graph_label_path != context.label_path[:-1]:
        raise GraphError(f"compiled node has invalid source context: {node.id}")
    if node.id not in scope_step_ids:
        raise GraphError(
            f"compiled node source scope does not contain its id: {node.id}"
        )
    if node.owner is not None:
        owner = _require_compiled_node(nodes, node.owner)
        if not isinstance(owner, CompiledComposite):
            raise GraphError(f"compiled node owner is not a composite: {node.id}")
    for prerequisite in node.after:
        _require_compiled_node(
            nodes,
            compiled_prerequisite_node(prerequisite),
        )
        if isinstance(prerequisite, CompiledConditionalDependency):
            _validate_compiled_condition(nodes, prerequisite.condition)
    if node.when is not None:
        _validate_compiled_condition(nodes, node.when)


def _validate_compiled_condition(
    nodes: dict[StepId, CompiledNode],
    condition: CompiledRuntimeCondition,
) -> None:
    """验证运行期条件读取的每个结果来源仍存在于编译产物中。"""

    for read in condition.reads:
        _require_compiled_node(nodes, read)


def _compiled_node_dependency_ids(node: CompiledNode) -> set[StepId]:
    """收集最终编译节点用于保守环检测的全部阻塞关系。"""

    dependency_ids: set[StepId] = set()
    for prerequisite in node.after:
        dependency_ids.add(compiled_prerequisite_node(prerequisite).id)
        if isinstance(prerequisite, CompiledConditionalDependency):
            dependency_ids.update(ref.id for ref in prerequisite.condition.reads)
    if node.when is not None:
        dependency_ids.update(ref.id for ref in node.when.reads)
    return dependency_ids


@dataclass(frozen=True, slots=True)
class _CompiledGraphFragment:
    """递归编译一层冻结图产生的可组合片段。

    `steps` 与 `composites` 包含当前图及全部后代的扁平编译节点；
    `roots` 只包含当前图直接声明的节点引用。父层据此填写
    `CompiledComposite.children`，无需重新扫描扁平节点或推断层级。

    片段不是可独立执行的图，也不重复保存依赖关系；它只用于递归编译时
    汇集结果，最终由根片段一次性构造 `GraphCompilation`。
    """

    steps: tuple[CompiledStep, ...]
    composites: tuple[CompiledComposite, ...]
    roots: tuple[CompiledNodeRef, ...]
    capability_bindings: tuple[FrozenCapabilityBinding, ...]


def compile_graph(source_graph: Graph, *, root_label: str) -> GraphCompilation:
    """冻结声明图并把所有声明期已知子图展开为扁平执行计划。"""

    frozen_graph = freeze_graph(source_graph, root_label=root_label)
    fragment = _compile_graph_fragment(
        frozen_graph,
        graph_label_path=(),
        owner=None,
    )
    fragment = _apply_capability_bindings(fragment)
    return GraphCompilation(
        source_graph=source_graph,
        source_frozen_graph=frozen_graph,
        label=frozen_graph.label,
        steps=fragment.steps,
        composites=fragment.composites,
        roots=fragment.roots,
    )


def _compile_graph_fragment(
    graph: FrozenGraph,
    *,
    graph_label_path: StepLabelPath,
    owner: CompiledCompositeRef | None,
    ancestor_graph_scopes: tuple[GraphScope, ...] = (),
    ancestor_composite_ids: tuple[StepId, ...] = (),
) -> _CompiledGraphFragment:
    """递归展开冻结图，同时保留每个 graph-valued step 的组合记录。

    当前图中的每个声明 step 都先获得 typed ref，随后才转换依赖和条件，
    因而同层前向引用不依赖声明顺序。叶子变成 `CompiledStep`；子图自身
    变成 `CompiledComposite`，其后代继续展开到同一份扁平节点集合。

    `owner` 表示当前图由哪个 composite 持有；根图传入 `None`。当前层
    产生的所有节点共享该 owner，而递归子图中的直接节点由对应的新
    composite 持有。
    """

    source_step_ids = {step.source_step: step.id for step in graph.steps}
    step_refs: dict[StepId, CompiledNodeRef] = {}
    for step in graph.steps:
        step_refs[step.id] = (
            CompiledCompositeRef(step.id)
            if isinstance(step.action, FrozenGraph)
            else CompiledStepRef(step.id)
        )

    steps: list[CompiledStep] = []
    composites: list[CompiledComposite] = []
    roots: list[CompiledNodeRef] = []
    capability_bindings = list(graph.capability_bindings)
    for step in graph.steps:
        ref = step_refs[step.id]
        roots.append(ref)
        label_path = (*graph_label_path, step.label)
        context = CompiledStepContext(
            label_path=label_path,
            graph_label_path=graph_label_path,
            graph_step_ids=source_step_ids,
            ancestor_graph_scopes=ancestor_graph_scopes,
            ancestor_composite_ids=ancestor_composite_ids,
        )
        after = tuple(
            _compile_prerequisite(prerequisite, step_refs=step_refs)
            for prerequisite in step.after
        )
        when = (
            None
            if step.when is None
            else _compile_runtime_condition(step.when, step_refs=step_refs)
        )
        action = step.action
        if isinstance(action, FrozenGraph):
            if not isinstance(ref, CompiledCompositeRef):
                raise GraphError(f"compiled graph action has leaf ref: {step.id}")
            child = _compile_graph_fragment(
                action,
                graph_label_path=label_path,
                owner=ref,
                ancestor_graph_scopes=(
                    GraphScope(step_ids=source_step_ids),
                    *ancestor_graph_scopes,
                ),
                ancestor_composite_ids=(step.id, *ancestor_composite_ids),
            )
            composites.append(
                CompiledComposite(
                    ref=ref,
                    source_graph=action.source_graph,
                    graph_label=action.label,
                    children=child.roots,
                    after=after,
                    when=when,
                    owner=owner,
                    observe=action.observe,
                    context=context,
                    graph_step_ids={
                        child_step.source_step: child_step.id
                        for child_step in action.steps
                    },
                )
            )
            steps.extend(child.steps)
            composites.extend(child.composites)
            capability_bindings.extend(child.capability_bindings)
            continue

        if not isinstance(ref, CompiledStepRef):
            raise GraphError(f"compiled leaf action has composite ref: {step.id}")
        steps.append(
            CompiledStep(
                ref=ref,
                action=action,
                after=after,
                when=when,
                owner=owner,
                context=context,
            )
        )

    return _CompiledGraphFragment(
        steps=tuple(steps),
        composites=tuple(composites),
        roots=tuple(roots),
        capability_bindings=tuple(capability_bindings),
    )


def _apply_capability_bindings(
    fragment: _CompiledGraphFragment,
) -> _CompiledGraphFragment:
    """把跨子图能力绑定 lower 为目标节点的普通编译依赖。"""

    refs: dict[StepId, CompiledNodeRef] = {
        node.id: node.ref for node in (*fragment.steps, *fragment.composites)
    }
    sources_by_target: dict[StepId, list[CompiledNodeRef]] = {}
    for binding in fragment.capability_bindings:
        source = refs.get(binding.source)
        if source is None:
            raise GraphError(
                f"compiled capability '{binding.capability.name}' has unknown "
                f"provider step: {binding.source}"
            )
        if binding.target not in refs:
            raise GraphError(
                f"compiled capability '{binding.capability.name}' has unknown "
                f"consumer step: {binding.target}"
            )
        target_sources = sources_by_target.setdefault(binding.target, [])
        if source not in target_sources:
            target_sources.append(source)

    def _with_capability_dependencies[NodeT: (CompiledStep, CompiledComposite)](
        node: NodeT,
    ) -> NodeT:
        additional = tuple(
            source
            for source in sources_by_target.get(node.id, ())
            if source not in node.after
        )
        if not additional:
            return node
        return replace(node, after=(*node.after, *additional))

    return _CompiledGraphFragment(
        steps=tuple(_with_capability_dependencies(step) for step in fragment.steps),
        composites=tuple(
            _with_capability_dependencies(composite)
            for composite in fragment.composites
        ),
        roots=fragment.roots,
        capability_bindings=fragment.capability_bindings,
    )


def _compile_prerequisite(
    prerequisite: StepId | RuntimeConditionalEdge,
    *,
    step_refs: dict[StepId, CompiledNodeRef],
) -> CompiledPrerequisite:
    """把声明层 prerequisite 映射为同层编译节点引用。

    普通依赖只替换引用；条件边同时保留目标节点和运行期条件，不生成
    额外的 gate step。
    """

    if isinstance(prerequisite, RuntimeConditionalEdge):
        return CompiledConditionalDependency(
            node=step_refs[prerequisite.step_id],
            condition=_compile_runtime_condition(
                prerequisite.condition,
                step_refs=step_refs,
            ),
        )
    return step_refs[prerequisite]


def _compile_runtime_condition(
    condition: FrozenRuntimeCondition,
    *,
    step_refs: dict[StepId, CompiledNodeRef],
) -> CompiledRuntimeCondition:
    """保留运行期 predicate，并把其结果读取映射为 typed ref。

    predicate 仍由 runtime 求值；compile 只固定它读取的节点身份，
    避免扁平化后再按局部字符串解析作用域。
    """

    return CompiledRuntimeCondition(
        when=condition.when,
        reads=tuple(step_refs[step_id] for step_id in condition.reads),
    )
