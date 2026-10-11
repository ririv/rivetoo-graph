# Step 开销基准

[English](../step_overhead_benchmark.md) | 简体中文

本文记录 2026-10-10 同步 action 快速路径优化的 A/B 测量。它不包含后续失败隔离等
改动，也不代表当前主线在任意机器上的性能；原始结果作为历史记录保留。

## 边界与结论

本次测量把声明、编译、执行分开，定位短 Python action 的固定成本。
空 action 用于暴露调度开销，不代表真实编译、网络或磁盘任务的吞吐量。

A/B 对比只改变返回值处理：基线对所有返回值创建事件循环；快速路径直接返回普通值，
仅为 awaitable 创建事件循环。拓扑、调度、日志策略与 worker 设置保持不变。

## 测量环境与方法

- Apple M1 Pro，arm64，macOS 26.4（25E246）
- Python 3.13.5
- 2026-10-10，本机当前安装的 Rust 扩展，两组之间未重新编译扩展
- 每项测量 5 次取中位数；前后基准分别顺序运行，未同时运行两组基准
- 微基准每项先预热一次；执行复用已编译声明，每次创建新的执行状态
- 未启用 observer、日志文件和最终执行记录输出，日志接入另行测量

## 1,000 个独立空 Step

| 阶段 | 修改前 | 修改后 |
|---|---:|---:|
| 声明 Graph 与 Step | 5.65 ms | 5.82 ms |
| 编译 | 8.69 ms | 9.04 ms |
| 串行参考执行器 | 227.21 ms | 16.99 ms |
| 线程执行器，1 worker | 272.10 ms | 51.93 ms |
| 线程执行器，8 workers | 282.28 ms | 39.60 ms |

声明和编译路径没有改动，小幅差异不作为收益或回归结论。
`GraphExecution.run(ThreadGraphRunOptions(workers=1))` 仍使用线程执行器，
不是表中的串行参考执行器。线程任务提交、同步及 Python/Rust 批量交互仍有成本，
不能把执行一个 Step 等同于调用一个 Python 函数。

## 2,000 个 recipe 子图

沿用 `benchmarks/recipe_scale.py`：每个 recipe 6 个串行叶子，合计 12,000 个
Python action、2,000 个 composite，20 条 recipe 依赖链，8 workers。

| 阶段 | 修改前中位数 | 修改后中位数 |
|---|---:|---:|
| 编译 | 0.490070 s | 0.500653 s |
| 执行 | 3.365259 s | 0.520759 s |

执行约加速 6.46 倍，但不能据此声称真实 package 构建也加速 6.46 倍。

执行原始样本（秒）：

```text
before = [3.369867, 3.348461, 3.354630, 3.374089, 3.365259]
after  = [0.546870, 0.525995, 0.520759, 0.520433, 0.515768]
```

## Rivetoo 日志接入验证

额外使用 Rivetoo 的真实 `RivetooGraphActionLog`，把 1,000 个 action 放入一个
composite 并给该 composite 绑定临时日志文件。每个 action 断言日志上下文已绑定；
写日志场景逐个调用 `append_log_text`，运行后确认恰好写出 1,000 行。
图编译和日志路径绑定不计入执行耗时，最终执行记录输出未启用。

这组在同一进程中交替比较旧的无条件 `asyncio.run` 返回值处理与新实现，
每种先预热一次，再取 5 次中位数；没有修改宿主日志实现。

| 8 workers 的场景 | 旧处理 | 新处理 |
|---|---:|---:|
| 只进入/退出真实日志作用域 | 320.92 ms | 56.67 ms |
| 每个 action 追加一行日志 | 459.09 ms | 178.91 ms |

日志路径解析及文件 I/O 不会因同步快速路径而消失；这些数值受文件系统状态影响，
不能将空图收益直接视为生产日志开启时的收益。

## 复现

在 Graph 仓库根目录中，使用已安装当前 Graph 扩展的虚拟环境：

```bash
.venv/bin/python benchmarks/step_overhead.py --nodes 1000 --repeats 5
.venv/bin/python benchmarks/recipe_scale.py \
  --recipes 2000 --steps 6 --lanes 20 --workers 8 --repeats 5
```

第一条分离声明、编译、串行执行和线程执行；第二条保留嵌套图及跨 recipe 依赖。
在 Rivetoo 主仓库中运行时，给脚本路径加 `rivetoo-graph/` 前缀。
这些命令测量当前 checkout；不会自动切换到表中的修改前版本。Rivetoo 日志接入测量
依赖宿主工程，不是独立 Graph 安装的自带功能。
