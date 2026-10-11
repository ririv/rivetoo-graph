# Capability dependencies and subgraph ports

English | [简体中文](./zh-CN/capabilities.md)

A capability dependency says “the consumer needs this contract,” not necessarily
“the consumer depends on this particular component.”

```text
A provides 123
C provides 123
B requires 123
```

A and C are candidate providers. Selection happens before execution; the runtime
does not choose whichever provider happens to finish first.

## Capability interfaces

A Graph may declare several input and output ports:

- `CapabilityInterface.inputs` maps a capability to one or more internal Steps
  where consumption begins.
- `CapabilityInterface.outputs` maps a capability to the single Step whose
  completion makes it ready.

Ports reference real Steps. They do not create empty actions or synthetic event nodes.

```python
from rivetoo_graph import Capability, CapabilityInterface

capability_123 = Capability("123")

a_graph = (a_1 >> a_2 >> a_3 >> a_4 >> a_5).graph(
    label="A",
    capabilities=CapabilityInterface(outputs={capability_123: a_3}),
)
b_graph = (b_a >> b_b >> b_c >> b_d >> b_e).graph(
    label="B",
    capabilities=CapabilityInterface(inputs={capability_123: b_b}),
)
a = a_graph.step(label="A")
b = b_graph.step(label="B")
```

An output is a stability promise: later steps must not destroy or replace what has
already been exposed. If later work produces a different fact, expose another
capability.

Ports may reference direct Steps or descendant Steps inside preserved Python
subgraphs. This allows wrapping a graph without losing its real readiness points:

```python
wrapped = (prepare >> child_graph >> normalize).graph(
    capabilities=CapabilityInterface(
        inputs=child_graph,
        outputs={installed_runtime: normalize},
    ),
)
```

If wiring was prepared separately, declare the complete interface when materializing it:

```python
graph = prepared_wiring.graph(
    capabilities=CapabilityInterface(
        inputs={toolchain: compile_stage},
        outputs={installed_runtime: install_stage},
    ),
)
```

There is no API to overwrite, merge, or append ports after Graph construction.

## Forwarding and reuse

Graph, Graph.from_wiring, and wiring.graph accept the same `capabilities` argument.
Omitting it means an empty interface, not automatic exposure of inner ports.

```python
# Forward the full interface without copying Steps or flattening the child.
outer = (prepare >> child_graph).graph(capabilities=child_graph)

# Forward inputs only; publish a different output.
outer = (child_graph >> normalize).graph(
    capabilities=CapabilityInterface(
        inputs=child_graph,
        outputs={installed_runtime: normalize},
    ),
)

# Reuse normalized mappings or an original mapping.
interface = CapabilityInterface(inputs=child_graph.capabilities.inputs)
shared_inputs = {toolchain: compile_stage}
shared = CapabilityInterface(inputs=shared_inputs)
another = CapabilityInterface(inputs=shared.inputs)
```

`inputs=graph` and `outputs=graph` forward only that direction, equivalent to
passing `graph.capabilities.inputs` or `.outputs`. Construction copies mappings
into read-only views; later changes to the caller's dictionary do not affect the
declaration. Inputs normalize to nonempty Step tuples; each output remains one Step.

Referenced Steps must be mounted exactly once in the resulting graph. Forwarding
does not make an external or ambiguously mounted Step valid.

A capability occupies one dictionary key. Duplicate Python keys are overwritten
before Graph sees them, so use a tuple to constrain multiple input Steps:

```python
consumer = ((prepare_a >> configure_a) | (prepare_b >> configure_b)).graph(
    capabilities=CapabilityInterface(
        inputs={toolchain: (configure_a, configure_b)},
    ),
)
```

Both preparations can start without the toolchain. Each configure waits for its
own predecessor and the toolchain, not for the other configure. A single binding
expands to both inputs, without an empty action or shared waiting node.
Outputs do not accept several Steps: that would require an explicit all/any
readiness model.

## Two requirement forms

```python
type CapabilityRequirement = AnyProviderRequirement | ExactProviderRequirement
```

Require a capability from an unspecified provider:

```python
AnyProviderRequirement(consumer=b, capability=capability_123)
```

Require it specifically from A:

```python
ExactProviderRequirement(consumer=b, provider=a, capability=capability_123)
```

One input can require several exact providers, for example both A and C. That
produces two required bindings, not an either/or selection. The variants are
explicit; `provider=None` is not a substitute for the sum type.

## Provider resolution

`resolve_capability_requirements()` turns symbolic requirements into definite
CapabilityBinding values before graph execution:

```text
AnyProviderRequirement(123)
    → select a provider
    → CapabilityBinding(A.123 → B.123)

ExactProviderRequirement(A.123)
    → verify that A provides 123
    → CapabilityBinding(A.123 → B.123)
```

An any-provider requirement resolves directly when only one provider exists.
With both A and C, select explicitly:

```python
# c is another graph-valued Step providing capability_123.
requirement = AnyProviderRequirement(consumer=b, capability=capability_123)
bindings = resolve_capability_requirements(
    (a, b, c),
    (requirement,),
    selections=(CapabilitySelection(requirement=requirement, provider=a),),
)
graph = Graph(label="root", steps=(a, b, c), edges=bindings)
```

Ambiguity without a selection is an error. There is no “first ready wins” policy
and no provider failover. Hosts can derive selections from their configuration
or lock data; Graph validates and lowers them without adopting domain policies.

## A plain-language example

B depends on A. A has steps 1, 2, 3, 4, and 5, and declares step 3 as its output.
B has steps a, b, c, d, and e, and declares step b as the input where it starts
needing that capability.

Before A reaches step 3, B can already do step a on its own. After step 3, A tells
B only “I'm ready.” B does not need to know which internal step A reached.
B continues from b while A continues with 4 and 5.

If B also needs a different capability from C, it can attach that input to a
different internal Step. There is no single input that must gate every dependency;
each capability is attached where it is first consumed.

## Failures at capability ports

Failures follow actual compiled dependencies, not mere membership in the same
subgraph. These examples assume explicit bindings: matching port names alone do
not create edges.

### The output is ready, then later work fails

```text
A: 1 → 2 → 3 [output: library-ready] → 4 [failed]
           │
           │ library-ready
           ▼
B: a ────→ b [input] → c
```

B's a can run early. Once A's 3 succeeds, b can start without waiting for 4.
Failure in 4 does not revoke the ready capability; b and c can continue.
A and the overall run still report failure.

In plain terms: A already handed B what it needs; A's later problem should not
stop work B can safely do. This requires a truthful output contract. If 4 still
modifies files B will read, the output belongs at 4 or later. Graph does not inspect
files to infer readiness.

### The output cannot become ready

```text
A: 1 → 2 [failed] → 3 [output: library-ready, blocked]
                   │
                   │ unavailable
                   ▼
B: a [can run] ──→ b [input, blocked] → c [blocked]
```

The same applies when the output Step itself fails. Only the dependent portion
of B is blocked. Blocked is not an explicit skip and cannot release downstream
work as though it succeeded.

### Several ports in one subgraph

```text
A:
├── Generate headers [success, output: headers-ready]
└── Generate library [failed, output: library-ready]

B:
├── Prepare sources [can run]
├── Check headers [input: headers-ready, can run]
└── Link program [input: library-ready, blocked]
```

The header output stays ready. This assumes header checking has no other failed
prerequisite. A failed output blocks all inputs bound to it. Failure in one consumer
does not propagate backward to the provider or sideways to independent consumers.

When one input requires both A and C, both must be ready. C's success cannot
compensate for A's failure, while unrelated consumer work can still proceed.

### Waiting for a whole subgraph is different

```python
graph = (a_graph >> b_graph).graph()
```

This waits for all of A to succeed before entering B:

```text
A [overall failure] → B [entire subgraph blocked]
```

Use capability bindings for early readiness and independent preparation. If both
a whole-graph ordering constraint and a capability binding are declared, both
apply; capability ports do not override ordering.

There is no rollback or provider switching. See [failure propagation](./execution.md#failure-propagation)
for exception aggregation, predicate failures, cancellation, and snapshots.

## Compilation and execution

The declaration compiler locates internal port nodes and lowers each binding to
an ordinary execution dependency:

```text
A.3 ── capability 123 ──> B.b
```

Both subgraph boundaries remain. The compiler does not turn this into “all of A
must finish before any of B starts.” Both public modes use the same lowering:

```python
GraphPlan(graph=graph).run(mode="compiled")
GraphPlan(graph=graph).run(mode="dynamic")
```

## Language boundaries

Python holds Step references and validates interface ownership. Components,
capabilities, requirements, and selections are encoded as integer IDs for one
Rust provider-resolution call. The resolver validates uniqueness, ambiguity, and
explicit selections without executing actions or providing failover.

Explicit bindings enter declaration compilation and lowering. Final DAG validation
and scheduling live in Rust; Python does not maintain a second provider solver.

Ports reference Python-declared Steps, not opaque NativeSteps inside a NativeGraph.
To publish completion of an entire native graph, mount it in a Python Step and
use that Step as an output. A dynamically returned graph cannot expose unknown
internal ports before it exists.

Provider components and their direct output Steps cannot use runtime `when` to
decide whether they exist. Provider selection is fixed before compilation; runtime
skipping is not a mechanism for changing candidates.
