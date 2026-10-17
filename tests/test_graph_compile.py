from __future__ import annotations

from dataclasses import dataclass, replace

import pytest

from rivetoo_graph import (
    CompiledCompositeRef,
    CompiledConditionalDependency,
    CompiledRuntimeCondition,
    CompiledStepRef,
    ConditionalPrerequisite,
    Graph,
    GraphCompilation,
    GraphError,
    GraphExecution,
    GraphExecutionState,
    GraphRenderOptions,
    GraphResultStore,
    LeafAction,
    ParentGraphStepRef,
    StaticCondition,
    StaticConditionEvaluator,
    StaticConditions,
    Step,
    StepId,
    ThreadGraphRunOptions,
    all_,
    any_,
    compile_graph,
    not_,
    render_execution_structure,
    render_graph,
    render_graph_compilation_mermaid,
    render_graph_execution,
    render_graph_mermaid,
    render_live_graph_execution,
    runtime_condition,
)


@dataclass(frozen=True, slots=True)
class _ConditionEvaluator(StaticConditionEvaluator):
    active: frozenset[str]

    def evaluate(self, condition: StaticCondition) -> bool:
        return condition.payload in self.active


def _condition(name: str) -> StaticCondition:
    return StaticCondition(payload=name, description=name)


def test_compile_graph_flattens_nested_graph_and_preserves_structure() -> None:
    stage_graph = _make_stage_graph()
    root_graph = _make_nested_graph(stage_graph=stage_graph)

    compilation = compile_graph(root_graph, root_label="plan")

    assert isinstance(compilation, GraphCompilation)
    assert compilation.label == "root"
    assert [step.id for step in compilation.steps] == [
        StepId((0,)),
        StepId((1, 0)),
        StepId((1, 1)),
        StepId((2,)),
    ]
    assert compilation.steps[0].after == ()
    assert compilation.steps[1].after == ()
    assert compilation.steps[2].after == (CompiledStepRef(StepId((1, 0))),)
    assert compilation.steps[3].after == (CompiledCompositeRef(StepId((1,))),)
    assert len(compilation.composites) == 1
    stage = compilation.composites[0]
    assert stage.label_path == ("stage",)
    assert stage.after == (CompiledStepRef(StepId((0,))),)
    assert stage.children == (
        CompiledStepRef(StepId((1, 0))),
        CompiledStepRef(StepId((1, 1))),
    )
    assert compilation.steps[1].context.label_path == (
        "stage",
        "configure",
    )
    assert compilation.steps[1].context.graph_label_path == ("stage",)
    assert compilation.steps[1].context.graph_step_ids == {
        stage_graph.steps[0]: StepId((1, 0)),
        stage_graph.steps[1]: StepId((1, 1)),
    }
    assert (
        compilation.steps[1].context.ancestor_graph_scopes
        is compilation.steps[2].context.ancestor_graph_scopes
    )
    assert (
        compilation.steps[1].context.ancestor_composite_ids
        is compilation.steps[2].context.ancestor_composite_ids
    )


def test_pruned_native_side_tables_keep_only_compiled_nodes() -> None:
    from rivetoo_graph._native import compile_declaration

    removed = Step(
        lambda: None,
        label="removed",
        when=runtime_condition(inputs=(), when=lambda: False),
    )
    kept = Step(lambda: None, label="kept")
    checked = Step(
        lambda: None,
        label="checked",
        when=runtime_condition(inputs=(kept,), when=lambda _ctx: True),
    )
    graph = Graph(
        steps=(removed, kept, checked),
        static=StaticConditions(
            evaluator=lambda _condition: False,
            conditions={removed: _condition("disabled")},
        ),
    )
    native = compile_declaration(graph, "test")
    assert native.declaration_nodes() == [1, 2]
    assert native.node_step_ids() == [[1], [2]]
    assert native.node_labels() == ["kept", "checked"]
    assert native.source_steps() == [kept, checked]
    assert len(native.actions()) == 2
    assert native.condition_reads() == [[1]]
    compiled = compile_graph(graph, root_label="test")
    assert [step.id for step in compiled.steps] == [StepId((1,)), StepId((2,))]
    assert compiled.steps[1].when is not None
    assert compiled.steps[1].when.inputs == (compiled.steps[0].ref,)


def test_graph_compilation_rejects_unknown_prerequisite() -> None:
    source_graph = Graph.from_wiring(
        label="root",
        wiring=(Step(label="build", action=LeafAction(fn=lambda: None)).wire(),),
    )
    compilation = GraphExecution(graph=source_graph).compile()
    invalid_step = replace(
        compilation.steps[0],
        after=(CompiledStepRef(StepId((99,))),),
    )

    with pytest.raises(GraphError, match="unknown node"):
        GraphCompilation(
            source_graph=source_graph,
            label=compilation.label,
            steps=(invalid_step,),
            composites=(),
            roots=(invalid_step.ref,),
        )


def test_graph_compilation_rejects_missing_composite_child() -> None:
    source_graph = _make_nested_graph(stage_graph=_make_stage_graph())
    compilation = GraphExecution(graph=source_graph).compile()
    invalid_composite = replace(compilation.composites[0], children=())

    with pytest.raises(GraphError, match="children do not match owned nodes"):
        GraphCompilation(
            source_graph=source_graph,
            label=compilation.label,
            steps=compilation.steps,
            composites=(invalid_composite,),
            roots=compilation.roots,
        )


def test_graph_compilation_rejects_dependency_cycle() -> None:
    source_graph = Graph.from_wiring(
        label="root",
        wiring=(
            Step(label="prepare", action=LeafAction(fn=lambda: None)).wire(),
            Step(label="build", action=LeafAction(fn=lambda: None)).wire(),
        ),
    )
    compilation = GraphExecution(graph=source_graph).compile()
    prepare, build = compilation.steps

    with pytest.raises(GraphError, match="dependency cycle"):
        GraphCompilation(
            source_graph=source_graph,
            label=compilation.label,
            steps=(
                replace(prepare, after=(build.ref,)),
                replace(build, after=(prepare.ref,)),
            ),
            composites=(),
            roots=compilation.roots,
        )


def test_graph_rejects_runtime_condition_read_cycle() -> None:
    build = Step(label="build", action=lambda: None)
    prepare = Step(
        label="prepare",
        when=runtime_condition(
            inputs=(build,),
            when=lambda _ctx: True,
        ),
        action=lambda: None,
    )
    with pytest.raises(GraphError, match="dependency cycle"):
        Graph.from_wiring(
            label="root",
            wiring=(
                prepare.wire(),
                build.wire(after=(prepare,)),
            ),
        )


def test_graph_compilation_rejects_runtime_condition_read_cycle() -> None:
    source_graph = Graph.from_wiring(
        label="root",
        wiring=(
            Step(label="prepare", action=lambda: None).wire(),
            Step(label="build", action=lambda: None).wire(),
        ),
    )
    compilation = GraphExecution(graph=source_graph).compile()
    prepare, build = compilation.steps

    with pytest.raises(GraphError, match="dependency cycle"):
        GraphCompilation(
            source_graph=source_graph,
            label=compilation.label,
            steps=(
                replace(
                    prepare,
                    when=CompiledRuntimeCondition(
                        inputs=(build.ref,),
                        when=lambda _ctx: True,
                    ),
                ),
                replace(build, after=(prepare.ref,)),
            ),
            composites=(),
            roots=compilation.roots,
        )


def test_run_compiled_matches_dynamic_run_and_preserves_local_step_lookup() -> None:
    dynamic_state = GraphExecutionState(results=GraphResultStore())
    compiled_state = GraphExecutionState(results=GraphResultStore())

    dynamic_plan = GraphExecution(
        graph=_make_value_graph(), execution_state=dynamic_state
    )
    compiled_plan = GraphExecution(
        graph=_make_value_graph(), execution_state=compiled_state
    )

    dynamic_plan.run(options=ThreadGraphRunOptions(workers=1))
    compiled_plan.run(options=ThreadGraphRunOptions(workers=1), mode="compiled")

    assert dynamic_state.executed_steps == [
        StepId((0,)),
        StepId((1,)),
        StepId((1, 0)),
        StepId((1, 1)),
        StepId((2,)),
    ]
    assert compiled_state.executed_steps == [
        StepId((0,)),
        StepId((1, 0)),
        StepId((1, 1)),
        StepId((2,)),
    ]
    assert compiled_state.entered_composites == {StepId((1,))}
    assert compiled_state.completed_composites == {StepId((1,))}
    assert "stage [graph] [completed]" in render_graph_execution(
        _make_value_graph(),
        compiled_state,
        root_label="root",
        mode="compiled",
    )
    assert dynamic_state.results is not None
    assert compiled_state.results is not None
    assert dynamic_state.results.require(StepId((1, 1))).value == "seed:configured"
    assert compiled_state.results.require(StepId((1, 1))).value == "seed:configured"
    assert dynamic_state.results.require(StepId((2,))).value == "packaged"
    assert compiled_state.results.require(StepId((2,))).value == "packaged"


def test_run_compiled_skips_composite_step_by_prefix() -> None:
    state = GraphExecutionState(
        initial_skip_requests={StepId((1,))},
        results=GraphResultStore(),
    )
    plan = GraphExecution(graph=_make_value_graph(), execution_state=state)

    plan.run(options=ThreadGraphRunOptions(workers=1), mode="compiled")

    assert state.executed_steps == [StepId((0,)), StepId((2,))]
    assert state.results is not None
    assert state.results.require(StepId((0,))).value == "seed"
    assert state.results.require(StepId((2,))).value == "packaged"
    assert not state.results.has(StepId((1, 0)))
    assert not state.results.has(StepId((1, 1)))


def test_render_graph_renders_terminal_tree() -> None:
    rendered = render_execution_structure(
        GraphExecution(
            graph=_make_nested_graph(stage_graph=_make_stage_graph()),
        ),
        options=GraphRenderOptions(),
    )

    assert rendered == "\n".join(
        [
            "root",
            "├─ prepare",
            "├─ stage [graph] [after: prepare]",
            "│  ├─ configure",
            "│  └─ build [after: configure]",
            "└─ package [after: stage]",
        ]
    )


def test_render_graph_accepts_graph_compilation_as_structure_view() -> None:
    rendered = render_graph(
        GraphExecution(
            graph=_make_nested_graph(stage_graph=_make_stage_graph())
        ).compile(),
        root_label="root",
    )

    assert rendered == "\n".join(
        [
            "root",
            "├─ prepare",
            "├─ stage [graph] [after: prepare]",
            "│  ├─ configure",
            "│  └─ build [after: configure]",
            "└─ package [after: stage]",
        ]
    )


def test_render_graph_can_render_compilation_view_from_graph() -> None:
    rendered = render_graph(
        _make_nested_graph(stage_graph=_make_stage_graph()),
        root_label="root",
        mode="compiled",
    )

    assert rendered == "\n".join(
        [
            "compilation: root",
            "compiled_steps",
            "- prepare",
            "- stage.configure",
            "- stage.build [after: stage.configure]",
            "- package [after: stage]",
            "compiled_composites",
            "- stage [children: stage.configure, stage.build] [after: prepare]",
        ]
    )


def test_render_graph_can_render_compilation_view_from_compilation() -> None:
    rendered = render_graph(
        GraphExecution(
            graph=_make_nested_graph(stage_graph=_make_stage_graph())
        ).compile(),
        root_label="root",
        mode="compiled",
    )

    assert rendered == "\n".join(
        [
            "compilation: root",
            "compiled_steps",
            "- prepare",
            "- stage.configure",
            "- stage.build [after: stage.configure]",
            "- package [after: stage]",
            "compiled_composites",
            "- stage [children: stage.configure, stage.build] [after: prepare]",
        ]
    )


def test_render_graph_mermaid_renders_graph_structure() -> None:
    rendered = render_graph_mermaid(
        _make_nested_graph(stage_graph=_make_stage_graph()),
        root_label="root",
    )

    assert 'subgraph graph_root["root"]' in rendered
    assert 'subgraph graph_root_stage["stage"]' in rendered
    assert 'step_root_1_stage["stage"]' in rendered
    assert "step_root_0_prepare --> step_root_1_stage" in rendered
    assert "step_root_stage_0_configure --> step_root_stage_1_build" in rendered


def test_render_graph_mermaid_accepts_graph_compilation_as_structure_view() -> None:
    rendered = render_graph_mermaid(
        GraphExecution(
            graph=_make_nested_graph(stage_graph=_make_stage_graph())
        ).compile(),
        root_label="root",
    )

    assert 'subgraph graph_root_stage["stage"]' in rendered
    assert "step_root_stage_0_configure --> step_root_stage_1_build" in rendered


def test_render_graph_compilation_mermaid_shows_compiled_graph_and_composites() -> None:
    rendered = render_graph_compilation_mermaid(
        GraphExecution(
            graph=_make_nested_graph(stage_graph=_make_stage_graph())
        ).compile(),
        root_label="root",
    )

    assert "subgraph compiled_graph" in rendered
    assert 'compiled_1["stage.configure"]' in rendered
    assert "subgraph compiled_composites" in rendered
    assert 'compiled_4["stage [composite]"]' in rendered


def test_render_graph_mermaid_can_render_compilation_view_from_graph() -> None:
    rendered = render_graph_mermaid(
        _make_nested_graph(stage_graph=_make_stage_graph()),
        root_label="root",
        mode="compiled",
    )

    assert "subgraph compiled_graph" in rendered
    assert 'compiled_1["stage.configure"]' in rendered
    assert 'compiled_4["stage [composite]"]' in rendered


def test_render_graph_mermaid_can_render_compilation_view_from_compilation() -> None:
    rendered = render_graph_mermaid(
        GraphExecution(
            graph=_make_nested_graph(stage_graph=_make_stage_graph())
        ).compile(),
        root_label="root",
        mode="compiled",
    )

    assert "subgraph compiled_graph" in rendered
    assert 'compiled_4["stage [composite]"]' in rendered


def test_render_graph_execution_overlays_runtime_status() -> None:
    probe = Step(label="probe", action=LeafAction(fn=lambda _ctx: True))
    build = Step(
        label="build",
        when=runtime_condition(
            inputs=(probe,),
            when=lambda result: not bool(result.unwrap()),
        ),
        action=LeafAction(fn=lambda _ctx: None),
    )
    graph = Graph.from_wiring(
        label="root",
        wiring=(
            probe.wire(),
            build.wire(after=(probe,)),
        ),
    )
    state = GraphExecutionState(results=GraphResultStore())

    GraphExecution(graph=graph, execution_state=state).run(
        options=ThreadGraphRunOptions(workers=1)
    )

    rendered = render_graph_execution(graph, state, root_label="root")

    assert rendered == "\n".join(
        [
            "execution: root (mode=dynamic)",
            "├─ probe [executed] [result=true]",
            "└─ build [when=false] [skipped] [after: probe]",
        ]
    )


def test_render_live_graph_execution_hides_not_reached_steps() -> None:
    probe = Step(label="probe", action=LeafAction(fn=lambda _ctx: True))
    build = Step(
        label="build",
        when=runtime_condition(
            inputs=(probe,),
            when=lambda result: not bool(result.unwrap()),
        ),
        action=LeafAction(fn=lambda _ctx: None),
    )
    package = Step(label="package", action=LeafAction(fn=lambda _ctx: None))
    graph = Graph.from_wiring(
        label="root",
        wiring=(
            probe.wire(),
            build.wire(after=(probe,)),
            package.wire(after=(build,)),
        ),
    )
    state = GraphExecutionState(results=GraphResultStore())

    rendered = render_live_graph_execution(graph, state, root_label="root")

    assert rendered == "graph=root mode=dynamic running=idle last=- remaining=3"


def test_compile_graph_resolves_static_conditions() -> None:
    prepare = Step(label="prepare", action=LeafAction(fn=lambda: None))
    android_shim = Step(label="android_shim", action=LeafAction(fn=lambda: None))
    archive = Step(label="archive", action=LeafAction(fn=lambda: None))
    graph = Graph.from_wiring(
        label="root",
        wiring=(
            prepare.wire(),
            android_shim.wire(),
            archive.wire(
                after=(
                    prepare,
                    android_shim,
                )
            ),
        ),
        static=StaticConditions(
            evaluator=_ConditionEvaluator(active=frozenset({"android"})),
            conditions={(android_shim, archive): _condition("android")},
        ),
    )

    compilation = compile_graph(graph, root_label="root")

    assert compilation.steps[2].after == (
        CompiledStepRef(StepId((0,))),
        CompiledStepRef(StepId((1,))),
    )


def test_compile_graph_removes_static_condition_edge_when_false() -> None:
    prepare = Step(label="prepare", action=lambda: None)
    android_shim = Step(label="android_shim", action=lambda: None)
    archive = Step(label="archive", action=lambda: None)
    graph = Graph.from_wiring(
        (
            prepare.wire(),
            android_shim.wire(),
            archive.wire(
                after=(
                    prepare,
                    android_shim,
                )
            ),
        ),
        label="root",
        static=StaticConditions(
            evaluator=_ConditionEvaluator(active=frozenset({"ios"})),
            conditions={(android_shim, archive): _condition("android")},
        ),
    )

    compilation = compile_graph(graph, root_label="root")

    assert compilation.steps[2].after == (CompiledStepRef(StepId((0,))),)


def test_compile_graph_preserves_runtime_condition_edge() -> None:
    probe = Step(label="probe", action=lambda: "miss")
    configure = Step(label="configure", action=lambda: None)
    condition = runtime_condition(
        inputs=(probe,),
        when=lambda result: result.unwrap() == "miss",
    )
    build = Step(label="build", action=lambda: None)
    graph = Graph.from_wiring(
        (
            probe.wire(),
            configure.wire(),
            build.wire(
                after=(
                    probe,
                    ConditionalPrerequisite(
                        step=configure,
                        condition=condition,
                    ),
                )
            ),
        ),
        label="root",
    )

    compilation = compile_graph(graph, root_label="root")

    assert compilation.steps[2].after == (
        CompiledStepRef(StepId((0,))),
        CompiledConditionalDependency(
            node=CompiledStepRef(StepId((1,))),
            condition=CompiledRuntimeCondition(
                inputs=(CompiledStepRef(StepId((0,))),),
                when=condition.when,
            ),
        ),
    )


def test_compile_graph_accepts_static_condition_eval_function() -> None:
    def _eval(condition: StaticCondition) -> bool:
        return condition.payload == _condition("android").payload

    prepare = Step(label="prepare", action=LeafAction(fn=lambda: None))
    android_shim = Step(label="android_shim", action=LeafAction(fn=lambda: None))
    archive = Step(label="archive", action=LeafAction(fn=lambda: None))
    graph = Graph.from_wiring(
        label="root",
        wiring=(
            prepare.wire(),
            android_shim.wire(),
            archive.wire(
                after=(
                    prepare,
                    android_shim,
                )
            ),
        ),
        static=StaticConditions(
            evaluator=_eval, conditions={(android_shim, archive): _condition("android")}
        ),
    )

    compilation = compile_graph(graph, root_label="root")

    assert compilation.steps[2].after == (
        CompiledStepRef(StepId((0,))),
        CompiledStepRef(StepId((1,))),
    )


def test_compile_graph_handles_builtin_static_condition_combinators() -> None:
    prepare = Step(label="prepare", action=LeafAction(fn=lambda: None))
    android_shim = Step(label="android_shim", action=LeafAction(fn=lambda: None))
    shared_only = Step(label="shared_only", action=LeafAction(fn=lambda: None))
    archive = Step(label="archive", action=LeafAction(fn=lambda: None))
    graph = Graph.from_wiring(
        label="root",
        wiring=(
            prepare.wire(),
            android_shim.wire(),
            shared_only.wire(),
            archive.wire(
                after=(
                    prepare,
                    android_shim,
                    shared_only,
                )
            ),
        ),
        static=StaticConditions(
            evaluator=_ConditionEvaluator(active=frozenset({"android"})),
            conditions={
                (android_shim, archive): all_(
                    _condition("android"),
                    not_(_condition("ios")),
                ),
                (shared_only, archive): any_(
                    _condition("ios"),
                    _condition("android"),
                ),
            },
        ),
    )

    compilation = compile_graph(graph, root_label="root")

    assert compilation.steps[3].after == (
        CompiledStepRef(StepId((0,))),
        CompiledStepRef(StepId((1,))),
        CompiledStepRef(StepId((2,))),
    )


def test_compile_graph_preserves_conditional_dependency_without_gate_step() -> None:
    probe = Step(label="probe", action=LeafAction(fn=lambda: "miss"))
    configure = Step(label="configure", action=LeafAction(fn=lambda: "configured"))
    build = Step(label="build", action=LeafAction(fn=lambda: "built"))
    graph = Graph.from_wiring(
        label="root",
        wiring=(
            probe.wire(),
            configure.wire(),
            build.wire(
                after=(
                    probe,
                    ConditionalPrerequisite(
                        step=configure,
                        condition=runtime_condition(
                            when=lambda result: result.unwrap() == "miss",
                            inputs=(probe,),
                        ),
                    ),
                )
            ),
        ),
    )

    compilation = GraphExecution(graph=graph).compile()

    assert [step.id for step in compilation.steps] == [
        StepId((0,)),
        StepId((1,)),
        StepId((2,)),
    ]
    build_step = compilation.steps[2]
    assert build_step.after[0] == CompiledStepRef(StepId((0,)))
    conditional = build_step.after[1]
    assert isinstance(conditional, CompiledConditionalDependency)
    assert conditional.node == CompiledStepRef(StepId((1,)))
    assert conditional.condition.inputs == (CompiledStepRef(StepId((0,))),)


def test_graph_execution_compile_supports_static_and_runtime_conditions() -> None:
    probe = Step(label="probe", action=LeafAction(fn=lambda: "miss"))
    configure = Step(label="configure", action=LeafAction(fn=lambda: "configured"))
    android_shim = Step(label="android_shim", action=LeafAction(fn=lambda: None))
    build = Step(label="build", action=LeafAction(fn=lambda: "built"))
    graph = Graph.from_wiring(
        label="root",
        wiring=(
            probe.wire(),
            configure.wire(),
            android_shim.wire(),
            build.wire(
                after=(
                    probe,
                    ConditionalPrerequisite(
                        step=configure,
                        condition=runtime_condition(
                            when=lambda result: result.unwrap() == "miss",
                            inputs=(probe,),
                        ),
                    ),
                    android_shim,
                )
            ),
        ),
        static=StaticConditions(
            evaluator=_ConditionEvaluator(active=frozenset({"android"})),
            conditions={(android_shim, build): _condition("android")},
        ),
    )

    compilation = GraphExecution(graph=graph).compile()

    assert [step.id for step in compilation.steps] == [
        StepId((0,)),
        StepId((1,)),
        StepId((2,)),
        StepId((3,)),
    ]
    build_after = compilation.steps[3].after
    assert build_after[0] == CompiledStepRef(StepId((0,)))
    assert isinstance(build_after[1], CompiledConditionalDependency)
    assert build_after[2] == CompiledStepRef(StepId((2,)))


def test_render_graph_compiled_shows_conditional_dependency() -> None:
    probe = Step(label="probe", action=LeafAction(fn=lambda: "miss"))
    configure = Step(label="configure", action=LeafAction(fn=lambda: "configured"))
    build = Step(label="build", action=LeafAction(fn=lambda: "built"))
    graph = Graph.from_wiring(
        label="root",
        wiring=(
            probe.wire(),
            configure.wire(),
            build.wire(
                after=(
                    probe,
                    ConditionalPrerequisite(
                        step=configure,
                        condition=runtime_condition(
                            when=lambda result: result.unwrap() == "miss",
                            inputs=(probe,),
                        ),
                    ),
                )
            ),
        ),
    )

    rendered = render_graph(graph, root_label="root", mode="compiled")

    assert "- build [after: probe, configure (条件边: runtime:" in rendered


def test_compile_graph_removes_static_disabled_step_and_dependency() -> None:
    prepare = Step(label="prepare", action=LeafAction(fn=lambda: None))
    android_only = Step(
        label="android_only",
        action=LeafAction(fn=lambda: None),
    )
    archive = Step(label="archive", action=LeafAction(fn=lambda: None))
    graph = Graph.from_wiring(
        label="root",
        wiring=(
            prepare.wire(),
            android_only.wire(),
            archive.wire(after=(prepare, android_only)),
        ),
        static=StaticConditions(
            evaluator=_ConditionEvaluator(active=frozenset({"ios"})),
            conditions={android_only: _condition("android")},
        ),
    )

    compilation = compile_graph(graph, root_label="root")

    assert [step.id for step in compilation.steps] == [StepId((0,)), StepId((2,))]
    assert compilation.steps[1].after == (CompiledStepRef(StepId((0,))),)


def test_compile_graph_preserves_runtime_when_on_leaf_step() -> None:
    probe = Step(label="probe", action=LeafAction(fn=lambda: False))
    write_cache = Step(
        label="write_cache",
        action=LeafAction(fn=lambda: None),
        when=runtime_condition(
            inputs=(probe,),
            when=lambda result: bool(result.unwrap()),
        ),
    )
    graph = Graph.from_wiring(
        label="root",
        wiring=(
            probe.wire(),
            write_cache.wire(),
        ),
    )

    compilation = GraphExecution(graph=graph).compile()

    assert compilation.steps[1].id == StepId((1,))
    assert compilation.steps[1].when is not None


def test_compile_graph_flattens_runtime_conditioned_composite() -> None:
    probe = Step(label="probe", action=LeafAction(fn=lambda: False))
    configure = Step(label="configure", action=LeafAction(fn=lambda: None))
    stage = Step(
        label="stage",
        when=runtime_condition(
            inputs=(probe,),
            when=lambda result: bool(result.unwrap()),
        ),
        action=Graph.from_wiring(
            label="stage",
            wiring=(configure.wire(),),
        ),
    )
    graph = Graph.from_wiring(
        label="root",
        wiring=(
            probe.wire(),
            stage.wire(after=(probe,)),
        ),
    )

    compilation = GraphExecution(graph=graph).compile()

    assert [step.id for step in compilation.steps] == [
        StepId((0,)),
        StepId((1, 0)),
    ]
    assert len(compilation.composites) == 1
    stage = compilation.composites[0]
    assert stage.label_path == ("stage",)
    assert stage.after == (CompiledStepRef(StepId((0,))),)
    assert stage.when is not None
    assert stage.when.inputs == (CompiledStepRef(StepId((0,))),)
    assert stage.children == (CompiledStepRef(StepId((1, 0))),)


def test_compile_graph_flattens_child_with_conditional_dependency() -> None:
    source = Step(label="source", action=lambda: None)
    target = Step(label="target", action=lambda: None)
    child = Graph.from_wiring(
        label="child",
        wiring=(
            source.wire(),
            target.wire(
                after=(
                    ConditionalPrerequisite(
                        step=source,
                        condition=runtime_condition(when=lambda: False),
                    ),
                )
            ),
        ),
    )
    child_step = Step(label="child", action=child)
    package = Step(label="package", action=lambda: None)
    graph = Graph.from_wiring(
        label="root",
        wiring=(
            child_step.wire(),
            package.wire(after=(child_step,)),
        ),
    )

    compilation = GraphExecution(graph=graph).compile()

    assert [step.id for step in compilation.steps] == [
        StepId((0, 0)),
        StepId((0, 1)),
        StepId((1,)),
    ]
    conditional = compilation.steps[1].after[0]
    assert isinstance(conditional, CompiledConditionalDependency)
    assert conditional.node == CompiledStepRef(StepId((0, 0)))
    assert compilation.steps[2].after == (CompiledCompositeRef(StepId((0,))),)
    assert compilation.composites[0].children == (
        CompiledStepRef(StepId((0, 0))),
        CompiledStepRef(StepId((0, 1))),
    )
    rendered = render_graph(compilation, root_label="root", mode="compiled")
    assert "- child.target [after: child.source (条件边:" in rendered
    assert "- child [children: child.source, child.target]" in rendered


def _make_nested_graph(*, stage_graph: Graph) -> Graph:
    prepare = Step(label="prepare", action=LeafAction(fn=lambda: None))
    stage = Step(label="stage", action=stage_graph)
    package = Step(label="package", action=LeafAction(fn=lambda: None))
    return Graph.from_wiring(
        label="root",
        wiring=(
            prepare.wire(),
            stage.wire(after=(prepare,)),
            package.wire(after=(stage,)),
        ),
    )


def _make_stage_graph() -> Graph:
    configure = Step(label="configure", action=LeafAction(fn=lambda: None))
    build = Step(label="build", action=LeafAction(fn=lambda: None))
    return Graph.from_wiring(
        label="stage",
        wiring=(
            configure.wire(),
            build.wire(after=(configure,)),
        ),
    )


def _make_value_graph() -> Graph:
    prepare = Step(label="prepare", action=LeafAction(fn=lambda: "seed"))
    configure = Step(
        label="configure",
        action=LeafAction(
            fn=lambda run_ctx: (
                f"{run_ctx.require_value(ParentGraphStepRef(step=prepare, levels_up=1))}:configured"
            )
        ),
    )
    build = Step(
        label="build",
        action=LeafAction(fn=lambda run_ctx: run_ctx.require_value(configure)),
    )
    stage = Step(
        label="stage",
        action=Graph.from_wiring(
            label="stage",
            wiring=(configure.wire(), build.wire(after=(configure,))),
        ),
    )
    package = Step(label="package", action=LeafAction(fn=lambda: "packaged"))
    return Graph.from_wiring(
        label="root",
        wiring=(
            prepare.wire(),
            stage.wire(after=(prepare,)),
            package.wire(after=(stage,)),
        ),
    )
