//! Pure graph models and algorithms used by every language adapter.

pub mod capability;
pub mod declaration;
pub mod execution;
pub mod lowering;
pub mod session;
pub mod topology;

pub use capability::{
    CapabilityId, CapabilityRequirement, CapabilityResolutionError, ComponentId, ProviderSelection,
    RequirementId, ResolvedCapabilityBinding, resolve_capabilities,
};
pub use declaration::{
    DeclarationError, DeclarationNode, DeclarationPrerequisite, GraphCompilation, GraphDeclaration,
    NodeActivation,
};
pub use execution::{
    CapabilityBinding, CompiledExecutionGraph, ConditionId, ExecutionGraphError, ExecutionNode,
    ExecutionNodeKind, Prerequisite, RuntimeCondition,
};
pub use lowering::{
    DeclaredGraph, DeclaredNode, DeclaredNodeKind, ExecutionGraphDeclaration, GraphId,
    LoweringError,
};
pub use session::{
    ConditionRequest, ConditionResolution, ExecutionBatch, ExecutionEvent, ExecutionSession,
    ExecutionUpdate, NodeState, SessionError, SessionStatus,
};
pub use topology::{Arc, DagTopology, DirectedTopology, NodeId, TopologyError};
