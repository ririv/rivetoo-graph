# Core model

English | [简体中文](./zh-CN/core_model.md)

Declarations, compilation, and execution are separate:

```text
Graph / Step
    ↓ compile_graph(...)
GraphCompilation
    ↓ Graph runtime
GraphExecutionState
```

Declarations describe structure, not a particular run's state. Internal identities
are assigned when nodes are mounted.

## Pure topology

`rivetoo_graph.topology` provides domain-independent Python types:

- `Arc[NodeT]`: a directed connection from source to target.
- `DirectedTopology[NodeT]`: nodes and arcs, with predecessor, successor, and
  reachability queries. Cycles are allowed.
- `DAGTopology[NodeT]`: a directed topology validated as acyclic at construction.

These types do not know about actions, conditions, capabilities, or execution state.
Execution Graph validation uses a separate path: it sends blocking relations to
Rust `dag_cycle` in a batch. The Rust compiler also validates the final DAG,
including cross-subgraph capability bindings. See [topology complexity](./topology_complexity.md).

## Graph

Defined in [model.py](../src/rivetoo_graph/model.py), `Graph` contains:

- `label: str | None`
- `steps: tuple[Step, ...]`
- `edges: tuple[GraphEdge, ...]`
- `capabilities: CapabilityInterface`
- `observation: GraphObservationMessages`
- `static_condition_eval: StaticConditionEval | None`

It describes an execution DAG and has no `run()` method. Capabilities hold
read-only input/output mappings. At construction, passing an inner Graph as
`capabilities` explicitly forwards its entire interface.

Labels are optional display names, not scheduling identities. An unlabeled root
uses `GraphPlan.label`; an unlabeled nested graph uses its mounting Step's resolved
label. An explicit Graph label takes precedence.

Explicit wiring lives in `Graph.edges`. A normalized Graph does not accept
StepWiring objects, and Step does not store `after`. Runtime-condition `reads`
are another explicit source of blocking relations and participate in validation
and scheduling.

## Step

A Step contains:

```python
label: str | None
action: LeafAction | Graph | NativeGraph
when: StaticCondition | RuntimeCondition | None
```

It has no public `id` or `after` field.

```python
prepare = Step(prepare_source, label="prepare")
compile_step = Step(lambda: compile_source(flags), label="compile")
```

Callables are normalized to `LeafAction`. The three action forms are:

- `LeafAction`: a Python leaf action.
- `Graph`: an explicit Python subgraph.
- `NativeGraph`: a complete Rust-native subgraph whose actions execute in Rust.

Duplicate labels are legal. Declaration identity is the Step object itself.
The same Step cannot be mounted twice in one Graph, but can be reused in separate
Graphs with different wiring. Ordinary edges and condition reads cannot reference
steps outside their graph; cross-subgraph synchronization uses capability ports.

## Identity and presentation

- Step references identify declarations for wiring, condition reads, and results.
- `StepId` identifies one mounted position, such as `0`, `1`, or `1.0`.
  Compilation, scheduling, and result storage use this identity.
- Step, Graph, and GraphPlan labels are only presentation.

An unlabeled Step uses its mounted StepId as its compiled display name. Lambdas
therefore need no fabricated stable label.

## Edges and wiring

```python
type GraphEdge = UnconditionalEdge | ConditionalEdge | CapabilityBinding
```

Ordinary and conditional edges reference actual Step objects:

```python
UnconditionalEdge(source, target)
ConditionalEdge(source, target, condition)
```

Usually, declare them through `wire()`:

```python
prepare = Step(prepare_source, label="prepare")
compile_step = Step(compile_source, label="compile")
graph = Graph.from_wiring(
    wiring=(
        prepare.wire(),
        compile_step.wire(after=(prepare,)),
    )
)
```

StepWiring is a construction input. `Graph.from_wiring()` normalizes it into
`steps` and `edges`, without retaining the wrapper. Conditional prerequisites
are explicit:

```python
archive.wire(
    after=(
        build,
        ConditionalPrerequisite(android_shim, condition=android_only),
    )
)
```

Strings cannot reference Steps. Object references avoid ambiguity from duplicate
labels, renaming, and nested scopes. CapabilityBinding represents resolved
cross-subgraph wiring; see [capabilities](./capabilities.md).

## Composition DSL

```python
A >> B
A | B
```

Both return GraphWiring, explicitly materialized with `.graph()`:

```python
graph = (prepare >> (compile_a | compile_b) >> install).graph()
```

- `A >> B` connects every structural exit of A to every structural entry of B.
- `A | B` combines nodes and edges without adding dependencies.
- `A >> (B | C)` is fan-out; `(A | B) >> C` is fan-in.

These are wiring boundaries, not named CapabilityInterface ports. The DSL does
not automatically expose or bind capabilities.

Using an existing Graph as an operand wraps it in a Step and preserves its boundary:

```python
pipeline = (prepare >> child_graph >> install).graph()
child_step = child_graph.step(label="build", when=condition)
```

Explicit `Graph.step()` and automatic DSL wrapping use the same semantics.

## Static selection before construction

`select_one()` requires exactly one active branch and returns its original value.
It does not wrap Steps, create subgraphs, or enter the graph topology.

```python
compiler: Step = select_one(
    StaticBranch(active=use_clang, value=compile_with_clang),
    StaticBranch(active=use_gcc, value=compile_with_gcc),
)
graph = (prepare >> compiler >> install).graph()
```

Zero or multiple active branches are errors. Branch conditions must already be
bool values; this helper does not accept StaticCondition or inspect request state.

To defer candidate construction, select a factory and invoke only the selected one:

```python
selected_graph = select_one(
    StaticBranch(active=is_android, value=android_graph),
    StaticBranch(active=is_ios, value=ios_graph),
)()
```

Unlike selection before construction, `Step.when` controls a node's participation
inside an already declared graph.

## LeafAction

LeafAction accepts callables invokable in one of two ways:

```python
Callable[[], object]
Callable[[StepRunContext], object]
```

It inspects the signature at construction and fixes the calling convention:
`fn()` or `fn(run_ctx)`. Business arguments belong in a closure:

```python
Step(lambda: generate_pkg_config_file(layout, version), label="pkgconfig")
Step(
    lambda run_ctx: compile_source(compiler, run_ctx.require_value(prepare)),
    label="compile",
)
```

If a callable supports both forms, the zero-argument form wins. Default captures
such as `lambda value=value: ...` are not overwritten with a runtime context.

The `step(action)` helper derives labels from named functions, LeafAction
functions, or Graph labels. Lambdas and anonymous callables remain unlabeled.
It neither binds business arguments nor introduces a second execution protocol.

## Returning a graph at runtime

A leaf action may return a Graph after inspecting runtime results:

```python
def select_graph(run_ctx: StepRunContext) -> Graph:
    selected = run_ctx.require_value(select_strategy)
    return graph_for(selected)
```

The returned graph is compiled and executed as an explicit subgraph in a nested
execution session. Its boundary is preserved. That nested session currently does
not share the outer session's global concurrency budget.

## Conditions

`Step.when` controls node participation; ConditionalEdge controls whether a
prerequisite applies. StaticCondition is evaluated during compilation;
RuntimeCondition is evaluated during execution.

```python
cache_miss = runtime_condition(
    reads=(cache_probe,),
    when=lambda ctx: not bool(ctx.require_value(cache_probe)),
)
```

`reads` blocks condition evaluation. It participates in cycle detection and in
wiring-boundary calculation once both endpoints are present, even though it is
not an ordinary prerequisite.

## GraphPlan

Defined in [plan.py](../src/rivetoo_graph/plan.py), GraphPlan binds a declaration to
one execution:

```python
plan = GraphPlan(
    label="build",
    graph=graph,
    execution_state=GraphExecutionState(),
    execution_control=GraphExecutionControl(),
)
```

It contains `label`, `graph`, `execution_state`, `execution_control`,
`action_log`, and `execution_output`, and provides `compile()`, `run(...)`,
and `require_result(step)`.

State is single-use. Control is independent by default; a host can explicitly
share one control between sequential plans to share cancellation, not results.
Once stopped, control cannot be reset.

GraphPlan does not read global host logging or stop state. By default it neither
connects a host action logger nor writes an execution file. For file output,
explicitly pass `TreeFileExecutionOutput(output_dir)`.

## GraphCompilation

The compiler in [compile.py](../src/rivetoo_graph/compile.py) evaluates static
conditions, assigns mounted identities and labels, and lowers runtime conditions
and capability bindings.

- `CompiledStep`: Python leaf executed by the Python driver.
- `CompiledNativeStep`: native leaf executed by Rust workers.
- `CompiledComposite`: non-executable subgraph lifecycle boundary.
- `roots`: typed references directly owned by the root graph.

A composite preserves its children, prerequisites, runtime condition, observation,
and overall completion state. It is not a no-op action. Both presentation modes
share identities and declaration-scope mappings, so result lookup is consistent.

## Results and runtime context

[results.py](../src/rivetoo_graph/results.py) stores successful return values by
StepId. Actions use declaration references:

```python
def compile_action(run_ctx: StepRunContext) -> None:
    source = run_ctx.require_value(prepare)
    compile_source(source)
```

Use a Step for a sibling and an explicit ancestor reference for an outer graph:

```python
ParentGraphStepRef(step=outer_prepare, levels_up=1)
```

There are no string references or implicit searches for the nearest ancestor.

StepRunContext combines `view: StepReadContext`, `control: StepControl`, and
`component_log_path`. Read APIs include `result/require_result`,
`value/require_value`, `has_result`, `step_id`, `label_path`, and
`graph_label_path`. Side-effectful skipping goes through
`run_ctx.control.skip_step(step_id)` or `skip_steps(step_ids)`.

Bind component logs before execution with
`GraphPlan.bind_component_log_path(step, path)`. For multiple components,
`bind_component_log_paths(...)` builds one mounting index instead of traversing
the graph repeatedly. Static and runtime-returned subgraphs inherit that log path;
actions cannot override it at runtime.

## Observation and state

GraphObservationMessages contains presentation-only `before` and
`after_success` messages. An injected GraphObserver receives GraphObservationEvent
values for tracing, timing, and display. It must not change results or replace
explicit Steps.

GraphExecutionState records executed/completed steps, entered/completed composites,
skips, failures, blocked nodes, condition results, and an optional GraphResultStore.
It cannot be used for a second run and has no reset or recovery fallback.
