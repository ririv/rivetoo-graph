# Recipe-scale graph benchmark

English | [简体中文](./zh-CN/recipe_scale_benchmark.md)

This is a historical measurement of the 2026-08-28 implementation, not current
mainline performance. Later synchronous-action improvements are covered in the
[Step overhead benchmark](./step_overhead_benchmark.md). Raw samples are preserved.

## Purpose

The experiment asks whether moving compilation and scheduling to Rust still helps
when one request contains thousands of recipes, each with several Steps.

It is not a build-throughput benchmark. All actions are empty, deliberately
excluding compilers, disks, networks, and subprocesses to expose graph compilation,
language-boundary coordination, and scheduling costs. These ratios must not be
applied directly to real build times.

## Graph shape

- 2,000 recipe subgraphs.
- 6 sequential leaves per recipe: 12,000 Python actions.
- One boundary-preserving composite per recipe: 2,000 composites.
- 20 inter-recipe chains, allowing up to 20 recipe-level lanes.
- 8 thread workers.
- 5 compilation and 5 execution samples, reported as medians.

This is a reproducible synthetic hierarchical DAG, not a snapshot of a real
repository. It retains recipe boundaries, internal phases, and cross-recipe
dependencies, unlike a benchmark containing only 2,000 independent Steps.

## Environment

- Apple M1 Pro, arm64.
- macOS 26.4 (25E246).
- Python 3.13.5.
- rustc 1.96.0-beta.5.
- 2026-08-28.

## Results

| Implementation | Compilation median | Runtime median | Sum of medians |
| --- | ---: | ---: | ---: |
| Pure Python | 0.915015s | 13.388637s | 14.303652s |
| Rust state machine + Python actions | 0.333768s | 9.413393s | 9.747161s |

Raw samples:

```text
pure-python compile = [0.9050715418998152, 0.9029900829773396,
                       0.915015000035055, 0.9332317499211058,
                       0.9470305419526994]
pure-python runtime = [13.388636792078614, 13.366689124959521,
                       13.345061874948442, 14.904916667030193,
                       14.302743416046724]

rust-python compile = [0.2818566670175642, 0.3337678749812767,
                       0.34090887499041855, 0.2765289170201868,
                       0.3360874579520896]
rust-python runtime = [9.41339295799844, 9.493625707924366,
                       9.459358749911189, 9.329700125032105,
                       9.271784832933918]
```

For this workload, the Rust path was approximately:

- 1.42× faster in runtime, reducing median time by about 30%.
- 2.74× faster in compilation, reducing median time by about 64%.
- 1.47× faster by the sum of medians, reducing that figure by about 32%.

The sum illustrates the approximate fixed cost of one compilation and one run.
It is not the median of per-sample total durations.

## Runtime driver experiment

A separate 2026-08-28 experiment compared batched Python execution with a private
Tokio driver calling Python once per action. On CPython 3.13 with the GIL,
macOS arm64, and a release extension, 2,000 independent no-op actions with
8 workers and 5 samples produced these medians:

| Driver | Median |
| --- | ---: |
| Batch | approximately 0.321 s |
| Tokio callback | approximately 1.280 s |

The callback/batch ratio was approximately 3.98. This is a historical scheduling
microbenchmark, not the recipe-scale workload above, and does not establish that
all Rust runtimes are slower. The experiment lives in `crates/tokio-runtime`
and `benchmarks/runtime_drivers.py`; production drivers do not depend on it.

## Reproduction

From the Graph repository root, use an environment with the native extension
installed. These commands measure the checked-out version, not the historical
one. `--implementation` labels results; it does not select an implementation.

```bash
PYTHONPATH=src .venv/bin/python benchmarks/recipe_scale.py \
  --implementation rust-python \
  --recipes 2000 \
  --steps 6 \
  --lanes 20 \
  --workers 8 \
  --repeats 5
```

The script uses public Python APIs. To compare the preserved Python branch, point
PYTHONPATH to that checkout while running the mainline script:

```bash
PYTHONPATH=/path/to/python-checkout/src \
  .venv/bin/python benchmarks/recipe_scale.py \
  --implementation pure-python \
  --recipes 2000 \
  --steps 6 \
  --lanes 20 \
  --workers 8 \
  --repeats 5
```

Replace /path/to/python-checkout with the actual Python-branch checkout. Verify
that imports come from it rather than the installed mainline package. Exact
historical reproduction also requires the corresponding code and tool versions.

## Interpretation limits

- Empty actions amplify fixed graph costs, not real recipe action distributions.
- Twenty chains do not reproduce a real repository's fan-in, fan-out, or critical path.
- The test excludes dependency resolution, downloads, caches, subprocesses, file
  systems, and build tools.
- Python still recovers opaque host objects. After fixing accidental complexity,
  further migration should be justified by new profiles and real workloads.
- These results record an engineering decision on the stated date, machine, and
  versions, not a cross-version performance guarantee.
