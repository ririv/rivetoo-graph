# 能力依赖与子图端口

能力依赖表达的是“消费者需要一项契约”，而不一定是“消费者依赖某个固定组件”。

例如：

```text
A provides 123
C provides 123
B requires 123
```

这里 B 的真实需求是能力 `123`。A 和 C 都可以提供这项能力，具体选择谁属于
构图前的解析；Graph runtime 不会在执行过程中临时挑选提供者。

## 能力接口

一张 Graph 可以声明多个能力入口和出口：

- `CapabilityInput`
  - 表示从哪个内部 Step 开始需要该能力
- `CapabilityOutput`
  - 表示哪个内部 Step 完成后该能力已经就绪

它们引用真实 Step，不会生成空 action、事件 Step 或其它假节点。

```python
from rivetoo_graph import Capability, CapabilityInput, CapabilityOutput

capability_123 = Capability("123")

a_graph = (
    a_1 >> a_2 >> a_3 >> a_4 >> a_5
).graph(
    label="A",
    capability_outputs=(
        CapabilityOutput(
            capability=capability_123,
            ready_after=a_3,
        ),
    ),
)

b_input = CapabilityInput(
    capability=capability_123,
    needed_by=b_b,
)

b_graph = (
    b_a >> b_b >> b_c >> b_d >> b_e
).graph(
    label="B",
    capability_inputs=(b_input,),
)

a = a_graph.step(label="A")
b = b_graph.step(label="B")
```

能力出口是一项稳定契约：提供者宣布就绪以后，后续 Step 不应再破坏或替换已经
暴露的内容。如果后续阶段才产生另一项事实，应把它声明为另一个能力出口。

端口可以引用当前 Graph 的直接 Step，也可以引用当前 Graph 内保留边界的子图后代
Step。后者用于给现有子图外包一层前置或后处理流程，同时仍把真实消费点或就绪点暴露
出去；Graph 不会因此拆开子图：

```python
wrapped = (
    prepare >> child_graph >> normalize
).graph(
    capability_inputs=child_graph.capability_inputs,
    capability_outputs=(
        CapabilityOutput(
            capability=installed_runtime,
            ready_after=normalize,
        ),
    ),
)
```

如果接线由其它声明组件提前准备，应在最终物化 Graph 时一次性声明完整接口：

```python
graph = prepared_wiring.graph(
    capability_inputs=(CapabilityInput(toolchain, compile_stage),),
    capability_outputs=(CapabilityOutput(installed_runtime, install_stage),),
)
```

能力接口属于 Graph 的结构契约，不提供在 Graph 物化后覆盖、合并或补充端口的入口。

## 两种需求

声明层明确区分两种语义：

```python
type CapabilityRequirement = (
    AnyProviderRequirement
    | ExactProviderRequirement
)
```

只要求能力，不指定提供者：

```python
AnyProviderRequirement(
    consumer=b,
    input=b_input,
)
```

明确要求 A 提供的能力：

```python
ExactProviderRequirement(
    consumer=b,
    provider=a,
    input=b_input,
)
```

同一个输入可以同时精确要求多个提供者，例如最终链接同时需要 A 和 C 的安装结果。
这种情况会产生两条确定的绑定；它不是任意提供者选择，也不存在二选一语义。

不能使用 `provider=None` 模拟这两个分支。它们具有不同的解析语义，应保持为
显式和类型。

## 提供者解析

`resolve_capability_requirements()` 在 Graph 执行前把符号需求解析为确定的
`CapabilityBinding`：

```text
AnyProviderRequirement(123)
    └── provider selection
        └── CapabilityBinding(A.123 -> B.123)

ExactProviderRequirement(A.123)
    └── validate A provides 123
        └── CapabilityBinding(A.123 -> B.123)
```

如果当前组合中只有一个提供者，任意提供者需求可以直接解析。如果 A、C 同时提供
`123`，调用方必须传入显式选择：

```python
# 这里的 c 是另一个同样提供 capability_123 的 graph-valued Step。
requirement = AnyProviderRequirement(
    consumer=b,
    input=b_input,
)

bindings = resolve_capability_requirements(
    (a, b, c),
    (requirement,),
    selections=(
        CapabilitySelection(
            requirement=requirement,
            provider=a,
        ),
    ),
)

graph = Graph(
    label="root",
    steps=(a, b, c),
    edges=bindings,
)
```

没有选择时，多提供者属于歧义并直接失败。runtime 不会采用“谁先完成就用谁”，
也不会在某个提供者失败后自动换另一个；这类行为会让产物、缓存身份和失败结果变得
不确定。

上层领域解析器可以根据自己的配置或锁定结果产生 `CapabilitySelection`，但这些选择
策略不进入 Graph core。Graph 只负责验证选择结果，并把绑定 lower 为确定的节点依赖。

## 口语化理解

B 依赖 A。A 有第 1、2、3、4、5 步，并把第 3 步声明成自己的能力出口；B 有
第 a、b、c、d、e 步，并声明自己从第 b 步开始才需要这项能力。

在 A 做完第 3 步以前，B 已经可以偷偷把与 A 无关的第 a 步做完。A 做完第 3 步后，
它只告诉 B：“我已经就绪了。”B 不知道 A 具体完成到了第几步，也不需要知道；它只
知道自己等待的能力现在可用了。随后 B 从第 b 步继续，而 A 仍然继续做第 4、5 步，
两边可以同时运行。

如果 B 还需要 C 提供的另一项能力，可以为另一个内部 Step 声明另一个
`CapabilityInput`。B 不存在一个覆盖全部依赖的唯一入口；每项能力分别连接到真正开始
消费它的位置。

## 编译与执行

能力绑定跨越保留边界的子图。freeze 阶段把接口引用解析成内部 `StepId`，compile
阶段再把它 lower 为提供者出口到消费者入口的普通编译依赖：

```text
A.3 ── capability 123 ──> B.b
```

A 和 B 的子图边界仍然保留；不会为了接线而展开声明图，也不会把整个 A 的完成状态
作为整个 B 的启动条件。

跨子图能力绑定依赖全局编译调度，因此当前必须显式使用：

```python
GraphPlan(label="root", graph=graph).run(mode="compiled")
```

`dynamic` 模式保持层级递归调度，不能正确表达跨子图内部节点的依赖，因此会明确拒绝
包含能力绑定的图，而不是退化成整张子图依赖。

## Rust 迁移边界

当前数据模型刻意保持为容易迁移的不可变值和和类型：

```rust
struct AnyProviderRequirement {
    consumer: StepHandle,
    input: CapabilityInputHandle,
}

struct ExactProviderRequirement {
    consumer: StepHandle,
    provider: StepHandle,
    input: CapabilityInputHandle,
}

enum CapabilityRequirement {
    AnyProvider(AnyProviderRequirement),
    ExactProvider(ExactProviderRequirement),
}

struct CapabilityBinding {
    source: StepHandle,
    target: StepHandle,
    capability: Capability,
}

struct CapabilitySelection {
    requirement: AnyProviderRequirement,
    provider: StepHandle,
}
```

Python 当前使用 Step 对象引用作为声明期 handle；迁移到 Rust 后可以直接替换成显式
`StepHandle`。提供者解析、freeze、lowering 和 runtime 之间只传数据，不依赖 Python
反射、动态属性或回调式选择器。
