use std::collections::VecDeque;
use std::error::Error;
use std::fmt::{self, Display, Formatter};
use std::sync::Arc;

use crate::execution::{CompiledExecutionGraph, ConditionId, ExecutionNodeKind, Prerequisite};
use topology::NodeId;

/// Runtime state of one compiled execution node.
#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub enum NodeState {
    Pending,
    Ready,
    Running,
    Active,
    Finished,
    Skipped,
    Failed,
    Blocked,
}

impl NodeState {
    const fn is_terminal(self) -> bool {
        matches!(
            self,
            Self::Finished | Self::Skipped | Self::Failed | Self::Blocked
        )
    }

    const fn is_failure(self) -> bool {
        matches!(self, Self::Failed | Self::Blocked)
    }
}

/// One runtime predicate whose result is now required from the action host.
#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub struct ConditionRequest {
    node: NodeId,
    condition: ConditionId,
}

impl ConditionRequest {
    #[must_use]
    pub const fn node(self) -> NodeId {
        self.node
    }

    #[must_use]
    pub const fn condition(self) -> ConditionId {
        self.condition
    }
}

/// A condition value returned by the action host.
#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub struct ConditionResolution {
    condition: ConditionId,
    value: Result<bool, ()>,
}

impl ConditionResolution {
    #[must_use]
    pub const fn new(condition: ConditionId, value: bool) -> Self {
        Self {
            condition,
            value: Ok(value),
        }
    }

    #[must_use]
    pub const fn failed(condition: ConditionId) -> Self {
        Self {
            condition,
            value: Err(()),
        }
    }
}

/// Structural events emitted without calling into the action host.
#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub enum ExecutionEvent {
    ConditionEvaluated { node: NodeId, value: bool },
    ConditionSkipped(NodeId),
    CompositeEntered(NodeId),
    CompositeCompleted(NodeId),
    NodeSkipped(NodeId),
    NodeFailed(NodeId),
    NodeBlocked(NodeId),
}

/// Whether a returned batch leaves more work or completes the session.
#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub enum SessionStatus {
    Running,
    Finished,
    Failed,
}

/// One event-driven exchange between the Rust session and an action executor.
#[derive(Clone, Debug, Eq, PartialEq)]
pub struct ExecutionBatch {
    actions: Vec<NodeId>,
    conditions: Vec<ConditionRequest>,
    events: Vec<ExecutionEvent>,
    status: SessionStatus,
}

impl ExecutionBatch {
    #[must_use]
    pub fn actions(&self) -> &[NodeId] {
        &self.actions
    }

    #[must_use]
    pub fn conditions(&self) -> &[ConditionRequest] {
        &self.conditions
    }

    #[must_use]
    pub fn events(&self) -> &[ExecutionEvent] {
        &self.events
    }

    #[must_use]
    pub const fn status(&self) -> SessionStatus {
        self.status
    }
}

/// A batch of host outcomes applied atomically before the next ready batch is derived.
#[derive(Clone, Debug, Default, Eq, PartialEq)]
pub struct ExecutionUpdate {
    completed: Vec<(NodeId, Result<(), ()>)>,
    conditions: Vec<ConditionResolution>,
    skipped: Vec<NodeId>,
}

impl ExecutionUpdate {
    #[must_use]
    pub fn new(
        completed: impl IntoIterator<Item = (NodeId, Result<(), ()>)>,
        conditions: impl IntoIterator<Item = ConditionResolution>,
        skipped: impl IntoIterator<Item = NodeId>,
    ) -> Self {
        Self {
            completed: completed.into_iter().collect(),
            conditions: conditions.into_iter().collect(),
            skipped: skipped.into_iter().collect(),
        }
    }
}

/// An invalid state transition supplied by an action executor.
#[derive(Clone, Debug, Eq, PartialEq)]
pub enum SessionError {
    InvalidCapacity,
    AlreadyStarted,
    NotStarted,
    UnknownNode(NodeId),
    UnknownCondition(ConditionId),
    ConditionNotRequested(ConditionId),
    ConditionAlreadyResolved(ConditionId),
    NodeNotRunning(NodeId),
    NodeNotLeaf(NodeId),
    Unresolved(Vec<NodeId>),
}

impl Display for SessionError {
    fn fmt(&self, formatter: &mut Formatter<'_>) -> fmt::Result {
        match self {
            Self::InvalidCapacity => write!(formatter, "execution capacity must be >= 1"),
            Self::AlreadyStarted => write!(formatter, "execution session is already started"),
            Self::NotStarted => write!(formatter, "execution session is not started"),
            Self::UnknownNode(node) => {
                write!(formatter, "unknown execution node: {}", node.index())
            }
            Self::UnknownCondition(condition) => {
                write!(
                    formatter,
                    "unknown runtime condition: {}",
                    condition.index()
                )
            }
            Self::ConditionNotRequested(condition) => write!(
                formatter,
                "runtime condition {} was not requested",
                condition.index()
            ),
            Self::ConditionAlreadyResolved(condition) => write!(
                formatter,
                "runtime condition {} is already resolved",
                condition.index()
            ),
            Self::NodeNotRunning(node) => {
                write!(formatter, "execution node {} is not running", node.index())
            }
            Self::NodeNotLeaf(node) => {
                write!(formatter, "execution node {} is not a leaf", node.index())
            }
            Self::Unresolved(nodes) => {
                write!(formatter, "execution session has unresolved nodes: ")?;
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

impl Error for SessionError {}

/// Rust-owned graph state machine driven through batched start/advance exchanges.
pub struct ExecutionSession {
    graph: Arc<CompiledExecutionGraph>,
    states: Vec<NodeState>,
    ready: VecDeque<NodeId>,
    evaluation_queue: VecDeque<NodeId>,
    queued: Vec<bool>,
    condition_values: Vec<Option<Result<bool, ()>>>,
    condition_requested: Vec<bool>,
    condition_waiters: Vec<Vec<NodeId>>,
    explicit_skips: Vec<bool>,
    pending_conditions: Vec<ConditionRequest>,
    pending_events: Vec<ExecutionEvent>,
    started: bool,
    remaining_children: Vec<usize>,
}

impl ExecutionSession {
    /// Creates a session with the host's explicit initial skip set.
    ///
    /// # Errors
    /// Returns [`SessionError::UnknownNode`] when the skip set references a node
    /// outside the compiled graph.
    pub fn new(
        graph: Arc<CompiledExecutionGraph>,
        initially_skipped: impl IntoIterator<Item = NodeId>,
    ) -> Result<Self, SessionError> {
        let mut explicit_skips = vec![false; graph.nodes().len()];
        for node in initially_skipped {
            let Some(slot) = explicit_skips.get_mut(node.index()) else {
                return Err(SessionError::UnknownNode(node));
            };
            *slot = true;
        }
        // owner 已在编译阶段校验；O(V) 建立计数，不复制每个子图的 children。
        let mut remaining_children = vec![0; graph.nodes().len()];
        for spec in graph.nodes() {
            if let Some(owner) = spec.owner() {
                remaining_children[owner.index()] += 1;
            }
        }
        Ok(Self {
            remaining_children,
            states: vec![NodeState::Pending; graph.nodes().len()],
            ready: VecDeque::new(),
            evaluation_queue: VecDeque::new(),
            queued: vec![false; graph.nodes().len()],
            condition_values: vec![None; graph.conditions().len()],
            condition_requested: vec![false; graph.conditions().len()],
            condition_waiters: vec![Vec::new(); graph.conditions().len()],
            explicit_skips,
            pending_conditions: Vec::new(),
            pending_events: Vec::new(),
            graph,
            started: false,
        })
    }

    /// Starts this session and returns the first dispatch batch.
    ///
    /// # Errors
    /// Returns an error when the session was already started or cannot converge.
    pub fn start(&mut self, max_actions: usize) -> Result<ExecutionBatch, SessionError> {
        if max_actions == 0 {
            return Err(SessionError::InvalidCapacity);
        }
        if self.started {
            return Err(SessionError::AlreadyStarted);
        }
        self.started = true;
        for root in self.graph.roots().to_vec() {
            self.queue_evaluation(root);
        }
        self.drive(max_actions)
    }

    /// Applies one host batch and immediately derives the next dispatch batch.
    ///
    /// # Errors
    /// Returns an error for an invalid completion, condition resolution, or skip identity.
    pub fn advance(
        &mut self,
        update: ExecutionUpdate,
        max_actions: usize,
    ) -> Result<ExecutionBatch, SessionError> {
        if !self.started {
            return Err(SessionError::NotStarted);
        }
        for node in update.skipped {
            let Some(slot) = self.explicit_skips.get_mut(node.index()) else {
                return Err(SessionError::UnknownNode(node));
            };
            *slot = true;
            self.queue_evaluation(node);
        }
        for resolution in update.conditions {
            let Some(value) = self.condition_values.get_mut(resolution.condition.index()) else {
                return Err(SessionError::UnknownCondition(resolution.condition));
            };
            if value.is_some() {
                return Err(SessionError::ConditionAlreadyResolved(resolution.condition));
            }
            if !self.condition_requested[resolution.condition.index()] {
                return Err(SessionError::ConditionNotRequested(resolution.condition));
            }
            *value = Some(resolution.value);
            self.condition_requested[resolution.condition.index()] = false;
            for waiter in std::mem::take(&mut self.condition_waiters[resolution.condition.index()])
            {
                self.queue_evaluation(waiter);
            }
        }
        for (node, outcome) in update.completed {
            let Some(spec) = self.graph.nodes().get(node.index()) else {
                return Err(SessionError::UnknownNode(node));
            };
            if spec.kind() != ExecutionNodeKind::Leaf {
                return Err(SessionError::NodeNotLeaf(node));
            }
            if self.states[node.index()] != NodeState::Running {
                return Err(SessionError::NodeNotRunning(node));
            }
            self.states[node.index()] = if outcome.is_ok() {
                NodeState::Finished
            } else {
                self.pending_events.push(ExecutionEvent::NodeFailed(node));
                NodeState::Failed
            };
            self.notify_terminal(node);
        }
        self.drive(max_actions)
    }

    #[must_use]
    pub fn state(&self, node: NodeId) -> Option<NodeState> {
        self.states.get(node.index()).copied()
    }

    fn drive(&mut self, max_actions: usize) -> Result<ExecutionBatch, SessionError> {
        self.drain_evaluations();
        let mut actions = Vec::new();
        while actions.len() < max_actions {
            let Some(node) = self.ready.pop_front() else {
                break;
            };
            if self.states[node.index()] != NodeState::Ready {
                continue;
            }
            if self.is_explicitly_skipped(node) {
                self.skip_node(node, false);
                self.drain_evaluations();
                continue;
            }
            self.states[node.index()] = NodeState::Running;
            actions.push(node);
        }
        let finished = self
            .graph
            .roots()
            .iter()
            .all(|root| self.is_terminal(*root));
        let conditions = std::mem::take(&mut self.pending_conditions);
        let events = std::mem::take(&mut self.pending_events);
        if !finished
            && actions.is_empty()
            && conditions.is_empty()
            && !self.states.contains(&NodeState::Running)
            && self.ready.is_empty()
        {
            return Err(SessionError::Unresolved(
                self.states
                    .iter()
                    .enumerate()
                    .filter_map(|(index, state)| {
                        (!state.is_terminal()).then_some(NodeId::new(index))
                    })
                    .collect(),
            ));
        }
        Ok(ExecutionBatch {
            actions,
            conditions,
            events,
            status: if !finished {
                SessionStatus::Running
            } else if self
                .graph
                .roots()
                .iter()
                .any(|root| self.states[root.index()].is_failure())
            {
                SessionStatus::Failed
            } else {
                SessionStatus::Finished
            },
        })
    }

    fn queue_evaluation(&mut self, node: NodeId) {
        if self.queued[node.index()] || self.states[node.index()] != NodeState::Pending {
            return;
        }
        self.queued[node.index()] = true;
        self.evaluation_queue.push_back(node);
    }

    fn drain_evaluations(&mut self) {
        while let Some(node) = self.evaluation_queue.pop_front() {
            self.queued[node.index()] = false;
            if self.states[node.index()] == NodeState::Pending {
                self.evaluate_node(node);
            }
        }
    }

    fn evaluate_node(&mut self, node_id: NodeId) {
        let node = self.graph.nodes()[node_id.index()].clone();
        if let Some(owner) = node.owner()
            && self.states[owner.index()] != NodeState::Active
        {
            return;
        }
        let explicitly_skipped = self.is_explicitly_skipped(node_id);
        if let Some(condition) = node.condition()
            && !explicitly_skipped
        {
            let Some(outcome) = self.condition_value(node_id, condition) else {
                return;
            };
            let Ok(value) = outcome else {
                self.block_node(node_id);
                return;
            };
            self.pending_events
                .push(ExecutionEvent::ConditionEvaluated {
                    node: node_id,
                    value,
                });
            if !value {
                self.skip_node(node_id, true);
                return;
            }
        }
        for prerequisite in node.prerequisites() {
            match self.prerequisite_satisfied(node_id, *prerequisite) {
                Ok(true) => {}
                Ok(false) => return,
                Err(()) => {
                    self.block_node(node_id);
                    return;
                }
            }
        }
        if explicitly_skipped {
            self.skip_node(node_id, false);
            return;
        }
        match node.kind() {
            ExecutionNodeKind::Leaf => {
                self.states[node_id.index()] = NodeState::Ready;
                self.ready.push_back(node_id);
            }
            ExecutionNodeKind::Composite => self.activate_composite(node_id),
        }
    }

    fn prerequisite_satisfied(
        &mut self,
        target: NodeId,
        prerequisite: Prerequisite,
    ) -> Result<bool, ()> {
        match prerequisite {
            Prerequisite::Required(node) => self.prerequisite_ready(node),
            Prerequisite::Conditional { node, condition } => {
                match self.condition_value(target, condition) {
                    Some(Ok(true)) => self.prerequisite_ready(node),
                    Some(Ok(false)) => Ok(true),
                    Some(Err(())) => Err(()),
                    None => Ok(false),
                }
            }
        }
    }

    fn prerequisite_ready(&self, node: NodeId) -> Result<bool, ()> {
        if self.states[node.index()].is_failure() {
            Err(())
        } else {
            Ok(self.is_terminal(node))
        }
    }

    fn condition_value(
        &mut self,
        node: NodeId,
        condition: ConditionId,
    ) -> Option<Result<bool, ()>> {
        let spec = &self.graph.conditions()[condition.index()];
        if spec
            .reads()
            .iter()
            .any(|read| self.states[read.index()].is_failure())
        {
            return Some(Err(()));
        }
        if spec.reads().iter().any(|read| !self.is_terminal(*read)) {
            return None;
        }
        if let Some(value) = self.condition_values[condition.index()] {
            return Some(value);
        }
        if !self.condition_requested[condition.index()] {
            self.condition_requested[condition.index()] = true;
            self.pending_conditions
                .push(ConditionRequest { node, condition });
        }
        if !self.condition_waiters[condition.index()].contains(&node) {
            self.condition_waiters[condition.index()].push(node);
        }
        None
    }

    fn activate_composite(&mut self, composite: NodeId) {
        self.states[composite.index()] = NodeState::Active;
        self.pending_events
            .push(ExecutionEvent::CompositeEntered(composite));
        let children = self.graph.children(composite).unwrap().to_vec();
        if children.is_empty() {
            self.complete_composite(composite);
            return;
        }
        for child in children {
            self.queue_evaluation(child);
        }
    }

    fn complete_composite(&mut self, composite: NodeId) {
        let failed = self
            .graph
            .children(composite)
            .unwrap()
            .iter()
            .any(|child| self.states[child.index()].is_failure());
        if failed {
            self.pending_events
                .push(ExecutionEvent::NodeFailed(composite));
            self.states[composite.index()] = NodeState::Failed;
        } else {
            self.pending_events
                .push(ExecutionEvent::CompositeCompleted(composite));
            self.states[composite.index()] = NodeState::Finished;
        }
        self.notify_terminal(composite);
    }

    fn block_node(&mut self, node: NodeId) {
        // 尚未激活的子图整体不可进入；每个后代只访问一次，不逐节点回溯祖先。
        // 阻断本身为 O(V_blocked + E_out)，额外栈空间 O(V_blocked)。
        let mut pending = vec![node];
        while let Some(blocked) = pending.pop() {
            pending.extend_from_slice(self.graph.children(blocked).unwrap());
            self.states[blocked.index()] = NodeState::Blocked;
            self.pending_events
                .push(ExecutionEvent::NodeBlocked(blocked));
            for dependent in self.graph.dependents(blocked).unwrap().to_vec() {
                self.queue_evaluation(dependent);
            }
        }
        self.notify_terminal(node);
    }

    fn skip_node(&mut self, node: NodeId, condition_skip: bool) {
        if condition_skip {
            self.pending_events
                .push(ExecutionEvent::ConditionSkipped(node));
        }
        if self.graph.nodes()[node.index()].kind() == ExecutionNodeKind::Composite {
            self.mark_descendants_skipped(node);
        }
        self.states[node.index()] = NodeState::Skipped;
        self.pending_events.push(ExecutionEvent::NodeSkipped(node));
        self.notify_terminal(node);
    }

    fn mark_descendants_skipped(&mut self, composite: NodeId) {
        let mut descendants = Vec::new();
        self.collect_descendants_skipped(composite, &mut descendants);
        for descendant in descendants {
            for dependent in self.graph.dependents(descendant).unwrap().to_vec() {
                self.queue_evaluation(dependent);
            }
        }
    }

    fn collect_descendants_skipped(&mut self, composite: NodeId, descendants: &mut Vec<NodeId>) {
        for child in self.graph.children(composite).unwrap().to_vec() {
            if self.graph.nodes()[child.index()].kind() == ExecutionNodeKind::Composite {
                self.collect_descendants_skipped(child, descendants);
            }
            self.states[child.index()] = NodeState::Skipped;
            self.pending_events.push(ExecutionEvent::NodeSkipped(child));
            descendants.push(child);
        }
    }

    fn notify_terminal(&mut self, node: NodeId) {
        for dependent in self.graph.dependents(node).unwrap().to_vec() {
            self.queue_evaluation(dependent);
        }
        let Some(owner) = self.graph.nodes()[node.index()].owner() else {
            return;
        };
        if self.states[owner.index()] != NodeState::Active {
            return;
        }
        // 每个直接子节点终态只计数一次：归并为 O(V)，避免每次完成扫描兄弟
        // 导致宽子图 O(V²)。最后一次完成才扫描直接子节点的成功/失败状态。
        self.remaining_children[owner.index()] -= 1;
        if self.remaining_children[owner.index()] == 0 {
            self.complete_composite(owner);
        }
    }

    fn is_terminal(&self, node: NodeId) -> bool {
        self.states[node.index()].is_terminal()
    }

    fn is_explicitly_skipped(&self, node: NodeId) -> bool {
        let mut current = Some(node);
        while let Some(candidate) = current {
            if self.explicit_skips[candidate.index()] {
                return true;
            }
            current = self.graph.nodes()[candidate.index()].owner();
        }
        false
    }
}

#[cfg(test)]
mod tests {
    use std::sync::Arc;

    use super::{ConditionResolution, ExecutionSession, ExecutionUpdate, NodeState, SessionStatus};
    use crate::execution::{
        CompiledExecutionGraph, ConditionId, ExecutionNode, Prerequisite, RuntimeCondition,
    };
    use topology::NodeId;

    fn node(index: usize) -> NodeId {
        NodeId::new(index)
    }

    #[test]
    fn failed_shared_condition_blocks_all_waiters_but_not_an_independent_node() {
        let condition = ConditionId::new(0);
        let graph = Arc::new(
            CompiledExecutionGraph::new(
                [
                    ExecutionNode::leaf(None, [], Some(condition)),
                    ExecutionNode::leaf(None, [], Some(condition)),
                    ExecutionNode::leaf(None, [], None),
                ],
                [RuntimeCondition::new([])],
            )
            .unwrap(),
        );
        let mut session = ExecutionSession::new(graph, []).unwrap();
        let first = session.start(1).unwrap();
        assert_eq!(first.conditions().len(), 1);
        assert_eq!(first.actions(), [node(2)]);
        let last = session
            .advance(
                ExecutionUpdate::new(
                    [(node(2), Ok(()))],
                    [ConditionResolution::failed(condition)],
                    [],
                ),
                1,
            )
            .unwrap();
        assert_eq!(last.status(), SessionStatus::Failed);
        assert_eq!(session.state(node(0)), Some(NodeState::Blocked));
        assert_eq!(session.state(node(1)), Some(NodeState::Blocked));
        assert_eq!(session.state(node(2)), Some(NodeState::Finished));
    }

    #[test]
    fn failure_propagates_through_a_long_chain_without_dispatching_descendants() {
        let count: usize = 10_000;
        let graph = Arc::new(
            CompiledExecutionGraph::new(
                (0..count).map(|index| {
                    ExecutionNode::leaf(
                        None,
                        index
                            .checked_sub(1)
                            .map(|previous| Prerequisite::Required(node(previous))),
                        None,
                    )
                }),
                [],
            )
            .unwrap(),
        );
        let mut session = ExecutionSession::new(graph, []).unwrap();
        assert_eq!(session.start(8).unwrap().actions(), [node(0)]);
        let last = session
            .advance(ExecutionUpdate::new([(node(0), Err(()))], [], []), 8)
            .unwrap();
        assert_eq!(last.status(), SessionStatus::Failed);
        assert!(last.actions().is_empty());
        assert_eq!(session.state(node(0)), Some(NodeState::Failed));
        assert_eq!(session.state(node(count - 1)), Some(NodeState::Blocked));
    }

    #[test]
    fn session_dispatches_only_after_prerequisites_complete() {
        let graph = Arc::new(
            CompiledExecutionGraph::new(
                [
                    ExecutionNode::leaf(None, [], None),
                    ExecutionNode::leaf(None, [Prerequisite::Required(node(0))], None),
                ],
                [],
            )
            .unwrap(),
        );
        let mut session = ExecutionSession::new(graph, []).unwrap();
        let first = session.start(8).unwrap();
        assert_eq!(first.actions(), [node(0)]);
        let second = session
            .advance(ExecutionUpdate::new([(node(0), Ok(()))], [], []), 8)
            .unwrap();
        assert_eq!(second.actions(), [node(1)]);
        let last = session
            .advance(ExecutionUpdate::new([(node(1), Ok(()))], [], []), 8)
            .unwrap();
        assert_eq!(last.status(), SessionStatus::Finished);
    }

    #[test]
    fn session_requests_condition_after_its_reads_finish() {
        let graph = Arc::new(
            CompiledExecutionGraph::new(
                [
                    ExecutionNode::leaf(None, [], None),
                    ExecutionNode::leaf(None, [], Some(ConditionId::new(0))),
                ],
                [RuntimeCondition::new([node(0)])],
            )
            .unwrap(),
        );
        let mut session = ExecutionSession::new(graph, []).unwrap();
        let first = session.start(8).unwrap();
        assert_eq!(first.actions(), [node(0)]);
        let condition = session
            .advance(ExecutionUpdate::new([(node(0), Ok(()))], [], []), 8)
            .unwrap();
        assert_eq!(condition.conditions().len(), 1);
        let skipped = session
            .advance(
                ExecutionUpdate::new(
                    [],
                    [ConditionResolution::new(ConditionId::new(0), false)],
                    [],
                ),
                8,
            )
            .unwrap();
        assert_eq!(skipped.status(), SessionStatus::Finished);
    }

    #[test]
    fn session_returns_actions_and_condition_requests_in_one_batch() {
        let graph = Arc::new(
            CompiledExecutionGraph::new(
                [
                    ExecutionNode::leaf(None, [], None),
                    ExecutionNode::leaf(None, [], Some(ConditionId::new(0))),
                ],
                [RuntimeCondition::new([])],
            )
            .unwrap(),
        );
        let mut session = ExecutionSession::new(graph, []).unwrap();
        let batch = session.start(8).unwrap();
        assert_eq!(batch.actions(), [node(0)]);
        assert_eq!(batch.conditions().len(), 1);
    }

    #[test]
    fn skipped_composite_notifies_dependents_of_its_children() {
        let graph = Arc::new(
            CompiledExecutionGraph::new(
                [
                    ExecutionNode::composite(None, [], Some(ConditionId::new(0))),
                    ExecutionNode::leaf(Some(node(0)), [], None),
                    ExecutionNode::leaf(None, [Prerequisite::Required(node(1))], None),
                ],
                [RuntimeCondition::new([])],
            )
            .unwrap(),
        );
        let mut session = ExecutionSession::new(graph, []).unwrap();
        let condition = session.start(8).unwrap();
        assert_eq!(condition.conditions().len(), 1);
        let next = session
            .advance(
                ExecutionUpdate::new(
                    [],
                    [ConditionResolution::new(ConditionId::new(0), false)],
                    [],
                ),
                8,
            )
            .unwrap();
        assert_eq!(next.actions(), [node(2)]);
    }

    #[test]
    fn session_rejects_unknown_initial_skip() {
        let graph = Arc::new(CompiledExecutionGraph::new([], []).unwrap());
        assert!(matches!(
            ExecutionSession::new(graph, [node(0)]),
            Err(super::SessionError::UnknownNode(value)) if value == node(0)
        ));
    }
}
