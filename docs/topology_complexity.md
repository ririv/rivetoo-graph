# Topology complexity and implementation costs

English | [简体中文](./zh-CN/topology_complexity.md)

Python's generic topology API, execution Graph declaration validation, and final
Rust DAG validation are distinct paths. These bounds exclude actions, host
predicates, and log I/O. Python hash-table operations assume average O(1);
user-defined hash/equality costs are additional.

## Two pure-topology entry points

| Entry point | Implementation | Purpose |
| --- | --- | --- |
| rivetoo_graph.topology | Python, with graphlib for DAG checks | Generic hashable domain nodes |
| crates/topology | Rust, integer nodes and adjacency indexes | Execution declaration and compiled-DAG validation |

Python DirectedTopology is not a per-method FFI wrapper. Execution Graph does not
construct a Python DirectedTopology before sending it to Rust.

## Python topology

For V nodes and A input arcs:

- DirectedTopology accepts iterables and normalizes nodes, arcs, and both adjacency
  indexes. Each arc is validated and inserted into predecessor/successor indexes
  during the same consumption pass.
- Input arcs are not automatically deduplicated.
- Construction time and retained space are O(V + A).
- predecessors_by_node and successors_by_node return existing read-only mappings.
- reachable_from first validates roots, then follows successors. Time is
  O(V + R + Aᵣ), where R is the number of input roots and Aᵣ counts reachable arcs.
  It still constructs the complete node set, so a small closure query is not
  solely proportional to the reachable nodes.
- The pending stack can contain repeated endpoints. Worst-case auxiliary space
  is O(V + A + R).

DAGTopology composes DirectedTopology and calls graphlib.TopologicalSorter.prepare
at construction. Validation takes O(V + A) time and auxiliary space.
topological_order is a separate O(V + A) operation; construction does not cache
the ordering.

## Execution Graph declaration validation

For one Graph level, let:

- V be its Step count.
- E be its ordinary, conditional and result edge count.
- A be the deduplicated local blocking-arc count, with A ≤ E.

Blocking relations come exclusively from explicit edges. Conditional edges conservatively participate in declaration-time cycle
checks even if their static conditions later evaluate to false.
Reads is an explicit data dependency, not a relation invented by topological sorting.

The actual [model.py](../src/rivetoo_graph/model.py) path is:

```text
Validate Steps and assign local integer indexes
    ↓
_validated_blocking_arcs: traverse edges and deduplicate Arc values
    ↓
Materialize a blocking_arcs tuple
    ↓
Convert to a list of integer endpoint pairs
    ↓ one batch call
Rust dag_cycle: construct indexes and detect cycles
```

The Arc tuple and integer-pair list are real intermediate allocations; this is not
a zero-intermediate streaming path directly into Rust. Projection, conversion,
indexing, and cycle detection each do linear work. Total time is O(V + E);
peak auxiliary space is O(V + A). Multiple representations add constant costs
without changing the asymptotic bound.

Result argument slot validation is also O(V + E). Callable signature validation
additionally depends on the number of parameters.

StaticConditions adds S explicit node/edge rules. When present, their membership
validation and lookup index cost O(V + E + S) time and space, independently of
predicate evaluation. With no static groups, this index path is O(1), and the Rust
collector skips per-node/per-edge static lookups. Each lookup with rules is O(1)
on average; evaluator cost depends on the host expression. The declaration remains
validated before pruning, so a false static rule cannot legalize a structural cycle.

## Capabilities and global compilation

CapabilityBinding validates components and ports at declaration time, but does not
become a local blocking arc between composites. The real dependency is from an
internal provider output to an internal consumer input, located and lowered during
compilation.

Interface ownership checks traverse mounted Python subgraphs once, index Step
mount counts, and validate references. For M mounted nodes and R port references,
this costs O(M + R) time and O(M) space. M is not merely the local V above.
Ambiguous multiply mounted references are errors.

The Rust compiler validates the complete DAG, including known subgraphs,
composite dependencies, inputs, and lowered capability bindings. For N final nodes
and L expanded blocking relations, pure DAG validation is O(N + L) in time and
space. A capability binding can expand to several inputs; count expanded relations,
not just binding objects.

This is not a blanket O(N + L) promise for the entire compiler. Declaration
traversal, hierarchical paths, Python side tables, static predicates, and PyO3
conversion have their own costs. Runtime-generated graphs also require separate
compilation and nested sessions.

## Static projection and identity-preserving compilation

In `crates/core/src/declaration.rs`, active graph discovery indexes active composites
by their containing graph once. For G graph scopes, V declared nodes and C composites,
time is O(G + V), auxiliary space O(G + C). The previous per-graph scan of every node
was O(G × V) in the worst case.

After validation and reachability, compilation chooses its path from the complete
declaration, not from the root Python Graph.static field:

- If all scopes are reachable, all nodes are active, and all runtime conditions are
  used, IDs stay unchanged. Conditions and capability bindings move directly into
  hierarchy lowering without compact mappings or copies of condition inputs.
- Otherwise, projection removes inactive nodes/scopes and unused conditions and
  remaps their references. Inactive ancestors still remove their descendants.

Both paths use the same hierarchy lowering, capability lowering, DAG validation and
execution indexes. False static edges may be filtered without changing node IDs.
Runtime predicates still run only during execution. The identity test costs
O(V + E + K), with K runtime conditions, or O(V) when K = 0.

## Declaration compilation measurements

Measured on 2026-10-17: Apple M1 Pro, macOS 26.4, Python 3.13.5, Rust
1.96.0-beta.5, release extension. Baseline is d387a20. Each variant ran in a
separate process; three rounds rotated variant order, with one warmup and five timed
samples per case per process. Values below are medians of 15 samples, in ms.
Each sample includes the Rust collector/compiler and Python result materialization,
but excludes graph construction, GC before the sample, and action execution.

| Shape | Before | Indexed traversal only | Indexed + identity path |
| --- | ---: | ---: | ---: |
| 24,000 independent leaves in one graph | 113.574 | 113.529 | 111.969 |
| 2,000 subgraphs × 12 sequential leaves | 216.738 | 174.329 | 170.860 |
| Same hierarchy, half the subgraphs pruned | 136.495 | 114.688 | 114.300 |
| Same hierarchy, one runtime condition per subgraph | 261.666 | 219.084 | 214.980 |

Subgraph cases include another 2,000 composite nodes. The pruning case executes
neither actions nor predicates at runtime; 12,000 leaves survive compilation.
Most improvement comes from indexed traversal; the identity path adds roughly 2%
for the full hierarchy. A separate 20 × 12 check remained around 1–2 ms, with changes
of tens of microseconds; it does not establish a small-graph speedup. No memory
usage or real recipe build speedup is claimed.

Reproduce with `python benchmarks/declaration_compile.py --repeats 5` against each
release extension. [Raw samples](../benchmarks/results/declaration_compile_2026-10-17.json)
include the small-graph check. Reusing the same declaration does not reuse a compiled
graph: every timed invocation recompiles it.

## GraphWiring boundaries

GraphWiring.entries and exits are structural DSL boundaries, not named capability
ports or a separately stored topology.

Each calculation reads explicit ordinary, conditional and result edges, and the
Step order used to return deterministic boundaries. Time is O(V + E) and
auxiliary space O(V).

Entries and exits are independent properties, so reading both calculates both.
Sequential composition usually reads the left exits and right entries.
Immutable declarations currently carry no mutable boundary cache.

## Interpreting performance

Rust does not lower the asymptotic complexity of these linear algorithms.
Benefits come from compact integer indexes, native loops, and batch scheduling;
costs still include Python traversal, temporary objects, and conversions.
“Uses Rust” does not imply every small graph is faster, and O(V + E) does not
mean exactly one loop.

Historical measurements are recorded in the
[recipe-scale benchmark](./recipe_scale_benchmark.md) and
[Step overhead benchmark](./step_overhead_benchmark.md), with their own dates and
workload boundaries.
