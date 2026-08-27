from __future__ import annotations

from dataclasses import dataclass, field, replace
from types import MappingProxyType
from typing import TYPE_CHECKING

from ._native import NativeCompiledExecutionGraph
from .errors import GraphError
from .frozen import (
    FrozenCapabilityBinding,
    FrozenGraph,
    FrozenRuntimeCondition,
    FrozenStep,
    RuntimeConditionalEdge,
    freeze_graph,
)
from .results import GraphScope

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
    capability_bindings: tuple[FrozenCapabilityBinding, ...] = ()
    _native_graph: NativeCompiledExecutionGraph = field(
        init=False,
        repr=False,
        compare=False,
    )
    _native_nodes: tuple[CompiledNode, ...] = field(
        init=False,
        repr=False,
        compare=False,
    )
    _native_node_indexes: Mapping[StepId, int] = field(
        init=False,
        repr=False,
        compare=False,
    )
    _native_conditions: tuple[CompiledRuntimeCondition, ...] = field(
        init=False,
        repr=False,
        compare=False,
    )

    @property
    def observe(self) -> GraphObserve:
        return self.source_graph.observe

    def __post_init__(self) -> None:
        # 编译产物会被 runtime 直接消费，构造时必须一次性验证引用图，
        # 避免把结构错误延迟到部分 step 已经执行之后。
        if not self.label:
            raise GraphError("compiled graph label must not be empty")

        nodes: dict[StepId, CompiledNode] = {}
        for node in (*self.steps, *self.composites):
            if node.id in nodes:
                raise GraphError(f"compiled graph has duplicate node id: {node.id}")
            nodes[node.id] = node

        root_ids = tuple(root.id for root in self.roots)
        if len(root_ids) != len(set(root_ids)):
            raise GraphError("compiled graph has duplicate root references")
        for root in self.roots:
            node = _require_compiled_node(nodes, root)
            if node.owner is not None:
                raise GraphError(f"compiled root has an owner: {node.id}")
        expected_root_ids = {node.id for node in nodes.values() if node.owner is None}
        if set(root_ids) != expected_root_ids:
            raise GraphError("compiled graph roots do not match ownerless nodes")

        for step in self.steps:
            _validate_compiled_node(nodes, step)
        for composite in self.composites:
            _validate_compiled_node(nodes, composite)
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
            expected_child_ids = {
                node.id for node in nodes.values() if node.owner == composite.ref
            }
            if set(child_ids) != expected_child_ids:
                raise GraphError(
                    "compiled composite children do not match owned nodes: "
                    f"{composite.id}"
                )

        native_nodes = tuple(nodes.values())
        native_node_indexes = {
            node.id: index for index, node in enumerate(native_nodes)
        }
        try:
            native_graph, native_conditions, effective_after = (
                _materialize_native_execution_graph(
                    native_nodes,
                    native_node_indexes,
                    self.capability_bindings,
                )
            )
        except ValueError as exc:
            raise GraphError(str(exc)) from exc

        if any(
            node.after != lowered_after
            for node, lowered_after in zip(native_nodes, effective_after, strict=True)
        ):
            native_nodes = tuple(
                replace(node, after=lowered_after)
                for node, lowered_after in zip(
                    native_nodes,
                    effective_after,
                    strict=True,
                )
            )
            object.__setattr__(
                self,
                "steps",
                tuple(node for node in native_nodes if isinstance(node, CompiledStep)),
            )
            object.__setattr__(
                self,
                "composites",
                tuple(
                    node for node in native_nodes if isinstance(node, CompiledComposite)
                ),
            )

        native_root_ids = {native_nodes[index].id for index in native_graph.roots()}
        if native_root_ids != expected_root_ids:
            raise GraphError("compiled graph native roots do not match ownerless nodes")
        for composite in self.composites:
            composite_index = native_node_indexes[composite.id]
            native_child_ids = {
                native_nodes[index].id
                for index in native_graph.children(composite_index)
            }
            if native_child_ids != {child.id for child in composite.children}:
                raise GraphError(
                    "compiled graph native children do not match composite: "
                    f"{composite.id}"
                )

        object.__setattr__(self, "_native_graph", native_graph)
        object.__setattr__(self, "_native_nodes", native_nodes)
        object.__setattr__(self, "_native_conditions", native_conditions)
        object.__setattr__(
            self,
            "_native_node_indexes",
            MappingProxyType(native_node_indexes),
        )

    def _dependent_refs(self, node_id: StepId) -> tuple[CompiledNodeRef, ...]:
        node_index = self._native_node_indexes[node_id]
        return tuple(
            self._native_nodes[index].ref
            for index in self._native_graph.dependents(node_index)
        )

    def _native_node(self, index: int) -> CompiledNode:
        return self._native_nodes[index]

    def _native_condition(self, index: int) -> CompiledRuntimeCondition:
        return self._native_conditions[index]


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
) -> None:
    """验证单个节点的声明作用域、所有权和直接依赖引用。"""

    context = node.context
    if not context.label_path or context.graph_label_path != context.label_path[:-1]:
        raise GraphError(f"compiled node has invalid source context: {node.id}")
    if node.id not in context.graph_step_ids.values():
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


def _materialize_native_execution_graph(
    nodes: tuple[CompiledNode, ...],
    node_indexes: Mapping[StepId, int],
    capability_bindings: tuple[FrozenCapabilityBinding, ...],
) -> tuple[
    NativeCompiledExecutionGraph,
    tuple[CompiledRuntimeCondition, ...],
    tuple[tuple[CompiledPrerequisite, ...], ...],
]:
    """一次性把 Python 编译结构交给 Rust 建立执行索引。"""

    condition_reads: list[list[int]] = []
    conditions: list[CompiledRuntimeCondition] = []

    def register_condition(condition: CompiledRuntimeCondition) -> int:
        condition_index = len(condition_reads)
        condition_reads.append([node_indexes[read.id] for read in condition.reads])
        conditions.append(condition)
        return condition_index

    composite_indexes = tuple(
        index for index, node in enumerate(nodes) if isinstance(node, CompiledComposite)
    )
    composite_child_graphs = {
        index: graph_index
        for graph_index, index in enumerate(composite_indexes, start=1)
    }
    graph_owners: list[int | None] = [None, *composite_indexes]
    node_graphs: list[int] = []
    prerequisites: list[list[tuple[int, int | None]]] = []
    when_conditions: list[int | None] = []
    for node in nodes:
        node_graphs.append(
            0
            if node.owner is None
            else composite_child_graphs[node_indexes[node.owner.id]]
        )
        prerequisites.append(
            [
                (
                    node_indexes[compiled_prerequisite_node(prerequisite).id],
                    register_condition(prerequisite.condition)
                    if isinstance(prerequisite, CompiledConditionalDependency)
                    else None,
                )
                for prerequisite in node.after
            ]
        )
        when_conditions.append(
            None if node.when is None else register_condition(node.when)
        )

    native_graph = NativeCompiledExecutionGraph(
        node_graphs,
        list(composite_child_graphs.items()),
        graph_owners,
        prerequisites,
        when_conditions,
        condition_reads,
        [
            (
                node_indexes.get(binding.source, len(nodes)),
                node_indexes.get(binding.target, len(nodes)),
            )
            for binding in capability_bindings
        ],
    )
    effective_after = tuple(
        tuple(
            nodes[node_index].ref
            if condition_index is None
            else CompiledConditionalDependency(
                node=nodes[node_index].ref,
                condition=conditions[condition_index],
            )
            for node_index, condition_index in prerequisites
        )
        for prerequisites in native_graph.all_prerequisites()
    )
    return (
        native_graph,
        tuple(conditions),
        effective_after,
    )


@dataclass(frozen=True, slots=True)
class _CollectedFrozenNode:
    """Python collector 为一个冻结节点保留的不透明宿主元数据。

    collector 不创建编译依赖，也不判断最终拓扑。`frozen_step` 中的结构引用
    会在所有节点收齐后统一映射；action、observe 与声明作用域继续留在
    Python，因为 Rust scheduler 不解释这些对象。
    """

    frozen_step: FrozenStep
    owner_id: StepId | None
    context: CompiledStepContext


@dataclass(slots=True)
class _FrozenGraphCollection:
    """一次遍历冻结层级图得到的宿主对象侧表。"""

    nodes: list[_CollectedFrozenNode] = field(default_factory=list)
    capability_bindings: list[FrozenCapabilityBinding] = field(default_factory=list)


def compile_graph(source_graph: Graph, *, root_label: str) -> GraphCompilation:
    """冻结声明图、收集宿主元数据，并一次提交结构 lowering。"""

    frozen_graph = freeze_graph(source_graph, root_label=root_label)
    collection = _FrozenGraphCollection()
    _collect_frozen_graph(
        frozen_graph,
        collection=collection,
        graph_label_path=(),
        owner_id=None,
    )
    steps, composites, roots = _materialize_collected_nodes(collection.nodes)
    return GraphCompilation(
        source_graph=source_graph,
        source_frozen_graph=frozen_graph,
        label=frozen_graph.label,
        steps=steps,
        composites=composites,
        roots=roots,
        capability_bindings=tuple(collection.capability_bindings),
    )


def _collect_frozen_graph(
    graph: FrozenGraph,
    *,
    collection: _FrozenGraphCollection,
    graph_label_path: StepLabelPath,
    owner_id: StepId | None,
    ancestor_graph_scopes: tuple[GraphScope, ...] = (),
    ancestor_composite_ids: tuple[StepId, ...] = (),
) -> None:
    """只遍历冻结层级并收集 Python action、observe 与声明作用域。"""

    source_step_ids = {step.source_step: step.id for step in graph.steps}
    collection.capability_bindings.extend(graph.capability_bindings)
    for step in graph.steps:
        label_path = (*graph_label_path, step.label)
        context = CompiledStepContext(
            label_path=label_path,
            graph_label_path=graph_label_path,
            graph_step_ids=source_step_ids,
            ancestor_graph_scopes=ancestor_graph_scopes,
            ancestor_composite_ids=ancestor_composite_ids,
        )
        collection.nodes.append(
            _CollectedFrozenNode(
                frozen_step=step,
                owner_id=owner_id,
                context=context,
            )
        )
        if isinstance(step.action, FrozenGraph):
            _collect_frozen_graph(
                step.action,
                collection=collection,
                graph_label_path=label_path,
                owner_id=step.id,
                ancestor_graph_scopes=(
                    GraphScope(step_ids=source_step_ids),
                    *ancestor_graph_scopes,
                ),
                ancestor_composite_ids=(step.id, *ancestor_composite_ids),
            )


def _materialize_collected_nodes(
    collected: list[_CollectedFrozenNode],
) -> tuple[
    tuple[CompiledStep, ...],
    tuple[CompiledComposite, ...],
    tuple[CompiledNodeRef, ...],
]:
    """用全局身份表恢复 Python runtime 所需的不透明元数据包装。"""

    refs: dict[StepId, CompiledNodeRef] = {}
    for item in collected:
        step = item.frozen_step
        if step.id in refs:
            raise GraphError(f"compiled graph has duplicate node id: {step.id}")
        refs[step.id] = (
            CompiledCompositeRef(step.id)
            if isinstance(step.action, FrozenGraph)
            else CompiledStepRef(step.id)
        )

    def require_ref(step_id: StepId) -> CompiledNodeRef:
        ref = refs.get(step_id)
        if ref is None:
            raise GraphError(f"compiled graph references unknown node: {step_id}")
        return ref

    def materialize_condition(
        condition: FrozenRuntimeCondition,
    ) -> CompiledRuntimeCondition:
        return CompiledRuntimeCondition(
            when=condition.when,
            reads=tuple(require_ref(step_id) for step_id in condition.reads),
        )

    def materialize_prerequisite(
        prerequisite: StepId | RuntimeConditionalEdge,
    ) -> CompiledPrerequisite:
        if isinstance(prerequisite, RuntimeConditionalEdge):
            return CompiledConditionalDependency(
                node=require_ref(prerequisite.step_id),
                condition=materialize_condition(prerequisite.condition),
            )
        return require_ref(prerequisite)

    steps: list[CompiledStep] = []
    composites: list[CompiledComposite] = []
    roots: list[CompiledNodeRef] = []
    for item in collected:
        frozen_step = item.frozen_step
        ref = require_ref(frozen_step.id)
        owner = (
            None
            if item.owner_id is None
            else _require_composite_ref(require_ref(item.owner_id))
        )
        if owner is None:
            roots.append(ref)
        after = tuple(
            materialize_prerequisite(prerequisite) for prerequisite in frozen_step.after
        )
        when = (
            None
            if frozen_step.when is None
            else materialize_condition(frozen_step.when)
        )
        if isinstance(frozen_step.action, FrozenGraph):
            composites.append(
                CompiledComposite(
                    ref=_require_composite_ref(ref),
                    source_graph=frozen_step.action.source_graph,
                    graph_label=frozen_step.action.label,
                    children=tuple(
                        require_ref(child.id) for child in frozen_step.action.steps
                    ),
                    after=after,
                    when=when,
                    owner=owner,
                    observe=frozen_step.action.observe,
                    context=item.context,
                    graph_step_ids={
                        child.source_step: child.id
                        for child in frozen_step.action.steps
                    },
                )
            )
            continue
        if not isinstance(ref, CompiledStepRef):
            raise GraphError(
                f"compiled leaf action has composite ref: {frozen_step.id}"
            )
        steps.append(
            CompiledStep(
                ref=ref,
                action=frozen_step.action,
                after=after,
                when=when,
                owner=owner,
                context=item.context,
            )
        )

    return tuple(steps), tuple(composites), tuple(roots)


def _require_composite_ref(ref: CompiledNodeRef) -> CompiledCompositeRef:
    """要求结构引用指向 composite，避免用可选 mode 字段表达节点类别。"""

    if not isinstance(ref, CompiledCompositeRef):
        raise GraphError(f"compiled graph expected a composite ref: {ref.id}")
    return ref
