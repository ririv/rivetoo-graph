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

当前 compiled 路径为：

```text
Python Graph
    │ freeze 与层级 lowering
    ▼
Python GraphCompilation 元数据
    │ 一次批量转换
    ▼
Rust CompiledExecutionGraph
    ├── 连续 NodeId
    ├── 组合 owner / children 索引
    ├── 条件 reads 与 prerequisite 索引
    ├── dependents 索引
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

动态 action 返回的运行期子图仍按 Python dynamic runtime 执行。它不属于声明期已知的
compiled graph，不能伪装成已经完成 Rust lowering 的节点。

## 唯一事实来源

Rust 编译执行图只接受每个节点的 `owner`。roots 和 composite children 在构造时从
owner 派生，不接受三套可能互相矛盾的层级声明。

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

## 待迁移内容

当前仍在 Python 的 compiled 路径内容包括：

- 静态条件求值与 freeze
- 冻结层级图展开为编译元数据
- capability binding lowering
- Python action、结果对象、观察事件和宿主日志

下一步优先迁移 freeze 后的结构 lowering。静态业务条件与 Python action 不进入 Rust
core；只有稳定、纯结构且能批量跨边界的数据适合下沉。
