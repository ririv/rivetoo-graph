from __future__ import annotations

from dataclasses import dataclass, field, replace
from types import MappingProxyType
from typing import TYPE_CHECKING, cast

from ._native import (
    NativeActionBindings,
    NativeCompiledExecutionGraph,
    NativeDeclarationCompilation,
    NativeGraph,
    compile_declaration,
)
from .errors import GraphError
from .model import Graph, GraphObservationMessages, StepId
from .results import GraphScope

if TYPE_CHECKING:
    from collections.abc import Mapping

    from .model import (
        ConditionalEdgePredicate,
        LeafAction,
        Step,
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
    """由 Python 执行器调用的 Python 叶子节点。"""

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
class CompiledNativeStep:
    """由 Rust 执行器直接调用的原生叶子节点。"""

    ref: CompiledStepRef
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
    source_graph: Graph | NativeGraph
    graph_label: str
    children: tuple[CompiledNodeRef, ...]
    after: tuple[CompiledPrerequisite, ...]
    when: CompiledRuntimeCondition | None
    owner: CompiledCompositeRef | None
    observation: GraphObservationMessages
    context: CompiledStepContext
    graph_step_ids: Mapping[Step, StepId]

    @property
    def id(self) -> StepId:
        return self.ref.id

    @property
    def label_path(self) -> StepLabelPath:
        return self.context.label_path


type CompiledLeaf = CompiledStep | CompiledNativeStep
type CompiledNode = CompiledLeaf | CompiledComposite


@dataclass(frozen=True, slots=True)
class _MaterializeExecutionGraph:
    """Request native execution indexes from the Python compilation model."""


@dataclass(frozen=True, slots=True)
class _PrecompiledExecutionGraph:
    """Carry one complete execution graph compiled by the Rust declaration compiler."""

    graph: NativeCompiledExecutionGraph
    conditions: tuple[CompiledRuntimeCondition, ...]
    nodes: tuple[CompiledNode, ...]
    native_actions: NativeActionBindings


type _ExecutionGraphInput = _MaterializeExecutionGraph | _PrecompiledExecutionGraph

_MATERIALIZE_EXECUTION_GRAPH = _MaterializeExecutionGraph()


@dataclass(frozen=True, slots=True)
class GraphCompilation:
    """声明图的扁平执行计划和组合生命周期记录。

    `steps` 是唯一的 action 执行事实来源。`composites` 不保存可执行子图，
    只描述原始子图边界；`roots` 则明确给出根图直接拥有的节点。
    """

    source_graph: Graph
    label: str
    steps: tuple[CompiledLeaf, ...]
    composites: tuple[CompiledComposite, ...]
    roots: tuple[CompiledNodeRef, ...]
    root_label_path: StepLabelPath = ()
    root_graph_step_ids: Mapping[Step, StepId] = field(
        default_factory=lambda: MappingProxyType({})
    )
    _execution_graph_input: _ExecutionGraphInput = field(
        default=_MATERIALIZE_EXECUTION_GRAPH,
        repr=False,
        compare=False,
    )
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
    _native_actions: NativeActionBindings = field(
        init=False,
        repr=False,
        compare=False,
    )

    @property
    def observation(self) -> GraphObservationMessages:
        return self.source_graph.observation

    def __post_init__(self) -> None:
        if not self.label:
            raise GraphError("compiled graph label must not be empty")

        match self._execution_graph_input:
            case _MaterializeExecutionGraph():
                self._initialize_materialized_execution_graph()
            case _PrecompiledExecutionGraph() as precompiled:
                self._initialize_precompiled_execution_graph(precompiled)

    def _initialize_materialized_execution_graph(self) -> None:
        """Validate public Python inputs and materialize their native execution graph."""

        # 编译产物会被 runtime 直接消费，构造时必须一次性验证引用图，
        # 避免把结构错误延迟到部分 step 已经执行之后。
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

        native_nodes = tuple(nodes.values())
        native_node_indexes = {
            node.id: index for index, node in enumerate(native_nodes)
        }
        try:
            native_graph, native_conditions, effective_after = (
                _materialize_native_execution_graph(
                    native_nodes,
                    native_node_indexes,
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
                tuple(
                    node
                    for node in native_nodes
                    if not isinstance(node, CompiledComposite)
                ),
            )
            object.__setattr__(
                self,
                "composites",
                tuple(
                    node for node in native_nodes if isinstance(node, CompiledComposite)
                ),
            )

        native_root_ids = {native_nodes[index].id for index in native_graph.roots()}
        if native_root_ids != ownerless_node_ids:
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

        self._set_native_execution_graph(
            graph=native_graph,
            nodes=native_nodes,
            node_indexes=native_node_indexes,
            conditions=native_conditions,
            native_actions=NativeActionBindings(len(native_nodes)),
        )

    def _initialize_precompiled_execution_graph(
        self,
        precompiled: _PrecompiledExecutionGraph,
    ) -> None:
        """Bind opaque Python side tables to a Rust-validated execution graph."""

        # Rust 已经完成声明结构、拓扑、owner、children 与 capability 校验。
        # Python 这里只验证跨语言侧表仍引用同一批对象，时间与空间均为 O(V)；
        # 不能再把整个编译图重复验证一遍。
        declared_nodes = (*self.steps, *self.composites)
        if len(precompiled.nodes) != len(declared_nodes) or {
            id(node) for node in precompiled.nodes
        } != {id(node) for node in declared_nodes}:
            raise GraphError("precompiled execution nodes do not match compiled nodes")

        native_node_indexes = {
            node.id: index for index, node in enumerate(precompiled.nodes)
        }
        if len(native_node_indexes) != len(precompiled.nodes):
            raise GraphError("precompiled execution graph has duplicate node ids")

        self._set_native_execution_graph(
            graph=precompiled.graph,
            nodes=precompiled.nodes,
            node_indexes=native_node_indexes,
            conditions=precompiled.conditions,
            native_actions=precompiled.native_actions,
        )

    def _set_native_execution_graph(
        self,
        *,
        graph: NativeCompiledExecutionGraph,
        nodes: tuple[CompiledNode, ...],
        node_indexes: dict[StepId, int],
        conditions: tuple[CompiledRuntimeCondition, ...],
        native_actions: NativeActionBindings,
    ) -> None:
        """Commit the complete native execution view as one internal state transition."""

        object.__setattr__(self, "_native_graph", graph)
        object.__setattr__(self, "_native_nodes", nodes)
        object.__setattr__(self, "_native_conditions", conditions)
        object.__setattr__(self, "_native_actions", native_actions)
        object.__setattr__(
            self,
            "_native_node_indexes",
            MappingProxyType(node_indexes),
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
    if isinstance(ref, CompiledStepRef) != (not isinstance(node, CompiledComposite)):
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


def _materialize_native_execution_graph(
    nodes: tuple[CompiledNode, ...],
    node_indexes: Mapping[StepId, int],
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
        [],
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


def compile_graph(
    source_graph: Graph,
    *,
    root_label: str,
    parent_step_id: StepId | None = None,
    root_label_path: StepLabelPath = (),
    ancestor_graph_scopes: tuple[GraphScope, ...] = (),
) -> GraphCompilation:
    """把完整 Python 声明一次性交给 Rust 编译，并恢复不透明宿主对象侧表。"""

    try:
        native = compile_declaration(
            source_graph,
            root_label,
            list(root_label_path),
            [] if parent_step_id is None else list(parent_step_id.segments),
        )
    except ValueError as exc:
        raise GraphError(str(exc)) from exc
    return _materialize_native_compilation(
        source_graph,
        native,
        external_ancestor_graph_scopes=ancestor_graph_scopes,
    )


def _materialize_native_compilation(
    source_graph: Graph,
    native: NativeDeclarationCompilation,
    *,
    external_ancestor_graph_scopes: tuple[GraphScope, ...] = (),
) -> GraphCompilation:
    """只恢复 action、predicate 与声明作用域，不参与任何图结构决策。"""

    active_declarations = native.declaration_nodes()
    declaration_to_compiled = {
        declaration: index for index, declaration in enumerate(active_declarations)
    }
    step_ids = tuple(StepId(tuple(value)) for value in native.node_step_ids())
    labels = native.node_labels()
    node_graphs = native.node_graphs()
    child_graphs = native.node_child_graphs()
    source_steps = tuple(
        None if value is None else cast("Step", value)
        for value in native.source_steps()
    )
    actions = native.actions()
    graph_labels = native.graph_labels()
    graph_label_paths = tuple(tuple(value) for value in native.graph_label_paths())
    graph_owners = native.graph_owners()
    graph_local_nodes = native.graph_local_nodes()
    source_graphs = tuple(native.source_graphs())

    refs_by_declaration: dict[int, CompiledNodeRef] = {}
    for declaration in active_declarations:
        step_id = step_ids[declaration]
        refs_by_declaration[declaration] = (
            CompiledCompositeRef(step_id)
            if child_graphs[declaration] is not None
            else CompiledStepRef(step_id)
        )

    graph_step_ids = tuple(
        MappingProxyType(
            {
                cast("Step", source_steps[declaration]): step_ids[declaration]
                for declaration in local_nodes
                if declaration in declaration_to_compiled
                and source_steps[declaration] is not None
            }
        )
        for local_nodes in graph_local_nodes
    )

    def ancestor_graph_scopes(graph_index: int) -> tuple[GraphScope, ...]:
        scopes: list[GraphScope] = []
        current = graph_index
        while (owner := graph_owners[current]) is not None:
            parent_graph = node_graphs[owner]
            scopes.append(GraphScope(step_ids=graph_step_ids[parent_graph]))
            current = parent_graph
        return (*scopes, *external_ancestor_graph_scopes)

    def ancestor_composite_ids(graph_index: int) -> tuple[StepId, ...]:
        result: list[StepId] = []
        current = graph_index
        while (owner := graph_owners[current]) is not None:
            result.append(step_ids[owner])
            current = node_graphs[owner]
        return tuple(result)

    predicates = native.predicates()
    condition_reads = native.condition_reads()
    compiled_conditions = tuple(
        CompiledRuntimeCondition(
            when=cast("ConditionalEdgePredicate", predicates[declaration]),
            reads=tuple(
                refs_by_declaration[read] for read in condition_reads[declaration]
            ),
        )
        for declaration in native.declaration_conditions()
    )
    native_after = native.graph.all_prerequisites()
    native_when = native.graph.when_conditions()

    def prerequisite(value: tuple[int, int | None]) -> CompiledPrerequisite:
        node, condition = value
        ref = refs_by_declaration[active_declarations[node]]
        if condition is None:
            return ref
        return CompiledConditionalDependency(
            node=ref,
            condition=compiled_conditions[condition],
        )

    steps: list[CompiledLeaf] = []
    composites: list[CompiledComposite] = []
    ordered_nodes: list[CompiledNode] = []
    for compiled_index, declaration in enumerate(active_declarations):
        graph_index = node_graphs[declaration]
        label_path = (*graph_label_paths[graph_index], labels[declaration])
        context = CompiledStepContext(
            label_path=label_path,
            graph_label_path=graph_label_paths[graph_index],
            graph_step_ids=graph_step_ids[graph_index],
            ancestor_graph_scopes=ancestor_graph_scopes(graph_index),
            ancestor_composite_ids=ancestor_composite_ids(graph_index),
        )
        owner_declaration = graph_owners[graph_index]
        owner = (
            None
            if owner_declaration is None
            else _require_composite_ref(refs_by_declaration[owner_declaration])
        )
        after = tuple(prerequisite(value) for value in native_after[compiled_index])
        when_index = native_when[compiled_index]
        when = None if when_index is None else compiled_conditions[when_index]
        ref = refs_by_declaration[declaration]
        child_graph = child_graphs[declaration]
        if child_graph is None:
            action = actions[declaration]
            compiled_step: CompiledLeaf
            if action is None:
                compiled_step = CompiledNativeStep(
                    ref=cast("CompiledStepRef", ref),
                    after=after,
                    when=when,
                    owner=owner,
                    context=context,
                )
            else:
                compiled_step = CompiledStep(
                    ref=cast("CompiledStepRef", ref),
                    action=cast("LeafAction", action),
                    after=after,
                    when=when,
                    owner=owner,
                    context=context,
                )
            steps.append(compiled_step)
            ordered_nodes.append(compiled_step)
            continue
        component_source_graph = cast(
            "Graph | NativeGraph",
            source_graphs[child_graph],
        )
        composite = CompiledComposite(
            ref=_require_composite_ref(ref),
            source_graph=component_source_graph,
            graph_label=graph_labels[child_graph],
            children=tuple(
                refs_by_declaration[child]
                for child in graph_local_nodes[child_graph]
                if child in declaration_to_compiled
            ),
            after=after,
            when=when,
            owner=owner,
            observation=(
                component_source_graph.observation
                if isinstance(component_source_graph, Graph)
                else GraphObservationMessages()
            ),
            context=context,
            graph_step_ids=graph_step_ids[child_graph],
        )
        composites.append(composite)
        ordered_nodes.append(composite)

    roots = tuple(
        refs_by_declaration[active_declarations[index]]
        for index in native.graph.roots()
    )
    return GraphCompilation(
        source_graph=source_graph,
        label=graph_labels[0],
        steps=tuple(steps),
        composites=tuple(composites),
        roots=roots,
        root_label_path=graph_label_paths[0],
        root_graph_step_ids=graph_step_ids[0],
        _execution_graph_input=_PrecompiledExecutionGraph(
            graph=native.graph,
            conditions=compiled_conditions,
            nodes=tuple(ordered_nodes),
            native_actions=native.native_actions(),
        ),
    )


def _require_composite_ref(ref: CompiledNodeRef) -> CompiledCompositeRef:
    """要求结构引用指向 composite，避免用可选 mode 字段表达节点类别。"""

    if not isinstance(ref, CompiledCompositeRef):
        raise GraphError(f"compiled graph expected a composite ref: {ref.id}")
    return ref
