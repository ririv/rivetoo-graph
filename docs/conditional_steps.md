# 条件 Step

## 目标

条件边解决的是：

- 某条依赖边是否成立

而条件 step 解决的是：

- 某个 step 本身这次是否参与图执行

当前 graph core 已经正式支持：

- `Step.when`

它统一覆盖两种情况：

- 普通 leaf step 的条件启用
- `action=Graph` 的组合 step 的条件启用

也就是说，当前不再需要额外引入：

- `ConditionalStep`
- `ConditionalGraph`

因为 graph 子图本来就是 `Step.action` 的一种形态。

## 模型

位置：

- `rivetoo-graph/src/rivetoo_graph/model.py`

当前 `Step` 模型核心字段是：

```python
Step(
    action=...,
    label=...,
    when=...,
)
```

其中：

- `label`
  - 只表示可选展示名
- `when`
  - 表示 step 本身是否启用

依赖关系不存放在 Step 中。作者通过 `Step.wire(after=...)` 声明接线，
`Graph.from_wiring(...)` 再把它解析成 `Graph.edges`。

`when` 的类型当前统一是：

- `Condition | None`

也就是：

- `StaticCondition`
- `RuntimeCondition`
- 或 `None`

## 语义

### `when=None`

表示：

- 这个 step 始终启用

### `when=StaticCondition`

表示：

- 这条条件在 freeze 阶段就能判定

处理规则：

- 条件为真
  - step 保留
- 条件为假
  - step 在 `freeze_graph(...)` 阶段直接裁掉

这时它的语义就是：

- 这次图里没有这个 step

### `when=RuntimeCondition`

表示：

- 这条条件只能在运行时根据 step result 判定

运行时：

- 条件还没准备好
  - step 暂时 not ready
- 条件判定为真
  - step 正常执行
- 条件判定为假
  - step 直接记为：
    - `skipped`
    - `finished`

这条规则很重要：

- `when=False` 的 step 语义是“视为不存在”
- 不是“执行一个返回 False 的动作”

这同样适用于：

- `action=Graph` 的组合 step

也就是说，若组合 step 的 `when=False`：

- 不进入子图
- 子图整体视为不存在

## 为什么不用条件边代替

因为条件边和条件 step 解决的不是同一件事。

### 条件边

表达的是：

- `B` 是否依赖 `A`

也就是：

- 依赖关系条件化

### 条件 step

表达的是：

- `B` 本身是否执行

也就是：

- 节点启用条件化

这两者不应混用。

例如：

```text
A --[condition]--> B
```

表示的是：

- 这条边是否成立

而：

```text
[condition] B
```

表示的是：

- `B` 这个节点是否存在

## 执行路径

两种公开 mode 都先由 Rust 声明编译器把 `Step.when` 转换成节点 activation，再由
Rust `ExecutionSession` 维护 pending、ready、running、skipped 与条件缓存。Python
只在 session 批量请求时求值 predicate，并把结果通过 `advance(...)` 交回。

这里的关键语义是：

- 条件为假时，step 不再等待自己的 `after`
- 因为“视为不存在”的 step 不应再阻塞下游
- composite 保留声明期子图边界，不会被伪装成 no-op leaf

## Compiled 路径

位置：

- `rivetoo-graph/src/rivetoo_graph/compile.py`

`freeze_graph(...)` 仍可独立用于结构实例化；正式执行则直接调用 `compile_graph(...)`。
条件 step 在编译中的处理是：

### 静态裁剪

- `StaticCondition`
  - 直接裁掉或清空

### 内部 lowering

- `ConditionalEdge`
  - 归一化成 `CompiledConditionalDependency`
- `Step.when`
  - 不会被改写成条件边
  - 归一化成节点自身的 `CompiledRuntimeCondition`

### 结构转换

- 所有声明期已知 graph step 都递归展开叶子
- graph step 自身变成非执行的 `CompiledComposite`
- runtime `when` 挂在 composite 上
- 条件成立后 composite 激活直接子节点
- 条件为假时 composite 及其后代进入 skipped 终态
- 空 composite 也保留完整 prerequisite / successor 边界

也就是说，当前 compiled 路径没有把：

- step 级 runtime 条件

lower 成另一套“条件边”结构，而是显式保留了：

- 节点自身的 `when`

## `reads` 的作用

`RuntimeCondition.reads` 表示：

- 为了判定这个条件，需要先读取哪些 step result

这和 `after` 不同：

- `after`
  - 是调度依赖
- `reads`
  - 是条件求值依赖

graph 创建时会校验：

- `when.reads` 里的 step 必须真实存在于当前图中

因为它本质上也是一种显式图内引用。

这里 `reads` 不能简单并入 `after`，原因是：

- `reads`
  - 允许 runtime 先根据少量必要结果，尽早判定这个 step 本次是否存在
- `after`
  - 只表达“如果这个 step 要执行，它必须排在谁后面”

例如：

```python
probe = Step(probe_cache, label="probe")
prepare_a = Step(prepare_a_source, label="prepare_a")
prepare_b = Step(prepare_b_source, label="prepare_b")
heavy_build = Step(
    build_source,
    label="heavy_build",
    when=runtime_condition(
        reads=(probe,),
        when=lambda ctx: bool(ctx.require_value(probe)),
    ),
)

graph = Graph.from_wiring(
    wiring=(
        probe.wire(),
        prepare_a.wire(),
        prepare_b.wire(),
        heavy_build.wire(after=(prepare_a, prepare_b)),
    )
)
```

这里的语义是：

- `probe`
  - 只负责决定 `heavy_build` 这次要不要执行
- `prepare_a` / `prepare_b`
  - 只负责在 `heavy_build` 确实要执行时，约束它的调度顺序

所以 runtime 可以这样处理：

- `probe` 一旦 finished
  - 就立刻判 `when`
- 若 `when=False`
  - `heavy_build` 直接视为不存在
  - 不需要再等 `prepare_a` / `prepare_b`
- 若 `when=True`
  - 再继续等待 `heavy_build.wire(after=(prepare_a, prepare_b))` 声明的 prerequisite

同一个 `RuntimeCondition` 在一次 graph run 中只会判定一次。条件的
`reads` 已满足、但其它 `after` 尚未完成时，调度器会复用已经记录的
布尔结果，不会重复调用 predicate。

如果没有 `reads`，而是把 `probe` 也混进普通 `after`，那条件判定就会被错误地拖到：

- `probe`
- `prepare_a`
- `prepare_b`

都完成之后。这样会让本来可以尽早裁掉的 step，白白多等一段无关前置。

`reads` 形成的阻塞关系也参与 wiring 的入口和出口计算。因此，当一个 step
只需要等待条件读取的 probe 时，不需要再重复声明同一条普通 `after`；只有
真正独立于条件读取的执行约束才写入 `after`。

## 当前边界

当前 graph 模型的条件能力已经分清：

- `ConditionalEdge`
  - 依赖关系条件化
- `Step.when`
  - 节点启用条件化

不要再把这两者混成一层语义。
