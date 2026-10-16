# 拓扑复杂度与实现开销

[English](../topology_complexity.md) | 简体中文

本文区分 Python 通用拓扑 API、执行 Graph 的声明校验以及 Rust 编译后的全局 DAG。
它们不是同一条调用链。以下复杂度不包含 action、宿主 predicate 或日志 I/O 的成本；
Python 哈希表操作按平均 O(1) 计，宿主自定义节点的 hash / equality 成本另计。

## 两套纯拓扑入口

| 入口 | 实现 | 用途 |
| --- | --- | --- |
| `rivetoo_graph.topology` | Python，DAG 校验使用 `graphlib` | 可哈希领域节点的通用有向图 |
| `crates/topology` | Rust，整数节点与邻接索引 | 执行图声明校验和编译后 DAG 校验 |

Python 的 `DirectedTopology` 并不是 Rust topology 的逐方法 FFI 包装。
执行 `Graph` 也不会先创建 Python `DirectedTopology` 再交给 Rust。

## Python 通用拓扑

对于 V 个节点、A 条输入 arc：

- `DirectedTopology(nodes, arcs)` 接受 iterable，创建规范化节点、arc 和双向邻接索引。
- 每条 arc 在同一次消费中验证端点并写入前驱、后继索引；不会主动去重输入 arc。
- 构造时间、持久空间均为 O(V + A)。
- `predecessors_by_node()` / `successors_by_node()` 返回已有只读映射，不重扫关系。
- `reachable_from(roots)` 先验证 roots，再沿后继遍历；时间为 O(V + R + Aᵣ)，
  R 是输入 root 数量，Aᵣ 是可达区域的 arc 数量。它仍会创建全体节点集合，不能把
  小闭包查询描述成只访问可达节点的成本。待处理栈可能保存重复端点引用，
  最坏辅助空间为 O(V + A + R)。

`DAGTopology` 组合 `DirectedTopology`，构造时调用 Python
`graphlib.TopologicalSorter.prepare()` 校验无环，时间和辅助空间为 O(V + A)。
`topological_order()` 是另一次 O(V + A) 操作，构造时不会预先保存排序结果。

## 执行 Graph 的声明校验

这里定义：

- V：本层 Graph 的 Step 数量。
- E：本层显式边数量。
- B：条件边和 `Step.when` 中所有 `RuntimeCondition.reads` 引用总数。
- A：去重后的本层阻塞 arc 数量，A ≤ E + B。

本层阻塞关系来自普通边、条件边的来源节点和运行时条件的读取依赖。
条件边在声明时保守参与无环校验，即使其静态条件在随后编译时为假。
`reads` 的遍历来自显式数据依赖，不是拓扑排序额外制造的业务关系。

当前 [model.py](../../src/rivetoo_graph/model.py) 的实际链路是：

```text
验证 Step 并分配本层整数索引
    ↓
_validated_blocking_arcs：遍历边与 reads，去重并生成 Arc
    ↓
物化 blocking_arcs 元组
    ↓
转换为整数端点对列表
    ↓ 一次批量调用
Rust dag_cycle：创建拓扑索引并检测环
```

这里确实存在 Arc 元组和整数端点列表，不是“生成一条关系就直接写入 Rust 索引”的
零中间集合实现。关系投影、转换、索引构造和环检测各自进行线性处理，总时间仍为
O(V + E + B)，峰值辅助空间为 O(V + A)。多个中间表示增加常数成本，不改变渐进阶。

B = 0 时，总时间为 O(V + E)。这也适用于不读取 Step 结果的运行时条件，
不能简单把所有运行时条件都视为增加一个 V 倍的循环。

StaticConditions 额外声明 S 条节点或边规则。存在规则组时，成员校验与查询索引的
时间、空间为 O(V + E + S)，不包含 predicate 求值成本。没有规则组时，此路径为 O(1)，
Rust collector 不进行逐节点、逐边静态查询；有规则时单次查询平均 O(1)，evaluator
耗时由宿主表达式决定。结构在裁剪前校验，所以不能靠静态条件为假来允许结构上的环。

## 能力端口与全局编译校验

`CapabilityBinding` 在声明阶段校验组件及端口，但不直接作为本层 composite 之间的
阻塞 arc。实际依赖是“提供者内部出口 → 消费者内部入口”，必须在编译时定位并 lower。

接口归属校验会遍历当前图的 Python 子图挂载结构，建立 Step 挂载次数索引，再验证
端口引用。若挂载节点数为 M、端口引用数为 R，则该项时间为 O(M + R)、空间为 O(M)；
它不是上节本层 V 的一部分。重复挂载导致端口引用有歧义时直接报错。

Rust 编译器随后校验完整执行 DAG，包括：

- 声明期已知子图与 composite 的依赖；
- 运行时条件的 reads；
- lower 后的跨子图 capability bindings。

若最终执行节点数为 N、展开后的阻塞关系数为 L，纯 DAG 校验时间和空间为
O(N + L)。一个能力绑定可能指向多个入口，L 应统计展开后的关系，而非只数 binding。

这不等于承诺整个编译过程恒为 O(N + L)：声明遍历、层级路径构造、Python 侧表、
静态条件求值和 PyO3 转换都有各自成本。运行期返回的新图还会单独编译并创建嵌套
session，不能在构图前把它的规模算入已知静态图。

## GraphWiring 的入口与出口

`GraphWiring.entries` / `exits` 是用于 DSL 接线的结构边界，不是
`CapabilityInterface` 的具名端口，也不是额外保存的一份拓扑。

一次边界计算读取：

- 显式普通边与条件边；
- 条件边和 Step 的运行时条件 reads；
- 本层 Step 顺序，用于返回有序边界。

时间为 O(V + E + B)，辅助空间为 O(V)。`entries` 与 `exits` 是独立属性，
分别读取会分别计算；串行 DSL 通常读取左侧 exits 和右侧 entries。
声明对象当前没有为此维护可变缓存。

## 性能结论的适用范围

Rust 并未降低上述线性算法的渐进复杂度。它的收益来自紧凑整数索引、原生循环和
批量调度；成本也包含 Python 声明遍历、临时对象及跨语言转换。不能只凭“使用 Rust”
推断某张小图一定更快，也不能把 O(V + E) 理解为只执行一遍循环。

历史测量见 [Recipe 规模基准](./recipe_scale_benchmark.md) 和
[Step 开销基准](./step_overhead_benchmark.md)。测量覆盖的版本与负载以各文档为准。
