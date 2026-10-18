# Conditional steps

English | [简体中文](./zh-CN/conditional_steps.md)

## Purpose

Conditional edges control whether a dependency applies. `Step.condition` controls
whether the Step itself participates. It works for Python leaves, Python Graph
composites, and NativeGraph composites.

## Declaration

Defined in [model.py](../src/rivetoo_graph/model.py):

```python
Step(action=..., label=..., condition=...)
```

Label is optional presentation. `condition` accepts
`RuntimeCondition | None`, not bool.
Wiring belongs to `Step.wire(after=...)` and Graph.edges, not to Step itself.

The outer `condition` binds a condition object, just like a conditional edge.
Inside `runtime_condition`, `when` is the function deciding whether it holds:

```python
probe = Step(lambda: 42)
condition = runtime_condition(
    when=lambda result: result.unwrap() == 42,
)
build = Step(lambda ctx, result: build_source(), condition=condition)
graph = Graph.from_wiring((probe.wire(), build.wire(inputs={condition: (probe,), build.action: (probe,)})))
```

`child_graph.step(condition=condition)` uses the same spelling for a subgraph mount.

## Semantics

### No condition

`condition=None` leaves the Step enabled.

### Static condition

Static rules belong to StaticConditions, not Step.condition:

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
A static node rule may coexist with runtime condition: static pruning happens first.

### Runtime condition

Before its inputs are ready, the Step is not ready. A true condition enables normal
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

- Inputs must be satisfied before evaluation; failed inputs block the affected node.
- Once the condition is false, other after prerequisites are not awaited.
- A skipped node no longer blocks downstream work by waiting on unused prerequisites.
- A composite remains a real subgraph boundary, not a no-op leaf.

In [compile.py](../src/rivetoo_graph/compile.py), conditional edges lower to
CompiledConditionalDependency, while runtime Step.condition lowers to
CompiledRuntimeCondition on the node itself. Step conditions are not rewritten
as conditional edges.

Declared subgraph leaves join the execution structure. Their boundary becomes
CompiledComposite, retaining prerequisites, successors, observation, and runtime
condition. Activation releases direct children; false skips the subtree. Empty composites
still retain their dependency boundary.

## Inputs versus action prerequisites

Step.wire(inputs=...) maps each existing receiver object to its own result sources. Node conditions wait only for their bound results. Action inputs and ordinary after prerequisites constrain execution after the node is enabled.
Inputs must refer to Steps in the current graph and participate in DAG validation.

```python
probe = Step(probe_cache, label="probe")
prepare_a = Step(prepare_a_source, label="prepare_a")
prepare_b = Step(prepare_b_source, label="prepare_b")
heavy_build = Step(
    lambda ctx, result: build_source(),
    label="heavy_build",
    condition=(cache_condition := runtime_condition(
        when=lambda result: bool(result.unwrap()),
    )),
)
graph = Graph.from_wiring(
    wiring=(
        probe.wire(),
        prepare_a.wire(),
        prepare_b.wire(),
        heavy_build.wire(
            after=(prepare_a, prepare_b),
            inputs={cache_condition: (probe,), heavy_build.action: (probe,)},
        ),
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
inputs is separate.

Inputs also participates in wiring entry/exit calculation. Do not repeat the same
probe in ordinary after solely to make the condition wait; declare after only for
independent ordering constraints.

## Result inputs

`inputs` declares both dependencies and positional predicate arguments:

```python
count_step = Step(lambda: 42)
approval_step = Step(lambda: True)

def should_run(count: Option[object], approval: Option[object]) -> bool:
    return count.unwrap() == 42 and approval.unwrap_or(False) is True

condition = runtime_condition(when=should_run)
consumer = Step(
    lambda ctx, count, approval: print(count.unwrap(), approval.unwrap()),
    condition=condition,
)
graph = Graph.from_wiring((
    count_step.wire(),
    approval_step.wire(),
    consumer.wire(inputs={
        condition: (count_step, approval_step),
        consumer.action: (count_step, approval_step),
    }),
))
```

When both Steps execute successfully, the predicate receives `Some(42)` and
`Some(True)`. The numbers and booleans are actual action return values, not result
presence markers.

No separate `after` is needed for these inputs. A predicate with no inputs takes
no arguments: `runtime_condition(when=lambda: True)`.
Declaring inputs automatically enables result recording before their producers run.
Graphs without inputs retain optional result recording.

`Option[T]` is `Some[T] | Nothing`:

- `Some(value)` contains a recorded result. `Some(None)` is a successful return of None.
- `Nothing()` means no recorded result, such as a skipped Step or a composite with no
  leaf return value. It is not a failed producer: producer failure blocks the consumer.
- `is_some()` and `is_none()` test presence.
- `unwrap()` returns the value, or raises ValueError for Nothing.
- `unwrap_or(default)` substitutes only for Nothing, never for Some(None) or Some(False).
- `bool(option)` raises TypeError. Test presence or unwrap explicitly.

Inputs reference Steps in the current graph. Reusing that graph under two parents
binds each input to its own mount, not to another execution of the same declaration.
Static pruning of an input is a compilation error, not Nothing.
Input order and duplicates are preserved, so `inputs={condition: (probe, probe)}` supplies two
arguments even though they wait for the same producer.

Step is not generic today: annotations should use `Option[object]`, then validate or
narrow the payload as needed. An annotation does not select a different calling
convention. Actions receive ctx followed by their own ordered Option inputs; ctx has
no result lookup API. Each condition waits only for its bound sources. A false node
condition skips the target without waiting for action inputs or conditional-edge
inputs. Producers are independent Steps and are not cancelled by that skip.

## Boundaries

Step conditions and conditional edges share condition types but retain different
semantics. Neither is an exactly-one branch selector; for selection before graph
construction, see [select_one](./core_model.md#static-selection-before-construction).
Failure, cancellation, and result recording follow the common
[execution rules](./execution.md).
