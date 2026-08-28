//! Domain-independent directed topology and DAG validation.

use std::collections::VecDeque;
use std::error::Error;
use std::fmt::{self, Display, Formatter};

/// Stable node identity inside one materialized topology.
#[derive(Clone, Copy, Debug, Eq, Hash, Ord, PartialEq, PartialOrd)]
pub struct NodeId(usize);

impl NodeId {
    #[must_use]
    pub const fn new(index: usize) -> Self {
        Self(index)
    }

    #[must_use]
    pub const fn index(self) -> usize {
        self.0
    }
}

/// A directed connection between two nodes.
#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub struct Arc {
    pub source: NodeId,
    pub target: NodeId,
}

impl Arc {
    #[must_use]
    pub const fn new(source: NodeId, target: NodeId) -> Self {
        Self { source, target }
    }
}

/// A topology construction or validation failure.
#[derive(Clone, Debug, Eq, PartialEq)]
pub enum TopologyError {
    UnknownSource(NodeId),
    UnknownTarget(NodeId),
    UnknownRoot(NodeId),
    Cycle(Vec<NodeId>),
}

impl Display for TopologyError {
    fn fmt(&self, formatter: &mut Formatter<'_>) -> fmt::Result {
        match self {
            Self::UnknownSource(node) => {
                write!(
                    formatter,
                    "topology arc has unknown source: {}",
                    node.index()
                )
            }
            Self::UnknownTarget(node) => {
                write!(
                    formatter,
                    "topology arc has unknown target: {}",
                    node.index()
                )
            }
            Self::UnknownRoot(node) => {
                write!(formatter, "topology has unknown root: {}", node.index())
            }
            Self::Cycle(cycle) => {
                let mut separator = "";
                write!(formatter, "topology contains cycle: ")?;
                for node in cycle {
                    write!(formatter, "{separator}{}", node.index())?;
                    separator = " -> ";
                }
                Ok(())
            }
        }
    }
}

impl Error for TopologyError {}

/// A directed topology without an acyclic invariant.
#[derive(Clone, Debug, Eq, PartialEq)]
pub struct DirectedTopology {
    arcs: Vec<Arc>,
    successors: Vec<Vec<NodeId>>,
    predecessors: Vec<Vec<NodeId>>,
}

impl DirectedTopology {
    /// Creates a directed topology and its adjacency indexes.
    ///
    /// # Errors
    ///
    /// Returns [`TopologyError::UnknownSource`] or
    /// [`TopologyError::UnknownTarget`] when an arc references a node outside
    /// `0..node_count`.
    pub fn new(
        node_count: usize,
        arcs: impl IntoIterator<Item = Arc>,
    ) -> Result<Self, TopologyError> {
        let mut normalized_arcs = Vec::new();
        let mut successors = vec![Vec::new(); node_count];
        let mut predecessors = vec![Vec::new(); node_count];

        for arc in arcs {
            let Some(source_successors) = successors.get_mut(arc.source.index()) else {
                return Err(TopologyError::UnknownSource(arc.source));
            };
            let Some(target_predecessors) = predecessors.get_mut(arc.target.index()) else {
                return Err(TopologyError::UnknownTarget(arc.target));
            };
            source_successors.push(arc.target);
            target_predecessors.push(arc.source);
            normalized_arcs.push(arc);
        }

        Ok(Self {
            arcs: normalized_arcs,
            successors,
            predecessors,
        })
    }

    #[must_use]
    pub fn node_count(&self) -> usize {
        self.successors.len()
    }

    #[must_use]
    pub fn arcs(&self) -> &[Arc] {
        &self.arcs
    }

    #[must_use]
    pub fn successors(&self, node: NodeId) -> Option<&[NodeId]> {
        self.successors.get(node.index()).map(Vec::as_slice)
    }

    #[must_use]
    pub fn predecessors(&self, node: NodeId) -> Option<&[NodeId]> {
        self.predecessors.get(node.index()).map(Vec::as_slice)
    }

    /// Resolves every node reachable from the supplied roots.
    ///
    /// # Errors
    ///
    /// Returns [`TopologyError::UnknownRoot`] when a root is outside this
    /// topology.
    pub fn reachable_from(
        &self,
        roots: impl IntoIterator<Item = NodeId>,
    ) -> Result<Vec<NodeId>, TopologyError> {
        let mut pending = Vec::new();
        for root in roots {
            if root.index() >= self.node_count() {
                return Err(TopologyError::UnknownRoot(root));
            }
            pending.push(root);
        }

        let mut reachable = vec![false; self.node_count()];
        while let Some(node) = pending.pop() {
            if reachable[node.index()] {
                continue;
            }
            reachable[node.index()] = true;
            pending.extend(self.successors[node.index()].iter().copied());
        }

        let mut resolved = Vec::new();
        for (index, is_reachable) in reachable.into_iter().enumerate() {
            if is_reachable {
                resolved.push(NodeId::new(index));
            }
        }
        Ok(resolved)
    }

    fn topological_order(&self) -> Result<Vec<NodeId>, TopologyError> {
        let mut indegrees = self.predecessors.iter().map(Vec::len).collect::<Vec<_>>();
        let mut ready = VecDeque::new();
        for (index, indegree) in indegrees.iter().enumerate() {
            if *indegree == 0 {
                ready.push_back(NodeId::new(index));
            }
        }
        let mut order = Vec::with_capacity(self.node_count());

        while let Some(node) = ready.pop_front() {
            order.push(node);
            for successor in &self.successors[node.index()] {
                let indegree = &mut indegrees[successor.index()];
                *indegree -= 1;
                if *indegree == 0 {
                    ready.push_back(*successor);
                }
            }
        }

        if order.len() == self.node_count() {
            return Ok(order);
        }
        Err(TopologyError::Cycle(self.find_cycle()))
    }

    fn find_cycle(&self) -> Vec<NodeId> {
        let mut colors = vec![0_u8; self.node_count()];
        let mut path = Vec::new();
        let mut path_positions = vec![None; self.node_count()];

        for root_index in 0..self.node_count() {
            if colors[root_index] != 0 {
                continue;
            }

            let root = NodeId::new(root_index);
            colors[root_index] = 1;
            path_positions[root_index] = Some(path.len());
            path.push(root);
            let mut stack = vec![(root, 0_usize)];

            while let Some((node, successor_index)) = stack.last_mut() {
                if *successor_index == self.successors[node.index()].len() {
                    colors[node.index()] = 2;
                    path_positions[node.index()] = None;
                    path.pop();
                    stack.pop();
                    continue;
                }

                let successor = self.successors[node.index()][*successor_index];
                *successor_index += 1;
                match colors[successor.index()] {
                    0 => {
                        colors[successor.index()] = 1;
                        path_positions[successor.index()] = Some(path.len());
                        path.push(successor);
                        stack.push((successor, 0));
                    }
                    1 => {
                        let cycle_start = path_positions[successor.index()]
                            .expect("active DFS node must have a path position");
                        let mut cycle = path[cycle_start..].to_vec();
                        cycle.push(successor);
                        return cycle;
                    }
                    _ => {}
                }
            }
        }

        unreachable!("Kahn validation reported a cycle but DFS did not find one")
    }
}

/// A directed topology validated as acyclic.
#[derive(Clone, Debug, Eq, PartialEq)]
pub struct DagTopology {
    directed: DirectedTopology,
    order: Vec<NodeId>,
}

impl DagTopology {
    /// Validates and preserves an acyclic directed topology.
    ///
    /// # Errors
    ///
    /// Returns [`TopologyError::Cycle`] with one concrete cycle when the
    /// directed topology is cyclic.
    pub fn new(directed: DirectedTopology) -> Result<Self, TopologyError> {
        let order = directed.topological_order()?;
        Ok(Self { directed, order })
    }

    #[must_use]
    pub fn directed(&self) -> &DirectedTopology {
        &self.directed
    }

    #[must_use]
    pub fn topological_order(&self) -> &[NodeId] {
        &self.order
    }
}

#[cfg(test)]
mod tests {
    use super::{Arc, DagTopology, DirectedTopology, NodeId, TopologyError};

    fn node(index: usize) -> NodeId {
        NodeId::new(index)
    }

    #[test]
    fn directed_topology_resolves_reachability_with_cycles() {
        let topology = DirectedTopology::new(
            4,
            [
                Arc::new(node(0), node(1)),
                Arc::new(node(1), node(2)),
                Arc::new(node(2), node(0)),
            ],
        )
        .unwrap();

        assert_eq!(
            topology.reachable_from([node(0)]).unwrap(),
            vec![node(0), node(1), node(2)]
        );
    }

    #[test]
    fn dag_rejects_a_cycle_and_preserves_its_path() {
        let topology =
            DirectedTopology::new(2, [Arc::new(node(0), node(1)), Arc::new(node(1), node(0))])
                .unwrap();

        assert_eq!(
            DagTopology::new(topology),
            Err(TopologyError::Cycle(vec![node(0), node(1), node(0)]))
        );
    }

    #[test]
    fn dag_returns_a_stable_topological_order() {
        let topology =
            DirectedTopology::new(3, [Arc::new(node(0), node(1)), Arc::new(node(1), node(2))])
                .unwrap();
        let dag = DagTopology::new(topology).unwrap();

        assert_eq!(dag.topological_order(), &[node(0), node(1), node(2)]);
    }

    #[test]
    fn topology_rejects_an_unknown_endpoint() {
        assert_eq!(
            DirectedTopology::new(1, [Arc::new(node(0), node(1))]),
            Err(TopologyError::UnknownTarget(node(1)))
        );
    }
}
