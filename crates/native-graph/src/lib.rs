//! Rust-owned graph declarations whose leaf actions stay in Rust.

use std::error::Error;
use std::fmt::{self, Display, Formatter};

use action::NativeActionHandle;
use rivetoo_graph::{
    Arc, DeclarationNode, DeclarationPrerequisite, DeclaredGraph, DeclaredNodeKind,
    GraphCompilation, GraphDeclaration, GraphId, NodeActivation, NodeId,
};

/// One Rust-owned executable step in a [`NativeGraph`].
#[derive(Clone, Debug)]
pub struct NativeStep {
    label: String,
    action: NativeActionHandle,
}

impl NativeStep {
    /// Creates a native step with an explicit display label.
    ///
    /// # Errors
    /// Returns an error when the label is empty.
    pub fn new(
        label: impl Into<String>,
        action: NativeActionHandle,
    ) -> Result<Self, NativeGraphError> {
        let label = label.into();
        if label.is_empty() {
            return Err(NativeGraphError::EmptyStepLabel);
        }
        Ok(Self { label, action })
    }

    #[must_use]
    pub fn label(&self) -> &str {
        &self.label
    }

    #[must_use]
    pub const fn action(&self) -> &NativeActionHandle {
        &self.action
    }
}

/// A complete Rust declaration graph with Rust-owned leaf actions.
#[derive(Clone, Debug)]
pub struct NativeGraph {
    label: String,
    steps: Vec<NativeStep>,
    arcs: Vec<Arc>,
}

impl NativeGraph {
    /// Creates a native graph while preserving edges as graph-owned topology.
    ///
    /// # Errors
    /// Returns an error when a label is empty, an edge references an unknown
    /// step, or the declared topology contains a cycle.
    pub fn new(
        label: impl Into<String>,
        steps: impl IntoIterator<Item = NativeStep>,
        arcs: impl IntoIterator<Item = Arc>,
    ) -> Result<Self, NativeGraphError> {
        let label = label.into();
        if label.is_empty() {
            return Err(NativeGraphError::EmptyGraphLabel);
        }
        let graph = Self {
            label,
            steps: steps.into_iter().collect(),
            arcs: arcs.into_iter().collect(),
        };
        // 编译一次只用于验证完整声明。时间和额外空间均为 O(V + E)。
        graph.compile_structure()?;
        Ok(graph)
    }

    #[must_use]
    pub fn label(&self) -> &str {
        &self.label
    }

    #[must_use]
    pub fn steps(&self) -> &[NativeStep] {
        &self.steps
    }

    #[must_use]
    pub fn arcs(&self) -> &[Arc] {
        &self.arcs
    }

    /// Compiles the native declaration with the shared graph compiler.
    ///
    /// # Errors
    /// Returns the common graph declaration error when the topology is invalid.
    pub fn compile_structure(&self) -> Result<GraphCompilation, NativeGraphError> {
        let mut prerequisites = vec![Vec::new(); self.steps.len()];
        for arc in &self.arcs {
            let Some(target) = prerequisites.get_mut(arc.target.index()) else {
                return Err(NativeGraphError::UnknownStep(arc.target));
            };
            if arc.source.index() >= self.steps.len() {
                return Err(NativeGraphError::UnknownStep(arc.source));
            }
            target.push(DeclarationPrerequisite::Required(arc.source));
        }
        GraphDeclaration::new(
            [DeclaredGraph::Root],
            prerequisites.into_iter().map(|prerequisites| {
                DeclarationNode::new(
                    GraphId::new(0),
                    DeclaredNodeKind::Leaf,
                    NodeActivation::Active {
                        prerequisites,
                        condition: None,
                    },
                )
            }),
            [],
            [],
        )
        .compile()
        .map_err(NativeGraphError::Declaration)
    }
}

/// A native graph declaration failure detected before execution.
#[derive(Clone, Debug, Eq, PartialEq)]
pub enum NativeGraphError {
    EmptyGraphLabel,
    EmptyStepLabel,
    UnknownStep(NodeId),
    Declaration(rivetoo_graph::DeclarationError),
}

impl Display for NativeGraphError {
    fn fmt(&self, formatter: &mut Formatter<'_>) -> fmt::Result {
        match self {
            Self::EmptyGraphLabel => formatter.write_str("native graph label must not be empty"),
            Self::EmptyStepLabel => formatter.write_str("native step label must not be empty"),
            Self::UnknownStep(step) => {
                write!(
                    formatter,
                    "native graph edge references unknown step: {}",
                    step.index()
                )
            }
            Self::Declaration(error) => Display::fmt(error, formatter),
        }
    }
}

impl Error for NativeGraphError {
    fn source(&self) -> Option<&(dyn Error + 'static)> {
        match self {
            Self::Declaration(error) => Some(error),
            _ => None,
        }
    }
}

#[cfg(test)]
mod tests {
    use action::{NativeAction, NativeActionContext, NativeActionError, NativeActionHandle};
    use rivetoo_graph::{Arc, NodeId};

    use super::{NativeGraph, NativeStep};

    struct Noop;

    impl NativeAction for Noop {
        fn execute(&self, _context: &NativeActionContext) -> Result<(), NativeActionError> {
            Ok(())
        }
    }

    fn step(label: &str) -> NativeStep {
        NativeStep::new(label, NativeActionHandle::new(Noop)).unwrap()
    }

    #[test]
    fn native_graph_uses_the_common_compiled_execution_graph() {
        let graph = NativeGraph::new(
            "native",
            [step("prepare"), step("compile")],
            [Arc::new(NodeId::new(0), NodeId::new(1))],
        )
        .unwrap();

        let compilation = graph.compile_structure().unwrap();
        assert_eq!(
            compilation
                .graph()
                .dependency_topology()
                .topological_order(),
            &[NodeId::new(0), NodeId::new(1)]
        );
    }
}
