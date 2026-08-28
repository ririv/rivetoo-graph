"""测量纯 Python 编译器与 compiled scheduler 的固定成本。"""

from __future__ import annotations

import argparse
import statistics
import time
from typing import TYPE_CHECKING

from rivetoo_graph import Graph, Step
from rivetoo_graph.compile import compile_graph
from rivetoo_graph.runtime import (
    GraphExecutionControl,
    GraphExecutionState,
    ParallelGraphExecutor,
)

if TYPE_CHECKING:
    from collections.abc import Callable

    from rivetoo_graph.compile import GraphCompilation


def _graph(nodes: int) -> Graph:
    return Graph(
        steps=tuple(
            Step(lambda: None, label=f"step-{index}") for index in range(nodes)
        )
    )


def _run(compilation: GraphCompilation, *, workers: int) -> None:
    ParallelGraphExecutor(
        GraphExecutionState(),
        execution_control=GraphExecutionControl(),
    ).execute(compilation, workers=workers)


def _measure(operation: Callable[[], None], repeats: int) -> list[float]:
    durations = []
    for _ in range(repeats):
        started = time.perf_counter()
        operation()
        durations.append(time.perf_counter() - started)
    return durations


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--nodes", type=int, default=10_000)
    parser.add_argument("--workers", type=int, default=8)
    parser.add_argument("--repeats", type=int, default=5)
    args = parser.parse_args()

    graph = _graph(args.nodes)
    compilation = compile_graph(graph, root_label="benchmark")
    compile_samples = _measure(
        lambda: compile_graph(graph, root_label="benchmark"),
        args.repeats,
    )
    runtime_samples = _measure(
        lambda: _run(compilation, workers=args.workers),
        args.repeats,
    )

    print(f"nodes={args.nodes} workers={args.workers} repeats={args.repeats}")
    print(
        "compile "
        f"median={statistics.median(compile_samples):.6f}s "
        f"samples={compile_samples}"
    )
    print(
        "runtime "
        f"median={statistics.median(runtime_samples):.6f}s "
        f"samples={runtime_samples}"
    )


if __name__ == "__main__":
    main()
