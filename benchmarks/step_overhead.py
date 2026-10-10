"""分离测量空 Step 的声明、编译和执行成本，不代表真实构建吞吐量。"""

from __future__ import annotations

import argparse
import statistics
import time
from functools import partial
from typing import TYPE_CHECKING

from rivetoo_graph import Graph, Step
from rivetoo_graph.compile import compile_graph
from rivetoo_graph.runtime import (
    GraphExecutionControl,
    ParallelGraphExecutor,
    SerialGraphExecutor,
)

if TYPE_CHECKING:
    from collections.abc import Callable

    from rivetoo_graph.compile import GraphCompilation


def _noop() -> None:
    pass


def _declare(nodes: int) -> Graph:
    return Graph(steps=tuple(Step(_noop, label=f"step-{i}") for i in range(nodes)))


def _execute_threads(compilation: GraphCompilation, workers: int) -> object:
    return ParallelGraphExecutor(execution_control=GraphExecutionControl()).execute(
        compilation, workers=workers
    )


def _measure(name: str, operation: Callable[[], object], repeats: int) -> None:
    operation()  # 预热导入与执行路径，不计入样本。
    samples: list[float] = []
    for _ in range(repeats):
        start = time.perf_counter()
        operation()
        samples.append(time.perf_counter() - start)
    print(
        f"{name}: median={statistics.median(samples):.6f}s "
        f"samples={[round(value, 6) for value in samples]}",
        flush=True,
    )


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--nodes", type=int, default=1_000)
    parser.add_argument("--repeats", type=int, default=5)
    parser.add_argument("--workers", type=int, nargs="+", default=[1, 8])
    args = parser.parse_args()
    if min(args.nodes, args.repeats, *args.workers) < 1:
        parser.error("nodes, repeats and workers must be positive")
    print(f"nodes={args.nodes} repeats={args.repeats}")
    _measure("declaration", lambda: _declare(args.nodes), args.repeats)
    graph = _declare(args.nodes)
    _measure(
        "compilation", lambda: compile_graph(graph, root_label="bench"), args.repeats
    )
    compilation = compile_graph(graph, root_label="bench")
    _measure(
        "serial execution",
        lambda: SerialGraphExecutor().execute(compilation),
        args.repeats,
    )
    for workers in args.workers:
        _measure(
            f"thread execution workers={workers}",
            partial(_execute_threads, compilation, workers),
            args.repeats,
        )


if __name__ == "__main__":
    main()
