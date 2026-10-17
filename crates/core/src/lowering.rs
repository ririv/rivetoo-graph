use std::error::Error;
use std::fmt::{self, Display, Formatter};

use crate::execution::{
    CapabilityBinding, CompiledExecutionGraph, ConditionId, ExecutionGraphError, ExecutionNode,
    Prerequisite, RuntimeCondition,
};
use topology::NodeId;

/// Stable identity of one graph scope in a hierarchical declaration.
#[derive(Clone, Copy, Debug, Eq, Hash, Ord, PartialEq, PartialOrd)]
pub struct GraphId(usize);

impl GraphId {
    #[must_use]
    pub const fn new(index: usize) -> Self {
        Self(index)
    }

    #[must_use]
    pub const fn index(self) -> usize {
        self.0
    }
}

/// Mutually exclusive ownership states of one declared graph scope.
#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub enum DeclaredGraph {
    Root,
    OwnedBy(NodeId),
}

/// Mutually exclusive behaviors of one node before hierarchy lowering.
#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub enum DeclaredNodeKind {
    Leaf,
    Composite { child_graph: GraphId },
}

/// One execution node attached to a declared graph scope.
#[derive(Clone, Debug, Eq, PartialEq)]
pub struct DeclaredNode {
    graph: GraphId,
    kind: DeclaredNodeKind,
    prerequisites: Vec<Prerequisite>,
    condition: Option<ConditionId>,
}

impl DeclaredNode {
    #[must_use]
    pub fn leaf(
        graph: GraphId,
        prerequisites: impl IntoIterator<Item = Prerequisite>,
        condition: Option<ConditionId>,
    ) -> Self {
        Self {
            graph,
            kind: DeclaredNodeKind::Leaf,
            prerequisites: prerequisites.into_iter().collect(),
            condition,
        }
    }

    #[must_use]
    pub fn composite(
        graph: GraphId,
        child_graph: GraphId,
        prerequisites: impl IntoIterator<Item = Prerequisite>,
        condition: Option<ConditionId>,
    ) -> Self {
        Self {
            graph,
            kind: DeclaredNodeKind::Composite { child_graph },
            prerequisites: prerequisites.into_iter().collect(),
            condition,
        }
    }
}

/// A hierarchy lowering failure before scheduler indexes are constructed.
#[derive(Clone, Debug, Eq, PartialEq)]
pub enum LoweringError {
    MissingRootGraph,
    MultipleRootGraphs,
    UnknownNodeGraph { node: NodeId, graph: GraphId },
    UnknownChildGraph { node: NodeId, graph: GraphId },
    UnknownGraphOwner { graph: GraphId, owner: NodeId },
    ChildGraphOwnerMismatch { node: NodeId, graph: GraphId },
    OwnedGraphCompositeMismatch { graph: GraphId, owner: NodeId },
    Execution(ExecutionGraphError),
}

impl Display for LoweringError {
    fn fmt(&self, formatter: &mut Formatter<'_>) -> fmt::Result {
        match self {
            Self::MissingRootGraph => write!(formatter, "execution declaration has no root graph"),
            Self::MultipleRootGraphs => {
                write!(formatter, "execution declaration has multiple root graphs")
            }
            Self::UnknownNodeGraph { node, graph } => write!(
                formatter,
                "execution node {} belongs to unknown graph {}",
                node.index(),
                graph.index()
            ),
            Self::UnknownChildGraph { node, graph } => write!(
                formatter,
                "execution composite {} owns unknown graph {}",
                node.index(),
                graph.index()
            ),
            Self::UnknownGraphOwner { graph, owner } => write!(
                formatter,
                "execution graph {} has unknown owner {}",
                graph.index(),
                owner.index()
            ),
            Self::ChildGraphOwnerMismatch { node, graph } => write!(
                formatter,
                "execution composite {} does not own child graph {}",
                node.index(),
                graph.index()
            ),
            Self::OwnedGraphCompositeMismatch { graph, owner } => write!(
                formatter,
                "execution graph {} owner {} does not reference it as a child",
                graph.index(),
                owner.index()
            ),
            Self::Execution(error) => Display::fmt(error, formatter),
        }
    }
}

impl Error for LoweringError {
    fn source(&self) -> Option<&(dyn Error + 'static)> {
        match self {
            Self::Execution(error) => Some(error),
            _ => None,
        }
    }
}

impl From<ExecutionGraphError> for LoweringError {
    fn from(error: ExecutionGraphError) -> Self {
        Self::Execution(error)
    }
}

/// Bulk hierarchical declaration lowered into one indexed execution graph.
#[derive(Clone, Debug, Eq, PartialEq)]
pub struct ExecutionGraphDeclaration {
    graphs: Vec<DeclaredGraph>,
    nodes: Vec<DeclaredNode>,
    conditions: Vec<RuntimeCondition>,
    capability_bindings: Vec<CapabilityBinding>,
}

impl ExecutionGraphDeclaration {
    #[must_use]
    pub fn new(
        graphs: impl IntoIterator<Item = DeclaredGraph>,
        nodes: impl IntoIterator<Item = DeclaredNode>,
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

    /// Derives node ownership from graph membership and validates both sides of
    /// every composite-child relation before constructing scheduler indexes.
    ///
    /// # Errors
    ///
    /// Returns [`LoweringError`] when graph membership is inconsistent or the
    /// resulting execution graph is invalid.
    pub fn lower(self) -> Result<CompiledExecutionGraph, LoweringError> {
        validate_root_graphs(&self.graphs)?;
        validate_hierarchy(&self.graphs, &self.nodes)?;

        let execution_nodes = self.nodes.into_iter().map(|node| {
            let owner = match self.graphs[node.graph.index()] {
                DeclaredGraph::Root => None,
                DeclaredGraph::OwnedBy(owner) => Some(owner),
            };
            match node.kind {
                DeclaredNodeKind::Leaf => {
                    ExecutionNode::leaf(owner, node.prerequisites, node.condition)
                }
                DeclaredNodeKind::Composite { .. } => {
                    ExecutionNode::composite(owner, node.prerequisites, node.condition)
                }
            }
        });
        CompiledExecutionGraph::lower(execution_nodes, self.conditions, self.capability_bindings)
            .map_err(LoweringError::from)
    }
}

fn validate_root_graphs(graphs: &[DeclaredGraph]) -> Result<(), LoweringError> {
    match graphs
        .iter()
        .filter(|graph| matches!(graph, DeclaredGraph::Root))
        .count()
    {
        0 => Err(LoweringError::MissingRootGraph),
        1 => Ok(()),
        _ => Err(LoweringError::MultipleRootGraphs),
    }
}

fn validate_hierarchy(
    graphs: &[DeclaredGraph],
    nodes: &[DeclaredNode],
) -> Result<(), LoweringError> {
    for (index, node) in nodes.iter().enumerate() {
        let node_id = NodeId::new(index);
        if node.graph.index() >= graphs.len() {
            return Err(LoweringError::UnknownNodeGraph {
                node: node_id,
                graph: node.graph,
            });
        }
        let DeclaredNodeKind::Composite { child_graph } = node.kind else {
            continue;
        };
        let Some(child) = graphs.get(child_graph.index()) else {
            return Err(LoweringError::UnknownChildGraph {
                node: node_id,
                graph: child_graph,
            });
        };
        if *child != DeclaredGraph::OwnedBy(node_id) {
            return Err(LoweringError::ChildGraphOwnerMismatch {
                node: node_id,
                graph: child_graph,
            });
        }
    }

    for (index, graph) in graphs.iter().enumerate() {
        let DeclaredGraph::OwnedBy(owner) = graph else {
            continue;
        };
        let graph_id = GraphId::new(index);
        let Some(owner_node) = nodes.get(owner.index()) else {
            return Err(LoweringError::UnknownGraphOwner {
                graph: graph_id,
                owner: *owner,
            });
        };
        if owner_node.kind
            != (DeclaredNodeKind::Composite {
                child_graph: graph_id,
            })
        {
            return Err(LoweringError::OwnedGraphCompositeMismatch {
                graph: graph_id,
                owner: *owner,
            });
        }
    }
    Ok(())
}

#[cfg(test)]
mod tests {
    use super::{DeclaredGraph, DeclaredNode, ExecutionGraphDeclaration, GraphId, LoweringError};
    use topology::NodeId;

    #[test]
    fn declaration_derives_nested_ownership() {
        let graph = ExecutionGraphDeclaration::new(
            [DeclaredGraph::Root, DeclaredGraph::OwnedBy(NodeId::new(0))],
            [
                DeclaredNode::composite(GraphId::new(0), GraphId::new(1), [], None),
                DeclaredNode::leaf(GraphId::new(1), [], None),
            ],
            [],
            [],
        )
        .lower()
        .unwrap();

        assert_eq!(graph.roots(), [NodeId::new(0)]);
        assert_eq!(
            graph.children(NodeId::new(0)),
            Some([NodeId::new(1)].as_slice())
        );
    }

    #[test]
    fn declaration_rejects_a_child_owned_by_another_composite() {
        let result = ExecutionGraphDeclaration::new(
            [DeclaredGraph::Root, DeclaredGraph::OwnedBy(NodeId::new(1))],
            [
                DeclaredNode::composite(GraphId::new(0), GraphId::new(1), [], None),
                DeclaredNode::leaf(GraphId::new(0), [], None),
            ],
            [],
            [],
        )
        .lower();

        assert_eq!(
            result,
            Err(LoweringError::ChildGraphOwnerMismatch {
                node: NodeId::new(0),
                graph: GraphId::new(1),
            })
        );
    }
}
