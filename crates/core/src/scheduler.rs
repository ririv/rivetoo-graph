use std::collections::VecDeque;
use std::error::Error;
use std::fmt::{self, Display, Formatter};
use std::sync::Arc;

use crate::execution::{CompiledExecutionGraph, ConditionId, ExecutionNodeKind, Prerequisite};
use crate::topology::NodeId;

/// Mutually exclusive lifecycle state of one compiled execution node.
#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub enum NodeState {
    Pending,
    Active,
    Ready,
    Running,
    Finished,
    Skipped,
}

impl NodeState {
    const fn is_terminal(self) -> bool {
        matches!(self, Self::Finished | Self::Skipped)
    }
}

/// Host callbacks kept outside the pure scheduler state machine.
pub trait SchedulerHost {
    type Error;

    /// # Errors
    /// Returns the host-specific failure for this callback.
    fn is_skipped(&mut self, node: NodeId) -> Result<bool, Self::Error>;

    /// # Errors
    /// Returns the host-specific failure for this callback.
    fn evaluate_condition(
        &mut self,
        node: NodeId,
        condition: ConditionId,
    ) -> Result<bool, Self::Error>;

    /// # Errors
    /// Returns the host-specific failure for this callback.
    fn record_condition(&mut self, node: NodeId, value: bool) -> Result<(), Self::Error>;

    /// # Errors
    /// Returns the host-specific failure for this callback.
    fn record_condition_skip(&mut self, node: NodeId) -> Result<(), Self::Error>;

    /// # Errors
    /// Returns the host-specific failure for this callback.
    fn enter_composite(&mut self, node: NodeId) -> Result<(), Self::Error>;

    /// # Errors
    /// Returns the host-specific failure for this callback.
    fn complete_composite(&mut self, node: NodeId) -> Result<(), Self::Error>;
}

/// A scheduler transition or host callback failure.
#[derive(Debug, Eq, PartialEq)]
pub enum SchedulerError<HostError> {
    Host(HostError),
    NotLeaf(NodeId),
    NotRunning(NodeId),
    Unresolved(Vec<NodeId>),
}

impl<HostError: Display> Display for SchedulerError<HostError> {
    fn fmt(&self, formatter: &mut Formatter<'_>) -> fmt::Result {
        match self {
            Self::Host(error) => write!(formatter, "scheduler host failed: {error}"),
            Self::NotLeaf(node) => {
                write!(formatter, "execution node is not a leaf: {}", node.index())
            }
            Self::NotRunning(node) => {
                write!(formatter, "execution leaf is not running: {}", node.index())
            }
            Self::Unresolved(nodes) => {
                write!(formatter, "compiled graph has unresolved nodes: ")?;
                let mut separator = "";
                for node in nodes {
                    write!(formatter, "{separator}{}", node.index())?;
                    separator = ", ";
                }
                Ok(())
            }
        }
    }
}

impl<HostError: Error + 'static> Error for SchedulerError<HostError> {}

/// Rust-owned coordinator for one compiled graph execution.
pub struct CompiledScheduler {
    graph: Arc<CompiledExecutionGraph>,
    states: Vec<NodeState>,
    ready: VecDeque<NodeId>,
    evaluation_queue: VecDeque<NodeId>,
    queued: Vec<bool>,
    condition_values: Vec<Option<bool>>,
}

impl CompiledScheduler {
    #[must_use]
    pub fn new(graph: Arc<CompiledExecutionGraph>) -> Self {
        Self {
            states: vec![NodeState::Pending; graph.nodes().len()],
            ready: VecDeque::new(),
            evaluation_queue: VecDeque::new(),
            queued: vec![false; graph.nodes().len()],
            condition_values: vec![None; graph.conditions().len()],
            graph,
        }
    }

    /// Activates every root and resolves all immediately available transitions.
    ///
    /// # Errors
    ///
    /// Returns [`SchedulerError::Host`] when a host callback fails.
    pub fn start<Host: SchedulerHost>(
        &mut self,
        host: &mut Host,
    ) -> Result<(), SchedulerError<Host::Error>> {
        let roots = self.graph.roots().to_vec();
        for root in roots {
            self.queue_evaluation(root);
        }
        self.drain_evaluations(host)
    }

    #[must_use]
    pub fn is_finished(&self) -> bool {
        self.graph
            .roots()
            .iter()
            .all(|root| self.is_terminal(*root))
    }

    #[must_use]
    pub fn state(&self, node: NodeId) -> Option<NodeState> {
        self.states.get(node.index()).copied()
    }

    /// Takes one ready leaf and fixes its state as running.
    ///
    /// # Errors
    ///
    /// Returns [`SchedulerError::Host`] when a host callback fails.
    pub fn take_ready_leaf<Host: SchedulerHost>(
        &mut self,
        host: &mut Host,
    ) -> Result<Option<NodeId>, SchedulerError<Host::Error>> {
        while let Some(node) = self.ready.pop_front() {
            if self.states[node.index()] != NodeState::Ready {
                continue;
            }
            if host.is_skipped(node).map_err(SchedulerError::Host)? {
                self.skip_node(node, false, host)?;
                self.drain_evaluations(host)?;
                continue;
            }
            self.states[node.index()] = NodeState::Running;
            return Ok(Some(node));
        }
        Ok(None)
    }

    /// Completes one running leaf and advances every affected node.
    ///
    /// # Errors
    ///
    /// Returns a transition error for a non-running/non-leaf node, or
    /// [`SchedulerError::Host`] when a host callback fails.
    pub fn complete_leaf<Host: SchedulerHost>(
        &mut self,
        node: NodeId,
        host: &mut Host,
    ) -> Result<(), SchedulerError<Host::Error>> {
        let Some(execution_node) = self.graph.nodes().get(node.index()) else {
            return Err(SchedulerError::NotLeaf(node));
        };
        if execution_node.kind() != ExecutionNodeKind::Leaf {
            return Err(SchedulerError::NotLeaf(node));
        }
        if self.states[node.index()] != NodeState::Running {
            return Err(SchedulerError::NotRunning(node));
        }
        self.states[node.index()] = NodeState::Finished;
        self.notify_terminal(node, host)?;
        self.drain_evaluations(host)
    }

    /// Verifies that every root reached a terminal state.
    ///
    /// # Errors
    ///
    /// Returns [`SchedulerError::Unresolved`] with all non-terminal nodes when
    /// execution cannot converge.
    pub fn require_finished<HostError>(&self) -> Result<(), SchedulerError<HostError>> {
        if self.is_finished() {
            return Ok(());
        }
        let mut unresolved = Vec::new();
        for (index, state) in self.states.iter().enumerate() {
            if !state.is_terminal() {
                unresolved.push(NodeId::new(index));
            }
        }
        Err(SchedulerError::Unresolved(unresolved))
    }

    fn queue_evaluation(&mut self, node: NodeId) {
        if self.queued[node.index()] || self.states[node.index()] != NodeState::Pending {
            return;
        }
        self.queued[node.index()] = true;
        self.evaluation_queue.push_back(node);
    }

    fn drain_evaluations<Host: SchedulerHost>(
        &mut self,
        host: &mut Host,
    ) -> Result<(), SchedulerError<Host::Error>> {
        while let Some(node) = self.evaluation_queue.pop_front() {
            self.queued[node.index()] = false;
            if self.states[node.index()] != NodeState::Pending {
                continue;
            }
            self.evaluate_node(node, host)?;
        }
        Ok(())
    }

    fn evaluate_node<Host: SchedulerHost>(
        &mut self,
        node_id: NodeId,
        host: &mut Host,
    ) -> Result<(), SchedulerError<Host::Error>> {
        let node = self.graph.nodes()[node_id.index()].clone();
        if let Some(owner) = node.owner()
            && self.states[owner.index()] != NodeState::Active
        {
            return Ok(());
        }

        let explicitly_skipped = host.is_skipped(node_id).map_err(SchedulerError::Host)?;
        if let Some(condition) = node.when()
            && !explicitly_skipped
        {
            let Some(value) = self.condition_value(node_id, condition, host)? else {
                return Ok(());
            };
            host.record_condition(node_id, value)
                .map_err(SchedulerError::Host)?;
            if !value {
                self.skip_node(node_id, true, host)?;
                return Ok(());
            }
        }

        for prerequisite in node.prerequisites() {
            let Some(satisfied) = self.prerequisite_satisfied(node_id, *prerequisite, host)? else {
                return Ok(());
            };
            if !satisfied {
                return Ok(());
            }
        }

        if explicitly_skipped {
            self.skip_node(node_id, false, host)?;
            return Ok(());
        }
        match node.kind() {
            ExecutionNodeKind::Leaf => {
                self.states[node_id.index()] = NodeState::Ready;
                self.ready.push_back(node_id);
            }
            ExecutionNodeKind::Composite => self.activate_composite(node_id, host)?,
        }
        Ok(())
    }

    fn prerequisite_satisfied<Host: SchedulerHost>(
        &mut self,
        target: NodeId,
        prerequisite: Prerequisite,
        host: &mut Host,
    ) -> Result<Option<bool>, SchedulerError<Host::Error>> {
        match prerequisite {
            Prerequisite::Required(node) => Ok(Some(self.is_terminal(node))),
            Prerequisite::Conditional { node, condition } => {
                let Some(active) = self.condition_value(target, condition, host)? else {
                    return Ok(None);
                };
                Ok(Some(!active || self.is_terminal(node)))
            }
        }
    }

    fn condition_value<Host: SchedulerHost>(
        &mut self,
        node: NodeId,
        condition: ConditionId,
        host: &mut Host,
    ) -> Result<Option<bool>, SchedulerError<Host::Error>> {
        let spec = &self.graph.conditions()[condition.index()];
        if spec.reads().iter().any(|read| !self.is_terminal(*read)) {
            return Ok(None);
        }
        if let Some(value) = self.condition_values[condition.index()] {
            return Ok(Some(value));
        }
        let value = host
            .evaluate_condition(node, condition)
            .map_err(SchedulerError::Host)?;
        self.condition_values[condition.index()] = Some(value);
        Ok(Some(value))
    }

    fn activate_composite<Host: SchedulerHost>(
        &mut self,
        composite: NodeId,
        host: &mut Host,
    ) -> Result<(), SchedulerError<Host::Error>> {
        self.states[composite.index()] = NodeState::Active;
        host.enter_composite(composite)
            .map_err(SchedulerError::Host)?;
        let children = self
            .graph
            .children(composite)
            .expect("validated composite must be present")
            .to_vec();
        if children.is_empty() {
            return self.complete_composite(composite, host);
        }
        for child in children {
            self.queue_evaluation(child);
        }
        Ok(())
    }

    fn complete_composite<Host: SchedulerHost>(
        &mut self,
        composite: NodeId,
        host: &mut Host,
    ) -> Result<(), SchedulerError<Host::Error>> {
        host.complete_composite(composite)
            .map_err(SchedulerError::Host)?;
        self.states[composite.index()] = NodeState::Finished;
        self.notify_terminal(composite, host)
    }

    fn skip_node<Host: SchedulerHost>(
        &mut self,
        node: NodeId,
        record_condition_skip: bool,
        host: &mut Host,
    ) -> Result<(), SchedulerError<Host::Error>> {
        if record_condition_skip {
            host.record_condition_skip(node)
                .map_err(SchedulerError::Host)?;
        }
        if self.graph.nodes()[node.index()].kind() == ExecutionNodeKind::Composite {
            self.mark_descendants_skipped(node);
        }
        self.states[node.index()] = NodeState::Skipped;
        self.notify_terminal(node, host)
    }

    fn mark_descendants_skipped(&mut self, composite: NodeId) {
        let children = self
            .graph
            .children(composite)
            .expect("validated composite must be present")
            .to_vec();
        for child in children {
            if self.graph.nodes()[child.index()].kind() == ExecutionNodeKind::Composite {
                self.mark_descendants_skipped(child);
            }
            self.states[child.index()] = NodeState::Skipped;
        }
    }

    fn notify_terminal<Host: SchedulerHost>(
        &mut self,
        node: NodeId,
        host: &mut Host,
    ) -> Result<(), SchedulerError<Host::Error>> {
        let dependents = self
            .graph
            .dependents(node)
            .expect("validated execution node must be present")
            .to_vec();
        for dependent in dependents {
            self.queue_evaluation(dependent);
        }

        let Some(owner) = self.graph.nodes()[node.index()].owner() else {
            return Ok(());
        };
        let owner_is_complete = self.states[owner.index()] == NodeState::Active
            && self
                .graph
                .children(owner)
                .expect("validated composite must be present")
                .iter()
                .all(|child| self.is_terminal(*child));
        if owner_is_complete {
            self.complete_composite(owner, host)?;
        }
        Ok(())
    }

    fn is_terminal(&self, node: NodeId) -> bool {
        self.states[node.index()].is_terminal()
    }
}

#[cfg(test)]
mod tests {
    use std::convert::Infallible;
    use std::sync::Arc;

    use super::{CompiledScheduler, NodeState, SchedulerHost};
    use crate::execution::{
        CompiledExecutionGraph, ConditionId, ExecutionNode, Prerequisite, RuntimeCondition,
    };
    use crate::topology::NodeId;

    #[derive(Default)]
    struct Host {
        conditions: Vec<bool>,
        skipped: Vec<NodeId>,
        entered: Vec<NodeId>,
        completed: Vec<NodeId>,
    }

    impl SchedulerHost for Host {
        type Error = Infallible;

        fn is_skipped(&mut self, node: NodeId) -> Result<bool, Self::Error> {
            Ok(self.skipped.contains(&node))
        }

        fn evaluate_condition(
            &mut self,
            _node: NodeId,
            condition: ConditionId,
        ) -> Result<bool, Self::Error> {
            Ok(self.conditions[condition.index()])
        }

        fn record_condition(&mut self, _node: NodeId, _value: bool) -> Result<(), Self::Error> {
            Ok(())
        }

        fn record_condition_skip(&mut self, node: NodeId) -> Result<(), Self::Error> {
            self.skipped.push(node);
            Ok(())
        }

        fn enter_composite(&mut self, node: NodeId) -> Result<(), Self::Error> {
            self.entered.push(node);
            Ok(())
        }

        fn complete_composite(&mut self, node: NodeId) -> Result<(), Self::Error> {
            self.completed.push(node);
            Ok(())
        }
    }

    fn node(index: usize) -> NodeId {
        NodeId::new(index)
    }

    fn graph(condition_value: bool) -> (Arc<CompiledExecutionGraph>, Host) {
        let graph = CompiledExecutionGraph::new(
            [
                ExecutionNode::composite(None, [], None),
                ExecutionNode::leaf(Some(node(0)), [], None),
                ExecutionNode::leaf(
                    Some(node(0)),
                    [Prerequisite::Required(node(1))],
                    Some(ConditionId::new(0)),
                ),
            ],
            [RuntimeCondition::new([node(1)])],
        )
        .unwrap();
        (
            Arc::new(graph),
            Host {
                conditions: vec![condition_value],
                ..Host::default()
            },
        )
    }

    #[test]
    fn scheduler_runs_leafs_and_completes_owners() {
        let (graph, mut host) = graph(true);
        let mut scheduler = CompiledScheduler::new(graph);

        scheduler.start(&mut host).unwrap();
        let first = scheduler.take_ready_leaf(&mut host).unwrap().unwrap();
        assert_eq!(first, node(1));
        scheduler.complete_leaf(first, &mut host).unwrap();
        let second = scheduler.take_ready_leaf(&mut host).unwrap().unwrap();
        assert_eq!(second, node(2));
        scheduler.complete_leaf(second, &mut host).unwrap();

        assert!(scheduler.is_finished());
        assert_eq!(scheduler.state(node(0)), Some(NodeState::Finished));
        assert_eq!(host.entered, vec![node(0)]);
        assert_eq!(host.completed, vec![node(0)]);
    }

    #[test]
    fn scheduler_skips_a_false_conditional_leaf() {
        let (graph, mut host) = graph(false);
        let mut scheduler = CompiledScheduler::new(graph);

        scheduler.start(&mut host).unwrap();
        let first = scheduler.take_ready_leaf(&mut host).unwrap().unwrap();
        scheduler.complete_leaf(first, &mut host).unwrap();

        assert_eq!(scheduler.take_ready_leaf(&mut host).unwrap(), None);
        assert!(scheduler.is_finished());
        assert_eq!(scheduler.state(node(2)), Some(NodeState::Skipped));
    }
}
