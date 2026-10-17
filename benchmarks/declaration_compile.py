"""测量声明编译；不执行 action，不把构图耗时算入编译样本。"""

from __future__ import annotations

import argparse
import gc
import json
import statistics
from time import perf_counter
from typing import Literal

from rivetoo_graph import (
    Graph,
    StaticCondition,
    StaticConditions,
    Step,
    UnconditionalEdge,
    compile_graph,
    runtime_condition,
)

type Case = Literal["flat", "subgraphs", "pruned", "runtime"]


def declaration(case: Case, recipes: int, steps: int) -> Graph:
    def noop() -> None:
        pass

    if case == "flat":
        leaves = tuple(Step(noop) for _ in range(recipes * steps))
        return Graph(steps=leaves)
    children: list[Step] = []
    for _ in range(recipes):
        leaves = tuple(Step(noop) for _ in range(steps))
        if case == "runtime":
            leaves = (
                leaves[0],
                Step(
                    noop,
                    when=runtime_condition(reads=(leaves[0],), when=lambda _ctx: True),
                ),
                *leaves[2:],
            )
        child = Graph(
            steps=leaves,
            edges=tuple(
                UnconditionalEdge(left, right)
                for left, right in zip(leaves, leaves[1:], strict=False)
            ),
        )
        children.append(child.step())
    static = ()
    if case == "pruned":
        static = (
            StaticConditions(
                evaluator=lambda condition: condition.payload is True,
                conditions={
                    child: StaticCondition(payload=False, description="disabled")
                    for child in children[::2]
                },
            ),
        )
    return Graph(steps=tuple(children), static=static)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--recipes", type=int, default=2000)
    parser.add_argument("--steps", type=int, default=12)
    parser.add_argument("--repeats", type=int, default=9)
    args = parser.parse_args()
    if args.recipes < 1 or args.steps < 2 or args.repeats < 1:
        parser.error("recipes/repeats must be positive and steps must be >= 2")
    for case in ("flat", "subgraphs", "pruned", "runtime"):
        graph = declaration(case, args.recipes, args.steps)
        compiled = compile_graph(graph, root_label="benchmark")
        samples: list[float] = []
        for _ in range(args.repeats):
            # 每轮先清理上一轮投影，避免两份投影同时存活和 GC 时机影响比较。
            del compiled
            gc.collect()
            started = perf_counter()
            compiled = compile_graph(graph, root_label="benchmark")
            samples.append((perf_counter() - started) * 1000)
        print(
            json.dumps(
                {
                    "case": case,
                    "recipes": args.recipes,
                    "steps": args.steps,
                    "compiled_leaves": len(compiled.steps),
                    "samples_ms": samples,
                    "median_ms": statistics.median(samples),
                }
            ),
            flush=True,
        )


if __name__ == "__main__":
    main()
