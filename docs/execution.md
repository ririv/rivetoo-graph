# Execution semantics

English | [简体中文](./zh-CN/execution.md)

## Dynamic and compiled modes

Both public modes use the same Rust declaration compiler and execution session:

- `GraphExecution.run(mode="dynamic")` retains compatibility presentation records:
  composites also appear in `executed_steps/completed_steps`.
- `GraphExecution.run(mode="compiled")` records composites through their dedicated
  entry/completion state. `GraphExecution.compile()` exposes the compiled structure.

Capability bindings use the same lowering in both modes. The runtime consumes
GraphCompilation, not a second Python scheduling model. Runtime-returned graphs
enter the same compilation pipeline with their parent StepId, label path, and
ancestor result scopes.

## Python action return values

Serial and thread drivers invoke a Python action, then inspect its return value:

- Ordinary values go directly to result recording, without creating an event loop.
- Awaitables run to completion in a new event loop in the execution thread.
  This includes coroutine functions, synchronous functions returning coroutines,
  and custom awaitables.
- A returned Graph executes as a dynamic subgraph before the outer Step completes.

Checking only `async def` is insufficient: a synchronous function can return an
awaitable. The async driver awaits async actions and bridges synchronous calls
with `asyncio.to_thread()`. `arun()` uses the caller's existing event loop.
See the [Step overhead benchmark](./step_overhead_benchmark.md).

## Conditional execution

[Conditional edges](./conditional_edges.md) decide whether dependencies apply;
[conditional steps](./conditional_steps.md) decide whether nodes participate.

In both modes:

- Static conditions are evaluated and removed during compilation.
- Runtime edges become CompiledConditionalDependency values.
- Runtime Step conditions become CompiledRuntimeCondition values.
- A false runtime node condition skips the node; a false edge condition removes
  only that dependency.
- Each compiled condition instance is evaluated at most once per session.
  Multiple instances may reuse the same Python condition object or predicate.
- Conditions inside subgraphs use the same lowering and scheduling rules.

## Rendering

See [render/](../src/rivetoo_graph/render/).

`render_graph(...)` and `render_graph_mermaid(...)` accept Graph or
GraphCompilation and require an explicit `root_label`. They do not infer the
execution root label from Graph.label.

- Dynamic mode renders the declaration structure. For GraphCompilation, it uses
  `source_graph`.
- Compiled mode renders the compilation. Passing a Graph compiles it first.
- `render_graph_compilation_mermaid(compilation, root_label=...)` is also
  available for an existing compilation.

Execution views use recorded condition results; they do not rerun predicates.

## Run options

[Run options](../src/rivetoo_graph/runtime/run.py) form a union:

- `ThreadGraphRunOptions(workers=..., observer=...)`
- `AsyncGraphRunOptions(concurrency=..., observer=...)`

Both concurrency limits default to 1. Observer is optional; without one, graph
observation is not emitted. Pass either option type to
`GraphExecution.run(options=..., mode=...)`.

Inside an existing event loop, use
`await GraphExecution(graph).arun(AsyncGraphRunOptions(concurrency=8))`.
Both entry points use the same asynchronous implementation.

## Execution control

GraphExecutionControl holds stop state and stop handlers, not action results.
Ordinary action or runtime-predicate exceptions do not request global cancellation.
Explicit host cancellation, GraphExecutionStopped, KeyboardInterrupt, and other
execution interrupts stop the run. The host can register handlers to reclaim
running subprocesses and other resources.

Control is single-use: once stopped, it stays stopped and has no reset.
An execution with already-stopped control raises before running. A stop during a run
prevents new dispatch, waits for already-started work, then reports cancellation.

Each execution has independent control by default:

```python
execution = GraphExecution(graph=graph)
```

A host may share control between sequential executions:

```python
control = GraphExecutionControl()
first_execution = GraphExecution(graph=first_graph, execution_control=control)
second_execution = GraphExecution(graph=second_graph, execution_control=control)
```

These remain separate runs with independent GraphExecutionState values. Sharing
control shares cancellation and resource-stop signals, not results. After an
explicit stop, the second execution cannot start. If the caller catches an ordinary
failure and explicitly starts the next execution, control does not prevent it.
For success dependencies, compose one graph or explicitly handle failure.

Process signals and cross-execution request management belong to the host.

Actions can cooperatively check `ctx.control.stop_requested()` or call
`ctx.control.require_running()`. Cancelling the task awaiting `arun()` requests
a stop and waits for started actions to clean up. Stop handlers run outside the
control lock; registering after a stop invokes the handler immediately. All stop
handlers are called before any handler failures are raised together.

## Step execution events

Pass `events=StepEventSink` to GraphExecution to receive `StepExecutionEvent`
values with a StepId, label path, and `StepExecutionPhase`:
`STARTED`, `COMPLETED`, `FAILED`, `BLOCKED`, or `SKIPPED`.
Static and runtime-generated subgraphs and Rust-native actions use the same
interface. Events describe committed transitions; composite events do not depend
on presentation mode. An event callback is synchronous and serialized. A callback
failure stops the execution with `GraphEventDeliveryError`, preserving the
transition already committed. With no subscription, no event objects or event
label index are created. GraphObserver remains the separate presentation interface.

## Failure propagation

An ordinary Step failure blocks only work that depends on it:

```text
A (failed) → B (blocked, not executed)
C (succeeded) → D (continues)
```

The overall run still reports failure.

- Finished, Skipped, Failed, and Blocked are distinct states.
- Active conditional dependencies and result edges propagate failure;
  inactive conditional edges do not.
- A predicate exception blocks affected nodes instead of reading missing results.
- An entered subgraph lets unrelated internal branches finish, then reports
  failure without AFTER_SUCCESS. Work depending on the whole subgraph is blocked.
  If a subgraph is blocked before entry, all descendants are marked blocked.
- A successful capability output remains ready even if another part of its
  provider subgraph later fails. A failed output blocks its bound inputs, not
  unrelated preparation inside the consumer.
- Failure in a dynamically returned graph fails its outer action.
- Python and Rust actions follow the same rules. Native errors and failure states
  cross the boundary in batches, not per-node exception queries.

See [port failure examples](./capabilities.md#failures-at-capability-ports).

After scheduling finishes, one error is raised through the exception chain with
its node path; multiple errors are reported together in a Python ExceptionGroup.
Blocked nodes do not produce duplicate errors. Execution snapshots record
`GraphNodeFailure.FAILED | BLOCKED` in `node_failures`, and rendering distinguishes
these terminal states from running work.

Compilation, scheduler-protocol, logging, and observation failures are infrastructure
errors; continuing after them is not guaranteed. Failure neither rolls back
successful work nor retries it. Dependencies, not exception names, determine which
work can continue.

Blocking propagation follows existing adjacency indexes rather than rescanning the
whole graph for every error: blocked-subtree traversal is O(V_blocked + E_out).
Direct-child counters make composite terminal-state aggregation O(V) overall.

## Host runtime interfaces

GraphExecution connects host facilities through two independent interfaces:

- GraphActionLog establishes an action log scope and appends Python exception text
  from parallel actions.
- GraphExecutionOutput consumes the final state after success or failure.
  TreeFileExecutionOutput writes a view into an explicitly supplied directory.

Standalone defaults do not connect host logging or write execution-view files.
Control remains the responsibility of GraphExecutionControl, not the logger.
There is no global-directory discovery or silent change of behavior.

## Explicit skipping

Explicit skip state can target a step path. In compiled mode, skipping a composite
also skips its descendants. This is distinct from failed or blocked state and does
not invoke the action or create its result.

The common declaration, compilation, and result APIs are described in the
[core model](./core_model.md); conditions and failure propagation follow the rules
above.

## Why there are no side-effectful runtime hooks

Graph does not provide hidden before/after-step hooks for cache probes, cache
writes, or skip decisions. Such behavior belongs in the visible declaration:

- Cache probes and writeback are explicit Steps.
- Logging is an explicit GraphActionLog integration or action wrapper.
- Skipping is an explicit host write through execution control/state APIs.

Observers exist for tracing, timing, visualization, and summaries, not for replacing
Steps or changing results.

## DAG constraints

At construction, Graph validates nonempty explicitly supplied labels, Step-only
node membership, duplicate mounting, local edge endpoints, and local acyclicity.
Omitting a label is valid.

Local explicit edges and runtime inputs are projected to integer nodes and blocking
arcs, then checked by Rust `dag_cycle`, not Python DirectedTopology/DAGTopology.
Cross-subgraph capability bindings are checked in the final lowered DAG.
See [topology complexity](./topology_complexity.md).

## Subgraphs and result scopes

For a declared subgraph:

- Leaves join the global execution structure.
- The boundary becomes a non-executable CompiledComposite.
- Children can become ready only after composite activation.
- A composite reaches its terminal state after all direct children do.
- An activated empty composite completes immediately.
- GraphObservationMessages.before/after_success follows composite lifecycle.

Runtime-returned graphs cannot be compiled ahead of time. They receive a nested
compilation/session rooted at the current leaf path.

Result edges resolve producer identities inside the current mount. Actions receive
their ordered Option inputs, not graph scope indexes or a result query context.
An action returning a child graph can capture these received values in its child
actions. There is no implicit lookup into an ancestor graph's result store.

Multiple structural entry nodes are legal. They may all become ready, but readiness
does not imply simultaneous execution.

## Concurrency boundaries

Thread options run synchronous Python calls directly in workers and bridge
awaitables with asyncio.run. Async options await async work and use
asyncio.to_thread for synchronous calls. Native actions run in controlled Rust
workers without blocking the async event loop. Both language branches share one
session's total capacity. ContextVar isolates action log scopes per task.

Only ready nodes are dispatched. Ordinary failures stop dependent work, while
unrelated ready nodes continue. Explicit cancellation stops new dispatch.
Already-running workers/tasks are not forcibly cancelled by Graph.

Runtime-returned subgraphs use nested Rust sessions coordinated by one driver.
Python sessions share one pool or event loop, and all active Python/native leaves
share the root concurrency budget. Waiting parents retain their dependency
boundary without occupying action capacity. Serial execution uses an explicit
session stack. Per-node readiness remains in Rust; the driver allocates capacity
between sessions. Safety of concurrent host-state writes remains the host's
responsibility. The budget does not limit work spawned inside individual actions.

Do not rely on ordering between independent Steps. Declare meaningful ordering
with `wire(after=...)`, edges, or `>>`. Graph manages DAG concurrency, not global
host resources, processes, or transactions.
