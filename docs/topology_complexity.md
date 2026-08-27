# 拓扑复杂度与实现开销

本文档说明 `Arc`、`DirectedTopology`、`DAGTopology` 以及执行 `Graph`
投影拓扑时的时间复杂度、空间复杂度和实际遍历次数。这里讨论的是纯拓扑处理，
不包含 step action 自身的执行成本。

## 记号

- `V`
  - 节点数量；对执行图而言就是 step 数量
- `E`
  - 领域模型显式声明的边数量
- `B`
  - 所有 `RuntimeCondition.reads` 引用的总数
- `A`
  - 去重后真正进入 `DirectedTopology` 的 arc 数量

对执行图而言：

```text
A <= E + B
```

`CapabilityBinding` 由组件端口 lowering 处理，不会直接作为阻塞 arc 进入当前
执行图拓扑，因此这里的 `E` 是复杂度上界，不表示每一条领域边都会生成 arc。

## 执行图的拓扑投影

执行 `Graph` 中存在三类会阻塞 step 的关系：

- 普通 `UnconditionalEdge`
- `ConditionalEdge` 的来源 step，以及它的运行时条件 `reads`
- `Step.when` 的运行时条件 `reads`

`_validated_blocking_arcs()` 以生成器方式读取这些关系：

```text
读取一条领域关系
    ↓
生成对应 Arc
    ↓
DirectedTopology 立即消费并写入索引
```

它不会先构造完整的中间 arc 列表，再重新扫描该列表。关系投影的时间复杂度是：

```text
O(V + E + B)
```

其中 `reads` 并不是拓扑排序自身引入的循环。它们是运行时条件的显式结果依赖，
必须先投影为阻塞关系，才能参与环检测和调度。

如果没有 `RuntimeCondition.reads`，即 `B = 0`，复杂度退化为：

```text
O(V + E)
```

这同样适用于不读取 step result 的运行时条件；决定因素是 `B`，而不是条件类型
名称本身。

## DirectedTopology 的索引创建

`DirectedTopology` 在消费每条 `Arc` 时同时完成：

- 验证 `source` 和 `target` 均属于节点集合
- 保存规范化后的 `Arc`
- 写入前驱索引
- 写入后继索引

因此不会为了创建前驱和后继索引再分别扫描完整的 `arcs`。这一阶段的时间复杂度
是：

```text
O(V + A)
```

创建完成后：

- `predecessors_by_node()` 直接返回只读索引，不扫描 `arcs`
- `successors_by_node()` 直接返回只读索引，不扫描 `arcs`
- `reachable_from()` 使用后继索引遍历可达子图，最坏为 `O(V + A)`

## DAG 校验

`DAGTopology` 使用已经创建好的前驱索引进行环检测。环检测必须读取节点及其前驱
关系，时间复杂度是：

```text
O(V + A)
```

因此，关系投影、索引创建和 DAG 校验组合后的渐进复杂度仍然是：

```text
O(V + E + B)
```

不能把环检测本身省略；能够避免的是在环检测前反复扫描同一份领域边或
`Arc`。

`DAGTopology.topological_order()` 当前是独立的按需操作。如果调用它，会再次创建
一次拓扑排序状态并执行 `O(V + A)` 的遍历；构造 `DAGTopology` 本身不会缓存或提前
计算这个顺序。

## 实际遍历次数

以一次执行图声明校验为例，和关系数量有关的完整遍历是：

- `Graph.edges`
  - 投影阶段读取一次
- `RuntimeCondition.reads`
  - 投影阶段读取一次
- 生成的 `Arc`
  - 由 `DirectedTopology` 流式消费一次，同时建立双向索引
- 前驱索引
  - 由 DAG 环检测读取一次

这里的“流式消费一次”仍然表示每条领域关系和每条 arc 都会执行固定数量的 Python
操作，但不存在“先完整扫描 `edges`，再完整扫描一个中间 arc 列表来创建索引”的
额外阶段。

节点集合会在以下职责中被常数次遍历：

- 校验重复节点
- 分配声明期内部身份
- 初始化前驱和后继索引
- 生成只读索引

这些遍历都只依赖 `V`，不会改变线性渐进复杂度。文档记录遍历次数的目的不是承诺
永远固定的 Python `for` 数量，而是保证同一批关系不会因抽象分层产生无职责的重复
全量扫描。

## GraphWiring 的入口与出口

`GraphWiring.entries` 和 `GraphWiring.exits` 是派生边界，不是另一份拓扑事实来源。
边界计算同时读取：

- 显式 `GraphEdge`
- `ConditionalEdge.condition.reads`
- `Step.when.reads`

当前一次边界计算会：

- 遍历一次 `edges`
- 遍历一次 `steps` 及其 `reads`
- 遍历一次 `steps` 生成有序边界结果

时间复杂度是：

```text
O(V + E + B)
```

辅助空间只保存去重后的节点集合：

```text
O(V)
```

`entries` 和 `exits` 是两个独立属性；若调用方对同一个 wiring 分别读取二者，当前
会分别执行一次线性边界计算。DSL 的串行组合通常只读取左侧 `exits` 和右侧
`entries`。目前没有为了消除这一次可预测计算而向不可变声明对象加入可变缓存。

## 空间复杂度

`DirectedTopology` 持有：

```text
nodes                 O(V)
arcs                  O(A)
predecessor index     O(V + A)
successor index       O(V + A)
```

因此它的持久空间复杂度是：

```text
O(V + A)
```

执行图投影期间还会临时持有 arc 去重集合和 Rust DAG 校验状态，峰值辅助空间仍然是：

```text
O(V + A)
```

代入 `A <= E + B` 后，可以写成：

```text
O(V + E + B)
```

对执行 `Graph` 而言，这份 `DirectedTopology` 只在声明校验期间存在，校验完成后不
保存到 `Graph`，所以增加的是构造期峰值内存。对 `ResolvedDependencyDAG` 而言，
拓扑会作为解析结果持续保存，因为依赖编排还需要后继查询和可达性分析。

## 相比旧模型的常数开销

如果旧实现已经用一次线性遍历直接创建 Python `graphlib` 前驱映射，那么 Rust 模型不会改善
它的渐进复杂度。新模型增加的常数成本包括：

- 为每条去重关系创建 `Arc` 对象
- 同时保存前驱和后继引用
- 在纯拓扑边界再次验证 arc 端点
- 执行图校验期间保存临时去重集合

因此新模型的收益不是更低的时间或空间复杂度，而是：

- `DirectedTopology` 与 `DAGTopology` 在类型上区分“允许有环”和“已经验证无环”
- 执行图与依赖图共享同一套环检测、前后继查询和可达性语义
- 领域模型不再分别维护重复的拓扑算法
- 纯数据边界可以直接映射为未来的 Rust `Arc<N>`、`DirectedTopology<N>` 和
  `DAGTopology<N>`

如果只有执行图一个消费者，这些常数成本未必值得。当前纯拓扑层同时服务执行图与
其它有向图领域，并且已经明确考虑把图核心迁移到 Rust，因此这层窄抽象成立。

## Rust 迁移后的预期变化

迁移到 Rust 不会改变 `O(V + E + B)` 的渐进复杂度，但可以降低常数开销：

- `Arc` 可以连续存储在 `Vec<Arc<NodeId>>` 中，避免每条关系一个 Python 对象
- 前驱和后继索引可以使用紧凑的 `NodeId` 与连续数组
- DAG 不变量可以只通过验证构造函数建立
- 环检测、可达性和拓扑顺序无需跨 Python 边界逐节点调用

Python 侧应继续只负责把领域图投影为纯节点和纯关系；`Step.action` 与其它宿主领域
对象不进入纯拓扑 FFI 边界。
