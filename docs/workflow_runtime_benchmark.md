# Workflow runtime and concurrency benchmark

English | [简体中文](./zh-CN/workflow_runtime_benchmark.md)

The 2026-10-16 measurements compare commit `d6b056d` with the working-tree changes
for asynchronous entry, shared dynamic-session capacity, step events, and
cooperative cancellation. Both Python and Rust baseline code were extracted and
built separately; the main checkout was not switched. These are local scheduling
measurements, not real software builds or model-request benchmarks.

## Method

- Apple M1 Pro, macOS 26.4 (25E246), Python 3.13.5, release Rust extension.
- Empty-step cases: nine samples after warm-up, median runtime, reused compilation.
- Recipe scale: 2,000 static subgraphs, six Steps each, 20 dependency lanes, eight
  workers; nine samples with fresh execution state.
- Dynamic case: branching width three, depth three, 108 leaf actions sleeping
  1 ms, requested capacity eight; seven samples after warm-up. Runtime includes
  dynamic graph construction and compilation.
- No event subscription, action log, or execution-file output. Groups ran
  sequentially; occasional scheduling outliers remain in the samples.

## Static workflow results

| Runtime case | Baseline | Changed |
| --- | ---: | ---: |
| 3,000 empty Steps, serial | 26.242 ms | 26.230 ms |
| 3,000 empty Steps, one worker | 85.437 ms | 84.976 ms |
| 3,000 empty Steps, eight workers | 101.498 ms | 103.097 ms |
| 2,000 recipes / 12,000 Steps, eight workers | 414.304 ms | 419.790 ms |

The eight-worker medians increased about 1.6% and 1.3%, respectively. Serial and
one-worker medians were approximately unchanged. These numbers expose costs for
very short actions and do not establish a universal regression bound. No full
recipe build was performed for this comparison.

## Dynamic workflow results

| Driver | Baseline median | Changed median | Baseline peak actions | Changed peak |
| --- | ---: | ---: | ---: | ---: |
| Threads | 26.575 ms | 25.002 ms | 23–34 | 8 |
| Async | 22.403 ms | 29.590 ms | 14 | 8 |

The old implementation exceeded the requested capacity. The changed implementation
respects the global limit, so these are not equal-concurrency throughput comparisons.
The async workload takes longer while using fewer simultaneous actions. Mixed
Python/native budget correctness is additionally checked by integration tests.

## Reproduction

From the Rivetoo checkout, with its virtual environment:

```bash
.venv/bin/python rivetoo-graph/benchmarks/step_overhead.py \
  --nodes 3000 --repeats 9 --workers 1 8
.venv/bin/python rivetoo-graph/benchmarks/recipe_scale.py \
  --recipes 2000 --steps 6 --lanes 20 --workers 8 --repeats 9
.venv/bin/python rivetoo-graph/benchmarks/workflow_runtime.py --repeats 7
.venv/bin/python rivetoo-graph/benchmarks/workflow_runtime.py --executor async --repeats 7
```

The workflow script also runs with the baseline Python package and extension. Use
an isolated baseline installation or source snapshot through `PYTHONPATH`; the
script does not change branches or install a baseline automatically. It prints
every duration sample and observed concurrency peak as JSON. The other scripts
print their raw samples as well.
