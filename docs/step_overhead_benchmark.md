# Step overhead benchmark

English | [简体中文](./zh-CN/step_overhead_benchmark.md)

This records the 2026-10-10 A/B measurement of the synchronous-action fast path.
It predates later changes such as failure isolation and is not a claim about
current performance on arbitrary machines. Original measurements are preserved.

## Scope and conclusion

Declaration, compilation, and execution were measured separately to locate fixed
costs for short Python actions. Empty actions expose scheduling overhead, not
compiler, network, or disk throughput.

The A/B comparison isolates return-value handling: the baseline creates an event
loop for every result; the fast path returns ordinary values directly and creates
an event loop only for awaitables. Other topology, scheduling, logging, and worker
settings remain unchanged.

## Environment and method

- Apple M1 Pro, arm64, macOS 26.4 (25E246).
- Python 3.13.5.
- 2026-10-10; the installed Rust extension was not rebuilt between groups.
- Five samples per case, reported as medians; before/after groups ran sequentially.
- One warm-up per microbenchmark case. Execution reused a compilation, with fresh
  execution state for each run.
- No observer, log files, or final execution output; logging was measured separately.

## 1,000 independent empty Steps

| Phase | Before | After |
| --- | ---: | ---: |
| Graph and Step declaration | 5.65 ms | 5.82 ms |
| Compilation | 8.69 ms | 9.04 ms |
| Serial reference driver | 227.21 ms | 16.99 ms |
| Thread driver, 1 worker | 272.10 ms | 51.93 ms |
| Thread driver, 8 workers | 282.28 ms | 39.60 ms |

Declaration and compilation were unchanged; their small differences do not
establish a gain or regression.

GraphPlan.run(ThreadGraphRunOptions(workers=1)) still uses a thread driver, not
the serial reference driver. Task submission, synchronization, and batch
Python/Rust exchange still cost more than an ordinary function call.

## 2,000 recipe subgraphs

The recipe_scale.py workload uses 6 sequential leaves per recipe: 12,000 Python
actions, 2,000 composites, 20 recipe chains, and 8 workers.

| Phase | Before median | After median |
| --- | ---: | ---: |
| Compilation | 0.490070 s | 0.500653 s |
| Execution | 3.365259 s | 0.520759 s |

Execution improved by about 6.46×. This does not imply a 6.46× speedup for real
package builds.

Raw execution samples, in seconds:

```text
before = [3.369867, 3.348461, 3.354630, 3.374089, 3.365259]
after  = [0.546870, 0.525995, 0.520759, 0.520433, 0.515768]
```

## Rivetoo logging integration

An additional measurement used the host's real RivetooGraphActionLog with 1,000
actions in one composite and a temporary component log path. Each action checked
that its log context was bound. The write case appended one line per action and
verified exactly 1,000 lines afterward.

Compilation and log-path binding were excluded from execution time, and final
execution-file output was disabled. In the same process, the benchmark alternated
the old unconditional asyncio.run handling and the new implementation, warmed up
each once, and took five-sample medians. Host logging itself was unchanged.

| Case, 8 workers | Old handling | New handling |
| --- | ---: | ---: |
| Enter/exit real log scope only | 320.92 ms | 56.67 ms |
| Append one log line per action | 459.09 ms | 178.91 ms |

Log-path resolution and file I/O remain. File-system state affects these numbers;
empty-graph improvements are not production-logging speedup guarantees.

## Reproduction

From the Graph repository root, with the current extension installed:

```bash
.venv/bin/python benchmarks/step_overhead.py --nodes 1000 --repeats 5
.venv/bin/python benchmarks/recipe_scale.py \
  --recipes 2000 --steps 6 --lanes 20 --workers 8 --repeats 5
```

The first script separates declaration, compilation, serial execution, and threaded
execution. The second retains nesting and cross-recipe dependencies. From the
Rivetoo parent repository, prefix script paths with rivetoo-graph/.

These commands measure the current checkout and do not automatically restore the
before version. The Rivetoo logging measurement requires that host project; it is
not an integration bundled with standalone Graph.
