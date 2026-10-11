# Python 图与 Rust 原生图

[English](../native-graphs.md) | 简体中文

`rivetoo-graph` 允许一张 Python 声明图挂载完整的 Rust 原生子图。两者最终进入同一份
Rust `CompiledExecutionGraph`，共享依赖、层级、失败传播和并发预算，但不会把原生子图
拆成一组 Python action：

```text
Python Graph / Step
        │
        ├── Python LeafAction ───────────────┐
        │                                    │
        └── Step(NativeGraph)                │
                └── NativeStep / Arc         │
                                             ▼
                               Rust CompiledExecutionGraph
                                  ├── Python leaf → Python driver
                                  └── Rust leaf   → Rust worker
```

Python 只看见一个不透明的 `NativeGraph`。它不能取得并逐个调用其中的 `NativeStep`
和 Rust action 对象，也不会先把它们翻译成 Python `Step`。因此原生图无论包含十个还是上万
个 action，执行 action 本身都不需要逐项回调 Python。混合图的 Python driver 仍会按批次
接收原生完成事件，以便统一维护结果、日志和展示状态。内部节点仍可以出现在编译图
与执行视图中；不透明的是原生 action 对象，不是把所有内部步骤从视图中隐藏。

## 声明边界

Python action 仍按原有方式声明：

```python
prepare = Step(prepare_source, label="prepare")
```

原生 adapter 在 Rust 中构造完整 `NativeGraph`，Python 只把它作为一个子图挂载：

```python
from rivetoo_ninja import import_clean_build_graph

ninja_graph = import_clean_build_graph(
    "build/build.ninja",
    shell="/bin/sh",
    env=build_env,
)

compile = Step(ninja_graph, label="compile")
graph = (prepare >> compile >> install).graph()
```

上例需要单独的实验性 `rivetoo-ninja` 包，不是 Graph 安装自带的能力。
`import_clean_build_graph` 仅支持选定输出尚不存在的 clean build，不能替代 Ninja 的
增量构建判定；是否使用该 adapter 由宿主显式选择。

这里 `compile` 是外层 composite。它保留子图边界；原生图内部的节点和边仍由 Rust
编译器看到并参与全局调度。Graph 不会隐式展开并丢掉这个边界。

`NativeGraph` 是 Python 可以持有的最小原生单位。Python 包不公开 `NativeAction` 或
`ProcessAction`；否则同一个原生 action 会同时拥有“Rust 直接执行”和“Python 逐项提交”
两套入口，重新产生跨语言往返和重复事实来源。

## 执行与结果

同一 execution session 按叶子所有权分发：

- Python leaf 批量返回 Python driver，支持任意 Python callable、结果和动态图。
- Rust leaf 直接提交 Rust worker；完成进入 Rust completion queue，再由混合 driver 批量推进
  同一 session。
- Python 接收原生节点的开始、完成与失败信息，用于统一状态、日志和展示。
- 两类 leaf 共用一份总并发预算，不会各自把 `workers` 用满后叠加超额并发。

当前 Rust leaf 成功结果固定为 `None`。它不能读取 Python `StepRunContext`，也不能返回
任意 Python 对象或动态图。如果以后需要原生结果，必须新增稳定的和类型结果协议，不能
用 `object` 或隐式序列化兜底。

## Rust crate 边界

原生能力按职责拆分：

- `crates/action`：Rust action trait、执行上下文、错误和 worker。
- `crates/native-graph`：`NativeStep`、graph-owned arcs 与完整 `NativeGraph`。
- `crates/topology`：整数节点与纯拓扑算法。
- `crates/core`：语言无关的声明编译和 execution session。
- `crates/python`：把 Python `Graph` 和整体 `NativeGraph` 接入同一编译过程。

adapter 可以在 Rust 中实现任意 action，但动态库之间不传递 Rust trait object。独立
adapter 应输出一份批量、稳定的原生图规格，再由 graph extension 一次构造
`NativeGraph`。这会保留包边界，也避免每个 action 经过 Python 调度。
