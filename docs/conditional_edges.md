# Conditional edges

English | [简体中文](./zh-CN/conditional_edges.md)

## Purpose

A conditional edge describes an optional prerequisite: “B depends on A, but only
if this condition holds.” It does not decide whether A or B executes.
For node participation, use [conditional steps](./conditional_steps.md).

The runtime form is `ConditionalEdge(source, target, condition)`. The wiring
API provides:

```python
target.wire(
    after=(ConditionalPrerequisite(source, condition=condition),),
)
```

Graph.from_wiring normalizes this into a ConditionalEdge. No hidden hook or runtime
topology mutation is involved. Explicit skip operations remain available, but
structural optional dependencies should be visible in the graph rather than hidden
inside action side effects.

## Two kinds of conditions

StaticConditions binds static rules to their evaluator. ConditionalEdge and
ConditionalPrerequisite accept only RuntimeCondition. Evaluation differs:

- During declaration compilation, an explicit evaluator resolves static conditions.
- Runtime conditions lower to prerequisites identified by ConditionId.
- The resulting graph enters the unified Rust execution session.

Compiled conditional dependencies do not create extra gate Steps or worker tasks. CompiledComposite preserves each subgraph
boundary: a false internal edge does not make the whole composite finish early.

## Static conditions

A static condition is known at compilation time and does not read action results.
Examples include a host-defined platform, linkage, or feature selection.

[static_condition.py](../src/rivetoo_graph/static_condition.py) models a condition as
an opaque payload and a display description:

```python
StaticCondition(
    payload=SomePayload(...),
    description="environment(production)",
)
```

Graph does not define domain-specific environment, platform, or artifact-kind
expressions. The host supplies their meaning through StaticConditions. Its
`evaluator` accepts either a StaticConditionEvaluator or
`Callable[[StaticCondition], bool]`.

Graph traverses declarations, calls the evaluator, and retains or removes the
dependency in the new compilation. It does not mutate the original declaration.
The host defines payloads and their interpretation.

Graph itself provides `all_(...)`, `any_(...)`, and `not_(...)` for combining
static conditions. Combining leaves does not remove the need for an evaluator:

```python
condition = all_(
    StaticCondition(payload="android", description="target OS is Android"),
    StaticCondition(payload="shared", description="shared linkage"),
)
```

These strings are host-defined payloads, not built-in Graph expressions.
StaticConditions requires an explicit evaluator.

### A static-edge example

Suppose archive must wait for an Android environment shim only on Android.
This example intentionally controls only the dependency; the shim Step stays enabled.

```python
build = Step(build_archive_input, label="build")
write_android_env_shim = Step(write_shim, label="write_android_env_shim")
archive = Step(create_archive, label="archive")

condition = StaticCondition(
    payload=AndroidOnlyPayload(),
    description="target_os(android)",
)
graph = Graph(
    steps=(build, write_android_env_shim, archive),
    edges=(
        UnconditionalEdge(source=build, target=archive),
        UnconditionalEdge(source=write_android_env_shim, target=archive),
    ),
    static=StaticConditions(
        evaluator=evaluate_target_condition,
        conditions={(write_android_env_shim, archive): condition},
    ),
)
```

When the evaluator returns true:

```text
build ────────────────────→ archive
write_android_env_shim ───→ archive
```

When it returns false:

```text
build ────────────────────→ archive
write_android_env_shim       (still runs independently)
```

A true static edge becomes an ordinary dependency; a false one disappears.
Removing an edge does not disable its source Step. Use a static node rule to disable the
source as well. Payload types, evaluator functions, and actions in this example
belong to the host.

## Static edge DSL

```python
static = StaticConditions(evaluator=evaluate_target_condition)
graph = static.edge(checks, build, when=wait_for_checks).graph()
```

This returns ordinary GraphWiring without gate Steps. Unlike static.when, false
removes only the edge; checks and build remain. Composed StaticConditions retain
their own evaluators. Duplicate rules for one target are rejected, never overridden.
The DSL does not automatically merge repeated Step objects.

## Runtime conditions

Runtime conditions are evaluated during execution, often from a cache probe or
other Step result. In [model.py](../src/rivetoo_graph/model.py), they contain:

```python
class RuntimeCondition:
    when: ConditionalEdgePredicate
    inputs: tuple[Step, ...] = ()
```

The predicate receives one `Option[object]` per declared input, in tuple order.
Graph resolves the results in the current mount; no context lookup is required.
See [result inputs](./conditional_steps.md#result-inputs) for absence and unwrapping.

Each compiled condition instance is evaluated at most once in its session.
If it is true but the source prerequisite is not yet complete, subsequent scheduling
waits without rerunning the predicate. Reusing one Python RuntimeCondition or
callable at several sites does not deduplicate those evaluations.

## Why inputs is not after

- An ordinary `after` prerequisite says what the target action must wait for.
- A condition's `inputs` says what is needed to determine whether a particular
  conditional dependency exists.

The condition must be evaluated before deciding whether its source is required.
A Step can have several conditional edges, each reading different results.

### One conditional dependency

Suppose build always waits for a cache probe and only waits for configure on a miss:

```python
compile_cache_probe = Step(probe_cache, label="compile_cache_probe")
configure = Step(configure_source, label="configure")
build = Step(build_source, label="build")

graph = Graph.from_wiring(
    wiring=(
        compile_cache_probe.wire(),
        configure.wire(),
        build.wire(
            after=(
                compile_cache_probe,
                ConditionalPrerequisite(
                    configure,
                    condition=runtime_condition(
                        when=cache_miss,
                        inputs=(compile_cache_probe,),
                    ),
                ),
            )
        ),
    )
)
```

The direct probe prerequisite expresses action ordering; inputs expresses predicate
inputs. Since inputs already blocks the target until evaluation, repeating the same
ordinary prerequisite is unnecessary unless it serves an independently intended
constraint. Neither dependency disables configure itself.

### Several conditional dependencies

```python
build.wire(
    after=(
        ConditionalPrerequisite(
            configure,
            condition=runtime_condition(when=config_miss, inputs=(probe_config,)),
        ),
        ConditionalPrerequisite(
            prepare_assets,
            condition=runtime_condition(when=assets_missing, inputs=(probe_assets,)),
        ),
    )
)
```

One predicate inputs probe_config and the other inputs probe_assets. A flat after
list cannot describe which result belongs to which condition. Both inputs sets
participate in cycle detection and scheduling.

## Runtime and compilation boundary

Rust declaration compilation removes static conditions and lowers runtime ones.
The session sees ordinary dependencies, ConditionId values, inputs, and compiled
conditional prerequisites. Python predicates remain in adapter side tables.
StaticCondition objects do not enter the runtime evaluation protocol.

Conceptually, a compiled Step's after list can contain:

```text
CompiledStepRef(probe)
CompiledConditionalDependency(
    node=CompiledStepRef(configure),
    condition=CompiledRuntimeCondition(inputs=(probe,), when=predicate),
)
```

The scheduler waits for inputs, evaluates the predicate, and either ignores configure
or waits for it. The dependency carries its own condition; no synthetic action or
metadata convention is needed.

The same rules apply inside subgraphs. Ordinary action failures propagate through
active conditional edges and inputs, but not inactive edges. See
[execution semantics](./execution.md#failure-propagation).

## Design boundary

Graph owns condition structure, evaluation timing, and scheduling semantics.
Hosts decide which domain relations are conditional and supply predicates.
Predicates must not hide side effects or domain fallback policies.
