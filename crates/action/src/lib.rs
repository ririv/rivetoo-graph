//! Rust-owned graph actions and their execution contracts.

mod executor;
mod process;

use std::error::Error;
use std::fmt::{self, Debug, Display, Formatter};
use std::path::{Path, PathBuf};
use std::sync::Arc;

pub use process::ProcessAction;

/// Immutable inputs available to a native action during one execution.
#[derive(Clone, Debug, Default, Eq, PartialEq)]
pub struct NativeActionContext {
    component_log_path: Option<PathBuf>,
}

impl NativeActionContext {
    #[must_use]
    pub fn new(component_log_path: Option<PathBuf>) -> Self {
        Self { component_log_path }
    }

    #[must_use]
    pub fn component_log_path(&self) -> Option<&Path> {
        self.component_log_path.as_deref()
    }
}

/// A Rust-owned leaf action whose implementation never calls back into Python.
pub trait NativeAction: Send + Sync + 'static {
    /// Executes this action once.
    ///
    /// # Errors
    /// Returns an action-owned error when execution fails.
    fn execute(&self, context: &NativeActionContext) -> Result<(), NativeActionError>;
}

/// Type-erased, cloneable ownership of one native action implementation.
#[derive(Clone)]
pub struct NativeActionHandle(Arc<dyn NativeAction>);

impl NativeActionHandle {
    #[must_use]
    pub fn new(action: impl NativeAction) -> Self {
        Self(Arc::new(action))
    }

    /// Executes the referenced action.
    ///
    /// # Errors
    /// Returns the concrete action failure without changing its message.
    pub fn execute(&self, context: &NativeActionContext) -> Result<(), NativeActionError> {
        self.0.execute(context)
    }
}

impl Debug for NativeActionHandle {
    fn fmt(&self, formatter: &mut Formatter<'_>) -> fmt::Result {
        formatter.write_str("NativeActionHandle(..)")
    }
}

/// Stable error value crossing native action and language-adapter boundaries.
#[derive(Clone, Debug, Eq, PartialEq)]
pub struct NativeActionError {
    message: String,
}

impl NativeActionError {
    #[must_use]
    pub fn new(message: impl Into<String>) -> Self {
        Self {
            message: message.into(),
        }
    }
}

impl Display for NativeActionError {
    fn fmt(&self, formatter: &mut Formatter<'_>) -> fmt::Result {
        formatter.write_str(&self.message)
    }
}

impl Error for NativeActionError {}
pub use executor::{NativeActionCompletion, NativeActionExecutor};
