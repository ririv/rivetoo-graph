# rivetoo-graph

English | [简体中文](./README.zh-CN.md)

`rivetoo-graph` is an explicit, composable DAG declaration and execution library
with a Python API and a Rust execution core.

It provides steps, subgraphs, conditional dependencies, thread-based and async
execution, result access, execution state, and graph rendering. Logging, terminal
integration, and request-level control belong to the host application and are
connected through explicit interfaces.

## When to use it

- Compose build or automation tasks with explicit dependencies.
- Run independent data-processing or validation tasks concurrently, then join them.
- Reuse subgraphs inside a larger workflow while keeping execution observable.

This is an in-process task graph library, not a distributed workflow service.
Your application owns the actions and integration; the library handles dependency
ordering and execution state.

## Architecture

On `main`, Rust implements declaration compilation, execution-graph validation,
and the scheduling state machine. Python retains graph declarations, arbitrary
callables, result objects, and host integration. Complete Rust-native subgraphs
can join the same execution graph and concurrency budget.

See [Rust architecture](./docs/rust-architecture.md) for implementation boundaries.

## Local development

You need Python 3.13 or later, a Rust toolchain, and maturin. With a virtual
environment activated, install the native extension into that environment:

```bash
maturin develop
```

The native extension is required. There is no pure-Python execution fallback.

## Quick example

```python
from rivetoo_graph import GraphExecution, Step, ThreadGraphRunOptions

prepare = Step(lambda: print("prepare"), label="prepare")
validate = Step(lambda: print("validate"), label="validate")
test = Step(lambda: print("test"), label="test")
finish = Step(lambda: print("finish"), label="finish")

checks = (validate | test).graph(label="checks")
graph = (prepare >> checks >> finish).graph(label="example")
GraphExecution(graph=graph).run(ThreadGraphRunOptions(workers=2))
```

After `prepare`, the `checks` subgraph can run `validate` and `test` concurrently.
`finish` waits for both to succeed; their print order is not guaranteed.

`>>` declares ordering; `|` composes independent branches. Run options determine
actual concurrency. An ordinary action failure blocks dependent work while
unrelated branches continue; the run ultimately reports the failures.

See the [documentation](./docs/index.md) for the model, composition API, and execution rules.

## License

This project is licensed under the [MIT License](./LICENSE).
