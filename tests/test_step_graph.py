from __future__ import annotations

import pytest

from rivetoo_graph import (
    FrozenGraph,
    Graph,
    GraphError,
    GraphWiring,
    LeafAction,
    StaticCondition,
    Step,
    StepRunContext,
    StepWiring,
    UnconditionalEdge,
    freeze_graph,
    render_graph,
    render_graph_mermaid,
    runtime_condition,
    step,
)


def _noop(_run_ctx: StepRunContext) -> None:
    pass


def test_graph_allows_duplicate_step_labels() -> None:
    graph = Graph.from_wiring(
        label="root",
        wiring=(
            Step(label="build", action=LeafAction(fn=_noop)).wire(),
            Step(label="build", action=LeafAction(fn=_noop)).wire(),
        ),
    )

    assert tuple(step.label for step in graph.steps) == ("build", "build")


def test_graph_rejects_duplicate_step_objects() -> None:
    step = Step(label="build", action=LeafAction(fn=_noop))
    with pytest.raises(GraphError):
        Graph.from_wiring(
            label="root",
            wiring=(
                step.wire(),
                step.wire(),
            ),
        )


def test_graph_rejects_unknown_dependencies() -> None:
    foreign = Step(label="prepare", action=LeafAction(fn=_noop))
    with pytest.raises(GraphError):
        Graph.from_wiring(
            label="root",
            wiring=(
                Step(
                    label="build",
                    action=LeafAction(fn=_noop),
                ).wire(after=(foreign,)),
            ),
        )


def test_graph_rejects_dependency_cycles() -> None:
    configure = Step(label="configure", action=LeafAction(fn=_noop))
    build = Step(label="build", action=LeafAction(fn=_noop))
    with pytest.raises(GraphError, match="dependency cycle"):
        Graph.from_wiring(
            label="root",
            wiring=(
                configure.wire(after=(build,)),
                build.wire(after=(configure,)),
            ),
        )


def test_graph_allows_nested_graphs() -> None:
    configure = Step(label="configure", action=LeafAction(fn=_noop))
    build = Step(label="build", action=LeafAction(fn=_noop))
    nested = Graph.from_wiring(
        label="stage-build",
        wiring=(
            configure.wire(),
            build.wire(after=(configure,)),
        ),
    )
    prepare = Step(label="prepare", action=LeafAction(fn=_noop))
    stage = Step(label="stage", action=nested)
    root = Graph.from_wiring(
        label="root",
        wiring=(
            prepare.wire(),
            stage.wire(after=(prepare,)),
        ),
    )

    assert root.steps[1].action is nested


def test_step_can_be_declared_with_different_predecessors() -> None:
    configure = Step(label="configure", action=LeafAction(fn=_noop))
    restore_cache = Step(label="restore_cache", action=LeafAction(fn=_noop))
    compile_step = Step(label="compile", action=LeafAction(fn=_noop))

    normal_graph = Graph.from_wiring(
        wiring=(
            configure.wire(),
            StepWiring(step=compile_step, after=(configure,)),
        )
    )
    cached_graph = Graph.from_wiring(
        wiring=(
            restore_cache.wire(),
            StepWiring(step=compile_step, after=(restore_cache,)),
        )
    )

    assert normal_graph.steps[1] is compile_step
    assert cached_graph.steps[1] is compile_step
    assert normal_graph.edges == (
        UnconditionalEdge(source=configure, target=compile_step),
    )
    assert cached_graph.edges == (
        UnconditionalEdge(source=restore_cache, target=compile_step),
    )


def test_graph_wiring_sequences_steps() -> None:
    prepare = Step(label="prepare", action=LeafAction(fn=_noop))
    compile_step = Step(label="compile", action=LeafAction(fn=_noop))
    install = Step(label="install", action=LeafAction(fn=_noop))

    wiring = prepare >> compile_step >> install
    graph = wiring.graph(label="root")

    assert isinstance(wiring, GraphWiring)
    assert graph.steps == (prepare, compile_step, install)
    assert graph.edges == (
        UnconditionalEdge(source=prepare, target=compile_step),
        UnconditionalEdge(source=compile_step, target=install),
    )


def test_graph_wiring_fans_out_and_in() -> None:
    prepare = Step(label="prepare", action=LeafAction(fn=_noop))
    compile_a = Step(label="compile_a", action=LeafAction(fn=_noop))
    compile_b = Step(label="compile_b", action=LeafAction(fn=_noop))
    install = Step(label="install", action=LeafAction(fn=_noop))

    graph = (prepare >> (compile_a | compile_b) >> install).graph(label="root")

    assert graph.steps == (prepare, compile_a, compile_b, install)
    assert graph.edges == (
        UnconditionalEdge(source=prepare, target=compile_a),
        UnconditionalEdge(source=prepare, target=compile_b),
        UnconditionalEdge(source=compile_a, target=install),
        UnconditionalEdge(source=compile_b, target=install),
    )


def test_graph_wiring_preserves_independent_branches() -> None:
    prepare_a = Step(label="prepare_a", action=LeafAction(fn=_noop))
    compile_a = Step(label="compile_a", action=LeafAction(fn=_noop))
    prepare_b = Step(label="prepare_b", action=LeafAction(fn=_noop))
    compile_b = Step(label="compile_b", action=LeafAction(fn=_noop))

    graph = ((prepare_a >> compile_a) | (prepare_b >> compile_b)).graph()

    assert graph.steps == (prepare_a, compile_a, prepare_b, compile_b)
    assert graph.edges == (
        UnconditionalEdge(source=prepare_a, target=compile_a),
        UnconditionalEdge(source=prepare_b, target=compile_b),
    )


def test_runtime_condition_reads_define_wiring_boundaries() -> None:
    probe = Step(label="probe", action=LeafAction(fn=_noop))
    conditional = Step(
        label="conditional",
        action=LeafAction(fn=_noop),
        when=runtime_condition(
            reads=(probe,),
            when=lambda ctx: bool(ctx.require_value(probe)),
        ),
    )

    wiring = GraphWiring(steps=(probe, conditional))

    assert wiring.entries == (probe,)
    assert wiring.exits == (conditional,)


def test_graph_wiring_wraps_graph_operand_without_expanding_it() -> None:
    child_prepare = Step(label="child_prepare", action=LeafAction(fn=_noop))
    child_compile = Step(label="child_compile", action=LeafAction(fn=_noop))
    child = (child_prepare >> child_compile).graph(label="child")
    before = Step(label="before", action=LeafAction(fn=_noop))
    after = Step(label="after", action=LeafAction(fn=_noop))

    outer = (before >> child >> after).graph(label="outer")

    assert len(outer.steps) == 3
    mounted_child = outer.steps[1]
    assert mounted_child.label == "child"
    assert mounted_child.action is child
    assert child_prepare not in outer.steps
    assert child_compile not in outer.steps
    assert outer.edges == (
        UnconditionalEdge(source=before, target=mounted_child),
        UnconditionalEdge(source=mounted_child, target=after),
    )


def test_graph_wiring_wraps_unlabeled_graph_operand() -> None:
    before = Step(label="before", action=LeafAction(fn=_noop))
    child = Graph(steps=(Step(action=LeafAction(fn=_noop)),))
    after = Step(label="after", action=LeafAction(fn=_noop))

    graph = (before >> child >> after).graph()
    frozen = freeze_graph(graph, root_label="root")

    mounted_child = graph.steps[1]
    assert mounted_child.label is None
    assert mounted_child.action is child
    assert frozen.steps[1].label == "1"
    assert isinstance(frozen.steps[1].action, FrozenGraph)
    assert frozen.steps[1].action.label == "1"
    assert frozen.steps[1].action.steps[0].label == "1.0"


def test_graph_from_wiring_accepts_composed_wiring() -> None:
    prepare = Step(label="prepare", action=LeafAction(fn=_noop))
    compile_step = Step(label="compile", action=LeafAction(fn=_noop))

    graph = Graph.from_wiring(prepare >> compile_step, label="root")

    assert graph.label == "root"
    assert graph.steps == (prepare, compile_step)
    assert graph.edges == (UnconditionalEdge(source=prepare, target=compile_step),)


def test_graph_wiring_rejects_duplicate_step_object() -> None:
    step = Step(label="shared", action=LeafAction(fn=_noop))

    with pytest.raises(GraphError, match="contains the same step more than once"):
        (step | step).graph()


def test_step_decorator_uses_function_name_and_composes() -> None:
    @step
    def prepare() -> None:
        pass

    @step
    def compile_step() -> None:
        pass

    graph = (prepare >> compile_step).graph()

    assert prepare.label == "prepare"
    assert compile_step.label == "compile_step"
    assert graph.steps == (prepare, compile_step)
    assert graph.edges == (UnconditionalEdge(source=prepare, target=compile_step),)


def test_step_function_accepts_normalized_leaf_action() -> None:
    action = LeafAction(fn=_noop)

    declaration = step(action)

    assert declaration.label == "_noop"
    assert declaration.action is action


def test_step_function_wraps_labeled_graph_without_expanding_it() -> None:
    child = Graph(label="child")

    declaration = step(child)

    assert declaration.label == "child"
    assert declaration.action is child


def test_graph_step_wraps_graph_with_mount_parameters() -> None:
    child = Graph(label="child")
    condition = StaticCondition(payload="enabled", description="enabled")

    default_mount = child.step()
    configured_mount = child.step(label="build", when=condition)

    assert default_mount.label == "child"
    assert default_mount.action is child
    assert configured_mount.label == "build"
    assert configured_mount.when is condition
    assert configured_mount.action is child


def test_step_function_leaves_action_without_a_name_unlabeled() -> None:
    class CallableAction:
        def __call__(self) -> None:
            pass

    declaration = step(CallableAction())

    assert declaration.label is None


def test_step_function_leaves_lambda_unlabeled() -> None:
    declaration = step(lambda: None)

    assert declaration.label is None


def test_unlabeled_steps_use_mounted_ids_in_frozen_and_rendered_graphs() -> None:
    first = Step(action=lambda: None)
    second = Step(action=lambda: None)
    graph = (first >> second).graph()

    frozen = freeze_graph(graph, root_label="root")
    tree = render_graph(graph, root_label="root")
    mermaid = render_graph_mermaid(graph, root_label="root")

    assert tuple(item.label for item in frozen.steps) == ("0", "1")
    assert "├─ 0" in tree
    assert "└─ 1 [after: 0]" in tree
    assert '["0"]' in mermaid
    assert '["1"]' in mermaid
