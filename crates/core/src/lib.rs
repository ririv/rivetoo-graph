//! Pure graph models and algorithms used by every language adapter.

pub mod capability;
pub mod execution;
pub mod lowering;
pub mod scheduler;
pub mod topology;

pub use capability::{
    CapabilityId, CapabilityRequirement, CapabilityResolutionError, ComponentId, ProviderSelection,
    RequirementId, ResolvedCapabilityBinding, resolve_capabilities,
};
pub use execution::{
    CapabilityBinding, CompiledExecutionGraph, ConditionId, ExecutionGraphError, ExecutionNode,
    ExecutionNodeKind, Prerequisite, RuntimeCondition,
};
pub use lowering::{
    DeclaredGraph, DeclaredNode, DeclaredNodeKind, ExecutionGraphDeclaration, GraphId,
    LoweringError,
};
pub use scheduler::{CompiledScheduler, NodeState, SchedulerError, SchedulerHost};
pub use topology::{Arc, DagTopology, DirectedTopology, NodeId, TopologyError};
