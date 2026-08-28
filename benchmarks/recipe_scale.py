"""测量大规模 recipe 子图的编译与完整执行开销。"""

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


def _recipe_graph(recipe_index: int, *, steps: int) -> Graph:
    wiring = []
    previous: Step | None = None
    for phase_index in range(steps):
        phase = Step(lambda: None, label=f"phase-{phase_index}")
        wiring.append(
            phase.wire(after=() if previous is None else (previous,))
        )
        previous = phase
    return Graph.from_wiring(
        wiring=tuple(wiring),
        label=f"recipe-{recipe_index}",
    )


def _root_graph(*, recipes: int, steps: int, lanes: int) -> Graph:
    components = tuple(
        Step(
            _recipe_graph(recipe_index, steps=steps),
            label=f"recipe-{recipe_index}",
        )
        for recipe_index in range(recipes)
    )
    return Graph.from_wiring(
        label="all-recipes",
        wiring=tuple(
            component.wire(
                after=(
                    ()
                    if recipe_index < lanes
                    else (components[recipe_index - lanes],)
                )
            )
            for recipe_index, component in enumerate(components)
        ),
    )


def _measure(operation: Callable[[], None], *, repeats: int) -> list[float]:
    samples = []
    for _ in range(repeats):
        started = time.perf_counter()
        operation()
        samples.append(time.perf_counter() - started)
    return samples


def _compile_samples(
    graph: Graph,
    *,
    repeats: int,
) -> tuple[GraphCompilation, list[float]]:
    compilation: GraphCompilation | None = None

    def _compile() -> None:
        nonlocal compilation
        compilation = compile_graph(graph, root_label="all-recipes")

    samples = _measure(_compile, repeats=repeats)
    if compilation is None:
        raise RuntimeError("benchmark produced no graph compilation")
    return compilation, samples


def _run(compilation: GraphCompilation, *, workers: int) -> None:
    ParallelGraphExecutor(
        GraphExecutionState(),
        execution_control=GraphExecutionControl(),
    ).execute(compilation, workers=workers)


def _positive(parser: argparse.ArgumentParser, name: str, value: int) -> int:
    if value < 1:
        parser.error(f"--{name} must be >= 1")
    return value


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--implementation", default="current")
    parser.add_argument("--recipes", type=int, default=2_000)
    parser.add_argument("--steps", type=int, default=6)
    parser.add_argument("--lanes", type=int, default=20)
    parser.add_argument("--workers", type=int, default=8)
    parser.add_argument("--repeats", type=int, default=3)
    args = parser.parse_args()

    recipes = _positive(parser, "recipes", args.recipes)
    steps = _positive(parser, "steps", args.steps)
    lanes = _positive(parser, "lanes", args.lanes)
    workers = _positive(parser, "workers", args.workers)
    repeats = _positive(parser, "repeats", args.repeats)
    if lanes > recipes:
        parser.error("--lanes must not exceed --recipes")

    graph = _root_graph(recipes=recipes, steps=steps, lanes=lanes)
    compilation, compile_samples = _compile_samples(graph, repeats=repeats)
    runtime_samples = _measure(
        lambda: _run(compilation, workers=workers),
        repeats=repeats,
    )

    compile_median = statistics.median(compile_samples)
    runtime_median = statistics.median(runtime_samples)
    print(
        f"implementation={args.implementation} recipes={recipes} "
        f"leaves={recipes * steps} composites={recipes} "
        f"lanes={lanes} workers={workers} repeats={repeats}"
    )
    print(f"compile median={compile_median:.6f}s samples={compile_samples}")
    print(f"runtime median={runtime_median:.6f}s samples={runtime_samples}")
    print(f"total median-sum={compile_median + runtime_median:.6f}s")


if __name__ == "__main__":
    main()
