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
- E be its explicit edge count.
- B count all RuntimeCondition.reads references on edges and Steps.
- A be the deduplicated local blocking-arc count, with A ≤ E + B.

Blocking relations come from ordinary edges, conditional-edge sources, and runtime
reads. Conditional edges conservatively participate in declaration-time cycle
checks even if their static conditions later evaluate to false.
Reads is an explicit data dependency, not a relation invented by topological sorting.

The actual [model.py](../src/rivetoo_graph/model.py) path is:

```text
Validate Steps and assign local integer indexes
    ↓
_validated_blocking_arcs: traverse edges/reads and deduplicate Arc values
    ↓
Materialize a blocking_arcs tuple
    ↓
Convert to a list of integer endpoint pairs
    ↓ one batch call
Rust dag_cycle: construct indexes and detect cycles
```

The Arc tuple and integer-pair list are real intermediate allocations; this is not
a zero-intermediate streaming path directly into Rust. Projection, conversion,
indexing, and cycle detection each do linear work. Total time is O(V + E + B);
peak auxiliary space is O(V + A). Multiple representations add constant costs
without changing the asymptotic bound.

With B = 0, time is O(V + E), including runtime conditions that read no results.
Runtime conditions do not inherently add another V-sized loop.

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
composite dependencies, reads, and lowered capability bindings. For N final nodes
and L expanded blocking relations, pure DAG validation is O(N + L) in time and
space. A capability binding can expand to several inputs; count expanded relations,
not just binding objects.

This is not a blanket O(N + L) promise for the entire compiler. Declaration
traversal, hierarchical paths, Python side tables, static predicates, and PyO3
conversion have their own costs. Runtime-generated graphs also require separate
compilation and nested sessions.

## GraphWiring boundaries

GraphWiring.entries and exits are structural DSL boundaries, not named capability
ports or a separately stored topology.

Each calculation reads explicit ordinary/conditional edges, runtime reads, and the
Step order used to return deterministic boundaries. Time is O(V + E + B) and
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
