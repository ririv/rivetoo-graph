use std::collections::HashSet;
use std::error::Error;
use std::fmt::{self, Display, Formatter};

use crate::topology::{Arc, DagTopology, DirectedTopology, NodeId, TopologyError};

/// Stable identity of one opaque runtime predicate inside a compiled graph.
#[derive(Clone, Copy, Debug, Eq, Hash, Ord, PartialEq, PartialOrd)]
pub struct ConditionId(usize);

impl ConditionId {
    #[must_use]
    pub const fn new(index: usize) -> Self {
        Self(index)
    }

    #[must_use]
    pub const fn index(self) -> usize {
        self.0
    }
}

/// Result dependencies that must finish before an opaque predicate is evaluated.
#[derive(Clone, Debug, Eq, PartialEq)]
pub struct RuntimeCondition {
    reads: Vec<NodeId>,
}

impl RuntimeCondition {
    #[must_use]
    pub fn new(reads: impl IntoIterator<Item = NodeId>) -> Self {
        Self {
            reads: reads.into_iter().collect(),
        }
    }

    #[must_use]
    pub fn reads(&self) -> &[NodeId] {
        &self.reads
    }
}

/// A prerequisite whose conditional branch is represented without boolean flags.
#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub enum Prerequisite {
    Required(NodeId),
    Conditional {
        node: NodeId,
        condition: ConditionId,
    },
}

impl Prerequisite {
    #[must_use]
    pub const fn node(self) -> NodeId {
        match self {
            Self::Required(node) | Self::Conditional { node, .. } => node,
        }
    }

    #[must_use]
    pub const fn condition(self) -> Option<ConditionId> {
        match self {
            Self::Required(_) => None,
            Self::Conditional { condition, .. } => Some(condition),
        }
    }
}

/// Mutually exclusive execution behavior of a compiled node.
#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub enum ExecutionNodeKind {
    Leaf,
    Composite,
}

/// Structural facts needed by the compiled scheduler.
#[derive(Clone, Debug, Eq, PartialEq)]
pub struct ExecutionNode {
    kind: ExecutionNodeKind,
    owner: Option<NodeId>,
    prerequisites: Vec<Prerequisite>,
    when: Option<ConditionId>,
}

impl ExecutionNode {
    #[must_use]
    pub fn leaf(
        owner: Option<NodeId>,
        prerequisites: impl IntoIterator<Item = Prerequisite>,
        when: Option<ConditionId>,
    ) -> Self {
        Self {
            kind: ExecutionNodeKind::Leaf,
            owner,
            prerequisites: prerequisites.into_iter().collect(),
            when,
        }
    }

    #[must_use]
    pub fn composite(
        owner: Option<NodeId>,
        prerequisites: impl IntoIterator<Item = Prerequisite>,
        when: Option<ConditionId>,
    ) -> Self {
        Self {
            kind: ExecutionNodeKind::Composite,
            owner,
            prerequisites: prerequisites.into_iter().collect(),
            when,
        }
    }

    #[must_use]
    pub const fn kind(&self) -> ExecutionNodeKind {
        self.kind
    }

    #[must_use]
    pub const fn owner(&self) -> Option<NodeId> {
        self.owner
    }

    #[must_use]
    pub fn prerequisites(&self) -> &[Prerequisite] {
        &self.prerequisites
    }

    #[must_use]
    pub const fn when(&self) -> Option<ConditionId> {
        self.when
    }
}

/// A compiled execution graph construction failure.
#[derive(Clone, Debug, Eq, PartialEq)]
pub enum ExecutionGraphError {
    UnknownOwner {
        node: NodeId,
        owner: NodeId,
    },
    LeafOwner {
        node: NodeId,
        owner: NodeId,
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
    OwnershipCycle(Vec<NodeId>),
    DependencyCycle(Vec<NodeId>),
}

impl Display for ExecutionGraphError {
    fn fmt(&self, formatter: &mut Formatter<'_>) -> fmt::Result {
        match self {
            Self::UnknownOwner { node, owner } => write!(
                formatter,
                "execution node {} has unknown owner {}",
                node.index(),
                owner.index()
            ),
            Self::LeafOwner { node, owner } => write!(
                formatter,
                "execution node {} has leaf owner {}",
                node.index(),
                owner.index()
            ),
            Self::UnknownPrerequisite { node, prerequisite } => write!(
                formatter,
                "execution node {} has unknown prerequisite {}",
                node.index(),
                prerequisite.index()
            ),
            Self::UnknownCondition { node, condition } => write!(
                formatter,
                "execution node {} has unknown condition {}",
                node.index(),
                condition.index()
            ),
            Self::UnknownConditionRead { condition, read } => write!(
                formatter,
                "runtime condition {} reads unknown node {}",
                condition.index(),
                read.index()
            ),
            Self::OwnershipCycle(cycle) => {
                write_cycle(formatter, "execution graph has ownership cycle", cycle)
            }
            Self::DependencyCycle(cycle) => {
                write_cycle(formatter, "execution graph has dependency cycle", cycle)
            }
        }
    }
}

impl Error for ExecutionGraphError {}

fn write_cycle(formatter: &mut Formatter<'_>, prefix: &str, cycle: &[NodeId]) -> fmt::Result {
    write!(formatter, "{prefix}: ")?;
    let mut separator = "";
    for node in cycle {
        write!(formatter, "{separator}{}", node.index())?;
        separator = " -> ";
    }
    Ok(())
}

/// Fully indexed structural graph consumed by the compiled scheduler.
///
/// `owner` on each node is the only hierarchy declaration. Roots and composite
/// children are derived once during construction and cannot disagree with it.
#[derive(Clone, Debug, Eq, PartialEq)]
pub struct CompiledExecutionGraph {
    nodes: Vec<ExecutionNode>,
    conditions: Vec<RuntimeCondition>,
    roots: Vec<NodeId>,
    children: Vec<Vec<NodeId>>,
    dependents: Vec<Vec<NodeId>>,
    dependency_topology: DagTopology,
}

impl CompiledExecutionGraph {
    /// Validates and indexes a compiled execution graph.
    ///
    /// # Errors
    ///
    /// Returns [`ExecutionGraphError`] when a node reference is invalid, an
    /// owner is not a composite, or ownership/dependency relations contain a
    /// cycle.
    pub fn new(
        nodes: impl IntoIterator<Item = ExecutionNode>,
        conditions: impl IntoIterator<Item = RuntimeCondition>,
    ) -> Result<Self, ExecutionGraphError> {
        let nodes = nodes.into_iter().collect::<Vec<_>>();
        let conditions = conditions.into_iter().collect::<Vec<_>>();
        validate_condition_reads(nodes.len(), &conditions)?;

        let mut roots = Vec::new();
        let mut children = vec![Vec::new(); nodes.len()];
        let mut owner_arcs = Vec::new();
        for (index, node) in nodes.iter().enumerate() {
            let node_id = NodeId::new(index);
            let Some(owner) = node.owner else {
                roots.push(node_id);
                continue;
            };
            let Some(owner_node) = nodes.get(owner.index()) else {
                return Err(ExecutionGraphError::UnknownOwner {
                    node: node_id,
                    owner,
                });
            };
            if owner_node.kind != ExecutionNodeKind::Composite {
                return Err(ExecutionGraphError::LeafOwner {
                    node: node_id,
                    owner,
                });
            }
            children[owner.index()].push(node_id);
            owner_arcs.push(Arc::new(owner, node_id));
        }
        validate_ownership(nodes.len(), owner_arcs)?;

        let mut dependency_arcs = Vec::new();
        let mut dependency_pairs = HashSet::new();
        let mut dependents = vec![Vec::new(); nodes.len()];
        for (index, node) in nodes.iter().enumerate() {
            let node_id = NodeId::new(index);
            for prerequisite in &node.prerequisites {
                let source = prerequisite.node();
                if source.index() >= nodes.len() {
                    return Err(ExecutionGraphError::UnknownPrerequisite {
                        node: node_id,
                        prerequisite: source,
                    });
                }
                add_dependency(
                    source,
                    node_id,
                    &mut dependency_pairs,
                    &mut dependency_arcs,
                    &mut dependents,
                );
                if let Some(condition_id) = prerequisite.condition() {
                    let condition = require_condition(node_id, condition_id, &conditions)?;
                    for read in condition.reads() {
                        add_dependency(
                            *read,
                            node_id,
                            &mut dependency_pairs,
                            &mut dependency_arcs,
                            &mut dependents,
                        );
                    }
                }
            }
            if let Some(condition_id) = node.when {
                let condition = require_condition(node_id, condition_id, &conditions)?;
                for read in condition.reads() {
                    add_dependency(
                        *read,
                        node_id,
                        &mut dependency_pairs,
                        &mut dependency_arcs,
                        &mut dependents,
                    );
                }
            }
        }

        let directed = validated_topology(nodes.len(), dependency_arcs);
        let dependency_topology = DagTopology::new(directed).map_err(|error| match error {
            TopologyError::Cycle(cycle) => ExecutionGraphError::DependencyCycle(cycle),
            _ => unreachable!("execution dependency endpoints were validated"),
        })?;

        Ok(Self {
            nodes,
            conditions,
            roots,
            children,
            dependents,
            dependency_topology,
        })
    }

    #[must_use]
    pub fn nodes(&self) -> &[ExecutionNode] {
        &self.nodes
    }

    #[must_use]
    pub fn conditions(&self) -> &[RuntimeCondition] {
        &self.conditions
    }

    #[must_use]
    pub fn roots(&self) -> &[NodeId] {
        &self.roots
    }

    #[must_use]
    pub fn children(&self, composite: NodeId) -> Option<&[NodeId]> {
        self.children.get(composite.index()).map(Vec::as_slice)
    }

    #[must_use]
    pub fn dependents(&self, node: NodeId) -> Option<&[NodeId]> {
        self.dependents.get(node.index()).map(Vec::as_slice)
    }

    #[must_use]
    pub const fn dependency_topology(&self) -> &DagTopology {
        &self.dependency_topology
    }
}

fn validate_condition_reads(
    node_count: usize,
    conditions: &[RuntimeCondition],
) -> Result<(), ExecutionGraphError> {
    for (index, condition) in conditions.iter().enumerate() {
        let condition_id = ConditionId::new(index);
        for read in condition.reads() {
            if read.index() >= node_count {
                return Err(ExecutionGraphError::UnknownConditionRead {
                    condition: condition_id,
                    read: *read,
                });
            }
        }
    }
    Ok(())
}

fn validate_ownership(node_count: usize, owner_arcs: Vec<Arc>) -> Result<(), ExecutionGraphError> {
    let directed = validated_topology(node_count, owner_arcs);
    DagTopology::new(directed)
        .map(|_| ())
        .map_err(|error| match error {
            TopologyError::Cycle(cycle) => ExecutionGraphError::OwnershipCycle(cycle),
            _ => unreachable!("execution owner endpoints were validated"),
        })
}

fn require_condition(
    node: NodeId,
    condition: ConditionId,
    conditions: &[RuntimeCondition],
) -> Result<&RuntimeCondition, ExecutionGraphError> {
    conditions
        .get(condition.index())
        .ok_or(ExecutionGraphError::UnknownCondition { node, condition })
}

fn add_dependency(
    source: NodeId,
    target: NodeId,
    pairs: &mut HashSet<(NodeId, NodeId)>,
    arcs: &mut Vec<Arc>,
    dependents: &mut [Vec<NodeId>],
) {
    if !pairs.insert((source, target)) {
        return;
    }
    arcs.push(Arc::new(source, target));
    dependents[source.index()].push(target);
}

fn validated_topology(node_count: usize, arcs: Vec<Arc>) -> DirectedTopology {
    match DirectedTopology::new(node_count, arcs) {
        Ok(topology) => topology,
        Err(error) => unreachable!("validated execution topology was invalid: {error}"),
    }
}

#[cfg(test)]
mod tests {
    use super::{
        CompiledExecutionGraph, ConditionId, ExecutionGraphError, ExecutionNode, ExecutionNodeKind,
        Prerequisite, RuntimeCondition,
    };
    use crate::topology::NodeId;

    fn node(index: usize) -> NodeId {
        NodeId::new(index)
    }

    fn condition(index: usize) -> ConditionId {
        ConditionId::new(index)
    }

    #[test]
    fn graph_derives_roots_children_and_dependents() {
        let graph = CompiledExecutionGraph::new(
            [
                ExecutionNode::composite(None, [], None),
                ExecutionNode::leaf(Some(node(0)), [], None),
                ExecutionNode::leaf(
                    Some(node(0)),
                    [Prerequisite::Required(node(1))],
                    Some(condition(0)),
                ),
            ],
            [RuntimeCondition::new([node(1)])],
        )
        .unwrap();

        assert_eq!(graph.children(node(0)), Some([node(1), node(2)].as_slice()));
        assert_eq!(graph.dependents(node(1)), Some([node(2)].as_slice()));
        assert_eq!(graph.nodes()[0].kind(), ExecutionNodeKind::Composite);
    }

    #[test]
    fn graph_rejects_a_leaf_owner() {
        let result = CompiledExecutionGraph::new(
            [
                ExecutionNode::leaf(None, [], None),
                ExecutionNode::leaf(Some(node(0)), [], None),
            ],
            [],
        );

        assert_eq!(
            result,
            Err(ExecutionGraphError::LeafOwner {
                node: node(1),
                owner: node(0),
            })
        );
    }

    #[test]
    fn graph_rejects_an_ownership_cycle() {
        let result = CompiledExecutionGraph::new(
            [
                ExecutionNode::composite(Some(node(1)), [], None),
                ExecutionNode::composite(Some(node(0)), [], None),
            ],
            [],
        );

        assert_eq!(
            result,
            Err(ExecutionGraphError::OwnershipCycle(vec![
                node(0),
                node(1),
                node(0),
            ]))
        );
    }

    #[test]
    fn graph_rejects_a_dependency_cycle_through_condition_reads() {
        let result = CompiledExecutionGraph::new(
            [
                ExecutionNode::leaf(None, [], Some(condition(0))),
                ExecutionNode::leaf(None, [Prerequisite::Required(node(0))], None),
            ],
            [RuntimeCondition::new([node(1)])],
        );

        assert_eq!(
            result,
            Err(ExecutionGraphError::DependencyCycle(vec![
                node(0),
                node(1),
                node(0),
            ]))
        );
    }
}
