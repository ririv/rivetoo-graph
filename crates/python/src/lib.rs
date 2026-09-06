//! Python adapter for the Rust graph core.

mod declaration;
mod experiment;
mod mixed_execution;
mod native_graph;

use action::NativeActionExecutor;
use pyo3::exceptions::PyValueError;
use pyo3::prelude::*;
use rivetoo_graph::{
    Arc as TopologyArc, CapabilityBinding, CapabilityId, CapabilityRequirement,
    CompiledExecutionGraph as CoreCompiledExecutionGraph, ComponentId, ConditionId,
    ConditionResolution, DagTopology, DeclaredGraph, DeclaredNode, DirectedTopology,
    ExecutionBatch, ExecutionEvent, ExecutionGraphDeclaration, ExecutionSession, ExecutionUpdate,
    GraphId, NodeId, Prerequisite, ProviderSelection, RequirementId, RuntimeCondition,
    SessionStatus, TopologyError, resolve_capabilities,
};
use std::collections::HashMap;
use std::sync::Arc;

/// Rust-owned structural execution graph.
#[derive(Clone)]
#[pyclass(frozen, module = "rivetoo_graph._native", skip_from_py_object)]
struct NativeCompiledExecutionGraph {
    pub(crate) graph: Arc<CoreCompiledExecutionGraph>,
}

#[pyclass(frozen, module = "rivetoo_graph._native")]
pub(crate) struct NativeExecutionBatch {
    pub(crate) actions: Vec<usize>,
    pub(crate) conditions: Vec<(usize, usize)>,
    pub(crate) events: Vec<(u8, usize, Option<bool>)>,
    pub(crate) finished: bool,
}

impl From<ExecutionBatch> for NativeExecutionBatch {
    fn from(batch: ExecutionBatch) -> Self {
        Self {
            actions: batch.actions().iter().map(|node| node.index()).collect(),
            conditions: batch
                .conditions()
                .iter()
                .map(|request| (request.node().index(), request.condition().index()))
                .collect(),
            events: batch.events().iter().map(encode_event).collect(),
            finished: batch.status() == SessionStatus::Finished,
        }
    }
}

#[pymethods]
impl NativeExecutionBatch {
    #[allow(clippy::type_complexity)]
    fn parts(
        &self,
    ) -> (
        Vec<usize>,
        Vec<(usize, usize)>,
        Vec<(u8, usize, Option<bool>)>,
        bool,
    ) {
        (
            self.actions.clone(),
            self.conditions.clone(),
            self.events.clone(),
            self.finished,
        )
    }
}

#[pyclass(module = "rivetoo_graph._native")]
struct NativeExecutionSession {
    session: ExecutionSession,
}

#[pymethods]
impl NativeExecutionSession {
    fn start(&mut self, max_actions: usize) -> PyResult<NativeExecutionBatch> {
        self.session
            .start(max_actions)
            .map(NativeExecutionBatch::from)
            .map_err(|error| PyValueError::new_err(error.to_string()))
    }

    #[pyo3(signature = (completed, conditions, skipped, max_actions))]
    fn advance(
        &mut self,
        completed: Vec<usize>,
        conditions: Vec<(usize, bool)>,
        skipped: Vec<usize>,
        max_actions: usize,
    ) -> PyResult<NativeExecutionBatch> {
        self.session
            .advance(
                ExecutionUpdate::new(
                    completed.into_iter().map(NodeId::new),
                    conditions.into_iter().map(|(condition, value)| {
                        ConditionResolution::new(ConditionId::new(condition), value)
                    }),
                    skipped.into_iter().map(NodeId::new),
                ),
                max_actions,
            )
            .map(NativeExecutionBatch::from)
            .map_err(|error| PyValueError::new_err(error.to_string()))
    }
}

pub(crate) fn encode_event(event: &ExecutionEvent) -> (u8, usize, Option<bool>) {
    match *event {
        ExecutionEvent::ConditionEvaluated { node, value } => (0, node.index(), Some(value)),
        ExecutionEvent::ConditionSkipped(node) => (1, node.index(), None),
        ExecutionEvent::CompositeEntered(node) => (2, node.index(), None),
        ExecutionEvent::CompositeCompleted(node) => (3, node.index(), None),
        ExecutionEvent::NodeSkipped(node) => (4, node.index(), None),
    }
}

/// Validates one complete DAG projection and returns its cycle when present.
#[pyfunction]
fn dag_cycle(node_count: usize, arcs: Vec<(usize, usize)>) -> PyResult<Option<Vec<usize>>> {
    let arcs = arcs
        .into_iter()
        .map(|(source, target)| TopologyArc::new(NodeId::new(source), NodeId::new(target)));
    let topology = DirectedTopology::new(node_count, arcs)
        .map_err(|error| PyValueError::new_err(error.to_string()))?;
    match DagTopology::new(topology) {
        Ok(_) => Ok(None),
        Err(TopologyError::Cycle(cycle)) => Ok(Some(node_indexes(&cycle))),
        Err(error) => Err(PyValueError::new_err(error.to_string())),
    }
}

/// Resolves one complete execution-graph capability declaration in Rust.
#[pyfunction]
fn resolve_capability_bindings(
    component_outputs: Vec<Vec<usize>>,
    requirement_count: usize,
    any_requirements: Vec<(usize, usize, usize)>,
    exact_requirements: Vec<(usize, usize, usize, usize)>,
    selections: Vec<(usize, usize)>,
) -> PyResult<Vec<(usize, usize, usize)>> {
    let mut requirements = vec![None; requirement_count];
    for (requirement, consumer, capability) in any_requirements {
        set_requirement(
            &mut requirements,
            requirement,
            CapabilityRequirement::Any {
                consumer: ComponentId::new(consumer),
                capability: CapabilityId::new(capability),
            },
        )?;
    }
    for (requirement, consumer, capability, provider) in exact_requirements {
        set_requirement(
            &mut requirements,
            requirement,
            CapabilityRequirement::Exact {
                consumer: ComponentId::new(consumer),
                capability: CapabilityId::new(capability),
                provider: ComponentId::new(provider),
            },
        )?;
    }
    let requirements = requirements
        .into_iter()
        .enumerate()
        .map(|(index, requirement)| {
            requirement.ok_or_else(|| {
                PyValueError::new_err(format!(
                    "capability requirement declaration is missing index {index}"
                ))
            })
        })
        .collect::<PyResult<Vec<_>>>()?;
    let component_outputs = component_outputs
        .into_iter()
        .map(|outputs| outputs.into_iter().map(CapabilityId::new).collect())
        .collect::<Vec<_>>();
    let selections = selections
        .into_iter()
        .map(|(requirement, provider)| {
            ProviderSelection::new(RequirementId::new(requirement), ComponentId::new(provider))
        })
        .collect::<Vec<_>>();

    resolve_capabilities(&component_outputs, &requirements, &selections)
        .map_err(|error| PyValueError::new_err(error.to_string()))
        .map(|bindings| {
            bindings
                .into_iter()
                .map(|binding| {
                    (
                        binding.provider.index(),
                        binding.consumer.index(),
                        binding.capability.index(),
                    )
                })
                .collect()
        })
}

fn set_requirement(
    requirements: &mut [Option<CapabilityRequirement>],
    index: usize,
    requirement: CapabilityRequirement,
) -> PyResult<()> {
    let Some(slot) = requirements.get_mut(index) else {
        return Err(PyValueError::new_err(format!(
            "capability requirement has unknown index {index}"
        )));
    };
    if slot.replace(requirement).is_some() {
        return Err(PyValueError::new_err(format!(
            "capability requirement index is declared more than once: {index}"
        )));
    }
    Ok(())
}

#[pymethods]
impl NativeCompiledExecutionGraph {
    #[new]
    fn new(
        node_graphs: Vec<usize>,
        composite_child_graphs: Vec<(usize, usize)>,
        graph_owners: Vec<Option<usize>>,
        prerequisites: Vec<Vec<(usize, Option<usize>)>>,
        when_conditions: Vec<Option<usize>>,
        condition_reads: Vec<Vec<usize>>,
        capability_bindings: Vec<(usize, usize)>,
    ) -> PyResult<Self> {
        let node_count = node_graphs.len();
        if prerequisites.len() != node_count || when_conditions.len() != node_count {
            return Err(PyValueError::new_err(
                "compiled execution graph node columns have different lengths",
            ));
        }

        let mut child_graphs = HashMap::new();
        for (node, child_graph) in composite_child_graphs {
            if node >= node_count {
                return Err(PyValueError::new_err(format!(
                    "execution declaration has unknown composite node: {node}"
                )));
            }
            if child_graphs.insert(node, child_graph).is_some() {
                return Err(PyValueError::new_err(format!(
                    "execution declaration repeats composite node: {node}"
                )));
            }
        }

        let nodes = node_graphs
            .into_iter()
            .zip(prerequisites)
            .zip(when_conditions)
            .enumerate()
            .map(|(index, ((graph, prerequisites), when))| {
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
                match child_graphs.get(&index) {
                    None => DeclaredNode::leaf(GraphId::new(graph), prerequisites, when),
                    Some(child_graph) => DeclaredNode::composite(
                        GraphId::new(graph),
                        GraphId::new(*child_graph),
                        prerequisites,
                        when,
                    ),
                }
            });
        let graphs = graph_owners.into_iter().map(|owner| {
            owner.map_or(DeclaredGraph::Root, |owner| {
                DeclaredGraph::OwnedBy(NodeId::new(owner))
            })
        });
        let conditions = condition_reads
            .into_iter()
            .map(|reads| RuntimeCondition::new(reads.into_iter().map(NodeId::new)));
        let capability_bindings = capability_bindings.into_iter().map(|(source, target)| {
            CapabilityBinding::new(NodeId::new(source), NodeId::new(target))
        });
        let graph = ExecutionGraphDeclaration::new(graphs, nodes, conditions, capability_bindings)
            .lower()
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

    fn all_prerequisites(&self) -> Vec<Vec<(usize, Option<usize>)>> {
        self.graph
            .nodes()
            .iter()
            .map(|node| {
                node.prerequisites()
                    .iter()
                    .map(|prerequisite| {
                        (
                            prerequisite.node().index(),
                            prerequisite.condition().map(ConditionId::index),
                        )
                    })
                    .collect()
            })
            .collect()
    }

    fn when_conditions(&self) -> Vec<Option<usize>> {
        self.graph
            .nodes()
            .iter()
            .map(|node| node.when().map(ConditionId::index))
            .collect()
    }

    fn topological_order(&self) -> Vec<usize> {
        node_indexes(self.graph.dependency_topology().topological_order())
    }

    fn session(&self, initially_skipped: Vec<usize>) -> PyResult<NativeExecutionSession> {
        Ok(NativeExecutionSession {
            session: ExecutionSession::new(
                Arc::clone(&self.graph),
                initially_skipped.into_iter().map(NodeId::new),
            )
            .map_err(|error| PyValueError::new_err(error.to_string()))?,
        })
    }

    fn mixed_session(
        &self,
        initially_skipped: Vec<usize>,
        native_actions: &mixed_execution::NativeActionBindings,
        native_contexts: Vec<(usize, Option<String>)>,
        workers: usize,
    ) -> PyResult<mixed_execution::NativeMixedExecutionSession> {
        let executor = NativeActionExecutor::new(workers)
            .map_err(|error| PyValueError::new_err(error.to_string()))?;
        let bindings = mixed_execution::NativeMixedExecutionSession::bindings(
            self.graph.nodes().len(),
            native_actions,
            native_contexts,
        )?;
        let session = ExecutionSession::new(
            Arc::clone(&self.graph),
            initially_skipped.into_iter().map(NodeId::new),
        )
        .map_err(|error| PyValueError::new_err(error.to_string()))?;
        Ok(mixed_execution::NativeMixedExecutionSession::new(
            session, executor, bindings,
        ))
    }
}

fn node_indexes(nodes: &[NodeId]) -> Vec<usize> {
    nodes.iter().map(|node| node.index()).collect()
}

/// Native implementation details used by the Python package.
#[pymodule]
#[allow(clippy::unnecessary_wraps)]
fn _native(module: &Bound<'_, PyModule>) -> PyResult<()> {
    module.add_function(wrap_pyfunction!(dag_cycle, module)?)?;
    module.add_function(wrap_pyfunction!(resolve_capability_bindings, module)?)?;
    module.add_function(wrap_pyfunction!(declaration::compile_declaration, module)?)?;
    module.add_function(wrap_pyfunction!(experiment::run_tokio_callbacks, module)?)?;
    module.add_function(wrap_pyfunction!(
        native_graph::native_process_graph,
        module
    )?)?;
    module.add_class::<NativeCompiledExecutionGraph>()?;
    module.add_class::<declaration::NativeDeclarationCompilation>()?;
    module.add_class::<NativeExecutionBatch>()?;
    module.add_class::<NativeExecutionSession>()?;
    module.add_class::<mixed_execution::NativeActionBindings>()?;
    module.add_class::<mixed_execution::NativeMixedExecutionBatch>()?;
    module.add_class::<mixed_execution::NativeMixedExecutionSession>()?;
    module.add_class::<native_graph::NativeGraph>()?;
    Ok(())
}
