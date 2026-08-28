"""比较正式批量驱动与私有 Tokio/PyO3 回调实验。

该基准刻意使用极短 Python action，使跨语言调度开销成为主要成本；真实构建
action 更重时，两种驱动的相对差距会被进一步稀释。它不是正式 API 的一部分。
"""

from __future__ import annotations

import argparse
import statistics
import time
from typing import TYPE_CHECKING

from rivetoo_graph import Graph, Step
from rivetoo_graph._native import _experimental_run_tokio_callbacks
from rivetoo_graph.compile import compile_graph
from rivetoo_graph.runtime.batch_driver import run_threaded_session
from rivetoo_graph.runtime.compiled import CompiledExecutionHost
from rivetoo_graph.runtime.state import GraphExecutionState

if TYPE_CHECKING:
    from collections.abc import Callable

    from rivetoo_graph.compile import GraphCompilation


def _compilation(nodes: int) -> GraphCompilation:
    graph = Graph(
        steps=tuple(Step(lambda: None, label=f"step-{index}") for index in range(nodes))
    )
    return compile_graph(graph, root_label="benchmark")


def _host(compilation: GraphCompilation) -> CompiledExecutionHost:
    return CompiledExecutionHost(
        compilation,
        GraphExecutionState(),
        observer=None,
        run_leaf=lambda _step: None,
    )


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
    compilation = _compilation(args.nodes)

    batch = _measure(
        lambda: run_threaded_session(
            compilation._native_graph,
            _host(compilation),
            workers=args.workers,
        ),
        args.repeats,
    )
    tokio = _measure(
        lambda: _experimental_run_tokio_callbacks(
            compilation._native_graph,
            _host(compilation),
            args.workers,
        ),
        args.repeats,
    )

    batch_median = statistics.median(batch)
    tokio_median = statistics.median(tokio)
    print(f"nodes={args.nodes} workers={args.workers} repeats={args.repeats}")
    print(f"batch median={batch_median:.6f}s samples={batch}")
    print(f"tokio median={tokio_median:.6f}s samples={tokio}")
    print(f"tokio/batch={tokio_median / batch_median:.3f}x")


if __name__ == "__main__":
    main()
