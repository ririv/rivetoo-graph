use std::sync::Arc;

use pyo3::prelude::*;
use rivetoo_graph::{ConditionRequest, ConditionResolution, ExecutionEvent, NodeId};
use tokio_runtime::CallbackHost;

use crate::{NativeCompiledExecutionGraph, encode_event};

struct PythonCallbackHost {
    host: Py<PyAny>,
}

impl PythonCallbackHost {
    fn call_unit(&self, name: &str) -> PyResult<()> {
        Python::attach(|py| {
            self.host.bind(py).call_method0(name)?;
            Ok(())
        })
    }
}

impl CallbackHost for PythonCallbackHost {
    type Error = PyErr;

    fn initially_skipped(&self) -> PyResult<Vec<NodeId>> {
        Python::attach(|py| {
            self.host
                .bind(py)
                .call_method0("skipped_nodes")?
                .extract::<Vec<usize>>()
                .map(|nodes| nodes.into_iter().map(NodeId::new).collect())
        })
    }

    fn resolve_conditions(
        &self,
        requests: &[ConditionRequest],
    ) -> PyResult<Vec<ConditionResolution>> {
        let encoded = requests
            .iter()
            .map(|request| (request.node().index(), request.condition().index()))
            .collect::<Vec<_>>();
        Python::attach(|py| {
            self.host
                .bind(py)
                .call_method1("resolve_conditions", (encoded,))?
                .extract::<Vec<(usize, Option<bool>)>>()
                .map(|resolutions| {
                    resolutions
                        .into_iter()
                        .map(|(condition, value)| {
                            let condition = rivetoo_graph::ConditionId::new(condition);
                            match value {
                                Some(value) => ConditionResolution::new(condition, value),
                                None => ConditionResolution::failed(condition),
                            }
                        })
                        .collect()
                })
        })
    }

    fn apply_events(&self, events: &[ExecutionEvent]) -> PyResult<()> {
        let encoded = events.iter().map(encode_event).collect::<Vec<_>>();
        Python::attach(|py| {
            self.host
                .bind(py)
                .call_method1("apply_events", (encoded,))?;
            Ok(())
        })
    }

    fn run_action(&self, node: NodeId) -> PyResult<()> {
        Python::attach(|py| {
            self.host
                .bind(py)
                .call_method1("run_leaf", (node.index(),))?;
            Ok(())
        })
    }

    fn stop_requested(&self) -> PyResult<bool> {
        Python::attach(|py| self.host.bind(py).call_method0("stop_requested")?.extract())
    }

    fn request_stop(&self) -> PyResult<()> {
        self.call_unit("request_stop")
    }

    fn require_running(&self) -> PyResult<()> {
        self.call_unit("require_running")?;
        self.call_unit("raise_failures")
    }
}

/// Runs the private Tokio/PyO3 callback experiment.
#[pyfunction(name = "_experimental_run_tokio_callbacks")]
pub(crate) fn run_tokio_callbacks(
    py: Python<'_>,
    graph: &NativeCompiledExecutionGraph,
    host: Py<PyAny>,
    concurrency: usize,
) -> PyResult<()> {
    let graph = Arc::clone(&graph.graph);
    let host = Arc::new(PythonCallbackHost { host });
    py.detach(move || {
        tokio_runtime::run(graph, host, concurrency)
            .map_err(|error| pyo3::exceptions::PyRuntimeError::new_err(error.to_string()))
    })
}
