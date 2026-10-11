# Rust architecture and language boundaries

English | [简体中文](./zh-CN/rust-architecture.md)

The public declaration API is Python. Production execution uses a Rust declaration
compiler and scheduling state machine. Rust owns the compiled graph, scheduling
indexes, and session state; Python retains arbitrary callables, results, and host
integration.

## Crate boundaries

- `crates/action` defines NativeAction, stable errors, and process actions without
  Python callbacks. It does not depend on PyO3, Tokio, or Graph core.
- `crates/native-graph` owns NativeStep, graph-owned arcs, and NativeGraph.
  Adapters connect complete graphs rather than exposing individual actions to Python.
- `crates/topology` uses only the Rust standard library for integer nodes,
  adjacency indexes, and DAG algorithms. It knows nothing about actions or domains.
- `crates/core` (package `rivetoo-graph`) depends on the workspace topology crate,
  not third-party libraries. It owns declaration compilation, capability resolution,
  the compiled execution graph, and scheduling.
- `crates/python` is the private PyO3 adapter. It converts declarations into integer
  IDs in batches, retains Python action/predicate/observation side tables, connects
  host control, and dispatches native actions to their Rust implementation.
- `crates/tokio-runtime` is a private experiment, not production API. It contains
  the Tokio dependency and measures per-action callbacks into Python through the
  project's CallbackHost interface.

PyO3 and third-party runtime types do not appear in core's public data model.

## External-library boundary

External graph, scheduling, or concurrency implementations belong in separate
adapter crates. An adapter owns the dependency and connects through project-defined
interfaces:

```text
core scheduler interface
        ↑ project-owned model
executor adapter
        ↓
third-party runtime
```

Third-party node, task, error, and executor types must not leak into Graph's public
model. Replacing an adapter should not require changing graph declarations.

## Production execution

All production paths share the same compiler and session implementation:

```text
Python Graph
    ↓ one PyO3 compilation call
Rust GraphDeclaration
    ├── static pruning
    ├── hierarchy and declaration scopes
    ├── explicit capability-port location
    └── conditions and prerequisites
    ↓
Rust CompiledExecutionGraph
    ├── hierarchy lowering
    ├── contiguous NodeId / GraphId
    ├── membership → owners / roots / children
    ├── reads, prerequisite, and dependent indexes
    ├── capability binding lowering
    └── DAG validation
    ↓
Rust ExecutionSession
    ├── Python leaves → batched Python driver
    │                    ├── arbitrary callables
    │                    ├── ThreadPoolExecutor
    │                    └── asyncio tasks
    └── native leaves → Rust workers → completion queue
```

Production runtime does not call back into Python per action. Static condition
leaves do call Python during compilation. At runtime, start/advance exchanges
batches when state changes:

- Ready Python actions, predicates to evaluate, and lifecycle events go to Python.
- Native action handles remain in Rust after graph registration.
- Python completions, predicate results, and explicit skips return in batches.
- Native completions are collected in batches and advance the same session.
- There is no timer-based polling loop; idle drivers wait for actual work completion.

Known subgraphs compile as composites and leaves without losing their boundaries.
A graph returned by an action is compiled later with the current path and ancestor
scopes, in a nested session. Nested sessions currently do not share the outer
session's global concurrency budget.

Dynamic and compiled modes retain different presentation/composite-recording
behavior, not different execution engines.

## Runtime experiment

The private `crates/tokio-runtime` and `benchmarks/runtime_drivers.py` compare
batched execution with per-action Tokio/PyO3 callbacks. They are not production
APIs. Historical measurements are recorded in the
[benchmark notes](./recipe_scale_benchmark.md#runtime-driver-experiment).

## Single sources of truth

Lowering receives graph scopes and node membership rather than independent owner,
roots, and children declarations:

- Scope: Root or OwnedBy(NodeId).
- Node: Leaf or Composite with child_graph.
- Every node belongs to one GraphId.

Rust validates parent/child graph consistency, derives owners from membership, and
derives roots and composite children from owners. Capability bindings are separate
inputs lowered into ordinary prerequisites. Python presentation recovers final
prerequisites rather than implementing another rewrite rule.

Prerequisites are unconditional or conditional variants. Runtime conditions use
ConditionId; predicates remain in Python side tables, while core records reads.
Core can therefore detect blocking and cycles without interpreting Python logic.

## Domain boundary

Package selection, versions, options, and platform predicates belong to the host,
not the execution core. Python's generic topology API and the Rust topology crate
are separate implementations; see [topology complexity](./topology_complexity.md).

## Python boundary

Capability resolution validates Python port ownership, encodes components,
capabilities, requirements, and selections as integers, and calls Rust once.
Provider indexing, uniqueness, explicit selection, and duplicate/mixed requirement
validation have no second Python solver.

Python retains host-facing objects:

- Static leaf evaluators and runtime predicates.
- Actions, results, observation, labels, and log paths.
- Compilation side tables for actions, predicates, observations, paths, and scopes.
- Execution state snapshots, explicit skips, and results.

Native actions form a separate branch owned by NativeGraph. Successful native
results are currently None, not arbitrary Python objects.

Readiness, scheduling states, condition blocking, hierarchy lifecycle, execution
DAG validation, provider resolution, and binding lowering have one Rust
implementation. Wrapping every remaining Python host object in a native object
would add conversions rather than remove another scheduler.
