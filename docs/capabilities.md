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

## 出入口遇到失败时

失败沿编译后的实际依赖关系传播，不以“同属一个子图”为理由停止全部内部步骤。
以下示例中的出入口均已通过能力绑定接线；仅声明同名接口不会自动产生依赖。

### 出口已就绪，后面的步骤失败

```text
A：1 → 2 → 3〔出口：library-ready〕→ 4〔失败〕
             │
             │ library-ready
             ▼
B：a ──────→ b〔入口〕→ c
```

- B 的 a 不需要这项能力，可以提前执行。
- A 完成 3 后，B 的 b 就可以开始，不必等待 A 的 4。
- A 的 4 失败，不会撤销第 3 步已经发布的能力；B 的 b、c 仍可执行。
- A 最终仍是失败的子图，整张图最终仍报告失败，不会因为 B 成功就忽略 A 的错误。

口语化地说：A 已经把 B 需要的东西交出来了，A 后面自己的事情失败，不应该让 B
已经可以做的事情也停下来。

把出口放在 3，是提供方明确承诺“消费者需要的东西到这里已经完整可用了”。后面的
4 不得再破坏或替换这些内容。如果 4 仍负责修改消费者要读取的文件，应把出口移到
4 或更晚的真正就绪点；Graph 不会通过检查文件来猜测这个声明是否正确。

### 出口无法就绪

```text
A：1 → 2〔失败〕→ 3〔出口：library-ready，被阻断〕
                   │
                   │ library-ready 不可用
                   ▼
B：a〔可执行〕───→ b〔入口，被阻断〕→ c〔被阻断〕
```

A 的 3 依赖失败的 2，所以不会执行。B 的 b 需要第 3 步提供的能力，也不会执行；
c 又依赖 b，因此一并被阻断。但 B 的 a 不依赖这个出口，仍然可以执行。

出口步骤自身失败时也是同样的规则。这里不是把整个 B 立即停止，而是阻断 B 中
真正需要该出口的部分。`blocked` 不等于主动 `skip`，不能作为成功前置放行后续步骤。

### 同一子图有多个出入口

```text
A：
├── 生成头文件〔成功，出口 headers-ready〕
└── 生成库文件〔失败，出口 library-ready〕

B：
├── 准备源码〔可执行〕
├── 检查头文件〔入口 headers-ready，可执行〕
└── 链接程序〔入口 library-ready，被阻断〕
```

两个出口分别判断，不会因为都属于 A，就把成功的头文件出口也判成失败。这张示意图
假定 B 的“检查头文件”没有其他失败前置；若它还依赖失败的步骤，仍会被那些依赖阻断。

同一个能力对应多个入口时，该出口失败会阻断所有绑定到它的入口。反过来，某个
消费入口自己的 action 失败，不会反向使提供方失败，也不会影响另一个没有依赖它的入口。

同一个入口需要 A 和 C 两个提供方时，两项依赖都必须满足。A 的能力失败，即使 C
已经就绪，也不能让该入口执行；B 中不依赖这个入口的其他工作仍可以继续。

### 等待整个子图与等待能力出口不同

```python
graph = (a_graph >> b_graph).graph()
```

这里要求整个 A 成功结束后才进入 B，不是只等待 A 的某个能力出口：

```text
A〔任一内部步骤失败，最终整体失败〕→ B〔整个子图被阻断〕
```

如果希望 B 提前准备、在 A 的某个中间步骤就绪后继续，应使用能力出入口接线，而不是
给整个 B 增加 `A >> B` 约束。两种关系同时声明时，两种约束都生效，能力接口不会覆盖
整图顺序约束。

失败不会回滚已经成功的工作，也不会自动切换到其他能力提供方。异常汇总、条件失败、
主动取消与执行快照的统一规则见[执行语义：失败传播](./execution.md#失败传播)。

## 编译与执行

能力绑定跨越保留边界的子图。声明编译器把接口引用定位到内部节点，再将其
lower 为提供者出口到消费者入口的普通编译依赖：

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

## 当前语言边界

Python 使用 Step 对象作为声明期引用。能力解析入口先校验接口引用与组件归属，再把
component、capability、requirement 和 selection 编码为整数 ID，一次交给 Rust 的
provider 解析器检查唯一性、歧义和显式选择。这里不会执行 action，也没有失败后换
provider 的隐式策略。

得到的显式 binding 随图进入声明编译器，定位内部端口并 lower 为执行依赖。最终 DAG
校验与执行状态机都在 Rust；Python 不维护另一套 provider 选择或 binding lowering 规则。

当前端口引用的是 Python 声明中的 Step，不能直接引用不透明 NativeGraph 内部的
NativeStep。需要公开原生子图整体完成状态时，可以由 Python Step 挂载它，并以该
Step 作为出口。运行期才返回的子图也不能提前向外公开尚不存在的内部端口。

能力提供组件及其直接出口不允许使用运行时 `when` 来决定是否存在；这与“执行失败”
不同。provider 的选择必须在编译前确定，不能靠运行时跳过来实现候选切换。
