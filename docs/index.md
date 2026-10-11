# rivetoo-graph

English | [简体中文](./zh-CN/index.md)

`rivetoo-graph` is an explicit, composable DAG declaration and execution library:

```python
from rivetoo_graph import Graph, GraphPlan, Step
```

## Features

- Directed topology and DAG validation.
- Step and Graph declarations with explicit wiring.
- Sequential `>>` and independent `|` composition.
- Exactly-one static selection before graph construction.
- Static conditions, runtime conditions, and conditional edges.
- Subgraph boundaries and capability ports.
- One Rust compilation/execution path with dynamic and compiled presentation modes.
- Thread-based and async execution drivers.
- Execution control, results, observation events, and rendering.

## Design goals

`Graph` describes structure; `GraphPlan` owns one execution scope. Declarations
refer directly to Step objects. Internal `StepId` values are assigned to mounted
nodes, while labels serve presentation only.

Dependencies, condition reads, and capability bindings participate explicitly in
topology validation. Composition preserves subgraph boundaries rather than
silently flattening declarations. Host logging, terminals, request state, and
domain workflows are connected explicitly, not read from implicit global facilities.

## Documentation

- [Core model](./core_model.md)
- [Topology complexity and implementation costs](./topology_complexity.md)
- [Execution semantics](./execution.md)
- [Python graphs and Rust-native graphs](./native-graphs.md)
- [Conditional edges](./conditional_edges.md)
- [Conditional steps](./conditional_steps.md)
- [Capability dependencies and subgraph ports](./capabilities.md)
- [Rust architecture and language boundaries](./rust-architecture.md)
- [Recipe-scale benchmark](./recipe_scale_benchmark.md)
- [Step overhead benchmark](./step_overhead_benchmark.md)

## Core boundaries

| Type or field | Responsibility |
| --- | --- |
| `Graph` | Declarative execution DAG |
| `GraphPlan` | A declaration and one execution scope |
| `Step` | Optional label and condition; Python action, Python subgraph, or Rust-native subgraph |
| `Graph.edges` | Explicit wiring |
| `RuntimeCondition.reads` | Result dependencies required before evaluating a condition |
| `StepWiring` / `GraphWiring` | Declaration-time composition |
| `GraphCompilation` | Runtime execution structure and composite boundaries |

## Outside Graph core

Graph core does not know about domain entities, build phases, caches, artifacts,
configuration, working directories, CLI requests, log directories, or process-level
signal handling. Hosts model these concerns and compose graphs or connect public
interfaces. There are no implicit branches for a particular host application.

## Current limitations

- A graph returned by an action creates a nested session. It does not yet share
  the outer session's global concurrency budget.
- `GraphExecutionState` is single-use, with no reset or reuse operation.
- The runtime does not aggregate host-domain results.
- Runtime-generated subgraphs cannot be compiled in advance. Once returned, they
  enter the same Rust compiler used for statically declared graphs.

## Languages

English is the default. Chinese pages live in [zh-CN/](./zh-CN/index.md), with the
same filenames and a language link on each page. Update both versions when
changing API semantics, examples, or limitations. Historical benchmark samples
remain tied to their recorded dates and workloads.
