# 工作流运行与并发基准

[English](../workflow_runtime_benchmark.md) | 简体中文

2026-10-16 对比 `d6b056d` 与当前工作区中的异步入口、动态子图统一预算、步骤事件和
协作式取消改造。基线的 Python 与 Rust 代码通过独立源码快照分别构建，主工作区没有
切换分支。这里测量的是本地调度成本，没有运行真实软件构建或模型请求。

## 测量方法

- Apple M1 Pro、macOS 26.4（25E246）、Python 3.13.5、release Rust 扩展。
- 空 Step 预热后测九次，复用 compilation，以执行时间中位数比较。
- Recipe 场景为 2,000 个静态子图、每图六个步骤、20 条依赖链、八个 worker；九次
  执行各自使用新的执行状态。
- 动态场景宽度三、深度三，共 108 个叶子动作，每个休眠 1 ms，请求并发为八；
  预热后测七次，耗时包含运行中构图和编译。
- 未订阅事件、未接入 action log 或执行文件输出；两组顺序运行，保留调度异常值。

## 静态工作流结果

| 执行场景 | 修改前 | 修改后 |
| --- | ---: | ---: |
| 3,000 个空 Step，串行 | 26.242 ms | 26.230 ms |
| 3,000 个空 Step，单 worker | 85.437 ms | 84.976 ms |
| 3,000 个空 Step，八 worker | 101.498 ms | 103.097 ms |
| 2,000 个 recipe / 12,000 个 Step，八 worker | 414.304 ms | 419.790 ms |

后两项中位数分别增加约 1.6% 和 1.3%，串行与单 worker 基本相同。这是极短 action 的
调度开销结果，不能承诺所有机器或工作流都在这一波动范围内；本轮没有真实 recipe
构建验收。

## 动态工作流结果

| 驱动 | 修改前中位数 | 修改后中位数 | 修改前实际并发峰值 | 修改后峰值 |
| --- | ---: | ---: | ---: | ---: |
| 线程 | 26.575 ms | 25.002 ms | 23～34 | 8 |
| 异步 | 22.403 ms | 29.590 ms | 14 | 8 |

旧实现在请求并发为八时实际超额派发，修改后严格遵守统一上限，因此不能把这两组
解释为相同并发下的吞吐比较。异步场景使用更少的同时执行动作，耗时也更长。另有
Python/Rust 混合动作的集成测试验证两者合计的预算，不把测试当作性能样本。

## 复现

在 Rivetoo 工作区使用现有虚拟环境：

```bash
.venv/bin/python rivetoo-graph/benchmarks/step_overhead.py \
  --nodes 3000 --repeats 9 --workers 1 8
.venv/bin/python rivetoo-graph/benchmarks/recipe_scale.py \
  --recipes 2000 --steps 6 --lanes 20 --workers 8 --repeats 9
.venv/bin/python rivetoo-graph/benchmarks/workflow_runtime.py --repeats 7
.venv/bin/python rivetoo-graph/benchmarks/workflow_runtime.py --executor async --repeats 7
```

新增 workflow 脚本也能在旧版 Python 包与扩展上运行。基线须通过独立安装或
`PYTHONPATH` 指向隔离快照，脚本不会自动切分支或安装旧版。它以 JSON 输出各次耗时
和实际并发峰值，另外两份脚本也会输出原始样本。
