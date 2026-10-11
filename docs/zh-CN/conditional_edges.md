# 条件边

[English](../conditional_edges.md) | 简体中文

## 用途

条件边表达“B 仅在条件成立时依赖 A”，不决定 A 或 B 是否执行。
节点本身的启用条件见[条件 Step](./conditional_steps.md)。

规范化形式是 `ConditionalEdge(source, target, condition)`，接线 API 则写为：

```python
target.wire(
    after=(ConditionalPrerequisite(source, condition=condition),),
)
```

`Graph.from_wiring()` 将其归一化为 ConditionalEdge，不创建隐藏 hook 或修改运行时拓扑。
显式 skip 仍可使用，但属于图结构的可选依赖应直接声明，不应藏在 action 副作用里。

## 两类条件

StaticCondition 和 RuntimeCondition 共用边模型，但求值时机不同：

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

宿主通过 `Graph.static_condition_eval` 提供解释，可以传入 StaticConditionEvaluator，
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

这里的字符串是宿主约定，不是 Graph 内置表达式。使用静态条件却没有 evaluator 会报错。

### 静态边示例

假设 archive 仅在 Android 上需要等待环境 shim。此例只控制依赖边，shim Step 始终启用：

```python
build = Step(build_archive_input, label="build")
write_android_env_shim = Step(write_shim, label="write_android_env_shim")
archive = Step(create_archive, label="archive")

graph = Graph.from_wiring(
    wiring=(
        build.wire(),
        write_android_env_shim.wire(),
        archive.wire(
            after=(
                build,
                ConditionalPrerequisite(
                    write_android_env_shim,
                    condition=StaticCondition(
                        payload=AndroidOnlyPayload(),
                        description="target_os(android)",
                    ),
                ),
            )
        ),
    ),
    static_condition_eval=evaluate_target_condition,
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

静态边为真时变成普通依赖，为假时移除。要同时禁用来源 Step，应声明 Step.when。
示例中的 payload、evaluator 和业务 action 都由宿主提供。

## 运行时条件

[model.py](../../src/rivetoo_graph/model.py) 中的 RuntimeCondition 包含：

```python
class RuntimeCondition:
    when: ConditionalEdgePredicate
    reads: tuple[Step, ...] = ()
```

predicate 接收 StepReadContext；reads 声明判定前需要哪些 Step 的结果。

每个编译条件实例在当前 session 中最多求值一次。条件为真但来源尚未完成时，
后续调度只等待来源，不重新调用 predicate。同一个 Python 条件对象或 callable 用于
多个位置，不代表这些位置共用一次求值。

## reads 与 after

- 普通 after 表达目标 action 必须等待谁。
- 条件的 reads 表达判断这条依赖是否成立前，需要读取谁。

一个 Step 可以有多个条件边，各自读取不同结果，因此不能只用一份 after 代替 reads。

### 一个条件依赖

build 等待缓存探测，并仅在 miss 时等待 configure：

```python
compile_cache_probe = Step(probe_cache, label="compile_cache_probe")
configure = Step(configure_source, label="configure")
build = Step(build_source, label="build")

graph = Graph.from_wiring(
    wiring=(
        compile_cache_probe.wire(),
        configure.wire(),
        build.wire(
            after=(
                compile_cache_probe,
                ConditionalPrerequisite(
                    configure,
                    condition=runtime_condition(
                        when=cache_miss,
                        reads=(compile_cache_probe,),
                    ),
                ),
            )
        ),
    )
)
```

普通 probe 前置表达 action 顺序，reads 表达 predicate 输入。reads 本身已阻塞目标，
所以若没有独立的顺序意图，不必在普通 after 中重复 probe。这些边都不会禁用 configure。

### 多个条件依赖

```python
build.wire(
    after=(
        ConditionalPrerequisite(
            configure,
            condition=runtime_condition(when=config_miss, reads=(probe_config,)),
        ),
        ConditionalPrerequisite(
            prepare_assets,
            condition=runtime_condition(when=assets_missing, reads=(probe_assets,)),
        ),
    )
)
```

两个 predicate 分别读取 probe_config 和 probe_assets，两份 reads 都参与环检测与调度。

## 编译与执行边界

声明编译器处理静态条件，再将运行时条件编译为 prerequisite。session 只处理普通依赖、
ConditionId、reads 与条件依赖；Python predicate 保存在 adapter 侧表。

编译后的 after 概念上类似：

```text
CompiledStepRef(probe)
CompiledConditionalDependency(
    node=CompiledStepRef(configure),
    condition=CompiledRuntimeCondition(reads=(probe,), when=predicate),
)
```

调度器先等 reads，再判定是否等待 configure。不需要伪造 action 或通过 metadata 解释
条件来源。子图内部使用相同规则。

失败沿激活的条件边与 reads 传播，不沿未激活的条件边传播，详见
[失败传播](./execution.md#失败传播)。

## 职责边界

Graph 负责条件结构、求值时机与调度语义；宿主决定业务条件并提供 predicate。
predicate 不应隐藏副作用或领域兜底策略。
