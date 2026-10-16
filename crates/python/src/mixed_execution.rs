use std::sync::Mutex;

use action::{NativeActionContext, NativeActionExecutor, NativeActionHandle};
use pyo3::exceptions::{PyRuntimeError, PyValueError};
use pyo3::prelude::*;
use rivetoo_graph::{
    ConditionId, ConditionResolution, ExecutionBatch, ExecutionSession, ExecutionUpdate, NodeId,
    SessionStatus,
};

use crate::NativeExecutionBatch;

const NATIVE_ACTION_STARTED: u8 = 5;
const NATIVE_ACTION_COMPLETED: u8 = 6;
type EncodedEvent = (u8, usize, Option<bool>);
type ActionOutcome = (NodeId, Result<(), ()>);
type NativeFailure = (usize, String);

#[derive(Clone)]
pub(crate) struct NativeActionBinding {
    action: NativeActionHandle,
    context: NativeActionContext,
}

/// Rust action ownership aligned with compiled execution node indexes.
#[pyclass(frozen, module = "rivetoo_graph._native", skip_from_py_object)]
#[derive(Clone)]
pub(crate) struct NativeActionBindings {
    actions: Vec<Option<NativeActionHandle>>,
}

impl NativeActionBindings {
    pub(crate) fn new(actions: Vec<Option<NativeActionHandle>>) -> Self {
        Self { actions }
    }
}

#[pymethods]
impl NativeActionBindings {
    #[new]
    fn empty(node_count: usize) -> Self {
        Self::new(vec![None; node_count])
    }
}

struct MixedExecutionState {
    session: ExecutionSession,
    native_running: usize,
}

/// Rust-owned session that dispatches Rust actions without a Python callback.
#[pyclass(frozen, module = "rivetoo_graph._native")]
pub(crate) struct NativeMixedExecutionSession {
    state: Mutex<MixedExecutionState>,
    executor: NativeActionExecutor,
    native_actions: Vec<Option<NativeActionBinding>>,
}

#[pyclass(frozen, module = "rivetoo_graph._native")]
pub(crate) struct NativeMixedExecutionBatch {
    actions: Vec<usize>,
    conditions: Vec<(usize, usize)>,
    events: Vec<(u8, usize, Option<bool>)>,
    finished: bool,
    native_running: usize,
    failures: Vec<NativeFailure>,
}

#[pymethods]
impl NativeMixedExecutionBatch {
    #[allow(clippy::type_complexity)]
    fn parts(
        &self,
    ) -> (
        Vec<usize>,
        Vec<(usize, usize)>,
        Vec<(u8, usize, Option<bool>)>,
        bool,
        usize,
        Vec<NativeFailure>,
    ) {
        (
            self.actions.clone(),
            self.conditions.clone(),
            self.events.clone(),
            self.finished,
            self.native_running,
            self.failures.clone(),
        )
    }
}

impl NativeMixedExecutionSession {
    pub(crate) fn new(
        session: ExecutionSession,
        executor: NativeActionExecutor,
        native_actions: Vec<Option<NativeActionBinding>>,
    ) -> Self {
        Self {
            state: Mutex::new(MixedExecutionState {
                session,
                native_running: 0,
            }),
            executor,
            native_actions,
        }
    }

    pub(crate) fn bindings(
        node_count: usize,
        declarations: &NativeActionBindings,
        contexts: Vec<(usize, Option<String>)>,
    ) -> PyResult<Vec<Option<NativeActionBinding>>> {
        if declarations.actions.len() != node_count {
            return Err(PyValueError::new_err(
                "native action table does not match execution graph",
            ));
        }
        let mut bindings = vec![None; node_count];
        for (node, component_log_path) in contexts {
            let Some(slot) = bindings.get_mut(node) else {
                return Err(PyValueError::new_err(format!(
                    "native action references unknown execution node: {node}"
                )));
            };
            if slot.is_some() {
                return Err(PyValueError::new_err(format!(
                    "execution node has more than one native action: {node}"
                )));
            }
            let Some(action) = declarations.actions[node].as_ref() else {
                return Err(PyValueError::new_err(format!(
                    "native action context references Python execution node: {node}"
                )));
            };
            *slot = Some(NativeActionBinding {
                action: action.clone(),
                context: NativeActionContext::new(component_log_path.map(Into::into)),
            });
        }
        Ok(bindings)
    }

    fn dispatch(
        &self,
        state: &mut MixedExecutionState,
        batch: &ExecutionBatch,
    ) -> PyResult<NativeMixedExecutionBatch> {
        let mut python_actions = Vec::new();
        let mut events = batch
            .events()
            .iter()
            .map(super::encode_event)
            .collect::<Vec<_>>();
        for node in batch.actions() {
            match &self.native_actions[node.index()] {
                Some(binding) => {
                    self.executor
                        .submit(
                            node.index(),
                            binding.action.clone(),
                            binding.context.clone(),
                        )
                        .map_err(|error| PyRuntimeError::new_err(error.to_string()))?;
                    state.native_running += 1;
                    events.push((NATIVE_ACTION_STARTED, node.index(), None));
                }
                None => python_actions.push(node.index()),
            }
        }
        Ok(NativeMixedExecutionBatch {
            actions: python_actions,
            conditions: batch
                .conditions()
                .iter()
                .map(|request| (request.node().index(), request.condition().index()))
                .collect(),
            events,
            finished: batch.status() != SessionStatus::Running,
            native_running: state.native_running,
            failures: Vec::new(),
        })
    }

    fn drain_native_completions(
        &self,
        state: &mut MixedExecutionState,
    ) -> (Vec<ActionOutcome>, Vec<EncodedEvent>, Vec<NativeFailure>) {
        let completions = self.executor.drain_completions();
        let mut completed = Vec::with_capacity(completions.len());
        let mut events = Vec::with_capacity(completions.len());
        let mut failures = Vec::new();
        for completion in completions {
            state.native_running = state
                .native_running
                .checked_sub(1)
                .expect("native completion count exceeds submitted action count");
            let node = completion.node();
            match completion.into_result() {
                Ok(()) => {
                    completed.push((NodeId::new(node), Ok(())));
                    events.push((NATIVE_ACTION_COMPLETED, node, None));
                }
                Err(error) => {
                    completed.push((NodeId::new(node), Err(())));
                    failures.push((node, error.to_string()));
                }
            }
        }
        (completed, events, failures)
    }
}

#[pymethods]
impl NativeMixedExecutionSession {
    fn start(&self, capacity: usize) -> PyResult<NativeMixedExecutionBatch> {
        let mut state = self
            .state
            .lock()
            .unwrap_or_else(std::sync::PoisonError::into_inner);
        let batch = state
            .session
            .start(capacity)
            .map_err(|error| PyValueError::new_err(error.to_string()))?;
        self.dispatch(&mut state, &batch)
    }

    #[pyo3(signature = (completed, conditions, skipped, available))]
    fn advance(
        &self,
        completed: Vec<(usize, bool)>,
        conditions: Vec<(usize, Option<bool>)>,
        skipped: Vec<usize>,
        available: usize,
    ) -> PyResult<NativeMixedExecutionBatch> {
        let mut state = self
            .state
            .lock()
            .unwrap_or_else(std::sync::PoisonError::into_inner);
        let (native_completed, mut native_events, failures) =
            self.drain_native_completions(&mut state);
        let newly_available = native_completed.len();
        let batch = state
            .session
            .advance(
                ExecutionUpdate::new(
                    completed
                        .into_iter()
                        .map(|(node, success)| {
                            (NodeId::new(node), if success { Ok(()) } else { Err(()) })
                        })
                        .chain(native_completed),
                    conditions
                        .into_iter()
                        .map(|(condition, value)| match value {
                            Some(value) => {
                                ConditionResolution::new(ConditionId::new(condition), value)
                            }
                            None => ConditionResolution::failed(ConditionId::new(condition)),
                        }),
                    skipped.into_iter().map(NodeId::new),
                ),
                available + newly_available,
            )
            .map_err(|error| PyValueError::new_err(error.to_string()))?;
        let mut result = self.dispatch(&mut state, &batch)?;
        native_events.append(&mut result.events);
        result.events = native_events;
        result.failures = failures;
        Ok(result)
    }

    /// Waits for outstanding Rust work without losing a concurrently drained notification.
    fn wait_native(&self, py: Python<'_>) -> PyResult<()> {
        let wait = {
            let state = self
                .state
                .lock()
                .unwrap_or_else(std::sync::PoisonError::into_inner);
            if state.native_running == 0 {
                return Ok(());
            }
            self.executor.prepare_wait()
        };
        py.detach(move || wait.wait());
        Ok(())
    }
}

impl From<NativeExecutionBatch> for NativeMixedExecutionBatch {
    fn from(batch: NativeExecutionBatch) -> Self {
        Self {
            actions: batch.actions,
            conditions: batch.conditions,
            events: batch.events,
            finished: batch.finished,
            native_running: 0,
            failures: Vec::new(),
        }
    }
}
