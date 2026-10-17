# 条件 Step

[English](../conditional_steps.md) | 简体中文

## 用途

条件边决定依赖是否成立；Step.when 决定节点自身是否参与执行。
它适用于 Python 叶子、Python Graph 子图和 NativeGraph 子图。

## 声明

定义见 [model.py](../../src/rivetoo_graph/model.py)：

```python
Step(action=..., label=..., when=...)
```

label 只用于展示。when 接受 `RuntimeCondition | None`，不接受 bool。
接线通过 `Step.wire(after=...)` 声明，最终保存在 Graph.edges，而非 Step 本身。

## 语义

| when | 行为 |
| --- | --- |
| None | 节点启用 |
| 运行时条件为真 | 按普通 prerequisite 等待并执行 |
| 运行时条件为假 | 进入 Skipped 终态，不执行 action、不产生 action 结果 |

运行时条件在 inputs 就绪前不会求值。Skipped 是终态，不是同时拥有 Skipped 与 Finished
两个状态，也不是“执行一个返回 False 的 action”。

对子图，运行时条件为假时不进入内部图，composite 与后代一并跳过。

## 静态节点和子图

静态条件不放在 Step.when 中，而由 StaticConditions 绑定解释器后声明：

```python
static = StaticConditions(evaluator=evaluate_target_condition)
graph = (prepare >> static.when(is_android, android) >> build).graph()
child_graph = static.when(is_android, android_graph).graph()
```

条件为假时，编译器移除节点及关联边，不自动把前驱接到后继。声明图保持不变，
可继续查看原始条件。子图必须先构造，静态规则不会延迟其 Python 构造函数。

不用 DSL 时，同一个 API 通过 Graph.static 接入：

```python
graph = Graph(
    steps=(android, checks, build),
    edges=(
        UnconditionalEdge(source=android, target=build),
        UnconditionalEdge(source=checks, target=build),
    ),
    static=StaticConditions(
        evaluator=evaluate_target_condition,
        conditions={
            android: is_android,
            (checks, build): wait_for_checks,
        },
    ),
)
```

规则必须引用图中已声明的节点或边。节点和边使用同一个映射，不需要两套 Step / Graph。
条件只在编译时求值；同一声明可重复编译，但 evaluator 应保持纯函数语义。
静态节点规则可以与节点自身的运行时 when 同时存在：先静态裁剪，再运行时判断。

## 与条件边的区别

```text
A --[condition]--> B
```

只决定 B 是否等待 A，不禁用任一节点。

```text
[condition] B
```

决定 B 是否参与执行。两者不能互相代替。

## 编译与执行

两种公开 mode 使用同一 Rust 声明编译器与 ExecutionSession：

- 静态条件在编译时处理。
- 运行时条件成为节点 activation，predicate 由 Python 按批次请求求值并回传。
- inputs 必须先满足；读取依赖失败会阻断节点。
- 条件为假时，不再等待其余 after prerequisite。
- composite 保留子图边界，不占用 worker，也不成为 no-op leaf。

[compile.py](../../src/rivetoo_graph/compile.py) 将条件边归一化为
CompiledConditionalDependency，将运行时 Step.when 归一化为节点的
CompiledRuntimeCondition，不把节点条件改写成条件边。

声明期子图的叶子加入统一执行结构，边界成为 CompiledComposite，保留依赖、后继、
observation 与 when。激活后才释放直接子节点；空 composite 也保留依赖边界。

## inputs 与执行前置

inputs 决定“判断节点是否启用需要的结果”，after 决定“启用后执行 action 必须等待谁”。
inputs 必须引用当前图中的 Step，并参与 DAG 校验。

```python
probe = Step(probe_cache, label="probe")
prepare_a = Step(prepare_a_source, label="prepare_a")
prepare_b = Step(prepare_b_source, label="prepare_b")
heavy_build = Step(
    build_source,
    label="heavy_build",
    when=runtime_condition(
        inputs=(probe,),
        when=lambda result: bool(result.unwrap()),
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

probe 成功后即可求值，无需等待 prepare_a / prepare_b。条件为假就跳过 heavy_build，
为真才继续等待两个准备步骤。

每个编译条件实例在当前 session 中最多求值一次。同一个 RuntimeCondition 复用于多个
节点，不会合并求值；已记录的结果则会在等待其他 prerequisite 时复用。

只有 after 无法区分“判定要用的 probe”和“真正执行才需要的准备步骤”，因此 inputs
单独声明。inputs 也参与 wiring 的入口、出口计算，不必为了等待 probe 再重复写 after。

## 结果输入

`inputs` 同时声明依赖和条件函数的位置参数：

```python
count_step = Step(lambda: 42)
approval_step = Step(lambda: True)

def should_run(count: Option[object], approval: Option[object]) -> bool:
    return count.unwrap() == 42 and approval.unwrap_or(False) is True

condition = runtime_condition(inputs=(count_step, approval_step), when=should_run)
```

两个步骤成功执行后，条件函数分别收到 `Some(42)` 和 `Some(True)`。
这里的数字和布尔值都是 action 的实际返回值，不是表示结果有无的标记。

不需要为这些输入再写一次 `after`。没有输入的条件接收零个参数：
`runtime_condition(when=lambda: True)`。声明 inputs 后，框架在生产者执行前自动
开启结果记录；没有 inputs 的图仍保留可选记录行为。

`Option[T] = Some[T] | Nothing`：

- `Some(value)` 表示有已记录的结果，`Some(None)` 是正常执行并返回 None。
- `Nothing()` 表示没有结果，例如步骤被跳过，或子图没有叶子返回值。
  生产者失败不转换为 Nothing，而是阻塞消费者。
- `is_some()` / `is_none()` 判断是否存在。
- `unwrap()` 返回原值；Nothing 解包抛出 ValueError。
- `unwrap_or(default)` 只替换 Nothing，不替换 Some(None) 或 Some(False)。
- `bool(option)` 抛出 TypeError，必须明确判断存在性或解包。

inputs 引用当前图中的 Step；同一子图挂载两次，各自读取自己挂载中的结果。
被静态裁剪的输入是编译错误，不是 Nothing。输入的顺序和重复项均保留，
`inputs=(probe, probe)` 会传两个参数，但等待的是同一个生产者。

当前 Step 不是泛型，条件参数应标为 `Option[object]`，按需校验或收窄原值类型。
类型标注不会切换调用约定。普通 action 仍使用上下文查询结果；成功结果的包装
统一为 Some，不叠加第二层包装。

## 边界

Step 条件和条件边共用条件类型，但语义不同。它们都不表达恰好选中一个分支；
构图前选择见[select_one](./core_model.md#构图前静态选择)。
失败、取消与结果记录遵循统一[执行语义](./execution.md)。
