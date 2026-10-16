# Conditional steps

English | [简体中文](./zh-CN/conditional_steps.md)

## Purpose

Conditional edges control whether a dependency applies. `Step.when` controls
whether the Step itself participates. It works for Python leaves, Python Graph
composites, and NativeGraph composites.

## Declaration

Defined in [model.py](../src/rivetoo_graph/model.py):

```python
Step(action=..., label=..., when=...)
```

Label is optional presentation. `when` accepts
`RuntimeCondition | None`, not bool.
Wiring belongs to `Step.wire(after=...)` and Graph.edges, not to Step itself.

## Semantics

### No condition

`when=None` leaves the Step enabled.

### Static condition

Static rules belong to StaticConditions, not Step.when:

```python
static = StaticConditions(evaluator=evaluate_target_condition)
graph = (prepare >> static.when(is_android, android) >> build).graph()
child_graph = static.when(is_android, android_graph).graph()
```

False removes the node and its incident edges during compilation, without connecting
its predecessors to successors. The declaration stays unchanged and remains
inspectable. The child Graph is constructed first; this API does not defer Python
graph construction.

Without the DSL:

```python
graph = Graph(
    steps=(android, checks, build),
    edges=(
        UnconditionalEdge(source=android, target=build),
        UnconditionalEdge(source=checks, target=build),
    ),
    static=StaticConditions(
        evaluator=evaluate_target_condition,
        conditions={
            android: is_android,
            (checks, build): wait_for_checks,
        },
    ),
)
```

Rules reference declared nodes or edges. One mapping covers both, without a second
Step or Graph type. Evaluation happens only during compilation; declarations can be
compiled again. Evaluators should be pure and must not mutate shared state.
A static node rule may coexist with runtime when: static pruning happens first.

### Runtime condition

Before its reads are ready, the Step is not ready. A true condition enables normal
execution. A false condition puts the Step in the Skipped terminal state without
invoking its action or producing an action result.

Skipped is a terminal state, not simultaneous Skipped and Finished states.
A false condition is not an action returning False.

For a Graph or NativeGraph composite, false means its subgraph is not entered.
A runtime-skipped composite and its descendants become skipped.

## Why a conditional edge is different

```text
A --[condition]--> B
```

This controls whether B waits for A; both nodes remain enabled.

```text
[condition] B
```

This controls whether B participates at all. Do not use one mechanism as a
substitute for the other.

## Compilation and execution

Both public modes use the Rust declaration compiler and ExecutionSession.
Static conditions are resolved during compilation; runtime conditions become node
activation conditions. The session manages node state, including failures,
blocking, and condition-result caching. Python evaluates predicates only when
requested in a batch and returns results through advance.

- Reads must be satisfied before evaluation; failed reads block the affected node.
- Once the condition is false, other after prerequisites are not awaited.
- A skipped node no longer blocks downstream work by waiting on unused prerequisites.
- A composite remains a real subgraph boundary, not a no-op leaf.

In [compile.py](../src/rivetoo_graph/compile.py), conditional edges lower to
CompiledConditionalDependency, while runtime Step.when lowers to
CompiledRuntimeCondition on the node itself. Step conditions are not rewritten
as conditional edges.

Declared subgraph leaves join the execution structure. Their boundary becomes
CompiledComposite, retaining prerequisites, successors, observation, and runtime
when. Activation releases direct children; false skips the subtree. Empty composites
still retain their dependency boundary.

## Reads versus action prerequisites

RuntimeCondition.reads names the results needed to decide whether the Step is
enabled. Ordinary after prerequisites constrain execution only if it is enabled.
Reads must refer to Steps in the current graph and participate in DAG validation.

```python
probe = Step(probe_cache, label="probe")
prepare_a = Step(prepare_a_source, label="prepare_a")
prepare_b = Step(prepare_b_source, label="prepare_b")
heavy_build = Step(
    build_source,
    label="heavy_build",
    when=runtime_condition(
        reads=(probe,),
        when=lambda ctx: bool(ctx.require_value(probe)),
    ),
)
graph = Graph.from_wiring(
    wiring=(
        probe.wire(),
        prepare_a.wire(),
        prepare_b.wire(),
        heavy_build.wire(after=(prepare_a, prepare_b)),
    )
)
```

After probe succeeds, the condition can be evaluated without waiting for either
preparation. If false, heavy_build is skipped immediately. If true, it still waits
for prepare_a and prepare_b.

Each compiled condition instance is evaluated at most once per session. Reusing
one RuntimeCondition object on several nodes does not merge those instances.
A recorded result is reused while other after prerequisites remain pending.

If the only model were a flat after list, it could not distinguish the early probe
needed for the decision from preparations needed only for execution. That is why
reads is separate.

Reads also participates in wiring entry/exit calculation. Do not repeat the same
probe in ordinary after solely to make the condition wait; declare after only for
independent ordering constraints.

## Boundaries

Step conditions and conditional edges share condition types but retain different
semantics. Neither is an exactly-one branch selector; for selection before graph
construction, see [select_one](./core_model.md#static-selection-before-construction).
Failure, cancellation, and result recording follow the common
[execution rules](./execution.md).
