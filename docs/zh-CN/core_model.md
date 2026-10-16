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
- `static_condition_eval: StaticConditionEval | None`

`Graph` 不负责执行，也不带 `run()`。

`label` 只用于展示，不是调度身份。它可以省略：

- 根图省略 label 时，显示为 `<unlabeled>`
- 嵌套图省略 label 时，使用承载它的 Step 的已解析 label
- Graph 显式声明 label 时，该值优先

显式接线只保存在 `Graph.edges` 中。规范化后的 `Graph` 不接受 `StepWiring`，
Step 本身不保存 `after`。运行时条件的 `reads` 是另一类显式阻塞关系，同样参与
校验与调度，不能忽略。

## `Step`

`Step` 是声明态执行节点，包含：

- `label: str | None`
- `action: LeafAction | Graph | NativeGraph`
- `when: StaticCondition | RuntimeCondition | None`

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
- 普通边与条件 `reads` 的跨图引用会在 Graph 构造时失败；跨子图同步使用能力端口

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
)
```

两种边都引用真实 Step 对象：

- `UnconditionalEdge(source, target)`
- `ConditionalEdge(source, target, condition)`

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
            android_shim,
            condition=android_only,
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
    when=condition,
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

这与 `Step.when` 的语义不同：`Step.when` 是执行图中一个节点的静态存在条件；
`select_one()` 是进入 Graph 之前的严格构图选择。

## `LeafAction`

`LeafAction` 只接受两种 callable：

```python
Callable[[], object]
Callable[[StepRunContext], object]
```

它在构造时检查签名并固定调用分支，runtime 不再猜测或转换参数：

- 零参数 action 调用 `fn()`
- 上下文 action 调用 `fn(run_ctx)`

业务参数由闭包绑定：

```python
Step(
    lambda: generate_pkg_config_file(layout, version),
    label="pkgconfig",
)
```

同时需要业务参数和运行上下文时，业务参数仍由闭包捕获，唯一显式形参留给
`StepRunContext`：

```python
Step(
    lambda run_ctx: compile_source(
        compiler,
        run_ctx.require_value(prepare),
    ),
    label="compile",
)
```

若 callable 同时支持零参数和单参数调用，固定按零参数调用。这样
`lambda value=value: ...` 的默认参数不会被运行上下文覆盖。

`step(action)` 是轻量声明入口：

- 普通函数使用函数名作为 label
- `LeafAction` 使用其函数名作为 label
- Graph 使用 `Graph.label`
- lambda 或无名 callable 保持无 label

它不绑定业务参数，也不提供另一套运行协议。

## 运行时返回子图

LeafAction 可以在运行时返回 Graph。runtime 会把返回值作为当前 Step 的显式子图
编译并在嵌套 execution session 中执行：

```python
def select_graph(run_ctx: StepRunContext) -> Graph:
    selected = run_ctx.require_value(select_strategy)
    return graph_for(selected)
```

这个能力用于先读取运行时结果，再选择下一层结构。它不会展开或改写返回的 Graph，
也不会创建隐藏执行入口。

## 条件

条件分成两层：

- `Step.when`
  - 控制 Step 本身是否参与执行
- `ConditionalEdge`
  - 控制一条 prerequisite 是否成立

条件值是和类型：

- `StaticCondition`
  - 在编译阶段求值
- `RuntimeCondition`
  - 在 runtime 中求值

运行时条件使用 Step 对象声明读取依赖：

```python
cache_miss = runtime_condition(
    reads=(cache_probe,),
    when=lambda ctx: not bool(ctx.require_value(cache_probe)),
)
```

`reads` 不是普通 prerequisite，但会阻塞条件求值，因此与 Graph edges 一起参与环检测，
并在引用两端均已进入当前 wiring 后参与入口和出口计算。

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
- `execution_state: GraphExecutionState`
- `execution_control: GraphExecutionControl`
- `action_log: GraphActionLog`
- `execution_output: GraphExecutionOutput`

并提供：

- `compile()`
- `run(...)`
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
- runtime `when`
- observation
- 整体完成状态

动态和编译路径使用同一批 `StepId` 与声明作用域映射，因此结果读取语义一致。

## 结果与运行上下文

位置：

- [src/rivetoo_graph/results.py](../../src/rivetoo_graph/results.py)

结果存储按 `StepId` 保存成功 Step 的返回值。声明 action 不直接使用 StepId，而是
通过 Step 对象读取：

```python
def compile_action(run_ctx: StepRunContext) -> None:
    source = run_ctx.require_value(prepare)
    compile_source(source)
```

`GraphResultStore` 是本次执行的结果写入端；同一 StepId 只能记录一次，不允许覆盖。
执行状态的 `results`、action 上下文和 observation event 均只暴露 `GraphResultView`，
提供 `get/require/has`，没有 `record`。这些入口共享同一个实时视图，不会为每个 Step
复制结果表。`GraphExecutionState.snapshot()` 则复制当时的记录，并以只读视图公开。

只读限制针对结果表，不是对返回对象的深度冻结：返回值仍是原对象，不应在发布结果后
继续修改其中的可变内容。需要共同修改的数据使用独立的共享状态，不改写步骤结果。

同级引用直接传 Step。跨到祖先图时使用：

```python
ParentGraphStepRef(
    step=outer_prepare,
    levels_up=1,
)
```

不支持字符串引用，也不会自动向最近祖先搜索。

`StepRunContext` 把只读视图和运行控制组合在同一个 action 入口：

- `view: StepReadContext`
- `control: StepControl`
- `step_log_path`

常用只读 API：

- `result(step)` / `require_result(step)`
- `value(step)` / `require_value(step)`
- `has_result(step)`
- `step_id`
- `label_path`
- `graph_label_path`

副作用控制统一位于 `run_ctx.control`：

- `skip_step(step_id)`
- `skip_steps(step_ids)`

日志路径在构造执行对象时声明：

```python
execution = GraphExecution(graph=graph, step_log_paths={step: log_path})
```

构造器对完整图一次性解析全部 Step 引用并保存路径快照；每个引用必须对应唯一的挂载
位置，不再提供构造后的绑定 API。V 个挂载步骤、K 个绑定的处理时间和空间均为
O(V + K)；没有绑定时不遍历图。
叶子动作直接使用自己的绑定；未声明时沿用最近的已绑定祖先路径。静态子图和 action
动态返回的子图也沿用所属步骤的路径；action 不提供运行时修改绑定的入口。

## Observation 与执行状态

`GraphObservationMessages` 只保存只读展示消息：

- `before`
- `after_success`

runtime 通过外部注入的 `GraphObserver` 发出 `GraphObservationEvent`。observer 用于
trace、timing 和展示，不修改执行结果，也不替代显式 Step。

`GraphExecutionState` 只服务一次执行，记录：

- 已执行和已完成的 StepId
- 已进入和已完成的 composite
- skip 状态
- failed / blocked 状态与节点失败记录
- runtime condition 结果
- 可选 `GraphResultStore`

同一个状态不能用于第二次 run，也不提供 reset 或兜底恢复语义。
