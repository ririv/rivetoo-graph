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

- `CapabilityInterface.inputs`：能力到内部 Step 的映射；一个能力可以对应多个
  Step，分别约束它们开始消费能力的位置。
- `CapabilityInterface.outputs`：能力到内部 Step 的映射；该 Step 完成后能力就绪。

它们引用真实 Step，不会生成空 action、事件 Step 或其它假节点。

```python
from rivetoo_graph import Capability, CapabilityInterface

capability_123 = Capability("123")

a_graph = (
    a_1 >> a_2 >> a_3 >> a_4 >> a_5
).graph(
    label="A",
    capabilities=CapabilityInterface(
        outputs={capability_123: a_3},
    ),
)

b_graph = (
    b_a >> b_b >> b_c >> b_d >> b_e
).graph(
    label="B",
    capabilities=CapabilityInterface(inputs={capability_123: b_b}),
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
    capabilities=CapabilityInterface(
        inputs=child_graph,
        outputs={installed_runtime: normalize},
    ),
)
```

如果接线由其它声明组件提前准备，应在最终物化 Graph 时一次性声明完整接口：

```python
graph = prepared_wiring.graph(
    capabilities=CapabilityInterface(
        inputs={toolchain: compile_stage},
        outputs={installed_runtime: install_stage},
    ),
)
```

能力接口属于 Graph 的结构契约，不提供在 Graph 物化后覆盖、合并或补充端口的入口。

## 转发与复用

三种 Graph 构造入口 `Graph(...)`、`Graph.from_wiring(...)`、`wiring.graph(...)`
均接受同一个 `capabilities` 参数。不写时使用空接口，不会自动公开内部图的端口。

```python
# 整体转发，保留子图边界，不复制 Step。
outer = (prepare >> child_graph).graph(capabilities=child_graph)

# 分方向转发；输出以最终收尾步骤为准，不公开 child 原来的输出。
outer = (child_graph >> normalize).graph(
    capabilities=CapabilityInterface(
        inputs=child_graph,
        outputs={installed_runtime: normalize},
    ),
)

# 也可以直接复用规范化后的映射。
interface = CapabilityInterface(inputs=child_graph.capabilities.inputs)

# 可以保存并复用完整接口，或者只复用共享字典。
shared_inputs = {toolchain: compile_stage}
shared = CapabilityInterface(inputs=shared_inputs)
another = CapabilityInterface(inputs=shared.inputs)
```

`inputs=graph` 只转发输入，`outputs=graph` 只转发输出。它们与分别传入
`graph.capabilities.inputs` / `graph.capabilities.outputs` 等价。
构造接口时复制映射，并保存为只读视图；修改调用方原字典不会改变既有声明。
输入值统一归一化为非空 Step 元组，输出值始终是一个 Step。
复用的 Step 必须位于最终图中，且只能挂载一次，不能因为转发而指向图外步骤。

同一能力只占一个字典键；不要写重复键，Python 会在调用 API 前覆盖它，图无法
诊断被覆盖的条目。需要约束多个步骤时应写在同一键下：

```python
consumer = (
    (prepare_a >> configure_a) | (prepare_b >> configure_b)
).graph(
    capabilities=CapabilityInterface(
        inputs={toolchain: (configure_a, configure_b)},
    ),
)
```

两个 prepare 都不需要等待工具链；两个 configure 各自等待自己的前驱和工具链，
彼此不互相等待。外部只连接一次该能力，编译器将绑定展开到全部输入步骤，不插入
空 action 或共享等待节点。输出暂不支持多个步骤，避免隐式引入“全部/任一就绪”语义。

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
    capability=capability_123,
)
```

明确要求 A 提供的能力：

```python
ExactProviderRequirement(
    consumer=b,
    provider=a,
    capability=capability_123,
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
    capability=capability_123,
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
输入映射条目。B 不存在一个覆盖全部依赖的唯一入口；每项能力分别连接到真正开始
消费它的位置。

## 编译与执行

能力绑定跨越保留边界的子图。freeze 阶段把接口引用解析成内部 `StepId`，compile
阶段再把它 lower 为提供者出口到消费者入口的普通编译依赖：

```text
A.3 ── capability 123 ──> B.b
```

A 和 B 的子图边界仍然保留；不会为了接线而展开声明图，也不会把整个 A 的完成状态
作为整个 B 的启动条件。

跨子图能力绑定依赖全局编译调度。两种公开 mode 现在都进入同一个 Rust compiler，
因此以下两种写法使用同一份 binding lowering：

```python
GraphPlan(label="root", graph=graph).run(mode="compiled")
GraphPlan(label="root", graph=graph).run(mode="dynamic")
```

## Rust 迁移边界

当前数据模型刻意保持为容易迁移的不可变值和和类型：

```rust
struct AnyProviderRequirement {
    consumer: StepHandle,
    capability: Capability,
}

struct ExactProviderRequirement {
    consumer: StepHandle,
    provider: StepHandle,
    capability: Capability,
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
