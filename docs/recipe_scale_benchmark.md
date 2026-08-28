# 大规模 Recipe 图基准

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
- 编译与 runtime 分别执行 3 次并取中位数

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
| 纯 Python | 11.314853s | 14.685974s | 26.000827s |
| Rust 状态机 + Python action | 11.605203s | 9.963016s | 21.568219s |

原始样本：

```text
pure-python compile = [10.802747625042684, 11.314852915937081, 11.956409415928647]
pure-python runtime = [15.187768624979071, 14.070446541998535, 14.685974457999691]

rust-python compile = [10.734254417009652, 11.605203083017841, 11.651235415949486]
rust-python runtime = [9.963016125024296, 10.11163379100617, 9.927421958069317]
```

在该输入下：

- Rust runtime 约为纯 Python 的 `1.47x`，中位耗时降低约 32%
- 编译阶段没有观察到明确收益；两组样本接近，Rust 中位数约慢 2.6%
- 编译与 runtime 的中位数相加后，Rust 路径约为纯 Python 的 `1.21x`，耗时降低约 17%

“两项中位数之和”仅用于展示一次编译加一次执行的大致固定成本，不是对逐次总耗时
样本取中位数。

## 调查中发现的问题

初次对照时，Rust 路径的完整 runtime 中位数约为 2.88 秒，而当时使用的较小纯 Python
微基准更快。原因不是 Rust 状态机，而是 Python bridge 在每次 session advance 时扫描
全部编译节点，把 skip 状态重新投影成 native index。大量短节点会让这段工作接近
`O(V²)`。

`48a0598` 改为一次建立 `StepId -> native index`，之后只增量传递新增 skip 请求。较小的
2,000 leaf 微基准中，Rust 完整 runtime 随之从约 2.88 秒降至约 1.37 秒。该问题说明：
迁移状态机并不自动消除 Python 开销，跨语言适配器本身也必须保持批量和增量语义。

## 如何复现

在 Rust 主线中：

```bash
PYTHONPATH=src ../.venv/bin/python benchmarks/recipe_scale.py \
  --implementation rust-python \
  --recipes 2000 \
  --steps 6 \
  --lanes 20 \
  --workers 8 \
  --repeats 3
```

同一个脚本只依赖公开 Python API。要测试保留在 `python` 分支的纯 Python 实现，可以让
`PYTHONPATH` 指向其 worktree，同时仍执行主线中的脚本：

```bash
PYTHONPATH=/Volumes/ssd/Proj/infinity/rivetoo/work/rivetoo-graph-python/src \
  ../.venv/bin/python benchmarks/recipe_scale.py \
  --implementation pure-python \
  --recipes 2000 \
  --steps 6 \
  --lanes 20 \
  --workers 8 \
  --repeats 3
```

## 解释边界

- 空 action 放大 Graph 固定成本，不代表真实 recipe action 的耗时分布
- 20 条依赖链是合成拓扑，不代表真实仓库的扇入、扇出和关键路径
- 本实验不测依赖解析、下载、缓存命中、子进程、文件系统或构建工具
- 本实验不证明编译阶段已经完成优化；当前结果反而表明 Python 声明投影与 PyO3 数据
  转换仍值得单独分析
- 结果是当前机器上的一次工程决策记录，不是跨版本性能承诺
