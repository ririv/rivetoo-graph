# 条件边

[English](../conditional_edges.md) | 简体中文

## 用途

条件边表达“B 仅在条件成立时依赖 A”，不决定 A 或 B 是否执行。
节点本身的启用条件见[条件 Step](./conditional_steps.md)。

运行时形式是 `ConditionalEdge(source, target, condition)`，接线 API 则写为：

```python
target.wire(
    after=(ConditionalPrerequisite(source, condition=condition),),
)
```

`Graph.from_wiring()` 将其归一化为 ConditionalEdge，不创建隐藏 hook 或修改运行时拓扑。
显式 skip 仍可使用，但属于图结构的可选依赖应直接声明，不应藏在 action 副作用里。

## 两类条件

静态条件通过绑定 evaluator 的 StaticConditions 声明；ConditionalEdge 和
ConditionalPrerequisite 只接收 RuntimeCondition。两者求值时机不同：

- 静态条件由声明编译器调用显式 evaluator 求值，保留或移除关系。
- 运行时条件编译为带 ConditionId 的 prerequisite，交给统一 Rust session 调度。
- 条件边不会生成 gate Step 或额外 worker 任务。
- CompiledComposite 保留子图边界，内部一条边为假不会让整个子图提前完成。

## 静态条件

静态条件不读取 action 结果。平台、链接方式或配置选项等业务条件由宿主定义。

[static_condition.py](../../src/rivetoo_graph/static_condition.py) 只要求不透明 payload
和展示说明：

```python
StaticCondition(
    payload=SomePayload(...),
    description="environment(production)",
)
```

宿主通过 `StaticConditions(evaluator=..., conditions=...)` 提供解释，可以传入 StaticConditionEvaluator，
也可以传入 `Callable[[StaticCondition], bool]`。Graph 遍历声明、调用 evaluator，
生成编译结果，不修改原声明。

Graph 提供 `all_(...)`、`any_(...)`、`not_(...)` 组合静态条件，但组合后的叶子
仍然需要 evaluator：

```python
condition = all_(
    StaticCondition(payload="android", description="target OS is Android"),
    StaticCondition(payload="shared", description="shared linkage"),
)
```

这里的字符串是宿主约定，不是 Graph 内置表达式。StaticConditions 必须显式提供 evaluator。

### 静态边示例

假设 archive 仅在 Android 上需要等待环境 shim。此例只控制依赖边，shim Step 始终启用：

```python
build = Step(build_archive_input, label="build")
write_android_env_shim = Step(write_shim, label="write_android_env_shim")
archive = Step(create_archive, label="archive")

condition = StaticCondition(
    payload=AndroidOnlyPayload(),
    description="target_os(android)",
)
graph = Graph(
    steps=(build, write_android_env_shim, archive),
    edges=(
        UnconditionalEdge(source=build, target=archive),
        UnconditionalEdge(source=write_android_env_shim, target=archive),
    ),
    static=StaticConditions(
        evaluator=evaluate_target_condition,
        conditions={(write_android_env_shim, archive): condition},
    ),
)
```

条件为真：

```text
build ────────────────────→ archive
write_android_env_shim ───→ archive
```

条件为假：

```text
build ────────────────────→ archive
write_android_env_shim       （仍独立执行）
```

静态边为真时变成普通依赖，为假时移除。要同时禁用来源 Step，应声明静态节点规则。
示例中的 payload、evaluator 和业务 action 都由宿主提供。

## DSL 静态边

```python
static = StaticConditions(evaluator=evaluate_target_condition)
graph = static.edge(checks, build, condition=wait_for_checks).graph()
```

这仍是普通 GraphWiring，不增加 gate Step。与 static.when 不同，条件为假只删除边，
checks 和 build 都仍然存在。组合多个 StaticConditions 时，每条规则保留自己的 evaluator；
同一节点或边的重复规则报错，不覆盖。DSL 不会自动合并重复出现的 Step 对象。

## 运行时条件

[model.py](../../src/rivetoo_graph/model.py) 中的 RuntimeCondition 包含：

```python
class RuntimeCondition:
    when: ConditionalEdgePredicate
```

predicate 按目标接线 inputs 的顺序接收各个 `Option[object]`。框架在当前挂载中读取
结果，不需要查询上下文。
缺失与解包规则见[结果输入](./conditional_steps.md#结果输入)。

每个编译条件实例在当前 session 中最多求值一次。条件为真但来源尚未完成时，
后续调度只等待来源，不重新调用 predicate。同一个 Python 条件对象或 callable 用于
多个位置，不代表这些位置共用一次求值。

## inputs 与 after

inputs 将现有声明对象绑定到各自来源，action、节点条件和每条条件边分别接收自己的参数。after 只表达执行顺序。

```python
permission = Step(lambda: True)
design = Step(lambda: "implement login")
environment_check = Step(lambda: True)
prepare = Step(lambda: print("prepare environment"))

allowed = runtime_condition(
    when=lambda result: result.is_some() and result.unwrap() is True,
)
prepare_dependency = ConditionalPrerequisite(
    prepare,
    condition=runtime_condition(
        when=lambda result: result.is_some() and result.unwrap() is True,
    ),
)
implementation = Step(
    lambda ctx, plan: print(plan.unwrap()),
    condition=allowed,
)
graph = Graph.from_wiring((
    permission.wire(),
    design.wire(),
    environment_check.wire(),
    prepare.wire(),
    implementation.wire(
        after=(prepare_dependency,),
        inputs={
            implementation.action: (design,),
            allowed: (permission,),
            prepare_dependency.condition: (environment_check,),
        },
    ),
))
```

节点条件读取 permission，条件边读取 environment_check，action 读取 design。节点条件为假时跳过 implementation；条件边为假仅取消等待 prepare，不跳过 implementation，也不取消 prepare。匿名函数不需要额外手动包装成 LeafAction。

绑定按对象身份区分。同一 RuntimeCondition 用于两条前置边时共享绑定，但不合并求值实例；包装相同函数的两个独立条件对象可以绑定不同来源。

## 编译与执行边界

声明编译器处理静态条件，再将运行时条件编译为 prerequisite。session 只处理普通依赖、
ConditionId、inputs 与条件依赖；Python predicate 保存在 adapter 侧表。

编译后的 after 概念上类似：

```text
CompiledStepRef(probe)
CompiledConditionalDependency(
    node=CompiledStepRef(configure),
    condition=CompiledRuntimeCondition(inputs=(probe,), when=predicate),
)
```

调度器先等 inputs，再判定是否等待 configure。不需要伪造 action 或通过 metadata 解释
条件来源。子图内部使用相同规则。

失败沿激活的条件边与 inputs 传播，不沿未激活的条件边传播，详见
[失败传播](./execution.md#失败传播)。

## 职责边界

Graph 负责条件结构、求值时机与调度语义；宿主决定业务条件并提供 predicate。
predicate 不应隐藏副作用或领域兜底策略。
