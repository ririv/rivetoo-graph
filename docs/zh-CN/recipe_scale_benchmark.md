# 大规模 Recipe 图基准

[English](../recipe_scale_benchmark.md) | 简体中文

本文保留 2026-08-28 版本的历史测量，不代表当前主线性能。后续同步 action 执行路径
已有优化，见 [Step 开销基准](./step_overhead_benchmark.md)。原始样本不随实现更新改写。

## 目的

该实验回答一个特定问题：当一次请求包含数千个 recipe，并且每个 recipe 都拥有多个
step 时，把执行图编译与调度状态机迁移到 Rust 是否仍有收益。

这不是构建系统吞吐量基准。实验中的 action 全部为空，刻意移除编译器、磁盘、网络和
子进程耗时，使 Graph 自身的声明编译、跨语言协调和调度成本可见。真实构建通常会被
外部工作主导，不能把这里的比例直接套用到完整构建耗时。

## 图结构

本次固定输入为：

- 2,000 个 recipe 子图
- 每个 recipe 包含 6 个串行 leaf step
- 共 12,000 个 Python action
- 每个 recipe 由一个 composite 保留子图边界，共 2,000 个 composite
- recipe 之间组成 20 条依赖链，允许最多 20 个 recipe 层级并行推进
- runtime 使用 8 个线程 worker
- 编译与 runtime 分别执行 5 次并取中位数

该结构不是某个真实 package repository 的依赖图快照，而是一个可重复的分层 DAG。
它同时保留 recipe 边界、recipe 内阶段顺序和跨 recipe 依赖，比“总共 2,000 个独立
step”的微基准更接近大规模构建编排。

## 环境

- Apple M1 Pro，arm64
- macOS 26.4（25E246）
- Python 3.13.5
- rustc 1.96.0-beta.5
- 2026-08-28

## 结果

| 实现 | 编译中位数 | Runtime 中位数 | 两项中位数之和 |
|---|---:|---:|---:|
| 纯 Python | 0.915015s | 13.388637s | 14.303652s |
| Rust 状态机 + Python action | 0.333768s | 9.413393s | 9.747161s |

原始样本：

```text
pure-python compile = [0.9050715418998152, 0.9029900829773396,
                       0.915015000035055, 0.9332317499211058,
                       0.9470305419526994]
pure-python runtime = [13.388636792078614, 13.366689124959521,
                       13.345061874948442, 14.904916667030193,
                       14.302743416046724]

rust-python compile = [0.2818566670175642, 0.3337678749812767,
                       0.34090887499041855, 0.2765289170201868,
                       0.3360874579520896]
rust-python runtime = [9.41339295799844, 9.493625707924366,
                       9.459358749911189, 9.329700125032105,
                       9.271784832933918]
```

在该输入下：

- Rust runtime 约为纯 Python 的 `1.42x`，中位耗时降低约 30%
- Rust 编译路径约为纯 Python 的 `2.74x`，中位耗时降低约 64%
- 编译与 runtime 的中位数相加后，Rust 路径约为纯 Python 的 `1.47x`，耗时降低约 32%

“两项中位数之和”仅用于展示一次编译加一次执行的大致固定成本，不是对逐次总耗时
样本取中位数。

## 运行时驱动实验

另一项 2026-08-28 实验比较了批量 Python 执行与逐 action 回调 Python 的私有 Tokio
驱动。在启用 GIL 的 CPython 3.13、macOS arm64、release 原生扩展上，2,000 个独立空
action、8 worker、5 次采样的中位数为：

| 驱动 | 中位数 |
| --- | ---: |
| Batch | 约 0.321 s |
| Tokio callback | 约 1.280 s |

callback / batch 约为 3.98。这是历史调度微基准，不是上面的 recipe 规模负载，
也不能证明所有 Rust runtime 都更慢。实验位于 `crates/tokio-runtime` 与
`benchmarks/runtime_drivers.py`，正式驱动不依赖它。

## 如何复现

在 Graph 仓库根目录中，使用已安装原生扩展的虚拟环境。以下命令测量当前 checkout，
不会自动还原历史版本；`--implementation` 只是结果标签，不负责选择或切换实现：

```bash
PYTHONPATH=src .venv/bin/python benchmarks/recipe_scale.py \
  --implementation rust-python \
  --recipes 2000 \
  --steps 6 \
  --lanes 20 \
  --workers 8 \
  --repeats 5
```

同一个脚本只依赖公开 Python API。要测试保留在 `python` 分支的纯 Python 实现，可以让
`PYTHONPATH` 指向其 worktree，同时仍执行主线中的脚本：

```bash
PYTHONPATH=/path/to/python-checkout/src \
  .venv/bin/python benchmarks/recipe_scale.py \
  --implementation pure-python \
  --recipes 2000 \
  --steps 6 \
  --lanes 20 \
  --workers 8 \
  --repeats 5
```

将 `/path/to/python-checkout` 换成实际 `python` 分支 checkout 的路径；必须确认导入的
模块来自该目录，而不是当前安装版本。历史数据的精确复现还需要对应代码和工具版本。

## 解释边界

- 空 action 放大 Graph 固定成本，不代表真实 recipe action 的耗时分布
- 20 条依赖链是合成拓扑，不代表真实仓库的扇入、扇出和关键路径
- 本实验不测依赖解析、下载、缓存命中、子进程、文件系统或构建工具
- Python 侧仍需恢复 action、predicate、label 与声明作用域等不透明宿主对象；修复复杂度
  问题后该过程已经是线性路径，是否继续下沉必须由新的 profile 和真实规模需求决定
- 结果是上述日期、机器与版本的一次工程决策记录，不是跨版本性能承诺
