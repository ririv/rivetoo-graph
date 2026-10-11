# Rust 架构与语言边界

[English](../rust-architecture.md) | 简体中文

`rivetoo-graph` 保留 Python 声明 API，正式执行使用 Rust 声明编译器和调度状态机。
Rust 持有完整的编译图、调度索引和单次执行状态；Python 保留任意 callable、
结果对象与宿主集成。本文描述已实现的边界，不是待执行的迁移计划。

## 分支

- `python`
  - 保留 Rust 迁移开始前的纯 Python 基线
- `main`
  - Rust 化主线

`python` 是历史对照，不是正式执行兜底。`main` 不保留第二套 Python 调度状态机。

## crate 边界

当前 workspace 包含：

- `crates/action`
  - 定义 Rust `NativeAction` trait 与稳定错误边界
  - 提供不回调 Python 的进程 action
  - 不依赖 PyO3、Tokio 或 Graph core
- `crates/native-graph`
  - 持有 `NativeStep`、graph-owned arcs 与完整 `NativeGraph`
  - 原生 adapter 以整图为边界接入，不向 Python 暴露单个 action
- `crates/topology`
  - 只依赖 Rust 标准库，持有整数节点、邻接索引和 DAG 算法
  - 不认识 action、条件或宿主领域
- `crates/core`
  - package 名为 `rivetoo-graph`
  - 依赖 workspace 内的 topology，不依赖第三方库
  - 持有声明编译、编译执行图、能力解析和调度状态机
- `crates/python`
  - 私有 Python adapter
  - 独占 PyO3 依赖
  - 把 Python 声明对象批量转换成连续整数 ID
  - 把条件 predicate、Python action、观察事件和宿主停止控制接回 Python
  - 把 Rust action 交给原生实现执行
- `crates/tokio-runtime`
  - 私有实验 crate，不属于正式 API
  - 独占 Tokio 依赖，用于测量“Rust runtime 逐 action 回调 Python”的成本
  - 只通过项目自己的 `CallbackHost` 接口消费 core，不向领域模型泄漏 Tokio 类型

core 的公开数据模型不出现 PyO3 或其它第三方库类型。

## 外部库约束

核心 crate 不直接依赖第三方图、调度或并发库。确实需要外部实现时，新增独立
adapter crate，由该 crate 独占外部依赖，并通过项目自身定义的窄接口接入 core。

例如未来若采用第三方 work-stealing runtime，应形成：

```text
core Scheduler 接口
        ▲
        │ 项目自有模型
executor adapter crate
        │
        ▼
第三方 runtime
```

不得让第三方节点、任务、错误或 executor 类型进入 Graph 的领域模型和公开 API。
移除或替换第三方实现时，Python recipe、Graph 声明和 core 模型不应跟着修改。

## 当前执行边界

所有正式执行路径共享同一套 Rust 声明编译器和 execution session 实现：

```text
Python Graph 声明
    │ 单次 PyO3 编译调用
    ▼
Rust GraphDeclaration
    ├── 静态节点裁剪
    ├── 层级与声明作用域收集
    ├── 显式 capability binding 的端口定位
    └── 条件与 prerequisite 投影
    │
    ▼
Rust CompiledExecutionGraph
    ├── 层级 ExecutionGraphDeclaration lowering
    ├── 连续 NodeId / GraphId
    ├── graph membership → owner / roots / children
    ├── 条件 reads 与 prerequisite 索引
    ├── dependents 索引
    ├── capability binding lowering
    └── DAG 校验
    │
    ▼
Rust ExecutionSession
    ├── Python action → 批量返回 Python execution driver
    │                   ├── 任意 Python callable
    │                   ├── ThreadPoolExecutor
    │                   └── asyncio task
    └── NativeGraph 中的 Rust action → Rust worker 直接执行 NativeAction
                        └── 完成进入 Rust completion queue
```

正式 runtime 不逐 action 反向调用 Python；编译时静态条件叶子的求值仍会调用 Python。
Python 在有状态变化时通过 `start/advance` 与 session
交换一批数据：

- Rust 只把 ready 的 Python action、待求值 predicate 和生命周期 event 批量返回 Python
- `NativeGraph` 在声明编译时一次接入；内部 action handle 留在 Rust
- driver 只把 Python action 的完成节点、predicate 结果和显式 skip 批量交回 Rust
- 混合 driver 批量取走 Rust completion，再和 Python 完成节点一起推进同一 session
- 没有 timer polling；没有工作时，线程或 asyncio executor 等待实际 action 完成

声明期已知子图由 Rust 编译为 composite 与 leaf；子图边界不会被隐式删除。action 在
运行期返回的新 `Graph` 无法提前编译，因此会以当前 step id、label path 和父作用域作为
显式输入，再次进入同一个 Rust 编译器，并创建嵌套 session，而不是回到 Python dynamic scheduler。
嵌套 session 当前不共享外层的全局并发预算；这与声明期挂载的子图不同。

`mode="dynamic"` 与 `mode="compiled"` 目前保留的是兼容的展示和 composite 记录语义，
不再代表两套执行引擎。capability binding 在两种模式下进入同一 lowering。

## 运行时实验结论

Tokio 实验位于 `crates/tokio-runtime`，入口只在 `_native` 中以私有名称暴露；正式
executor 不依赖它。`benchmarks/runtime_drivers.py` 使用同一张图和同一组零成本 Python
action，对比：

- 正式方案：Rust 批量返回 ready，Python 执行后批量 `advance`
- 实验方案：Tokio `spawn_blocking`，每个 action 通过 PyO3 反向调用 Python

2026-08-28 在 CPython 3.13（GIL 启用）、macOS arm64、release 原生扩展上，2,000 个
独立空 action、8 worker、5 次采样的中位数为：

- batch：约 0.321 秒
- Tokio callback：约 1.280 秒
- Tokio callback / batch：约 3.98 倍

该基准专门放大调度边界成本，不代表真实构建耗时比例；真实 action 较重时差距会被
稀释。这是当时版本、机器和工作负载下的结果，不能据此断言任何 Rust runtime 都更慢，
也不是当前版本的性能数值。后续同步 action 快速路径的测量见
[Step 开销基准](./step_overhead_benchmark.md)。
Tokio 仍保留为可重复实验，正式路径选择事件驱动的混合协议。Rust action 则改变了
action 所有权：Python 只挂载完整 `NativeGraph`，原生 leaf 的 action
调用不经过 Python；混合 driver 只在批次边界接收完成事件并推进统一 session。

## 唯一事实来源

Rust lowering 接受图作用域和节点成员关系，而不是同时接受 `owner`、roots 和 children：

- graph scope 使用 `Root | OwnedBy(NodeId)` 表达互斥状态
- node 使用 `Leaf | Composite { child_graph }` 表达互斥行为
- 每个 node 只声明自己属于哪个 `GraphId`

Rust 会校验 composite 与 child graph 的双向关系，再从 graph membership 派生 node
owner；roots 和 composite children 最终只从 owner 建立。这样层级关系不会出现三套可
互相矛盾的声明。定位后的 capability binding 也作为独立结构输入交给 Rust，再由 Rust
lower 为普通 prerequisite；Python 展示模型从 Rust 的最终 prerequisite 恢复，不保留
另一套改写规则。

节点行为使用和类型区分：

- leaf
- composite

prerequisite 同样使用和类型区分：

- unconditional
- conditional

运行时条件由 `ConditionId` 引用；predicate 对象保留在 Python adapter，core 只保存
其显式 `reads`。因此 core 可以判断阻塞关系和环，但不会解释 Python 业务函数。

## 领域依赖图的边界

package 选择、版本、选项、平台条件等属于宿主的依赖解析业务，不属于执行图核心。
本仓库不规定宿主的依赖图必须迁入 Rust，也不把某个宿主的迁移计划作为 Graph API。

Python 的通用拓扑 API 与 Rust 的 topology crate 分别提供纯图操作。宿主可以按自己的
数据边界选择使用；执行图内部使用 Rust topology，并不意味着 Python 的通用拓扑 API
已经成为它的 FFI 包装。若未来需要跨语言批量分析，应先测量完整数据转换与算法成本，
避免逐节点往返，并继续让不稳定的领域语义留在宿主。

## 保留的宿主边界

能力声明先由 Python adapter 验证端口对象确实属于对应子图，再把 component、capability、
requirement 与 selection 映射成连续整数 ID。provider 索引、任意 provider 的唯一性、
显式 provider 选择、重复与混合 requirement 校验均在 Rust 中一次完成；Python 不保留
第二套 provider 求解规则。

当前仍留在 Python 的内容都是宿主边界，而不是另一套执行图算法：

- 静态条件叶子和运行时条件 predicate 是 Python callable
- Python action、结果对象、observe、日志路径和展示 label 是 Python 对象
- Rust action 是 `NativeGraph` 内部的独立和类型分支；当前成功结果固定为 `None`，不会
  携带任意 Python 对象，也不作为单个 Python API 暴露
- PyO3 adapter 在一次编译调用中遍历声明对象；静态 predicate 的业务求值仍调用 Python
- Python `GraphCompilation` 只恢复 action、predicate、observe、label path 与结果作用域侧表
- `GraphExecutionState` 保存可供 Python action 动态写入的 skip、结果与渲染快照

ready 队列、节点状态机、条件阻塞、层级生命周期、执行 DAG 校验、capability provider
求解与 binding lowering 均只有 Rust 一份实现。继续把上述宿主对象包装成原生对象只会
增加逐对象 FFI，不属于执行图核心迁移。
