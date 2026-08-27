//! Python adapter for the Rust graph core.

use pyo3::exceptions::{PyRuntimeError, PyValueError};
use pyo3::prelude::*;
use rivetoo_graph::{
    CompiledExecutionGraph as CoreCompiledExecutionGraph, CompiledScheduler, ConditionId,
    ExecutionNode, NodeId, Prerequisite, RuntimeCondition, SchedulerError, SchedulerHost,
};
use std::sync::{Arc, Mutex, mpsc};
use std::thread;

/// Node behavior accepted by the private Python adapter.
#[pyclass(eq, eq_int, from_py_object, module = "rivetoo_graph._native")]
#[derive(Clone, Copy, Debug, Eq, PartialEq)]
enum NativeNodeKind {
    Leaf,
    Composite,
}

/// Rust-owned structural execution graph.
#[pyclass(frozen, module = "rivetoo_graph._native")]
struct NativeCompiledExecutionGraph {
    graph: Arc<CoreCompiledExecutionGraph>,
}

#[pymethods]
impl NativeCompiledExecutionGraph {
    #[new]
    fn new(
        kinds: Vec<NativeNodeKind>,
        owners: Vec<Option<usize>>,
        prerequisites: Vec<Vec<(usize, Option<usize>)>>,
        when_conditions: Vec<Option<usize>>,
        condition_reads: Vec<Vec<usize>>,
    ) -> PyResult<Self> {
        let node_count = kinds.len();
        if owners.len() != node_count
            || prerequisites.len() != node_count
            || when_conditions.len() != node_count
        {
            return Err(PyValueError::new_err(
                "compiled execution graph node columns have different lengths",
            ));
        }

        let nodes = kinds
            .into_iter()
            .zip(owners)
            .zip(prerequisites)
            .zip(when_conditions)
            .map(|(((kind, owner), prerequisites), when)| {
                let owner = owner.map(NodeId::new);
                let prerequisites = prerequisites.into_iter().map(|(node, condition)| {
                    condition.map_or_else(
                        || Prerequisite::Required(NodeId::new(node)),
                        |condition| Prerequisite::Conditional {
                            node: NodeId::new(node),
                            condition: ConditionId::new(condition),
                        },
                    )
                });
                let when = when.map(ConditionId::new);
                match kind {
                    NativeNodeKind::Leaf => ExecutionNode::leaf(owner, prerequisites, when),
                    NativeNodeKind::Composite => {
                        ExecutionNode::composite(owner, prerequisites, when)
                    }
                }
            });
        let conditions = condition_reads
            .into_iter()
            .map(|reads| RuntimeCondition::new(reads.into_iter().map(NodeId::new)));
        let graph = CoreCompiledExecutionGraph::new(nodes, conditions)
            .map_err(|error| PyValueError::new_err(error.to_string()))?;
        Ok(Self {
            graph: Arc::new(graph),
        })
    }

    fn roots(&self) -> Vec<usize> {
        node_indexes(self.graph.roots())
    }

    fn children(&self, node: usize) -> PyResult<Vec<usize>> {
        self.graph
            .children(NodeId::new(node))
            .map(node_indexes)
            .ok_or_else(|| PyValueError::new_err(format!("unknown execution node: {node}")))
    }

    fn dependents(&self, node: usize) -> PyResult<Vec<usize>> {
        self.graph
            .dependents(NodeId::new(node))
            .map(node_indexes)
            .ok_or_else(|| PyValueError::new_err(format!("unknown execution node: {node}")))
    }

    fn topological_order(&self) -> Vec<usize> {
        node_indexes(self.graph.dependency_topology().topological_order())
    }

    fn run_serial(&self, host: &Bound<'_, PyAny>) -> PyResult<()> {
        let mut scheduler = CompiledScheduler::new(Arc::clone(&self.graph));
        let mut scheduler_host = PythonSchedulerHost { host };
        scheduler
            .start(&mut scheduler_host)
            .map_err(scheduler_error)?;
        while !scheduler.is_finished() {
            let Some(node) = scheduler
                .take_ready_leaf(&mut scheduler_host)
                .map_err(scheduler_error)?
            else {
                scheduler
                    .require_finished::<PyErr>()
                    .map_err(scheduler_error)?;
                break;
            };
            scheduler_host.run_leaf(node)?;
            scheduler
                .complete_leaf(node, &mut scheduler_host)
                .map_err(scheduler_error)?;
        }
        scheduler
            .require_finished::<PyErr>()
            .map_err(scheduler_error)
    }

    fn run_threaded(&self, py: Python<'_>, host: Py<PyAny>, workers: usize) -> PyResult<()> {
        if workers == 0 {
            return Err(PyValueError::new_err("graph workers must be >= 1"));
        }
        let graph = Arc::clone(&self.graph);
        py.detach(move || run_threaded(graph, &host, workers))
    }
}

fn node_indexes(nodes: &[NodeId]) -> Vec<usize> {
    nodes.iter().map(|node| node.index()).collect()
}

struct PythonSchedulerHost<'py> {
    host: &'py Bound<'py, PyAny>,
}

impl PythonSchedulerHost<'_> {
    fn run_leaf(&self, node: NodeId) -> PyResult<()> {
        self.host.call_method1("run_leaf", (node.index(),))?;
        Ok(())
    }
}

impl SchedulerHost for PythonSchedulerHost<'_> {
    type Error = PyErr;

    fn is_skipped(&mut self, node: NodeId) -> Result<bool, Self::Error> {
        self.host
            .call_method1("is_skipped", (node.index(),))?
            .extract()
    }

    fn evaluate_condition(
        &mut self,
        node: NodeId,
        condition: ConditionId,
    ) -> Result<bool, Self::Error> {
        self.host
            .call_method1("evaluate_condition", (node.index(), condition.index()))?
            .extract()
    }

    fn record_condition(&mut self, node: NodeId, value: bool) -> Result<(), Self::Error> {
        self.host
            .call_method1("record_condition", (node.index(), value))?;
        Ok(())
    }

    fn record_condition_skip(&mut self, node: NodeId) -> Result<(), Self::Error> {
        self.host
            .call_method1("record_condition_skip", (node.index(),))?;
        Ok(())
    }

    fn enter_composite(&mut self, node: NodeId) -> Result<(), Self::Error> {
        self.host.call_method1("enter_composite", (node.index(),))?;
        Ok(())
    }

    fn complete_composite(&mut self, node: NodeId) -> Result<(), Self::Error> {
        self.host
            .call_method1("complete_composite", (node.index(),))?;
        Ok(())
    }
}

fn scheduler_error(error: SchedulerError<PyErr>) -> PyErr {
    match error {
        SchedulerError::Host(error) => error,
        other => PyValueError::new_err(other.to_string()),
    }
}

fn run_threaded(
    graph: Arc<CoreCompiledExecutionGraph>,
    host: &Py<PyAny>,
    workers: usize,
) -> PyResult<()> {
    let mut scheduler = CompiledScheduler::new(graph);
    let mut scheduler_host = OwnedPythonSchedulerHost { host };
    scheduler
        .start(&mut scheduler_host)
        .map_err(scheduler_error)?;

    thread::scope(|scope| -> PyResult<()> {
        let (work_sender, work_receiver) = mpsc::channel::<NodeId>();
        let work_receiver = Arc::new(Mutex::new(work_receiver));
        let (result_sender, result_receiver) = mpsc::channel::<(NodeId, PyResult<()>)>();

        for _ in 0..workers {
            let work_receiver = Arc::clone(&work_receiver);
            let result_sender = result_sender.clone();
            scope.spawn(move || {
                loop {
                    let Ok(node) = work_receiver
                        .lock()
                        .expect("graph work receiver lock was poisoned")
                        .recv()
                    else {
                        break;
                    };
                    let result = Python::attach(|py| {
                        host.bind(py).call_method1("run_leaf", (node.index(),))?;
                        Ok(())
                    });
                    if result_sender.send((node, result)).is_err() {
                        break;
                    }
                }
            });
        }
        drop(result_sender);

        let mut inflight = 0_usize;
        let mut failure: Option<PyErr> = None;
        loop {
            while failure.is_none() && !scheduler_host.stop_requested()? && inflight < workers {
                let Some(node) = scheduler
                    .take_ready_leaf(&mut scheduler_host)
                    .map_err(scheduler_error)?
                else {
                    break;
                };
                work_sender
                    .send(node)
                    .map_err(|_| PyRuntimeError::new_err("graph worker channel closed"))?;
                inflight += 1;
            }

            if inflight == 0 {
                if let Some(error) = failure {
                    return Err(error);
                }
                scheduler_host.require_running()?;
                scheduler
                    .require_finished::<PyErr>()
                    .map_err(scheduler_error)?;
                break;
            }

            let (node, result) = result_receiver
                .recv()
                .map_err(|_| PyRuntimeError::new_err("all graph workers stopped"))?;
            inflight -= 1;
            match result {
                Ok(()) if failure.is_none() => scheduler
                    .complete_leaf(node, &mut scheduler_host)
                    .map_err(scheduler_error)?,
                Ok(()) => {}
                Err(error) => {
                    if failure.is_none() {
                        scheduler_host.request_stop()?;
                        failure = Some(error);
                    }
                }
            }
        }

        scheduler_host.require_running()?;
        Ok(())
    })
}

struct OwnedPythonSchedulerHost<'py> {
    host: &'py Py<PyAny>,
}

impl OwnedPythonSchedulerHost<'_> {
    fn stop_requested(&self) -> PyResult<bool> {
        Python::attach(|py| self.host.bind(py).call_method0("stop_requested")?.extract())
    }

    fn request_stop(&self) -> PyResult<()> {
        Python::attach(|py| {
            self.host.bind(py).call_method0("request_stop")?;
            Ok(())
        })
    }

    fn require_running(&self) -> PyResult<()> {
        Python::attach(|py| {
            self.host.bind(py).call_method0("require_running")?;
            Ok(())
        })
    }
}

impl SchedulerHost for OwnedPythonSchedulerHost<'_> {
    type Error = PyErr;

    fn is_skipped(&mut self, node: NodeId) -> Result<bool, Self::Error> {
        Python::attach(|py| {
            self.host
                .bind(py)
                .call_method1("is_skipped", (node.index(),))?
                .extract()
        })
    }

    fn evaluate_condition(
        &mut self,
        node: NodeId,
        condition: ConditionId,
    ) -> Result<bool, Self::Error> {
        Python::attach(|py| {
            self.host
                .bind(py)
                .call_method1("evaluate_condition", (node.index(), condition.index()))?
                .extract()
        })
    }

    fn record_condition(&mut self, node: NodeId, value: bool) -> Result<(), Self::Error> {
        Python::attach(|py| {
            self.host
                .bind(py)
                .call_method1("record_condition", (node.index(), value))?;
            Ok(())
        })
    }

    fn record_condition_skip(&mut self, node: NodeId) -> Result<(), Self::Error> {
        Python::attach(|py| {
            self.host
                .bind(py)
                .call_method1("record_condition_skip", (node.index(),))?;
            Ok(())
        })
    }

    fn enter_composite(&mut self, node: NodeId) -> Result<(), Self::Error> {
        Python::attach(|py| {
            self.host
                .bind(py)
                .call_method1("enter_composite", (node.index(),))?;
            Ok(())
        })
    }

    fn complete_composite(&mut self, node: NodeId) -> Result<(), Self::Error> {
        Python::attach(|py| {
            self.host
                .bind(py)
                .call_method1("complete_composite", (node.index(),))?;
            Ok(())
        })
    }
}

/// Native implementation details used by the Python package.
#[pymodule]
#[allow(clippy::unnecessary_wraps)]
fn _native(module: &Bound<'_, PyModule>) -> PyResult<()> {
    module.add_class::<NativeNodeKind>()?;
    module.add_class::<NativeCompiledExecutionGraph>()?;
    Ok(())
}
