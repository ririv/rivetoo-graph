# Python graphs and Rust-native graphs

English | [简体中文](./zh-CN/native-graphs.md)

A Python declaration can mount a complete Rust-native subgraph. Both enter one
Rust CompiledExecutionGraph and share dependencies, hierarchy, failure propagation,
and the session's concurrency budget. Native subgraphs are not converted into
Python actions.

```text
Python Graph / Step
    ├── Python LeafAction ──────────┐
    └── Step(NativeGraph)           │
          └── NativeStep / Arc     │
                                   ▼
                      Rust CompiledExecutionGraph
                          ├── Python leaf → Python driver
                          └── Rust leaf   → Rust worker
```

Python holds an opaque NativeGraph, not individually callable NativeStep or
Rust action objects. Native action execution does not require a per-action Python
callback. The mixed driver receives batches of native completion events for
results, logs, and presentation. Internal nodes remain visible in compiled and
execution views; opacity does not hide them as a single undifferentiated action.

## Declaration boundary

Python actions retain their usual declaration:

```python
prepare = Step(prepare_source, label="prepare")
```

A native adapter constructs a complete NativeGraph; Python mounts it:

```python
from rivetoo_ninja import import_clean_build_graph

ninja_graph = import_clean_build_graph(
    "build/build.ninja",
    shell="/bin/sh",
    env=build_env,
)
compile = Step(ninja_graph, label="compile")
graph = (prepare >> compile >> install).graph()
```

This example requires the separate, experimental rivetoo-ninja package.
It is not included with Graph. The importer requires selected outputs to be absent:
it imports clean builds, not Ninja's incremental-build decisions. The host must
explicitly choose this adapter.

Here compile is an outer composite. Rust sees the internal nodes and edges for
global scheduling while preserving the composite boundary.

NativeGraph is the smallest native unit exposed to Python. There is no public
Python NativeAction or ProcessAction API that would create a second per-action
submission path.

## Execution and results

Within one execution session:

- Python leaves are returned in batches to the Python driver, with arbitrary
  callables, Python results, and runtime-returned graphs.
- Rust leaves go directly to Rust workers and a native completion queue.
- The mixed driver receives native start, completion, and failure information.
- Both languages share total capacity; they do not each independently consume the
  full worker limit.

A successful native leaf currently returns None. It cannot read Python
StepRunContext, return arbitrary Python objects, or produce dynamic graphs.
Supporting native results would require an explicit stable result protocol,
not object fields or implicit serialization.

## Rust crates

- action: native action trait, execution context, errors, and workers.
- native-graph: NativeStep, graph-owned arcs, and NativeGraph.
- topology: integer nodes and pure graph algorithms.
- core: language-independent declaration compilation and execution sessions.
- python: integration of Python Graph and complete NativeGraph declarations.

Adapters may implement arbitrary Rust actions, but Rust trait objects are not
passed between dynamic libraries. An independent adapter should provide a stable
batch graph specification, from which the Graph extension constructs NativeGraph
once. This preserves package boundaries without routing each action through Python.
