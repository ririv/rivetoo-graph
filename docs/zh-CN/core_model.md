# 核心模型

[English](../core_model.md) | 简体中文

Graph 模块把声明、编译和执行分成独立阶段：

```text
Graph / Step
    ↓ compile_graph(...)
GraphCompilation
    ↓ Graph runtime
GraphExecutionState
```

声明层只表达结构，不保存某次执行的状态；内部身份只在图挂载时产生。

## 纯拓扑层

`rivetoo_graph.topology` 提供不携带执行或领域行为的基础图结构：

- `Arc[NodeT]`
  - 一条从 `source` 指向 `target` 的有向边
- `DirectedTopology[NodeT]`
  - 只保存节点与有向边
  - 允许存在环
  - 提供前驱、后继和从根节点计算可达集合的能力
- `DAGTopology[NodeT]`
  - 组合一份 `DirectedTopology`
  - 构造时完成无环校验
  - 只有无环拓扑才能产生该类型

这层不认识 `Step`、action、条件、capability 或运行状态，是独立的 Python 通用拓扑 API。
执行 `Graph` 的校验不经过它，而是将阻塞关系批量交给 Rust `dag_cycle`；Rust 编译器
还会校验包含跨子图能力绑定的最终 DAG。两条路径的实现与复杂度见
[拓扑复杂度](./topology_complexity.md)。

## `Graph`

位置：

- [src/rivetoo_graph/model.py](../../src/rivetoo_graph/model.py)

`Graph` 是声明态执行 DAG，包含：

- `label: str | None`
- `steps: tuple[Step, ...]`
- `edges: tuple[GraphEdge, ...]`
- `capabilities: CapabilityInterface`：统一持有输入和输出的只读映射；声明时也可传入
  内部 Graph，显式转发它的完整接口。
- `observation: GraphObservationMessages`
- `static: tuple[StaticConditions, ...]`

`Graph` 不负责执行，也不带 `run()`。

`label` 只用于展示，不是调度身份。它可以省略：

- 根图省略 label 时，显示为 `<unlabeled>`
- 嵌套图省略 label 时，使用承载它的 Step 的已解析 label
- Graph 显式声明 label 时，该值优先

显式接线只保存在 `Graph.edges` 中。规范化后的 `Graph` 不接受 `StepWiring`，
Step 本身不保存 `after` 或 `inputs`。ResultEdge 承载数据绑定，条件中不再另存
一份依赖关系；所有阻塞关系均来自图的接线。

## `Step`

`Step` 是声明态执行节点，包含：

- `label: str | None`
- `action: LeafAction | Graph | NativeGraph`
- `condition: RuntimeCondition | None`

`Step` 没有公开 `id`，也不保存 `after`。

```python
prepare = Step(prepare_source, label="prepare")
compile_step = Step(lambda: compile_source(flags), label="compile")
```

直接传入 callable 时，`Step` 会把它规范化为 `LeafAction`。Graph core 和
runtime 消费三种 action：

- `LeafAction`：执行叶子动作
- `Graph`：保留显式 Python 子图边界
- `NativeGraph`：挂载完整 Rust 原生子图，内部 action 由 Rust 执行

同名 label 合法。声明期的 Step 身份是对象本身，因此：

- 同一个 Step 不能在同一张 Graph 中挂载两次
- 同一个 Step 可以在不同 Graph 中使用不同接线
- 普通边与结果输入的跨图引用会在 Graph 构造时失败；跨子图同步使用能力端口

## 身份与展示

当前模型严格区分身份与展示：

- `Step` 对象引用
  - 声明期引用
  - 用于边、运行时条件读取和结果读取
- `StepId`
  - Step 在一次具体挂载中的内部结构身份
  - 由挂载位置生成，例如 `0`、`1`、`1.0`
  - 供 compiler、runtime 和结果存储使用
- `Step.label`
  - 可选展示名
- `Graph.label`
  - 图自身的可选展示名

`Graph.label` 和 `Step.label` 都不承担身份语义。

未填写 Step label 时，编译阶段使用该挂载位置的 `StepId` 作为展示名。
因此匿名 lambda 可以保持无 label，不需要为了显示而伪造稳定身份。

## 拓扑与接线

规范化边是和类型：

```python
type GraphEdge = (
    UnconditionalEdge
    | ConditionalEdge
    | CapabilityBinding
    | ResultEdge
)
```

这些边引用真实 Step 对象：

- `UnconditionalEdge(source, target)`
- `ConditionalEdge(source, target, condition)`
- `ResultEdge(source, target, argument_index, receiver)`：同时声明等待关系与结果参数位置

作者通常不需要直接构造边，可以使用 `Step.wire(...)`：

```python
prepare = Step(prepare_source, label="prepare")
compile_step = Step(compile_source, label="compile")

graph = Graph.from_wiring(
    wiring=(
        prepare.wire(),
        compile_step.wire(after=(prepare,)),
    )
)
```

`StepWiring` 只是一份一次性接线输入。`Graph.from_wiring(...)` 会把它解析成
`Graph.steps + Graph.edges`，之后不再保留包装器。

条件 prerequisite 使用显式类型：

```python
archive.wire(
    after=(
        build,
        ConditionalPrerequisite(
            configure,
            condition=cache_miss,
        ),
    )
)
```

字符串不能引用 Step。声明图必须直接引用对象，避免 label 同名、重命名或子图作用域
导致引用歧义。

`CapabilityBinding` 是解析完成后的跨子图能力边。能力接口、任意提供者需求、精确
提供者需求和 compiled lowering 见 [capabilities.md](./capabilities.md)。

## 组合 DSL

Graph DSL 只保留两个运算符：

```python
A >> B
# B 依赖 A

A | B
# A 与 B 并列，不增加依赖
```

它们返回 `GraphWiring`，需要显式物化成 Graph：

```python
graph = (
    prepare
    >> (compile_a | compile_b)
    >> install
).graph()
```

组合规则是：

- `A >> B`：A 的所有出口连接到 B 的所有入口
- `A | B`：合并两侧步骤和边，不新增边
- `A >> (B | C)`：fan-out
- `(A | B) >> C`：fan-in

这里的入口、出口是 wiring 的结构边界，不是 `CapabilityInterface` 的具名端口；
`>>` 不会自动公开或绑定子图的能力接口。

已有 Graph 作为 operand 时会被包装成一个 Step，不会自动展开：

```python
pipeline = (prepare >> child_graph >> install).graph()
```

这样会保留 `child_graph` 的子图边界。Graph 也可以显式转换为 Step，并传递挂载参数：

```python
child_step = child_graph.step(
    label="build",
    condition=condition,
)
```

`Graph.step()` 与 DSL 的自动包装使用同一套 Graph→Step 语义。

## 构图前静态选择

`select_one()` 在 Graph 组装前从已求值的分支中要求恰好一个结果。它返回分支持有的
原始值，不包装 Step，不创建子图，也不进入 Graph 拓扑：

```python
compiler: Step = select_one(
    StaticBranch(active=use_clang, value=compile_with_clang),
    StaticBranch(active=use_gcc, value=compile_with_gcc),
)

graph = (prepare >> compiler >> install).graph()
```

没有 active 分支或同时存在多个 active 分支都会失败。条件必须由宿主在调用前求值成
`bool`；`select_one()` 不接收 `StaticCondition`，也不读取隐式请求上下文。

如果候选值的创建需要延迟，应先选择构造函数，再只调用选中的函数：

```python
selected_graph = select_one(
    StaticBranch(active=is_android, value=android_graph),
    StaticBranch(active=is_ios, value=ios_graph),
)()
```

这与 `Step.condition` 的语义不同：`Step.condition` 是执行图中一个节点的运行时启用条件；
`select_one()` 是进入 Graph 之前的严格构图选择。

## `LeafAction`

没有结果输入时，调用 `fn()` 或 `fn(ctx)`。两种形式都有效时固定使用零参，
保留 `lambda value=value: ...` 这种闭包默认值。

通过 `wire(inputs={consumer.action: (producer, ...)})` 接入结果后，固定调用
`fn(ctx, first_result, ...)`。每个结果是 `Option[object]`，构图时校验完整签名，
不根据参数名或类型标注猜测调用方式。

```python
def compile_action(ctx: StepRunContext, source: Option[object]) -> None:
    ctx.control.require_running()
    compile_source(source.unwrap())

prepare = Step(prepare_source)
compile_step = Step(compile_action)
graph = Graph.from_wiring((
    prepare.wire(),
    compile_step.wire(inputs={compile_step.action: (prepare,)}),
))
```

`step(action)` 从具名函数或 Graph 名称推导 label，不绑定业务参数，
也不引入第二套执行协议。

## 运行时返回子图

LeafAction 可以在运行时返回 Graph。runtime 会把返回值作为当前 Step 的显式子图
编译成嵌套 execution session，交给本次执行的同一个驱动调度：

```python
def select_graph(ctx: StepRunContext, selected: Option[object]) -> Graph:
    return graph_for(selected.unwrap())

selection = Step(select_graph)
graph = Graph.from_wiring((select_strategy.wire(), selection.wire(inputs={selection.action: (select_strategy,)})))
```

这个能力用于先读取运行时结果，再选择下一层结构。父 Step 等待子图整体完成后才完成；
等待期间不占动作名额。线程执行的所有 Python 子图共用一个线程池，异步执行的所有
子图共用调用方的事件循环。根图、动态子图和 Rust 原生 action 合计受同一
`workers` / `concurrency` 上限约束；串行执行使用显式栈，不递归调用执行器。

此上限计算正在执行的 Graph 叶子动作，不限制 action 自己启动的线程、子进程或模型
请求数量。它保持已有的子图边界、结果引用、能力端口和失败传播规则，不会修改已经
声明的图或允许任意向运行中的图插入节点。纯 Python session 不创建空的 Rust 线程池。

## 条件

条件分成两层：

- `Step.condition`
  - 控制 Step 本身是否参与执行
- `ConditionalEdge`
  - 控制一条 prerequisite 是否成立

Step.condition 和 ConditionalEdge 只接受 RuntimeCondition。
静态条件通过 `StaticConditions(evaluator=..., conditions=...)` 绑定节点或边，
在编译阶段求值；直接构造与 DSL 写法见[条件 Step](./conditional_steps.md)。

运行时条件只声明判断函数，生产者引用放在目标步骤的接线中：

```python
cache_miss = runtime_condition(
    when=lambda result: not bool(result.unwrap()),
)
```

目标通过 `wire(inputs={condition: (cache_probe,)})` 声明结果绑定，产生明确的 ResultEdge，
参与调度、环检测和接线边界计算。条件对象本身不保存生产者引用。

## `GraphExecution`

位置：

- [src/rivetoo_graph/execution.py](../../src/rivetoo_graph/execution.py)

`GraphExecution` 把声明图绑定到一次执行：

```python
execution = GraphExecution(
    graph=graph,
    execution_state=GraphExecutionState(),
    execution_control=GraphExecutionControl(),
)
```

它包含：

- `graph: Graph`
- `shared`：显式注入的业务共享对象
- `execution_state: GraphExecutionState`
- `execution_control: GraphExecutionControl`
- `action_log: GraphActionLog`
- `execution_output: GraphExecutionOutput`

并提供：

- `compile()`
- `run(...)`
- `await arun(...)`
- `require_result(step)`

图名只在 `Graph.label` 声明。编译、渲染和执行输出统一读取 `graph.display_label`：
有名字时使用声明值，匿名图显示为 `<unlabeled>`。这个展示值不是身份，调度和结果
定位继续使用 `StepId`；GraphExecution 不再为图另外起名。

`GraphExecutionState` 只属于当前 GraphExecution 的一次运行。`GraphExecutionControl`
默认同样由 GraphExecution 独享；宿主若需要把多个顺序 GraphExecution 归入同一次停止控制，可以显式
把同一实例传给它们，但不会因此合并两份执行结果。control 收到停止请求后不可
恢复；需要新的执行作用域时必须创建新实例。

`GraphExecution` 本身不读取宿主的全局日志目录、日志上下文或停止状态。独立使用时，
`action_log` 不接入宿主日志设施，`execution_output` 也不写外部文件；需要文件输出时，
宿主显式传入 Graph 提供的 `TreeFileExecutionOutput(output_dir)`。

已有事件循环的程序直接使用异步入口：

```python
state = await GraphExecution(graph=graph).arun(
    AsyncGraphRunOptions(concurrency=8),
)
```

`arun()` 不创建嵌套事件循环；底层 `AsyncGraphExecutor.aexecute()` 同样可被等待。
同步 `run(AsyncGraphRunOptions(...))` 则在自己的事件循环中调用同一条异步执行路径。

## `GraphCompilation`

位置：

- [src/rivetoo_graph/compile.py](../../src/rivetoo_graph/compile.py)

Rust 编译路径会把声明期已知子图转换为统一执行结构：

- 求值静态条件并裁剪未启用节点与边
- 为挂载节点分配 `StepId` 并解析展示 label
- 把运行期条件和 capability binding 降级为执行依赖

- `CompiledStep`
  - 由 Python driver 执行的 Python 叶子
- `CompiledNativeStep`
  - 由 Rust worker 执行的原生叶子，action 保留在 Rust
- `CompiledComposite`
  - 子图边界的非执行生命周期投影
- `roots`
  - 根图直接拥有的 typed refs

`CompiledComposite` 不执行 action，也不生成 no-op 节点。它保留：

- 子图边界
- 直接子节点
- prerequisite
- runtime `condition`
- observation
- 整体完成状态

动态和编译路径使用同一批 `StepId` 与声明作用域映射，因此结果读取语义一致。

## 结果与运行上下文

`wire(after=(prepare,), inputs={consumer.action: (producer,)})` 区分纯执行前置和最终结果绑定。
两者都在图的接线处声明；inputs 已包含等待关系，不必再把同一来源写进 after。

Graph.from_wiring 把 inputs 归一化成
`ResultEdge(source, target, argument_index, receiver)`。直接构造 Graph 时可以放入 edges；
参数索引必须从零连续且不重复。输入顺序与重复来源均保留。
inputs 的键是已有 LeafAction / RuntimeCondition 声明对象，按对象身份区分，
不按内部函数或字段值比较。action、节点条件、每条条件边的条件分别接收自己的输入，
条件函数不接收 ctx。同一对象在一次接线中复用时共享一组绑定；包装同一函数的不同
条件对象可以分别绑定。不同接线及不同挂载之间独立。

键必须属于当前步骤或它的某条条件前置边；无关对象即使绑定空元组也会报错。
不使用函数名查找，不增加端口包装类型。
类型检查时可以保留具名 condition 变量，或先收窄 step.condition；无条件步骤的该字段为 None。

结果绑定到当前挂载；同一子图重复挂载时不会串读其它挂载的结果。
运行时返回的 Graph 可以通过闭包捕获声明动作已经收到的参数，不再通过祖先引用查结果。
预先声明的 Graph-valued Step 没有接收参数的 callable，其 inputs 用于条件判断。
需要把运行期值带入子图时，使用接收结果参数、返回 Graph 的 Python action。

GraphResultStore 按 StepId 将成功结果登记一次，包装为 Some(value)。
显式结果边在生产者执行前自动开启记录。
Nothing() 表示生产者已终态但没有结果，例如被跳过；
Some(None) 表示正常执行并返回 None。生产者失败会阻断消费者，不转换成 Nothing。
输入来源被静态裁剪时编译报错，不静默传入缺失值。

执行状态和观察器公开 GraphResultView，只提供 get/require/has，不提供 record。
快照复制当时的记录，但值本身不深拷贝、不冻结。发布后的值应视为不可变；
需要共同修改的数据使用显式共享状态。

StepRunContext 只提供 step_id、label_path、graph_label_path、step_log_path、
control 和 shared(Type)。不再提供结果查询、结果视图或作用域索引绕行入口。
StepReadContext 与祖先结果引用已移除；执行外部的
GraphExecution.require_result 仍可用于查看结果。

长任务应在自己的循环和外部调用之间协作检查停止状态：

```python
async def task(ctx: StepRunContext) -> None:
    for item in items:
        ctx.control.require_running()
        await process_item(item)
```

宿主通过 `execution.execution_control.request_stop()` 发起停止，也可以取消正在等待
`arun()` 的 asyncio task。驱动会停止派发新动作，并等待已经启动的动作清理退出；
不会强杀 Python 函数或自动回滚副作用。action 若一直不检查停止状态，仍须等它自行
结束。停止处理器在锁外执行，停止后才注册的处理器也会立即执行；多个处理器报错时
全部调用后聚合异常。

日志路径在构造执行对象时声明：

```python
execution = GraphExecution(graph=graph, step_log_paths={step: log_path})
```

构造器对完整图一次性解析全部 Step 引用并保存路径快照；每个引用必须对应唯一的挂载
位置，不再提供构造后的绑定 API。V 个挂载步骤、K 个绑定的处理时间和空间均为
O(V + K)；没有绑定时不遍历图。
叶子动作直接使用自己的绑定；未声明时沿用最近的已绑定祖先路径。静态子图和 action
动态返回的子图也沿用所属步骤的路径；action 不提供运行时修改绑定的入口。

## 执行级共享状态

`GraphExecution(shared=...)` 接收调用方显式构造的业务对象，各 Python action 通过
`ctx.shared(ExpectedType)` 取得同一个对象。此入口返回确定的类型，并在类型不符或未
提供共享对象时明确报错；不创建默认字典，也不按类型查找或创建其他实例。

```python
from dataclasses import dataclass, field
from threading import Lock

from rivetoo_graph import GraphExecution, Step, StepRunContext, ThreadGraphRunOptions


@dataclass
class Progress:
    completed: int = 0
    lock: Lock = field(default_factory=Lock)

    def advance(self) -> None:
        with self.lock:
            self.completed += 1


def task(ctx: StepRunContext) -> None:
    progress = ctx.shared(Progress)
    progress.advance()


graph = (Step(task) >> Step(task)).graph()
progress = Progress()
GraphExecution(graph=graph, shared=progress).run(ThreadGraphRunOptions(workers=2))
assert progress.completed == 2
```

共享对象绑定到执行入口，不进入 `Graph` 声明、`GraphExecutionState`、结果 store 或
执行快照。静态子图和 action 动态返回的子图沿用同一引用；每个 action 只增加 O(1)
引用传递，不复制对象。直接使用串行、线程或异步执行器时，同样可以传 `shared=`。

对象的锁、资源释放和使用寿命由调用方负责；多种共享能力可以组合进一个有类型的
对象。多次执行若需要隔离，应分别构造共享对象；显式复用同一实例就表示共享。
共享状态不产生调度依赖，读取若必须等待写入完成，仍须声明边。动态条件只接收只读
Option 参数，不会因此获得共享状态。此接口不把 Python 对象投影给 Rust 原生 action。

## Observation 与执行状态

`GraphObservationMessages` 只保存只读展示消息：

- `before`
- `after_success`

runtime 通过外部注入的 `GraphObserver` 发出 `GraphObservationEvent`。observer 用于
trace、timing 和展示，不修改执行结果，也不替代显式 Step。

需要跟踪步骤状态时，显式订阅独立的执行事件：

```python
from rivetoo_graph import GraphExecution, StepExecutionEvent


class Events:
    def emit(self, event: StepExecutionEvent) -> None:
        print(event.step_id, event.label_path, event.phase)


execution = GraphExecution(graph=graph, events=Events())
execution.run()
```

`StepExecutionPhase` 有 `STARTED`、`COMPLETED`、`FAILED`、`BLOCKED`、`SKIPPED`。
状态已提交后才通知；静态子图、动态子图、Python 和原生步骤都使用挂载后的身份与
标签路径。组合边界的开始/完成事件不占动作名额，也不依赖 dynamic/compiled 展示模式。
被阻塞或跳过的节点不会先收到开始事件。

事件回调同步、串行执行，应保持短小，耗时会计入执行时间。回调报错抛出
`GraphEventDeliveryError` 并停止执行；不会把已经完成的 Step 重新标成失败。
未配置 `events` 时不构造事件、标签索引或消息文本，也不复制结果表。事件只描述运行
状态，不负责消息传递、持久化或从历史状态恢复。

`GraphExecutionState` 只服务一次执行，记录：

- 已执行和已完成的 StepId
- 已进入和已完成的 composite
- skip 状态
- failed / blocked 状态与节点失败记录
- runtime condition 结果
- 可选 `GraphResultStore`

同一个状态不能用于第二次 run，也不提供 reset 或兜底恢复语义。
