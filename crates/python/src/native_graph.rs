use std::collections::{BTreeMap, HashMap};
use std::path::PathBuf;

use action::{NativeActionHandle, ProcessAction};
use native_graph::{NativeGraph as CoreNativeGraph, NativeStep};
use pyo3::exceptions::PyValueError;
use pyo3::prelude::*;
use rivetoo_graph::{Arc as GraphArc, NodeId};

type ProcessDeclaration = (String, String, Vec<String>, String, HashMap<String, String>);

/// Python-visible opaque ownership of one complete Rust declaration graph.
#[pyclass(frozen, module = "rivetoo_graph._native", skip_from_py_object)]
#[derive(Clone)]
pub(crate) struct NativeGraph {
    graph: CoreNativeGraph,
}

impl NativeGraph {
    pub(crate) const fn graph(&self) -> &CoreNativeGraph {
        &self.graph
    }
}

#[pymethods]
impl NativeGraph {
    #[getter]
    fn label(&self) -> &str {
        self.graph.label()
    }
}

/// Constructs one graph from a complete process declaration in a single FFI call.
///
/// This is an adapter boundary for Rust importers and is intentionally not
/// re-exported by the public Python package.
#[pyfunction(name = "_native_process_graph")]
pub(crate) fn native_process_graph(
    label: String,
    processes: Vec<ProcessDeclaration>,
    arcs: Vec<(usize, usize)>,
) -> PyResult<NativeGraph> {
    let steps = processes
        .into_iter()
        .map(|(label, program, arguments, cwd, environment)| {
            NativeStep::new(
                label,
                NativeActionHandle::new(ProcessAction::new(
                    program,
                    arguments,
                    PathBuf::from(cwd),
                    environment.into_iter().collect::<BTreeMap<_, _>>(),
                )),
            )
        })
        .collect::<Result<Vec<_>, _>>()
        .map_err(|error| PyValueError::new_err(error.to_string()))?;
    let arcs = arcs
        .into_iter()
        .map(|(source, target)| GraphArc::new(NodeId::new(source), NodeId::new(target)));
    CoreNativeGraph::new(label, steps, arcs)
        .map(|graph| NativeGraph { graph })
        .map_err(|error| PyValueError::new_err(error.to_string()))
}
