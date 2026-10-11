# rivetoo-graph

English | [简体中文](./README.zh-CN.md)

`rivetoo-graph` is an explicit, composable DAG declaration and execution library
with a Python API and a Rust execution core.

It provides steps, subgraphs, conditional dependencies, thread-based and async
execution, result access, execution state, and graph rendering. Logging, terminal
integration, and request-level control belong to the host application and are
connected through explicit interfaces.

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
from rivetoo_graph import GraphPlan, Step

prepare = Step(lambda: print("prepare"), label="prepare")
finish = Step(lambda: print("finish"), label="finish")
graph = (prepare >> finish).graph()
GraphPlan(label="example", graph=graph).run()
```

`>>` declares ordering; `|` composes independent branches. Run options determine
actual concurrency. An ordinary action failure blocks dependent work while
unrelated branches continue; the run ultimately reports the failures.

See the [documentation](./docs/index.md) for the model, composition API, and execution rules.

## License

This project is licensed under the [MIT License](./LICENSE).
