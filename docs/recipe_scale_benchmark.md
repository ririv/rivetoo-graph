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

## Issues found during investigation

An earlier comparison measured about 2.88 seconds for the Rust-backed runtime on
the smaller workload, slower than the corresponding Python microbenchmark.
The problem was not the Rust state machine: each session advance rescanned all
compiled nodes to project skip state into native indexes, approaching O(V²) with
many short actions.

Commit `48a0598` built StepId-to-native-index mapping once and then sent only new
skip requests. On the 2,000-leaf microbenchmark, runtime fell from about 2.88 to
1.37 seconds. Moving a state machine does not automatically remove Python overhead;
the adapter must also be batched and incremental.

### Compilation validation complexity

The first large-graph comparison took over 11 seconds to compile on both paths.
Rust compile_declaration took only about 0.1 seconds. Most cost was in Python
GraphCompilation.__post_init__: each composite rescanned all nodes to validate
ownership, taking O(C × V). With 2,000 composites and 14,000 nodes, this produced
about 28 million scans and tens of millions of dataclass equality calls.

The fix built these indexes once:

- StepId → CompiledNode.
- CompiledCompositeRef → set[StepId].
- One frozenset[StepId] per distinct declaration scope.

For V nodes, A direct dependency/child references, and S Steps across distinct
scopes, validation became O(V + A + S) with O(V + S) extra space.

Avoid eagerly evaluating an expensive default:

```python
index.setdefault(key, frozenset(scope.values()))
```

Python evaluates that default even when the key exists. The fix checks membership
first and constructs each scope set only once.

Compilation medians fell from about 11.61 to 0.49 seconds on Rust and from about
11.31 to 1.00 seconds on Python. Profiled function calls dropped from roughly
55.04 million to 1.18 million.

### Duplicate cross-language validation

After that fix, Python GraphCompilation still repeated full structural validation
of Rust-validated graphs. Three related optional fields represented the native
graph and condition/node side tables, admitting incomplete combinations and
obscuring two different construction paths.

An explicit sum type separated them:

- Hand-constructed Python compilations receive full validation before creating
  the native execution graph.
- Rust-compiled inputs provide graph and complete side tables together. Python
  performs O(V) identity and unique-ID checks.

Five-sample A/B measurements reduced compilation from about 0.41 to 0.33 seconds.
Runtime medians of 9.38 and 9.41 seconds differed by about 0.4%, consistent with
thread-scheduling noise. A separate 15-sample compilation-only comparison reduced
the median from 0.3894 to 0.3149 seconds and the mean from 0.3885 to 0.3033 seconds.

That profile measured compile_declaration at about 0.08 seconds and Python side-table
recovery at about 0.39 seconds, including profiler overhead. Remaining Python work
recovers opaque actions, predicates, label paths, observation, and result scopes;
it is not a second topology or scheduling algorithm.

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
