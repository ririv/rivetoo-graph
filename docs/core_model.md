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
is displayed as `<unlabeled>`; an unlabeled nested graph uses its mounting Step's resolved
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
- Step and Graph labels are only presentation.

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

The returned graph is compiled into a nested session managed by the same execution
driver. The parent Step completes after the child graph; while waiting it occupies
no action slot. All Python sessions share one thread pool or the caller's event
loop. Root, runtime-created, and Rust-native actions share the same `workers` or
`concurrency` limit. Serial execution uses an explicit stack rather than recursive
executor calls. Python-only sessions create no empty Rust worker pool.

The limit counts active Graph leaf actions, not threads, subprocesses, or requests
created inside an action. Child boundaries, result scopes, capability ports, and
failure propagation are preserved. This does not permit arbitrary edits to an
already running graph.

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

## GraphExecution

Defined in [execution.py](../src/rivetoo_graph/execution.py), GraphExecution binds a declaration to
one execution:

```python
execution = GraphExecution(
    graph=graph,
    execution_state=GraphExecutionState(),
    execution_control=GraphExecutionControl(),
)
```

It contains `graph`, `shared`, `execution_state`, `execution_control`,
`action_log`, and `execution_output`, and provides `compile()`, `run(...)`, `arun(...)`,
and `require_result(step)`.

The graph's name is declared only through `Graph.label`. Compilation, rendering,
and execution output all use `graph.display_label`: the declared name, or
`<unlabeled>` for an anonymous graph. This display value is not an identity;
scheduling and result lookup use `StepId`. GraphExecution does not rename the graph.

State is single-use. Control is independent by default; a host can explicitly
share one control between sequential executions to share cancellation, not results.
Once stopped, control cannot be reset.

GraphExecution does not read global host logging or stop state. By default it neither
connects a host action logger nor writes an execution file. For file output,
explicitly pass `TreeFileExecutionOutput(output_dir)`.

Use the asynchronous entry point inside an existing event loop:

```python
state = await GraphExecution(graph=graph).arun(
    AsyncGraphRunOptions(concurrency=8),
)
```

`arun()` and the lower-level `AsyncGraphExecutor.aexecute()` use the caller's loop.
Synchronous `run(AsyncGraphRunOptions(...))` runs that same asynchronous path in
its own event loop.

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

`GraphResultStore` is the write side of one execution's result records. Each StepId
can be recorded only once. Execution state, action contexts, and observation events
expose a concrete `GraphResultView` with `get/require/has`, without `record`. They
share one live view rather than copying the table for each Step. A state snapshot
copies the records and exposes a read-only view of that copy.

Read-only access applies to the table, not to the returned objects: values are not
deep-copied or frozen. Treat published results as immutable; use separate shared
state for data that actions intentionally modify together.

Use a Step for a sibling and an explicit ancestor reference for an outer graph:

```python
ParentGraphStepRef(step=outer_prepare, levels_up=1)
```

There are no string references or implicit searches for the nearest ancestor.

StepRunContext combines `view: StepReadContext`, `control: StepControl`, and
`step_log_path`. Read APIs include `result/require_result`,
`value/require_value`, `has_result`, `step_id`, `label_path`, and
`graph_label_path`. Side-effectful skipping goes through
`run_ctx.control.skip_step(step_id)` or `skip_steps(step_ids)`.

Long-running actions can call `ctx.control.stop_requested()` or
`ctx.control.require_running()` between iterations and external calls:

```python
async def task(ctx: StepRunContext) -> None:
    for item in items:
        ctx.control.require_running()
        await process_item(item)
```

The host requests a stop through `execution.execution_control.request_stop()`, or
by cancelling the task awaiting `arun()`. The driver stops dispatching and waits
for started actions to clean up. It does not kill Python functions or roll back
side effects: an action that never checks must finish itself. Stop handlers run
outside the control lock; handlers registered after a stop run immediately.
Handler failures are collected after all handlers have been called.

Bind logs when constructing the execution:

```python
execution = GraphExecution(graph=graph, step_log_paths={step: log_path})
```

The constructor resolves all Step references against the complete graph in one
batch and snapshots the paths. A reference must identify exactly one mounted
Step. There is no post-construction binding API. Resolving V mounted steps and K
bindings takes O(V + K) time and O(V + K) space; an empty mapping needs no traversal.
A leaf action uses its own binding. Without one, it inherits the nearest bound
ancestor's path. Static and runtime-returned subgraphs also inherit the path;
actions cannot change the bindings at runtime.

## Execution shared state

Pass an explicitly constructed business object with `GraphExecution(shared=...)`.
Python actions access that same object through `ctx.shared(ExpectedType)`, which
returns the requested type and rejects a missing object or a type mismatch. It
does not create a default dictionary or resolve objects from a registry.

```python
from dataclasses import dataclass, field
from threading import Lock

from rivetoo_graph import GraphExecution, Step, StepRunContext, ThreadGraphRunOptions


@dataclass
class Progress:
    completed: int = 0
    lock: Lock = field(default_factory=Lock)

    def advance(self) -> None:
        with self.lock:
            self.completed += 1


def task(ctx: StepRunContext) -> None:
    progress = ctx.shared(Progress)
    progress.advance()


graph = (Step(task) >> Step(task)).graph()
progress = Progress()
GraphExecution(graph=graph, shared=progress).run(ThreadGraphRunOptions(workers=2))
assert progress.completed == 2
```

Shared state belongs to the execution input, not the Graph declaration, scheduler
state, result store, or execution snapshot. Static and runtime-returned subgraphs
receive the same reference, at O(1) cost per action without copying the object.
The serial, threaded, and async executors also accept `shared=` directly.

The caller owns synchronization, resource cleanup, and lifetime. Compose several
shared capabilities into a typed object if needed. Supply a fresh object to
isolate runs; explicitly reusing one instance shares it across runs. Sharing does
not add scheduling edges: declare a dependency when a read must wait for a write.
Runtime predicates retain their read-only result context and do not receive shared
state. Python shared objects are not projected into Rust native actions.

## Observation and state

GraphObservationMessages contains presentation-only `before` and
`after_success` messages. An injected GraphObserver receives GraphObservationEvent
values for tracing, timing, and display. It must not change results or replace
explicit Steps.

Subscribe separately to step transitions when needed:

```python
from rivetoo_graph import GraphExecution, StepExecutionEvent


class Events:
    def emit(self, event: StepExecutionEvent) -> None:
        print(event.step_id, event.label_path, event.phase)


execution = GraphExecution(graph=graph, events=Events())
execution.run()
```

`StepExecutionPhase` contains `STARTED`, `COMPLETED`, `FAILED`, `BLOCKED`, and
`SKIPPED`. Events follow committed transitions, using mounted identities and label
paths for static subgraphs, runtime-created subgraphs, Python, and native actions.
Composite start/completion events consume no action slot and do not depend on the
dynamic/compiled presentation mode. Blocked and skipped nodes need no start event.

Callbacks execute synchronously and serially; keep them short, as their cost is
part of execution time. A callback failure raises `GraphEventDeliveryError` and
stops execution, without relabeling a completed Step as failed. Without `events`,
no events, label index, message text, or result copies are constructed. Events do
not implement messaging, persistence, or replay-based recovery.

GraphExecutionState records executed/completed steps, entered/completed composites,
skips, failures, blocked nodes, condition results, and an optional GraphResultStore.
It cannot be used for a second run and has no reset or recovery fallback.
