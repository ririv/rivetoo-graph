# Rust 架构与迁移边界

`rivetoo-graph` 保留 Python 声明 API，并把规模敏感的编译执行图逐步迁移到 Rust。
迁移目标不是为零散 Python 类型增加原生包装，而是让 Rust 一次持有完整的编译图、
调度索引和单次执行状态。

## 分支

- `python`
  - 保留 Rust 迁移开始前的纯 Python 基线
- `main`
  - Rust 化主线

两条分支从同一个 Python 基线提交分出。`main` 不为旧实现保留并行状态机；某层语义
完成迁移并通过行为测试后，删除被替代的 Python 实现。

## crate 边界

当前 workspace 包含：

- `crates/core`
  - package 名为 `rivetoo-graph`
  - 只依赖 Rust 标准库
  - 持有纯拓扑、编译执行图和调度状态机
- `crates/python`
  - 私有 Python adapter
  - 独占 PyO3 依赖
  - 把 Python 声明对象批量转换成连续整数 ID
  - 把条件 predicate、leaf action、观察事件和宿主停止控制接回 Python

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

当前 compiled 与 dynamic 路径共享同一个 Rust scheduler：

```text
Python Graph
    │ freeze 与层级结构收集
    ▼
Python GraphCompilation 元数据
    │ 一次批量转换
    ▼
Rust CompiledExecutionGraph
    ├── 层级 ExecutionGraphDeclaration lowering
    ├── 连续 NodeId / GraphId
    ├── graph membership → owner / roots / children
    ├── 条件 reads 与 prerequisite 索引
    ├── dependents 索引
    ├── capability provider 解析与歧义校验
    ├── capability binding lowering
    └── DAG 校验
    │
    ▼
Rust CompiledScheduler
    ├── 串行协调
    ├── 标准库线程 worker 协调
    └── asyncio leaf 调度协调
```

Python 不再调用 `take_ready`、`complete` 等细粒度原生方法。每次 compiled run 只进入
一次 Rust 调度入口；Rust 只在以下真实宿主边界回调 Python：

- 求值不透明的运行时 predicate
- 执行 Python leaf action
- 记录组合生命周期和观察事件
- 读取或写入宿主停止控制

dynamic 路径不会展开子图边界。每层 `FrozenGraph` 会批量物化成一个浅层 Rust 调度图；
图 action 作为该层 leaf 由 Python 回调递归进入下一层，下一层再独立进入 Rust scheduler。
因此运行期返回的图不需要伪装成声明期 compiled 节点，但 ready 队列、条件缓存、依赖
刷新和串行/线程协调都不再由 Python 实现。

## 唯一事实来源

Rust lowering 接受图作用域和节点成员关系，而不是同时接受 `owner`、roots 和 children：

- graph scope 使用 `Root | OwnedBy(NodeId)` 表达互斥状态
- node 使用 `Leaf | Composite { child_graph }` 表达互斥行为
- 每个 node 只声明自己属于哪个 `GraphId`

Rust 会校验 composite 与 child graph 的双向关系，再从 graph membership 派生 node
owner；roots 和 composite children 最终只从 owner 建立。这样层级关系不会出现三套可
互相矛盾的声明。冻结后的 capability binding 也作为独立结构输入交给 Rust，再由 Rust
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

## 依赖图的后续迁移

依赖图暂时继续使用 Python 模型，但 core 的 `NodeId`、`Arc`、`DirectedTopology` 和
`DagTopology` 保持领域无关，未来可以直接复用。

依赖图接入时不能把通用拓扑包装成逐方法 FFI API，例如反复执行：

```text
Python 创建 Rust topology
Python 查询 reachable
Python 再创建 Rust topology
Python 查询 adjacency
```

正确边界是一次传入 `nodes + roots + enabled edges`，由 Rust 在一次调用内完成：

- 可达性
- 不可达节点裁剪
- 环检测
- 拓扑序
- 最终邻接索引

然后一次返回完整解析结果。Python 只负责把领域 identity 映射到连续整数 ID，并在返回
时恢复领域对象。依赖边条件在迁移前仍由 Python 求值，不需要为了复用拓扑而把平台领域
模型塞进 core。

## 保留的宿主边界

能力声明先由 Python adapter 验证端口对象确实属于对应子图，再把 component、capability、
requirement 与 selection 映射成连续整数 ID。provider 索引、任意 provider 的唯一性、
显式 provider 选择、重复与混合 requirement 校验均在 Rust 中一次完成；Python 不保留
第二套 provider 求解规则。

当前仍留在 Python 的内容都是宿主边界，而不是另一套执行图算法：

- 静态条件叶子和运行时条件 predicate 是 Python callable
- action、结果对象、observe、日志路径和展示 label 是 Python 对象
- freeze 负责求值静态业务条件，并建立这些对象到稳定整数 ID 的侧表
- `GraphExecutionState` 保存可供 Python action 动态写入的 skip、结果与渲染快照

ready 队列、节点状态机、条件阻塞、层级生命周期、执行 DAG 校验、capability provider
求解与 binding lowering 均只有 Rust 一份实现。继续把上述宿主对象包装成原生对象只会
增加逐对象 FFI，不属于执行图核心迁移。
