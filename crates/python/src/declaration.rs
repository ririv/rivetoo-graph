use std::collections::HashMap;
use std::sync::Arc;

use pyo3::exceptions::PyValueError;
use pyo3::prelude::*;
use pyo3::types::PyDict;
use rivetoo_graph::{
    CapabilityBinding, ConditionId, DeclarationNode, DeclarationPrerequisite, DeclaredGraph,
    DeclaredNodeKind, GraphDeclaration, GraphId, NodeActivation, NodeId, RuntimeCondition,
};

use crate::NativeCompiledExecutionGraph;

struct PythonTypes<'py> {
    graph: Bound<'py, PyAny>,
    conditional_edge: Bound<'py, PyAny>,
    capability_binding: Bound<'py, PyAny>,
    runtime_condition: Bound<'py, PyAny>,
    static_condition: Bound<'py, PyAny>,
    evaluate_static_condition: Bound<'py, PyAny>,
}

impl<'py> PythonTypes<'py> {
    fn import(py: Python<'py>) -> PyResult<Self> {
        let model = py.import("rivetoo_graph.model")?;
        let capability = py.import("rivetoo_graph.capability")?;
        let static_condition = py.import("rivetoo_graph.static_condition")?;
        Ok(Self {
            graph: model.getattr("Graph")?,
            conditional_edge: model.getattr("ConditionalEdge")?,
            capability_binding: capability.getattr("CapabilityBinding")?,
            runtime_condition: model.getattr("RuntimeCondition")?,
            static_condition: static_condition.getattr("StaticCondition")?,
            evaluate_static_condition: static_condition.getattr("evaluate_static_condition")?,
        })
    }
}

struct GraphRecord {
    source_graph: Py<PyAny>,
    label: String,
    label_path: Vec<String>,
    owner: Option<usize>,
    local_steps: HashMap<usize, usize>,
    local_step_order: Vec<usize>,
}

struct NodeRecord {
    source_step: Py<PyAny>,
    action: Py<PyAny>,
    graph: usize,
    child_graph: Option<usize>,
    step_id: Vec<usize>,
    label: String,
}

struct ConditionRecord {
    predicate: Py<PyAny>,
    reads: Vec<usize>,
}

struct Collector<'py> {
    py: Python<'py>,
    types: PythonTypes<'py>,
    graphs: Vec<GraphRecord>,
    nodes: Vec<NodeRecord>,
    declaration_nodes: Vec<DeclarationNode>,
    activations: Vec<NodeActivation>,
    conditions: Vec<ConditionRecord>,
    runtime_conditions: Vec<RuntimeCondition>,
    capability_bindings: Vec<CapabilityBinding>,
}

impl<'py> Collector<'py> {
    fn new(py: Python<'py>) -> PyResult<Self> {
        Ok(Self {
            py,
            types: PythonTypes::import(py)?,
            graphs: Vec::new(),
            nodes: Vec::new(),
            declaration_nodes: Vec::new(),
            activations: Vec::new(),
            conditions: Vec::new(),
            runtime_conditions: Vec::new(),
            capability_bindings: Vec::new(),
        })
    }

    fn collect_root(
        mut self,
        source_graph: &Bound<'py, PyAny>,
        root_label: &str,
        root_label_path: Vec<String>,
        id_prefix: &[usize],
    ) -> PyResult<NativeDeclarationCompilation> {
        self.collect_graph(
            source_graph,
            root_label,
            root_label_path,
            None,
            id_prefix,
            true,
        )?;
        let graph_owners = self.graphs.iter().map(|graph| {
            graph.owner.map_or(DeclaredGraph::Root, |owner| {
                DeclaredGraph::OwnedBy(NodeId::new(owner))
            })
        });
        let compilation = GraphDeclaration::new(
            graph_owners,
            self.declaration_nodes,
            self.runtime_conditions,
            self.capability_bindings,
        )
        .compile()
        .map_err(|error| PyValueError::new_err(error.to_string()))?;
        let declaration_nodes = compilation
            .declaration_nodes()
            .iter()
            .map(|node| node.index())
            .collect();
        let declaration_conditions = compilation
            .declaration_conditions()
            .iter()
            .map(|condition| condition.index())
            .collect();
        Ok(NativeDeclarationCompilation {
            graph: NativeCompiledExecutionGraph {
                graph: Arc::new(compilation.into_graph()),
            },
            declaration_nodes,
            declaration_conditions,
            graphs: self.graphs,
            nodes: self.nodes,
            conditions: self.conditions,
        })
    }

    fn collect_graph(
        &mut self,
        source_graph: &Bound<'py, PyAny>,
        default_label: &str,
        label_path: Vec<String>,
        owner: Option<usize>,
        id_prefix: &[usize],
        parent_active: bool,
    ) -> PyResult<usize> {
        let declared_label = source_graph.getattr("label")?;
        let label = if declared_label.is_none() {
            default_label.to_owned()
        } else {
            declared_label.extract::<String>()?
        };
        let graph_index = self.graphs.len();
        self.graphs.push(GraphRecord {
            source_graph: source_graph.clone().unbind(),
            label,
            label_path,
            owner,
            local_steps: HashMap::new(),
            local_step_order: Vec::new(),
        });

        let steps = source_graph.getattr("steps")?;
        let mut step_values = Vec::new();
        for step in steps.try_iter()? {
            step_values.push(step?);
        }
        for (local_index, step) in step_values.iter().enumerate() {
            let node_index = self.nodes.len();
            self.graphs[graph_index]
                .local_steps
                .insert(object_key(step), node_index);
            self.graphs[graph_index].local_step_order.push(node_index);

            let mut step_id = id_prefix.to_owned();
            step_id.push(local_index);
            let label_object = step.getattr("label")?;
            let step_label = if label_object.is_none() {
                step_id
                    .iter()
                    .map(usize::to_string)
                    .collect::<Vec<_>>()
                    .join(".")
            } else {
                label_object.extract::<String>()?
            };
            let when = step.getattr("when")?;
            let active = parent_active && self.static_node_active(source_graph, &when)?;
            let action = step.getattr("action")?;
            self.nodes.push(NodeRecord {
                source_step: step.clone().unbind(),
                action: action.clone().unbind(),
                graph: graph_index,
                child_graph: None,
                step_id: step_id.clone(),
                label: step_label.clone(),
            });
            let activation = if active {
                NodeActivation::Active {
                    prerequisites: Vec::new(),
                    when: None,
                }
            } else {
                NodeActivation::Inactive
            };
            self.activations.push(activation.clone());
            self.declaration_nodes.push(DeclarationNode::new(
                GraphId::new(graph_index),
                DeclaredNodeKind::Leaf,
                activation,
            ));

            if action.is_instance(&self.types.graph)? {
                let mut child_label_path = self.graphs[graph_index].label_path.clone();
                child_label_path.push(step_label.clone());
                let child_graph = self.collect_graph(
                    &action,
                    &step_label,
                    child_label_path,
                    Some(node_index),
                    &step_id,
                    active,
                )?;
                self.nodes[node_index].child_graph = Some(child_graph);
                self.replace_node_kind(
                    node_index,
                    DeclaredNodeKind::Composite {
                        child_graph: GraphId::new(child_graph),
                    },
                );
            }
        }

        self.collect_node_conditions(source_graph, graph_index, parent_active)?;
        self.collect_edges(source_graph, graph_index, parent_active)?;
        Ok(graph_index)
    }

    fn static_node_active(
        &self,
        source_graph: &Bound<'py, PyAny>,
        condition: &Bound<'py, PyAny>,
    ) -> PyResult<bool> {
        if condition.is_none() || !condition.is_instance(&self.types.static_condition)? {
            return Ok(true);
        }
        self.evaluate_static(source_graph, condition)
    }

    fn evaluate_static(
        &self,
        source_graph: &Bound<'py, PyAny>,
        condition: &Bound<'py, PyAny>,
    ) -> PyResult<bool> {
        let evaluator = source_graph.getattr("static_condition_eval")?;
        if evaluator.is_none() {
            return Err(PyValueError::new_err(
                "graph contains static condition but static_condition_eval is None",
            ));
        }
        let kwargs = PyDict::new(self.py);
        kwargs.set_item("static_condition_eval", evaluator)?;
        self.types
            .evaluate_static_condition
            .call((condition,), Some(&kwargs))?
            .extract()
    }

    fn collect_node_conditions(
        &mut self,
        source_graph: &Bound<'py, PyAny>,
        graph_index: usize,
        parent_active: bool,
    ) -> PyResult<()> {
        let node_indexes = self.graphs[graph_index].local_step_order.clone();
        for node_index in node_indexes {
            if !parent_active
                || matches!(self.node_activation(node_index), NodeActivation::Inactive)
            {
                continue;
            }
            let step = self.nodes[node_index].source_step.bind(self.py).clone();
            let when = step.getattr("when")?;
            if when.is_instance(&self.types.runtime_condition)? {
                let condition = self.register_runtime_condition(&when, graph_index)?;
                self.set_node_when(node_index, condition);
            } else if !when.is_none() && !when.is_instance(&self.types.static_condition)? {
                return Err(PyValueError::new_err("unsupported graph step condition"));
            }
        }
        let _ = source_graph;
        Ok(())
    }

    fn collect_edges(
        &mut self,
        source_graph: &Bound<'py, PyAny>,
        graph_index: usize,
        graph_active: bool,
    ) -> PyResult<()> {
        let edges = source_graph.getattr("edges")?;
        for edge in edges.try_iter()? {
            let edge = edge?;
            if edge.is_instance(&self.types.capability_binding)? {
                self.collect_capability_binding(&edge, graph_index)?;
                continue;
            }
            let source = self.require_local_node(graph_index, &edge.getattr("source")?)?;
            let target = self.require_local_node(graph_index, &edge.getattr("target")?)?;
            if !graph_active || matches!(self.node_activation(target), NodeActivation::Inactive) {
                continue;
            }
            let prerequisite = if edge.is_instance(&self.types.conditional_edge)? {
                let condition = edge.getattr("condition")?;
                if condition.is_instance(&self.types.runtime_condition)? {
                    DeclarationPrerequisite::RuntimeConditional {
                        node: NodeId::new(source),
                        condition: self.register_runtime_condition(&condition, graph_index)?,
                    }
                } else if condition.is_instance(&self.types.static_condition)? {
                    DeclarationPrerequisite::StaticConditional {
                        node: NodeId::new(source),
                        enabled: self.evaluate_static(source_graph, &condition)?,
                    }
                } else {
                    return Err(PyValueError::new_err("unsupported graph edge condition"));
                }
            } else {
                DeclarationPrerequisite::Required(NodeId::new(source))
            };
            self.push_node_prerequisite(target, prerequisite);
        }
        Ok(())
    }

    fn collect_capability_binding(
        &mut self,
        edge: &Bound<'py, PyAny>,
        graph_index: usize,
    ) -> PyResult<()> {
        let provider_component = self.require_local_node(graph_index, &edge.getattr("source")?)?;
        let consumer_component = self.require_local_node(graph_index, &edge.getattr("target")?)?;
        let capability = edge.getattr("capability")?;
        let provider_graph = self.require_child_graph(provider_component)?;
        let consumer_graph = self.require_child_graph(consumer_component)?;
        let provider_port = require_capability_port(
            self.graphs[provider_graph].source_graph.bind(self.py),
            "outputs",
            &capability,
        )?;
        let consumer_port = require_capability_port(
            self.graphs[consumer_graph].source_graph.bind(self.py),
            "inputs",
            &capability,
        )?;
        let provider = self.require_descendant_node(provider_component, &provider_port)?;
        // 一个公开输入可以约束多个内部步骤；不插入额外的运行时等待节点。
        for step in consumer_port.try_iter()? {
            let consumer = self.require_descendant_node(consumer_component, &step?)?;
            self.capability_bindings.push(CapabilityBinding::new(
                NodeId::new(provider),
                NodeId::new(consumer),
            ));
        }
        Ok(())
    }

    fn register_runtime_condition(
        &mut self,
        condition: &Bound<'py, PyAny>,
        graph_index: usize,
    ) -> PyResult<ConditionId> {
        let mut reads = Vec::new();
        for read in condition.getattr("reads")?.try_iter()? {
            reads.push(NodeId::new(self.require_local_node(graph_index, &read?)?));
        }
        let condition_id = ConditionId::new(self.conditions.len());
        self.conditions.push(ConditionRecord {
            predicate: condition.getattr("when")?.unbind(),
            reads: reads.iter().map(|node| node.index()).collect(),
        });
        self.runtime_conditions.push(RuntimeCondition::new(reads));
        Ok(condition_id)
    }

    fn require_local_node(&self, graph_index: usize, step: &Bound<'py, PyAny>) -> PyResult<usize> {
        self.graphs[graph_index]
            .local_steps
            .get(&object_key(step))
            .copied()
            .ok_or_else(|| PyValueError::new_err("graph edge references a step outside its graph"))
    }

    fn require_child_graph(&self, node: usize) -> PyResult<usize> {
        self.nodes[node]
            .child_graph
            .ok_or_else(|| PyValueError::new_err("capability component must contain a graph"))
    }

    fn require_descendant_node(
        &self,
        component: usize,
        step: &Bound<'py, PyAny>,
    ) -> PyResult<usize> {
        let prefix = &self.nodes[component].step_id;
        let matches = self
            .nodes
            .iter()
            .enumerate()
            .filter(|(_, node)| {
                node.step_id.starts_with(prefix)
                    && node.step_id.len() > prefix.len()
                    && object_key(node.source_step.bind(self.py)) == object_key(step)
            })
            .map(|(index, _)| index)
            .collect::<Vec<_>>();
        match matches.as_slice() {
            [node] => Ok(*node),
            [] => Err(PyValueError::new_err(
                "capability port references a step outside its component",
            )),
            _ => Err(PyValueError::new_err(
                "capability port references a step mounted more than once",
            )),
        }
    }

    fn node_activation(&self, node: usize) -> &NodeActivation {
        &self.activations[node]
    }

    fn replace_node_kind(&mut self, node: usize, kind: DeclaredNodeKind) {
        let activation = self.node_activation(node).clone();
        self.activations[node] = activation.clone();
        self.declaration_nodes[node] =
            DeclarationNode::new(GraphId::new(self.nodes[node].graph), kind, activation);
    }

    fn set_node_when(&mut self, node: usize, condition: ConditionId) {
        let NodeActivation::Active {
            prerequisites,
            when: _,
        } = self.node_activation(node).clone()
        else {
            return;
        };
        let activation = NodeActivation::Active {
            prerequisites,
            when: Some(condition),
        };
        self.activations[node] = activation.clone();
        self.declaration_nodes[node] = DeclarationNode::new(
            GraphId::new(self.nodes[node].graph),
            self.node_kind(node),
            activation,
        );
    }

    fn push_node_prerequisite(&mut self, node: usize, prerequisite: DeclarationPrerequisite) {
        let NodeActivation::Active {
            mut prerequisites,
            when,
        } = self.node_activation(node).clone()
        else {
            return;
        };
        prerequisites.push(prerequisite);
        let activation = NodeActivation::Active {
            prerequisites,
            when,
        };
        self.activations[node] = activation.clone();
        self.declaration_nodes[node] = DeclarationNode::new(
            GraphId::new(self.nodes[node].graph),
            self.node_kind(node),
            activation,
        );
    }

    fn node_kind(&self, node: usize) -> DeclaredNodeKind {
        self.nodes[node]
            .child_graph
            .map_or(DeclaredNodeKind::Leaf, |child_graph| {
                DeclaredNodeKind::Composite {
                    child_graph: GraphId::new(child_graph),
                }
            })
    }
}

fn object_key(value: &Bound<'_, PyAny>) -> usize {
    value.as_ptr() as usize
}

fn require_capability_port<'py>(
    graph: &Bound<'py, PyAny>,
    attribute: &str,
    capability: &Bound<'py, PyAny>,
) -> PyResult<Bound<'py, PyAny>> {
    // 映射按能力直接查找，平均 O(1)，不再逐端口跨语言扫描。
    graph
        .getattr("capabilities")?
        .getattr(attribute)?
        .get_item(capability)
}

/// Native compilation plus opaque Python declaration metadata.
#[pyclass(frozen, module = "rivetoo_graph._native")]
pub(crate) struct NativeDeclarationCompilation {
    pub(crate) graph: NativeCompiledExecutionGraph,
    declaration_nodes: Vec<usize>,
    declaration_conditions: Vec<usize>,
    graphs: Vec<GraphRecord>,
    nodes: Vec<NodeRecord>,
    conditions: Vec<ConditionRecord>,
}

#[pymethods]
impl NativeDeclarationCompilation {
    #[getter]
    fn graph(&self) -> NativeCompiledExecutionGraph {
        self.graph.clone()
    }

    fn declaration_nodes(&self) -> Vec<usize> {
        self.declaration_nodes.clone()
    }

    fn declaration_conditions(&self) -> Vec<usize> {
        self.declaration_conditions.clone()
    }

    fn node_step_ids(&self) -> Vec<Vec<usize>> {
        self.nodes.iter().map(|node| node.step_id.clone()).collect()
    }

    fn node_labels(&self) -> Vec<String> {
        self.nodes.iter().map(|node| node.label.clone()).collect()
    }

    fn node_graphs(&self) -> Vec<usize> {
        self.nodes.iter().map(|node| node.graph).collect()
    }

    fn node_child_graphs(&self) -> Vec<Option<usize>> {
        self.nodes.iter().map(|node| node.child_graph).collect()
    }

    fn source_steps(&self, py: Python<'_>) -> Vec<Py<PyAny>> {
        self.nodes
            .iter()
            .map(|node| node.source_step.clone_ref(py))
            .collect()
    }

    fn actions(&self, py: Python<'_>) -> Vec<Py<PyAny>> {
        self.nodes
            .iter()
            .map(|node| node.action.clone_ref(py))
            .collect()
    }

    fn graph_labels(&self) -> Vec<String> {
        self.graphs
            .iter()
            .map(|graph| graph.label.clone())
            .collect()
    }

    fn graph_label_paths(&self) -> Vec<Vec<String>> {
        self.graphs
            .iter()
            .map(|graph| graph.label_path.clone())
            .collect()
    }

    fn graph_owners(&self) -> Vec<Option<usize>> {
        self.graphs.iter().map(|graph| graph.owner).collect()
    }

    fn graph_local_nodes(&self) -> Vec<Vec<usize>> {
        self.graphs
            .iter()
            .map(|graph| graph.local_step_order.clone())
            .collect()
    }

    fn source_graphs(&self, py: Python<'_>) -> Vec<Py<PyAny>> {
        self.graphs
            .iter()
            .map(|graph| graph.source_graph.clone_ref(py))
            .collect()
    }

    fn predicates(&self, py: Python<'_>) -> Vec<Py<PyAny>> {
        self.conditions
            .iter()
            .map(|condition| condition.predicate.clone_ref(py))
            .collect()
    }

    fn condition_reads(&self) -> Vec<Vec<usize>> {
        self.conditions
            .iter()
            .map(|condition| condition.reads.clone())
            .collect()
    }
}

#[pyfunction]
#[pyo3(signature = (source_graph, root_label, root_label_path=Vec::new(), id_prefix=Vec::new()))]
#[allow(clippy::needless_pass_by_value)]
pub(crate) fn compile_declaration(
    source_graph: &Bound<'_, PyAny>,
    root_label: &str,
    root_label_path: Vec<String>,
    id_prefix: Vec<usize>,
) -> PyResult<NativeDeclarationCompilation> {
    Collector::new(source_graph.py())?.collect_root(
        source_graph,
        root_label,
        root_label_path,
        &id_prefix,
    )
}
