"""比较静态工作流与动态子图的执行耗时和实际并发，可对旧版运行。"""

from __future__ import annotations

import argparse
import json
import statistics
import threading
import time
from dataclasses import dataclass, field

from rivetoo_graph import (
    AsyncGraphRunOptions,
    Graph,
    GraphExecution,
    Step,
    ThreadGraphRunOptions,
)


@dataclass
class Activity:
    active: int = 0
    peak: int = 0
    completed: int = 0
    delay: float = 0.001
    lock: threading.Lock = field(default_factory=threading.Lock)

    def work(self) -> None:
        with self.lock:
            self.active += 1
            self.peak = max(self.peak, self.active)
        try:
            if self.delay:
                time.sleep(self.delay)
        finally:
            with self.lock:
                self.active -= 1
                self.completed += 1


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--implementation", default="current")
    parser.add_argument("--executor", choices=("threads", "async"), default="threads")
    parser.add_argument("--width", type=int, default=3)
    parser.add_argument("--depth", type=int, default=3)
    parser.add_argument("--leaves", type=int, default=4)
    parser.add_argument("--capacity", type=int, default=8)
    parser.add_argument("--delay", type=float, default=0.001)
    parser.add_argument("--repeats", type=int, default=7)
    args = parser.parse_args()
    if (
        min(args.width, args.leaves, args.capacity, args.repeats) < 1
        or args.depth < 0
        or args.delay < 0
    ):
        parser.error(
            "width, leaves, capacity, repeats must be positive; depth and delay must be nonnegative"
        )
    samples: list[float] = []
    peaks: list[int] = []
    expected = args.width**args.depth * args.leaves
    for repeat in range(args.repeats + 1):
        activity = Activity(delay=args.delay)

        def branch(depth: int, activity: Activity = activity) -> Graph:
            if depth == 0:
                return Graph(
                    steps=tuple(Step(activity.work) for _ in range(args.leaves))
                )
            return Graph(
                steps=tuple(Step(lambda: branch(depth - 1)) for _ in range(args.width))
            )

        graph = branch(args.depth)
        options = (
            AsyncGraphRunOptions(concurrency=args.capacity)
            if args.executor == "async"
            else ThreadGraphRunOptions(workers=args.capacity)
        )
        start = time.perf_counter()
        GraphExecution(graph).run(options)
        elapsed = time.perf_counter() - start
        if activity.completed != expected:
            raise RuntimeError(
                f"expected {expected} completed actions, got {activity.completed}"
            )
        if repeat:
            samples.append(elapsed)
            peaks.append(activity.peak)
    print(
        json.dumps(
            {
                "implementation": args.implementation,
                "executor": args.executor,
                "width": args.width,
                "depth": args.depth,
                "leaves": expected,
                "capacity": args.capacity,
                "delay": args.delay,
                "median_seconds": statistics.median(samples),
                "samples_seconds": samples,
                "peak_actions": peaks,
            }
        )
    )


if __name__ == "__main__":
    main()
