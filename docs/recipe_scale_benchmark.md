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
| 纯 Python | 1.000669s | 13.787106s | 14.787775s |
| Rust 状态机 + Python action | 0.487382s | 9.995732s | 10.483114s |

原始样本：

```text
pure-python compile = [0.8881366660352796, 1.0006690410664305, 1.0822665840387344]
pure-python runtime = [13.787106417003088, 13.342708665993996, 13.979397624963894]

rust-python compile = [0.48738204198889434, 0.42751637496985495, 0.49038170801941305]
rust-python runtime = [11.291682832990773, 9.995732290903106, 9.904609041986987]
```

在该输入下：

- Rust runtime 约为纯 Python 的 `1.38x`，中位耗时降低约 28%
- Rust 编译路径约为纯 Python 的 `2.05x`，中位耗时降低约 51%
- 编译与 runtime 的中位数相加后，Rust 路径约为纯 Python 的 `1.41x`，耗时降低约 29%

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

### 编译校验复杂度

第一次大图对照中，两条路径的编译中位数都超过 11 秒。profile 表明 Rust
`compile_declaration` 只消耗约 0.1 秒，主要时间来自 Python
`GraphCompilation.__post_init__`：每个 composite 都重新扫描所有节点以验证 owner，复杂度
为 `O(C * V)`。2,000 个 composite 与 14,000 个节点会产生约 2,800 万次扫描，并进一步
触发数千万次 dataclass equality。

校验现改为单次建立以下索引：

- `StepId -> CompiledNode`
- `CompiledCompositeRef -> set[StepId]`
- 每个不同声明作用域的 `frozenset[StepId]`

设 `V` 为节点数、`A` 为直接依赖与 child 引用总数、`S` 为所有不同声明作用域的 step
总数，校验时间复杂度收敛为 `O(V + A + S)`，额外空间为 `O(V + S)`。

实现时不能使用：

```python
index.setdefault(key, frozenset(scope.values()))
```

即使 `key` 已存在，Python 也会先计算默认值；在大根作用域下仍会重复执行数百万次 hash。
当前实现先判断 key 是否存在，再只为每个不同作用域构造一次集合。

修复后，Rust 编译中位数从约 11.61 秒降至 0.49 秒，纯 Python 从约 11.31 秒降至
1.00 秒。profile 中函数调用量也从约 5,504 万次降至约 118 万次。

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
- Python 侧仍需恢复 action、predicate、label 与声明作用域等不透明宿主对象；修复复杂度
  问题后该过程已经是线性路径，是否继续下沉必须由新的 profile 和真实规模需求决定
- 结果是当前机器上的一次工程决策记录，不是跨版本性能承诺
