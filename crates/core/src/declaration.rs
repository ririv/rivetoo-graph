use std::collections::{HashMap, HashSet, VecDeque};
use std::error::Error;
use std::fmt::{self, Display, Formatter};

use crate::execution::{
    CapabilityBinding, CompiledExecutionGraph, ConditionId, Prerequisite, RuntimeCondition,
};
use crate::lowering::{
    DeclaredGraph, DeclaredNode, DeclaredNodeKind, ExecutionGraphDeclaration, GraphId,
    LoweringError,
};
use topology::NodeId;

/// A declaration node that either participates in this projection or is absent.
#[derive(Clone, Debug, Eq, PartialEq)]
pub enum NodeActivation {
    Active {
        prerequisites: Vec<DeclarationPrerequisite>,
        when: Option<ConditionId>,
    },
    Inactive,
}

/// A declaration prerequisite before static conditions are projected out.
#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub enum DeclarationPrerequisite {
    Required(NodeId),
    StaticConditional {
        node: NodeId,
        enabled: bool,
    },
    RuntimeConditional {
        node: NodeId,
        condition: ConditionId,
    },
}

impl DeclarationPrerequisite {
    const fn node(self) -> NodeId {
        match self {
            Self::Required(node)
            | Self::StaticConditional { node, .. }
            | Self::RuntimeConditional { node, .. } => node,
        }
    }
}

/// One complete declaration node before inactive subtrees are removed.
#[derive(Clone, Debug, Eq, PartialEq)]
pub struct DeclarationNode {
    graph: GraphId,
    kind: DeclaredNodeKind,
    activation: NodeActivation,
}

impl DeclarationNode {
    #[must_use]
    pub const fn new(graph: GraphId, kind: DeclaredNodeKind, activation: NodeActivation) -> Self {
        Self {
            graph,
            kind,
            activation,
        }
    }
}

/// A fully declared graph whose static decisions have already been evaluated by its host.
#[derive(Clone, Debug, Eq, PartialEq)]
pub struct GraphDeclaration {
    graphs: Vec<DeclaredGraph>,
    nodes: Vec<DeclarationNode>,
    conditions: Vec<RuntimeCondition>,
    capability_bindings: Vec<CapabilityBinding>,
}

impl GraphDeclaration {
    #[must_use]
    pub fn new(
        graphs: impl IntoIterator<Item = DeclaredGraph>,
        nodes: impl IntoIterator<Item = DeclarationNode>,
        conditions: impl IntoIterator<Item = RuntimeCondition>,
        capability_bindings: impl IntoIterator<Item = CapabilityBinding>,
    ) -> Self {
        Self {
            graphs: graphs.into_iter().collect(),
            nodes: nodes.into_iter().collect(),
            conditions: conditions.into_iter().collect(),
            capability_bindings: capability_bindings.into_iter().collect(),
        }
    }

    /// Projects static decisions and lowers the remaining hierarchy in one operation.
    ///
    /// # Errors
    ///
    /// Returns an error when the declaration contains an invalid identity, an active
    /// runtime condition reads an inactive node, or the projected graph is invalid.
    pub fn compile(self) -> Result<GraphCompilation, DeclarationError> {
        validate_declaration(&self)?;
        let active_graphs = active_graphs(&self)?;
        if active_graphs.len() == self.graphs.len() && keeps_all_identities(&self) {
            return self.compile_without_projection();
        }
        self.compile_projected(&active_graphs)
    }

    fn compile_projected(
        self,
        active_graphs: &HashSet<GraphId>,
    ) -> Result<GraphCompilation, DeclarationError> {
        let active_nodes = self
            .nodes
            .iter()
            .map(|node| {
                active_graphs.contains(&node.graph)
                    && matches!(node.activation, NodeActivation::Active { .. })
            })
            .collect::<Vec<_>>();
        let node_mapping = compact_mapping(&active_nodes);

        let used_conditions = used_conditions(&self, &active_nodes, &node_mapping)?;
        let condition_mapping = compact_mapping(&used_conditions);
        let projected_conditions =
            project_conditions(&self.conditions, &used_conditions, &node_mapping)?;
        let (projected_graphs, graph_mapping) =
            project_graphs(&self.graphs, active_graphs, &node_mapping);
        let projected_nodes = project_nodes(
            &self.nodes,
            &active_nodes,
            &node_mapping,
            &graph_mapping,
            &condition_mapping,
        )?;
        let projected_bindings =
            project_capability_bindings(&self.capability_bindings, &active_nodes, &node_mapping)?;
        let declaration_nodes = active_nodes
            .iter()
            .enumerate()
            .filter_map(|(index, active)| active.then_some(NodeId::new(index)))
            .collect::<Vec<_>>();
        let declaration_conditions = used_conditions
            .iter()
            .enumerate()
            .filter_map(|(index, used)| used.then_some(ConditionId::new(index)))
            .collect::<Vec<_>>();
        GraphCompilation::lower(
            ExecutionGraphDeclaration::new(
                projected_graphs,
                projected_nodes,
                projected_conditions,
                projected_bindings,
            ),
            declaration_nodes,
            declaration_conditions,
        )
    }

    fn compile_without_projection(self) -> Result<GraphCompilation, DeclarationError> {
        // 全部身份保留时直接移动条件和能力边，无需生成裁剪映射或复制 reads。
        // V 个节点、E 条前置、C 个条件：转换时间 O(V + E + C)，额外身份列表 O(V + C)。
        let declaration_nodes = (0..self.nodes.len()).map(NodeId::new).collect();
        let declaration_conditions = (0..self.conditions.len()).map(ConditionId::new).collect();
        let nodes = self.nodes.into_iter().map(|node| {
            let NodeActivation::Active {
                prerequisites,
                when,
            } = node.activation
            else {
                unreachable!("identity-preserving compilation requires active nodes");
            };
            let prerequisites =
                prerequisites
                    .into_iter()
                    .filter_map(|prerequisite| match prerequisite {
                        DeclarationPrerequisite::Required(source)
                        | DeclarationPrerequisite::StaticConditional {
                            node: source,
                            enabled: true,
                        } => Some(Prerequisite::Required(source)),
                        DeclarationPrerequisite::StaticConditional { enabled: false, .. } => None,
                        DeclarationPrerequisite::RuntimeConditional { node, condition } => {
                            Some(Prerequisite::Conditional { node, condition })
                        }
                    });
            match node.kind {
                DeclaredNodeKind::Leaf => DeclaredNode::leaf(node.graph, prerequisites, when),
                DeclaredNodeKind::Composite { child_graph } => {
                    DeclaredNode::composite(node.graph, child_graph, prerequisites, when)
                }
            }
        });
        GraphCompilation::lower(
            ExecutionGraphDeclaration::new(
                self.graphs,
                nodes,
                self.conditions,
                self.capability_bindings,
            ),
            declaration_nodes,
            declaration_conditions,
        )
    }
}

/// Rust compilation output plus mappings back to opaque host declarations.
#[derive(Clone, Debug, Eq, PartialEq)]
pub struct GraphCompilation {
    graph: CompiledExecutionGraph,
    declaration_nodes: Vec<NodeId>,
    declaration_conditions: Vec<ConditionId>,
}

impl GraphCompilation {
    fn lower(
        declaration: ExecutionGraphDeclaration,
        declaration_nodes: Vec<NodeId>,
        declaration_conditions: Vec<ConditionId>,
    ) -> Result<Self, DeclarationError> {
        let graph = declaration.lower().map_err(DeclarationError::Lowering)?;
        Ok(Self {
            graph,
            declaration_nodes,
            declaration_conditions,
        })
    }

    #[must_use]
    pub const fn graph(&self) -> &CompiledExecutionGraph {
        &self.graph
    }

    #[must_use]
    pub fn declaration_nodes(&self) -> &[NodeId] {
        &self.declaration_nodes
    }

    #[must_use]
    pub fn declaration_conditions(&self) -> &[ConditionId] {
        &self.declaration_conditions
    }

    #[must_use]
    pub fn into_graph(self) -> CompiledExecutionGraph {
        self.graph
    }
}

/// A declaration failure detected before any action can run.
#[derive(Clone, Debug, Eq, PartialEq)]
pub enum DeclarationError {
    MissingRootGraph,
    MultipleRootGraphs,
    UnknownGraphOwner {
        graph: GraphId,
        owner: NodeId,
    },
    UnknownNodeGraph {
        node: NodeId,
        graph: GraphId,
    },
    UnknownChildGraph {
        node: NodeId,
        graph: GraphId,
    },
    UnknownPrerequisite {
        node: NodeId,
        prerequisite: NodeId,
    },
    UnknownCondition {
        node: NodeId,
        condition: ConditionId,
    },
    UnknownConditionRead {
        condition: ConditionId,
        read: NodeId,
    },
    ActiveConditionReadsInactiveNode {
        condition: ConditionId,
        read: NodeId,
    },
    ActiveCapabilityUsesInactiveProvider {
        source: NodeId,
        target: NodeId,
    },
    UnknownCapabilitySource(NodeId),
    UnknownCapabilityTarget(NodeId),
    Lowering(LoweringError),
}

impl Display for DeclarationError {
    fn fmt(&self, formatter: &mut Formatter<'_>) -> fmt::Result {
        match self {
            Self::MissingRootGraph => write!(formatter, "graph declaration has no root graph"),
            Self::MultipleRootGraphs => {
                write!(formatter, "graph declaration has multiple root graphs")
            }
            Self::UnknownGraphOwner { graph, owner } => write!(
                formatter,
                "declared graph {} has unknown owner {}",
                graph.index(),
                owner.index()
            ),
            Self::UnknownNodeGraph { node, graph } => write!(
                formatter,
                "declaration node {} belongs to unknown graph {}",
                node.index(),
                graph.index()
            ),
            Self::UnknownChildGraph { node, graph } => write!(
                formatter,
                "declaration composite {} owns unknown graph {}",
                node.index(),
                graph.index()
            ),
            Self::UnknownPrerequisite { node, prerequisite } => write!(
                formatter,
                "declaration node {} has unknown prerequisite {}",
                node.index(),
                prerequisite.index()
            ),
            Self::UnknownCondition { node, condition } => write!(
                formatter,
                "declaration node {} has unknown runtime condition {}",
                node.index(),
                condition.index()
            ),
            Self::UnknownConditionRead { condition, read } => write!(
                formatter,
                "runtime condition {} reads unknown node {}",
                condition.index(),
                read.index()
            ),
            Self::ActiveConditionReadsInactiveNode { condition, read } => write!(
                formatter,
                "active runtime condition {} reads inactive node {}",
                condition.index(),
                read.index()
            ),
            Self::ActiveCapabilityUsesInactiveProvider { source, target } => write!(
                formatter,
                "active capability consumer {} uses inactive provider {}",
                target.index(),
                source.index()
            ),
            Self::UnknownCapabilitySource(node) => write!(
                formatter,
                "graph declaration capability has unknown source {}",
                node.index()
            ),
            Self::UnknownCapabilityTarget(node) => write!(
                formatter,
                "graph declaration capability has unknown target {}",
                node.index()
            ),
            Self::Lowering(error) => Display::fmt(error, formatter),
        }
    }
}

impl Error for DeclarationError {
    fn source(&self) -> Option<&(dyn Error + 'static)> {
        match self {
            Self::Lowering(error) => Some(error),
            _ => None,
        }
    }
}

fn validate_declaration(declaration: &GraphDeclaration) -> Result<(), DeclarationError> {
    match declaration
        .graphs
        .iter()
        .filter(|graph| matches!(graph, DeclaredGraph::Root))
        .count()
    {
        0 => return Err(DeclarationError::MissingRootGraph),
        1 => {}
        _ => return Err(DeclarationError::MultipleRootGraphs),
    }
    for (index, graph) in declaration.graphs.iter().enumerate() {
        if let DeclaredGraph::OwnedBy(owner) = graph
            && owner.index() >= declaration.nodes.len()
        {
            return Err(DeclarationError::UnknownGraphOwner {
                graph: GraphId::new(index),
                owner: *owner,
            });
        }
    }
    for (index, node) in declaration.nodes.iter().enumerate() {
        let node_id = NodeId::new(index);
        if node.graph.index() >= declaration.graphs.len() {
            return Err(DeclarationError::UnknownNodeGraph {
                node: node_id,
                graph: node.graph,
            });
        }
        if let DeclaredNodeKind::Composite { child_graph } = node.kind
            && child_graph.index() >= declaration.graphs.len()
        {
            return Err(DeclarationError::UnknownChildGraph {
                node: node_id,
                graph: child_graph,
            });
        }
        if let NodeActivation::Active {
            prerequisites,
            when,
        } = &node.activation
        {
            for prerequisite in prerequisites {
                if prerequisite.node().index() >= declaration.nodes.len() {
                    return Err(DeclarationError::UnknownPrerequisite {
                        node: node_id,
                        prerequisite: prerequisite.node(),
                    });
                }
                if let DeclarationPrerequisite::RuntimeConditional { condition, .. } = prerequisite
                    && condition.index() >= declaration.conditions.len()
                {
                    return Err(DeclarationError::UnknownCondition {
                        node: node_id,
                        condition: *condition,
                    });
                }
            }
            if let Some(condition) = when
                && condition.index() >= declaration.conditions.len()
            {
                return Err(DeclarationError::UnknownCondition {
                    node: node_id,
                    condition: *condition,
                });
            }
        }
    }
    for (index, condition) in declaration.conditions.iter().enumerate() {
        for read in condition.reads() {
            if read.index() >= declaration.nodes.len() {
                return Err(DeclarationError::UnknownConditionRead {
                    condition: ConditionId::new(index),
                    read: *read,
                });
            }
        }
    }
    for binding in &declaration.capability_bindings {
        if binding.source().index() >= declaration.nodes.len() {
            return Err(DeclarationError::UnknownCapabilitySource(binding.source()));
        }
        if binding.target().index() >= declaration.nodes.len() {
            return Err(DeclarationError::UnknownCapabilityTarget(binding.target()));
        }
    }
    Ok(())
}

fn active_graphs(declaration: &GraphDeclaration) -> Result<HashSet<GraphId>, DeclarationError> {
    let root = declaration
        .graphs
        .iter()
        .position(|graph| matches!(graph, DeclaredGraph::Root))
        .ok_or(DeclarationError::MissingRootGraph)?;
    let mut active = HashSet::from([GraphId::new(root)]);
    let mut queue = VecDeque::from([GraphId::new(root)]);
    // 一次 O(V + G) 建立父图到活跃 composite 的索引，遍历时只访问所属子图。
    // 原先每个活跃图扫描全部 V 个节点，最坏 O(G * V)。空间 O(G + C)，C 为 composite 数。
    let mut children = vec![Vec::new(); declaration.graphs.len()];
    for (index, node) in declaration.nodes.iter().enumerate() {
        if let DeclaredNodeKind::Composite { child_graph } = node.kind
            && matches!(node.activation, NodeActivation::Active { .. })
        {
            children[node.graph.index()].push((index, child_graph));
        }
    }
    while let Some(graph) = queue.pop_front() {
        for &(index, child_graph) in &children[graph.index()] {
            match declaration.graphs[child_graph.index()] {
                DeclaredGraph::OwnedBy(owner) if owner == NodeId::new(index) => {}
                _ => {
                    return Err(DeclarationError::Lowering(
                        LoweringError::ChildGraphOwnerMismatch {
                            node: NodeId::new(index),
                            graph: child_graph,
                        },
                    ));
                }
            }
            if active.insert(child_graph) {
                queue.push_back(child_graph);
            }
        }
    }
    Ok(active)
}

fn keeps_all_identities(declaration: &GraphDeclaration) -> bool {
    // O(V + E + C) 检查完整声明，不根据 Python 根图是否传 static 猜测。
    // 未使用的原生条件仍应被删除；此时保留普通投影路径及其旧 ID 映射语义。
    if declaration.conditions.is_empty() {
        return declaration
            .nodes
            .iter()
            .all(|node| matches!(node.activation, NodeActivation::Active { .. }));
    }
    let mut used = vec![false; declaration.conditions.len()];
    for node in &declaration.nodes {
        let NodeActivation::Active {
            prerequisites,
            when,
        } = &node.activation
        else {
            return false;
        };
        if let Some(condition) = when {
            used[condition.index()] = true;
        }
        for prerequisite in prerequisites {
            if let DeclarationPrerequisite::RuntimeConditional { condition, .. } = prerequisite {
                used[condition.index()] = true;
            }
        }
    }
    used.into_iter().all(|used| used)
}

fn compact_mapping(active: &[bool]) -> Vec<Option<usize>> {
    let mut next = 0;
    active
        .iter()
        .map(|active| {
            if !active {
                return None;
            }
            let index = next;
            next += 1;
            Some(index)
        })
        .collect()
}

fn used_conditions(
    declaration: &GraphDeclaration,
    active_nodes: &[bool],
    node_mapping: &[Option<usize>],
) -> Result<Vec<bool>, DeclarationError> {
    let mut used = vec![false; declaration.conditions.len()];
    for (node_index, node) in declaration.nodes.iter().enumerate() {
        if !active_nodes[node_index] {
            continue;
        }
        let NodeActivation::Active {
            prerequisites,
            when,
        } = &node.activation
        else {
            continue;
        };
        for prerequisite in prerequisites {
            if node_mapping[prerequisite.node().index()].is_none() {
                continue;
            }
            if let DeclarationPrerequisite::RuntimeConditional { condition, .. } = prerequisite {
                used[condition.index()] = true;
            }
        }
        if let Some(condition) = when {
            used[condition.index()] = true;
        }
    }
    for (condition_index, is_used) in used.iter().copied().enumerate() {
        if !is_used {
            continue;
        }
        for read in declaration.conditions[condition_index].reads() {
            if node_mapping[read.index()].is_none() {
                return Err(DeclarationError::ActiveConditionReadsInactiveNode {
                    condition: ConditionId::new(condition_index),
                    read: *read,
                });
            }
        }
    }
    Ok(used)
}

fn project_conditions(
    conditions: &[RuntimeCondition],
    used: &[bool],
    node_mapping: &[Option<usize>],
) -> Result<Vec<RuntimeCondition>, DeclarationError> {
    conditions
        .iter()
        .enumerate()
        .filter(|(index, _)| used[*index])
        .map(|(index, condition)| {
            condition
                .reads()
                .iter()
                .map(|read| {
                    node_mapping[read.index()].map(NodeId::new).ok_or(
                        DeclarationError::ActiveConditionReadsInactiveNode {
                            condition: ConditionId::new(index),
                            read: *read,
                        },
                    )
                })
                .collect::<Result<Vec<_>, _>>()
                .map(RuntimeCondition::new)
        })
        .collect()
}

fn project_graphs(
    graphs: &[DeclaredGraph],
    active: &HashSet<GraphId>,
    node_mapping: &[Option<usize>],
) -> (Vec<DeclaredGraph>, HashMap<GraphId, GraphId>) {
    let mapping = graphs
        .iter()
        .enumerate()
        .filter_map(|(index, _)| {
            let old = GraphId::new(index);
            active.contains(&old).then_some(old)
        })
        .enumerate()
        .map(|(new, old)| (old, GraphId::new(new)))
        .collect::<HashMap<_, _>>();
    let projected = graphs
        .iter()
        .enumerate()
        .filter_map(|(index, graph)| {
            let old = GraphId::new(index);
            mapping.get(&old)?;
            Some(match graph {
                DeclaredGraph::Root => DeclaredGraph::Root,
                DeclaredGraph::OwnedBy(owner) => DeclaredGraph::OwnedBy(NodeId::new(
                    node_mapping[owner.index()].expect("active graph owner must be active"),
                )),
            })
        })
        .collect();
    (projected, mapping)
}

fn project_nodes(
    nodes: &[DeclarationNode],
    active: &[bool],
    node_mapping: &[Option<usize>],
    graph_mapping: &HashMap<GraphId, GraphId>,
    condition_mapping: &[Option<usize>],
) -> Result<Vec<DeclaredNode>, DeclarationError> {
    nodes
        .iter()
        .enumerate()
        .filter(|(index, _)| active[*index])
        .map(|(_, node)| {
            let NodeActivation::Active {
                prerequisites,
                when,
            } = &node.activation
            else {
                unreachable!("active node must have active declaration state")
            };
            let prerequisites = prerequisites
                .iter()
                .filter_map(|prerequisite| {
                    let source = node_mapping[prerequisite.node().index()].map(NodeId::new)?;
                    match prerequisite {
                        DeclarationPrerequisite::Required(_) => {
                            Some(Ok(Prerequisite::Required(source)))
                        }
                        DeclarationPrerequisite::StaticConditional { enabled, .. } => {
                            enabled.then_some(Ok(Prerequisite::Required(source)))
                        }
                        DeclarationPrerequisite::RuntimeConditional { condition, .. } => {
                            Some(condition_mapping[condition.index()].map_or_else(
                                || {
                                    Err(DeclarationError::UnknownCondition {
                                        node: source,
                                        condition: *condition,
                                    })
                                },
                                |condition| {
                                    Ok(Prerequisite::Conditional {
                                        node: source,
                                        condition: ConditionId::new(condition),
                                    })
                                },
                            ))
                        }
                    }
                })
                .collect::<Result<Vec<_>, _>>()?;
            let when = when.map(|condition| {
                condition_mapping[condition.index()]
                    .map(ConditionId::new)
                    .ok_or(DeclarationError::UnknownCondition {
                        node: NodeId::new(0),
                        condition,
                    })
            });
            let when = when.transpose()?;
            let graph = graph_mapping[&node.graph];
            Ok(match node.kind {
                DeclaredNodeKind::Leaf => DeclaredNode::leaf(graph, prerequisites, when),
                DeclaredNodeKind::Composite { child_graph } => {
                    DeclaredNode::composite(graph, graph_mapping[&child_graph], prerequisites, when)
                }
            })
        })
        .collect()
}

fn project_capability_bindings(
    bindings: &[CapabilityBinding],
    active: &[bool],
    node_mapping: &[Option<usize>],
) -> Result<Vec<CapabilityBinding>, DeclarationError> {
    let mut projected = Vec::new();
    for binding in bindings {
        if !active[binding.target().index()] {
            continue;
        }
        if !active[binding.source().index()] {
            return Err(DeclarationError::ActiveCapabilityUsesInactiveProvider {
                source: binding.source(),
                target: binding.target(),
            });
        }
        projected.push(CapabilityBinding::new(
            NodeId::new(node_mapping[binding.source().index()].expect("active source mapping")),
            NodeId::new(node_mapping[binding.target().index()].expect("active target mapping")),
        ));
    }
    Ok(projected)
}

#[cfg(test)]
mod tests {
    use super::{DeclarationNode, DeclarationPrerequisite, GraphDeclaration, NodeActivation};
    use crate::execution::{CapabilityBinding, ConditionId, RuntimeCondition};
    use crate::lowering::{DeclaredGraph, DeclaredNodeKind, GraphId};
    use topology::NodeId;

    fn active_hierarchy() -> GraphDeclaration {
        let active = |prerequisites, when| NodeActivation::Active {
            prerequisites,
            when,
        };
        GraphDeclaration::new(
            [DeclaredGraph::Root, DeclaredGraph::OwnedBy(NodeId::new(0))],
            [
                DeclarationNode::new(
                    GraphId::new(0),
                    DeclaredNodeKind::Composite {
                        child_graph: GraphId::new(1),
                    },
                    active(vec![], None),
                ),
                DeclarationNode::new(
                    GraphId::new(1),
                    DeclaredNodeKind::Leaf,
                    active(vec![], None),
                ),
                DeclarationNode::new(
                    GraphId::new(1),
                    DeclaredNodeKind::Leaf,
                    active(
                        vec![
                            DeclarationPrerequisite::RuntimeConditional {
                                node: NodeId::new(1),
                                condition: ConditionId::new(0),
                            },
                            DeclarationPrerequisite::StaticConditional {
                                node: NodeId::new(3),
                                enabled: false,
                            },
                        ],
                        Some(ConditionId::new(0)),
                    ),
                ),
                DeclarationNode::new(
                    GraphId::new(0),
                    DeclaredNodeKind::Leaf,
                    active(
                        vec![DeclarationPrerequisite::Required(NodeId::new(0))],
                        None,
                    ),
                ),
            ],
            [RuntimeCondition::new([NodeId::new(1)])],
            [CapabilityBinding::new(NodeId::new(1), NodeId::new(3))],
        )
    }

    #[test]
    fn identity_path_matches_projection_with_hierarchy_conditions_and_capabilities() {
        let declaration = active_hierarchy();
        assert!(super::keeps_all_identities(&declaration));
        let active = super::active_graphs(&declaration).unwrap();
        let projected = declaration.clone().compile_projected(&active).unwrap();
        assert_eq!(declaration.compile().unwrap(), projected);
    }

    #[test]
    fn unused_conditions_still_have_compact_identity_mapping() {
        let mut declaration = active_hierarchy();
        declaration.conditions.insert(0, RuntimeCondition::new([]));
        let NodeActivation::Active {
            prerequisites,
            when,
        } = &mut declaration.nodes[2].activation
        else {
            unreachable!();
        };
        *when = Some(ConditionId::new(1));
        prerequisites[0] = DeclarationPrerequisite::RuntimeConditional {
            node: NodeId::new(1),
            condition: ConditionId::new(1),
        };
        assert!(!super::keeps_all_identities(&declaration));
        let compiled = declaration.compile().unwrap();
        assert_eq!(compiled.declaration_conditions(), [ConditionId::new(1)]);
        assert_eq!(
            compiled.graph().nodes()[2].when(),
            Some(ConditionId::new(0))
        );
    }

    #[test]
    fn inactive_parent_removes_active_children() {
        let mut declaration = active_hierarchy();
        declaration.nodes[0].activation = NodeActivation::Inactive;
        declaration.capability_bindings.clear();
        let active = super::active_graphs(&declaration).unwrap();
        assert_eq!(active.len(), 1);
        assert_eq!(
            declaration.compile().unwrap().declaration_nodes(),
            [NodeId::new(3)]
        );
    }

    #[test]
    fn empty_graph_matches_projection() {
        let declaration = GraphDeclaration::new([DeclaredGraph::Root], [], [], []);
        let active = super::active_graphs(&declaration).unwrap();
        assert_eq!(
            declaration.clone().compile().unwrap(),
            declaration.compile_projected(&active).unwrap()
        );
    }

    #[test]
    fn compilation_removes_inactive_nodes_and_their_edges() {
        let result = GraphDeclaration::new(
            [DeclaredGraph::Root],
            [
                DeclarationNode::new(
                    GraphId::new(0),
                    DeclaredNodeKind::Leaf,
                    NodeActivation::Inactive,
                ),
                DeclarationNode::new(
                    GraphId::new(0),
                    DeclaredNodeKind::Leaf,
                    NodeActivation::Active {
                        prerequisites: vec![DeclarationPrerequisite::Required(NodeId::new(0))],
                        when: None,
                    },
                ),
            ],
            [],
            [],
        )
        .compile()
        .unwrap();

        assert_eq!(result.declaration_nodes(), [NodeId::new(1)]);
        assert!(result.graph().nodes()[0].prerequisites().is_empty());
    }

    #[test]
    fn compilation_rejects_condition_reading_inactive_node() {
        let result = GraphDeclaration::new(
            [DeclaredGraph::Root],
            [
                DeclarationNode::new(
                    GraphId::new(0),
                    DeclaredNodeKind::Leaf,
                    NodeActivation::Inactive,
                ),
                DeclarationNode::new(
                    GraphId::new(0),
                    DeclaredNodeKind::Leaf,
                    NodeActivation::Active {
                        prerequisites: vec![],
                        when: Some(ConditionId::new(0)),
                    },
                ),
            ],
            [RuntimeCondition::new([NodeId::new(0)])],
            [],
        )
        .compile();

        assert!(result.is_err());
    }

    #[test]
    fn compilation_rejects_active_consumer_with_inactive_provider() {
        let result = GraphDeclaration::new(
            [DeclaredGraph::Root],
            [
                DeclarationNode::new(
                    GraphId::new(0),
                    DeclaredNodeKind::Leaf,
                    NodeActivation::Inactive,
                ),
                DeclarationNode::new(
                    GraphId::new(0),
                    DeclaredNodeKind::Leaf,
                    NodeActivation::Active {
                        prerequisites: vec![],
                        when: None,
                    },
                ),
            ],
            [],
            [CapabilityBinding::new(NodeId::new(0), NodeId::new(1))],
        )
        .compile();

        assert!(result.is_err());
    }
}
