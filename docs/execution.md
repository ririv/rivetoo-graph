# 执行语义

## Dynamic / Compiled 模式

`GraphPlan` 保留两个公开模式，但二者不再对应两套执行器：

- dynamic
  - `GraphPlan.run(mode="dynamic")`
  - 使用 Rust 声明编译器和 Rust execution session
  - 为兼容结构视图，composite 同时记录到 `executed_steps/completed_steps`
  - capability binding 与 compiled 模式使用同一 lowering
- compiled
  - `GraphPlan.compile()`
  - `GraphPlan.run(mode="compiled")`
  - 使用同一个 Rust 声明编译器和 Rust execution session
  - composite 只记录专门的进入与完成状态
  - 支持 capability binding lowering

正式 runtime 只接收 `GraphCompilation`。`FrozenGraph` 与 `freeze_graph()` 仍是公开的
结构实例化 API，但不再承载另一套 Python 调度路径。action 在运行期返回的 `Graph`
会携带父 step id、label path 与父结果作用域，再进入同一个编译执行流程。

## 条件执行

条件模型现在分成两类：

- [conditional_edges.md](./conditional_edges.md)
  - 解决依赖关系是否成立
- [conditional_steps.md](./conditional_steps.md)
  - 解决 step / 子图本身是否启用

这里只保留执行层面的统一结论：

- 两种模式共同：
  - Rust 编译器裁剪 `StaticCondition`
  - 运行时条件边编译为 `CompiledConditionalDependency`
  - step 级 runtime `when` 编译为 `CompiledRuntimeCondition`
  - 条件位于子图内部时，叶子仍然扁平化
  - 条件为假时节点进入 skipped 终态；条件依赖为假时只移除该依赖
  - 每个 predicate 在一次 compiled run 中只求值一次

## 图渲染

位置：

- `rivetoo-graph/src/rivetoo_graph/render/`

当前渲染接口分两类：

- `render_graph(...)`
  - 接受 `Graph | GraphCompilation`
  - 必须显式接收根执行 `root_label`，不从 `Graph.label` 推导
  - `mode="dynamic"` 时返回终端友好的组合结构树
  - `mode="compiled"` 时返回 compilation 视图文本
  - 若传入 `GraphCompilation` 且 `mode="dynamic"`，按 `source_graph` 的组合结构渲染
- `render_graph_mermaid(...)`
  - 接受 `Graph | GraphCompilation`
  - 必须显式接收根执行 `root_label`，不从 `Graph.label` 推导
  - `mode="dynamic"` 时返回原始 `Graph` 的结构 mermaid
  - `mode="compiled"` 时返回 compilation mermaid
  - 若传入 `Graph` 且 `mode="compiled"`，内部会先 `compile_graph(...)`

另外保留一个更具体的 compilation mermaid 入口：

- `render_graph_compilation_mermaid(compilation, root_label=...)`

## Run Options

位置：

- `rivetoo-graph/src/rivetoo_graph/runtime/run.py`

当前运行选项拆成两类：

- `ThreadGraphRunOptions`
  - `workers`
  - `observer`
- `AsyncGraphRunOptions`
  - `concurrency`
  - `observer`

语义固定为：

- 线程后端使用 `workers`
- 异步后端使用 `concurrency`
- 两者默认值都为 `1`
- `observer` 是可选的
- 未传 `observer` 时，不会发出 graph observation
- `GraphPlan.run(options=..., mode=...)` 接受它们的联合类型

## 执行控制

`GraphExecutionControl` 保存停止状态和停止处理器，不保存 step 结果。并行或异步
runtime 捕获到首个失败时，会通过当前 control 请求停止；宿主可以注册资源停止
处理器，在同一信号下回收仍在运行的子进程。

control 是一次性的执行作用域：一旦收到停止请求，就不会恢复，也不提供 `reset()`。
运行前已经停止的 Plan 会直接抛出 `GraphExecutionStopped`；运行过程中收到停止请求
后，runtime 不再派发新的 ready step，但会等待已经启动的 step 结束，再报告停止。

`GraphPlan` 默认创建独享 control：

```python
plan = GraphPlan(label="build", graph=graph)
```

独立使用 Graph 时，不会触碰宿主的进程级停止状态。若宿主把多个顺序 Plan 视为同一
次请求，可以显式共享一份 control：

```python
control = GraphExecutionControl()
first_plan = GraphPlan(label="first", graph=first_graph, execution_control=control)
second_plan = GraphPlan(
    label="second",
    graph=second_graph,
    execution_control=control,
)
```

这仍然是两次顺序执行，而不是在一个 Plan 的 step 内嵌套启动另一个 Plan。两个
Plan 各自持有独立的 `GraphExecutionState`；共享的只有请求停止与宿主资源回收信号。
前一个 Plan 失败后，后一个 Plan 会在开始执行前看到停止状态，因此不会继续运行。
信号处理、子进程终止和多个 Plan 的请求级协调由宿主负责，不属于 Graph core。

## 宿主运行设施边界

Graph runtime 只负责图本身的调度、状态和结果，不隐式读取宿主的进程级设施。
`GraphPlan` 通过两个独立端口接入宿主：

- `GraphActionLog`
  - 为 action 建立宿主日志作用域
  - 在并行 action 抛出 Python 异常时追加异常文本
- `GraphExecutionOutput`
  - 在一次执行结束或失败后消费最终图状态
  - Graph 提供 `TreeFileExecutionOutput(output_dir)`，把执行视图写入显式目录

独立使用 `GraphPlan` 时，这两个端口采用明确的 standalone 行为：既不接入宿主日志，
也不产生外部执行视图文件。停止控制只由独立的 `GraphExecutionControl` 负责，不属于
action 日志端口。需要日志或执行视图时，宿主必须显式传入自己的 `GraphActionLog`
实现和 `GraphExecutionOutput`；核心不会探测全局目录或静默切换行为。

## 执行规则

统一 runtime 在：

- `rivetoo-graph/src/rivetoo_graph/runtime/`

当前执行规则：

1. 图必须是 DAG
2. 按 `Graph.edges` 做拓扑执行
3. 若图里有 `RuntimeCondition`：
   - 条件边按运行期结果判定依赖是否生效
   - `Step.when` 按运行期结果判定当前节点是否启用
4. 若 `Step.when=False`：
   - 该 step 直接视为不存在
   - 不等待自己的 prerequisite
   - 不进入子图 action
5. 声明期子图由 Rust 编译为 composite；运行期返回图重新进入同一编译器
6. `ExecutionSession` 批量派发叶子，并用 `CompiledStep.context` 恢复声明作用域
7. composite 只在协调线程维护生命周期，不提交给 worker，不写入 `executed_steps`
8. 条件边保留为 `CompiledConditionalDependency`，不生成伪造的 gate step
9. `GraphExecutionState` 可显式 skip 某个 step path
10. 若 skip 的是组合节点路径，compiled 模式下其所有后代路径也会一起跳过
11. 若启用了结果模块，成功 step 会把结果写入 `GraphExecutionState.results`
12. 每个运行时条件在一次 graph run 中只求值一次；其它依赖尚未完成时复用已判定结果
13. 执行渲染读取记录下来的条件结果，不重新执行 predicate
14. step 抛错即终止，不做 fallback

`RuntimeCondition.reads` 虽然不是普通 Graph edge，但它会阻塞条件求值，因此参与
`Graph`、`FrozenGraph` 和 `GraphCompilation` 的 DAG 环检测。读取依赖和边共同成环时，
会在构造或编译阶段直接失败，不会留到 runtime 报 unresolved dependencies。

## 为什么当前不提供 runtime hooks

当前 Graph runtime 仍然不提供 side-effectful hooks，例如：

- `before_step`
- `after_step`
- cache probe / cache writeback hook
- skip 决策 hook

原因不是 hooks 做不到，而是它们会把核心执行语义从图上挪走。

当前模型刻意要求这些行为都留在声明层显式表达：

- cache：显式 probe step / writeback step
- log：声明层 action wrapper
- skip：由宿主显式写入 `GraphExecutionState`

这样图本身就能直接表达：

- 哪些步骤会做 cache probe
- 哪些步骤可能写回外部状态
- 哪些步骤会被跳过

如果这些行为进入 runtime hooks，图上就看不出真正流程，执行语义也会变隐式，不符合当前“显式、可追踪、组合优先”的约束。

当前已经提供只读 observer/event 接口，用于 trace、timing、可视化与图级摘要输出；但它不应修改执行结果，也不替代显式 graph step。

## DAG 约束

`Graph` 在构造阶段就会校验：

- graph label 非空
- `steps` 只包含 Step
- 同一个 Step 对象没有重复挂载
- edge 的 source / target 都属于当前图
- 无环

Graph 会把显式边和运行时条件读取投影为 `DirectedTopology`，再通过
`DAGTopology` 前置完成环检测；图一创建就失败，不等到执行期。

## 子图语义

父图中的 `step.action = subgraph` 是声明态组合节点。

Rust compiler 会转换所有声明期已知子图：

- 叶子 action 进入全局扁平执行集合
- 原子图边界变成非执行的 `CompiledComposite`
- composite 激活后才允许其直接子节点进入 ready
- 全部直接子节点终态后 composite 才完成
- 空 composite 激活后立即完成
- `GraphObserve.before/after_success` 挂在 composite 生命周期上

因此增加 runtime condition、条件边或 observation 不会改变执行模型。action 在
运行期返回的 `Graph` 无法提前编译；返回后会以当前叶子路径为显式输入，建立一个
嵌套 compilation 与 execution session。

对子图中的 action 来说，runtime 当前还会显式补齐：

- 当前图的 `graph_step_ids: Mapping[Step, StepId]`
- 祖先图作用域链 `ancestor_graph_scopes`

所以子图里既可以：

- 读取当前图里的同级 step
- 也可以通过 `ParentGraphStepRef(step=..., levels_up=...)`
  显式读取外层图里的 step

## 多入口子图

子图允许多个入口节点。

例如：

- `prepare_a`
- `prepare_b`
- `build.wire(after=(prepare_a, prepare_b))`

这在当前模型下合法。多个入口只是“同时 ready”，不是“同时并行执行”。

## 当前并发语义

当前 Graph runtime 通过运行选项联合类型控制执行后端与并发度：

- `ThreadGraphRunOptions(workers=...)`
  - 同步 step 直接执行，异步 step 通过 `asyncio.run(...)` 桥接
- `AsyncGraphRunOptions(concurrency=...)`
  - 异步 step 直接 await，同步 step 通过 `asyncio.to_thread(...)` 桥接
  - 同步和异步 action 的 component log 都通过 `ContextVar` 保持任务级隔离

并发语义当前保持保守：

- 失败即停止派发新的 ready step
- 已经在跑的 step 等自然结束
- 异步后端不会取消已经创建并开始运行的 task
- 运行期返回图通过嵌套 compilation/session 执行

不要依赖两个互不相干 Step 的隐式顺序；如果顺序有业务意义，必须通过
`Step.wire(after=...)`、Graph edge 或 `>>` 显式声明。

## 并发边界

Graph 已提供线程与异步两种受控调度后端，但仍保持以下边界：

- 并发上限由显式 run options 决定
- 只调度拓扑上已经 ready 的节点
- 失败后停止派发新节点
- 已经开始执行的同步 worker 或异步 task 不会被 Graph 强制取消
- 运行期返回图的嵌套 session 暂不共享外层 session 的全局 ready 队列
- action 对宿主共享状态的并发写入安全由宿主负责

因此 Graph 提供的是 DAG 调度并发，不是宿主资源、进程或事务的全局并发管理器。
