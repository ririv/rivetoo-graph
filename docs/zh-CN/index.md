# rivetoo-graph

[English](../index.md) | 简体中文

`rivetoo-graph` 是一个显式、可组合的 Python DAG 声明与执行库。Python 导入入口为：

```python
from rivetoo_graph import Graph, GraphExecution, Step
```

库本身提供：

- 纯有向拓扑与 DAG 校验
- Step、Graph 和声明期接线
- `>>` 串行与 `|` 并列组合
- 构图前要求恰好一个结果的静态选择
- 静态条件、运行时条件和条件边
- 子图边界与能力端口
- 统一 Rust 编译执行路径，以及 dynamic / compiled 两种展示模式
- 线程与异步调度后端
- 执行控制、结果读取、观察事件和渲染

## 设计目标

- `Graph` 只表达声明结构，单次执行状态由 `GraphExecution` 持有
- 声明期直接引用 Step 对象，挂载后才分配内部 `StepId`
- 依赖、条件读取和能力绑定全部显式参与拓扑校验
- 子图可以组合，但不会被声明 DSL 隐式展开
- 宿主日志、终端、请求状态和领域流程通过公开端口接入
- 不读取宿主进程中的隐式全局设施

## 文档

- [核心模型](./core_model.md)
- [拓扑复杂度与实现开销](./topology_complexity.md)
- [执行语义](./execution.md)
- [Python 图与 Rust 原生图](./native-graphs.md)
- [条件边](./conditional_edges.md)
- [条件 Step](./conditional_steps.md)
- [能力依赖与子图端口](./capabilities.md)
- [Rust 架构与语言边界](./rust-architecture.md)

## 历史基准

以下记录保留测量负载与结果，不属于当前 API 说明，也不承诺最新版本的性能。

- [大规模 Recipe 图基准](./recipe_scale_benchmark.md)
- [Step 开销基准](./step_overhead_benchmark.md)
- [工作流运行与并发基准](./workflow_runtime_benchmark.md)

## 核心边界

- `Graph`
  - 声明态 DAG 结构
- `GraphExecution`
  - 声明图与一次执行作用域
- `Step`
  - 可选展示 label、条件，以及 Python action、Python 子图或完整 Rust 原生子图
- `Graph.edges`
  - 显式接线关系
- `RuntimeCondition.reads`
  - 条件求值前必须完成的结果依赖
- `StepWiring` / `GraphWiring`
  - 声明期组合结构
- `GraphCompilation`
  - 正式 runtime 使用的执行结构与 composite 边界

声明期身份、展示 label 与运行时 `StepId` 是三层不同语义。调度和结果定位使用
`StepId`；label 只用于展示。

## 不属于 Graph core 的内容

Graph core 不知道任何具体宿主领域，例如：

- 领域实体或领域阶段
- 领域缓存策略或产物
- 领域配置或工作目录
- CLI 请求、日志目录或进程级信号处理

这些内容应由宿主先建模，再组合为 Graph 或通过 Graph 的公开端口接入。Graph core
不为某个宿主领域提供隐式分支。

## 当前限制

- 动态子图与根图共享动作预算，但 action 内部自行启动的工作不受此预算控制
- `GraphExecutionState` 只允许一次执行，不提供 reset 或复用语义
- Graph runtime 不聚合宿主领域结果
- 运行时返回的子图无法提前编译，但返回后会立即进入同一 Rust 编译器

## 文档语言

英文是默认入口。中文版保留相同文件名，每个页面提供语言切换链接。
修改 API 语义、示例或限制时，应同步更新两种语言。历史基准的原始样本保持不变，
结论以对应日期和测量负载为准。
