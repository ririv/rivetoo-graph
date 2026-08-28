from __future__ import annotations

import asyncio
import logging
import threading
import time
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field, replace
from pathlib import Path

import pytest
from _pytest.logging import LogCaptureFixture

from rivetoo_graph import (
    AsyncGraphRunOptions,
    ConditionalPrerequisite,
    Graph,
    GraphExecutionControl,
    GraphExecutionState,
    GraphExecutionStopped,
    GraphMode,
    GraphObservationEvent,
    GraphObserve,
    GraphPlan,
    GraphResultStore,
    LeafAction,
    ParentGraphStepRef,
    SerialGraphExecutor,
    StaticCondition,
    StaticConditionEvaluator,
    Step,
    StepId,
    StepRunContext,
    ThreadGraphRunOptions,
    TreeFileExecutionOutput,
    render_graph_execution,
    render_live_graph_execution,
    runtime_condition,
)
from rivetoo_graph.compile import compile_graph
from rivetoo_graph.runtime.compiled import CompiledExecutionHost


@dataclass(frozen=True, slots=True)
class _ConditionEvaluator(StaticConditionEvaluator):
    active: frozenset[str]

    def evaluate(self, condition: StaticCondition) -> bool:
        return condition.payload in self.active


def _condition(name: str) -> StaticCondition:
    return StaticCondition(payload=name, description=name)


def test_compiled_host_forwards_only_new_skip_requests() -> None:
    first = Step(lambda: None, label="first")
    second = Step(lambda: None, label="second")
    compilation = compile_graph(Graph(steps=(first, second)), root_label="root")
    state = GraphExecutionState(skipped_steps={StepId((0,))})
    host = CompiledExecutionHost(
        compilation,
        state,
        observer=None,
        run_leaf=lambda _step: None,
    )

    assert host.skipped_nodes() == [0]
    assert host.skipped_nodes() == []

    state.skip_step(StepId((1,)))
    assert host.skipped_nodes() == [1]
    assert host.skipped_nodes() == []


@dataclass(slots=True)
class _RecordingActionLog:
    active_path: Path | None = None
    appended: list[tuple[Path, str]] = field(default_factory=list)

    @contextmanager
    def scope(self, path: Path) -> Iterator[None]:
        if self.active_path is not None:
            raise RuntimeError("action log scope is already active")
        self.active_path = path
        try:
            yield
        finally:
            self.active_path = None

    def append(self, path: Path, text: str) -> None:
        self.appended.append((path, text))


def test_graph_execution_control_requests_stop_once() -> None:
    control = GraphExecutionControl()
    calls: list[str] = []

    def _handle_stop() -> None:
        calls.append("stop")

    control.register_stop_handler(_handle_stop)
    control.register_stop_handler(_handle_stop)
    control.request_stop()
    control.request_stop()

    assert control.stop_requested()
    assert calls == ["stop"]


def test_graph_plan_rejects_pre_stopped_execution_control() -> None:
    calls: list[str] = []
    control = GraphExecutionControl()
    control.request_stop()
    plan = GraphPlan(
        label="root",
        graph=Graph.from_wiring(
            wiring=(Step(action=lambda: calls.append("run")).wire(),),
        ),
        execution_control=control,
    )

    with pytest.raises(GraphExecutionStopped):
        plan.run()

    assert calls == []


def test_graph_plan_run_executes_nested_graphs_in_topological_order() -> None:
    calls: list[str] = []
    configure = Step(
        label="configure",
        action=LeafAction(fn=lambda _ctx: calls.append("configure")),
    )
    build = Step(
        label="build",
        action=LeafAction(fn=lambda _ctx: calls.append("build")),
    )
    nested = Graph.from_wiring(
        label="nested",
        wiring=(
            configure.wire(),
            build.wire(after=(configure,)),
        ),
    )
    prepare = Step(
        label="prepare",
        action=LeafAction(fn=lambda _ctx: calls.append("prepare")),
    )
    stage = Step(label="stage", action=nested)
    root = Graph.from_wiring(
        label="root",
        wiring=(
            prepare.wire(),
            stage.wire(after=(prepare,)),
        ),
    )

    state = GraphPlan(
        label="root",
        graph=root,
        execution_state=GraphExecutionState(results=GraphResultStore()),
    ).run(options=ThreadGraphRunOptions(workers=1))

    assert calls == ["prepare", "configure", "build"]
    assert state.executed_steps == [
        StepId((0,)),
        StepId((1,)),
        StepId((1, 0)),
        StepId((1, 1)),
    ]


def test_graph_execution_state_cannot_be_reused() -> None:
    plan = GraphPlan(label="root", graph=Graph(label="root"))

    plan.run()

    with pytest.raises(RuntimeError, match="graph execution state cannot be reused"):
        plan.run()


def test_step_action_preserves_default_argument_capture() -> None:
    captured: list[str] = []

    def _capture(value: str = "captured") -> None:
        captured.append(value)

    GraphPlan(
        label="root",
        graph=Graph.from_wiring(
            label="root",
            wiring=(
                Step(
                    label="capture",
                    action=_capture,
                ).wire(),
            ),
        ),
    ).run()

    assert captured == ["captured"]


def test_graph_plan_run_skips_marked_steps() -> None:
    calls: list[str] = []
    prepare = Step(
        label="prepare",
        action=LeafAction(fn=lambda _ctx: calls.append("prepare")),
    )
    build = Step(
        label="build",
        action=LeafAction(fn=lambda _ctx: calls.append("build")),
    )
    graph = Graph.from_wiring(
        label="root",
        wiring=(
            prepare.wire(),
            build.wire(after=(prepare,)),
        ),
    )

    state = GraphExecutionState(
        skipped_steps={StepId((1,))},
        results=GraphResultStore(),
    )
    GraphPlan(
        label="root",
        graph=graph,
        execution_state=state,
    ).run(options=ThreadGraphRunOptions(workers=1))

    assert calls == ["prepare"]
    assert state.executed_steps == [StepId((0,))]


def test_graph_plan_run_uses_provided_state() -> None:
    calls: list[str] = []
    graph = Graph.from_wiring(
        label="root",
        wiring=(
            Step(
                label="build",
                action=LeafAction(fn=lambda _ctx: calls.append("build")),
            ).wire(),
        ),
    )

    state = GraphPlan(
        label="root",
        graph=graph,
        execution_state=GraphExecutionState(results=GraphResultStore()),
    ).run()

    assert calls == ["build"]
    assert state.executed_steps == [StepId((0,))]


def test_graph_plan_run_records_sync_step_results() -> None:
    prepare = Step(label="prepare", action=LeafAction(fn=lambda _ctx: "prepared"))
    build = Step(
        label="build",
        action=LeafAction(fn=lambda run_ctx: f"{run_ctx.require_value(prepare)}:built"),
    )
    graph = Graph.from_wiring(
        label="root",
        wiring=(
            prepare.wire(),
            build.wire(after=(prepare,)),
        ),
    )

    state = GraphPlan(
        label="root",
        graph=graph,
        execution_state=GraphExecutionState(results=GraphResultStore()),
    ).run(options=ThreadGraphRunOptions(workers=1))

    assert state.results is not None
    assert state.results.get(StepId((0,))) is not None
    assert state.results.require(StepId((0,))).value == "prepared"
    assert state.results.require(StepId((1,))).value == "prepared:built"


def test_graph_plan_run_records_async_step_results() -> None:
    async def _build(_run_ctx: StepRunContext) -> str:
        return "built"

    graph = Graph.from_wiring(
        label="root",
        wiring=(
            Step(
                label="build",
                action=LeafAction(fn=_build),
            ).wire(),
        ),
    )

    state = GraphPlan(
        label="root",
        graph=graph,
        execution_state=GraphExecutionState(results=GraphResultStore()),
    ).run(options=ThreadGraphRunOptions(workers=1))

    assert state.results is not None
    assert state.results.require(StepId((0,))).value == "built"


def test_graph_plan_run_records_none_result() -> None:
    graph = Graph.from_wiring(
        label="root",
        wiring=(
            Step(
                label="build",
                action=LeafAction(fn=lambda _ctx: None),
            ).wire(),
        ),
    )

    state = GraphPlan(
        label="root",
        graph=graph,
        execution_state=GraphExecutionState(results=GraphResultStore()),
    ).run()

    assert state.results is not None
    assert state.results.has(StepId((0,)))
    assert state.results.require(StepId((0,))).value is None


def test_graph_plan_run_does_not_record_results_for_skipped_steps() -> None:
    graph = Graph.from_wiring(
        label="root",
        wiring=(
            Step(
                label="build",
                action=LeafAction(fn=lambda _ctx: "built"),
            ).wire(),
        ),
    )
    state = GraphExecutionState(
        skipped_steps={StepId((0,))},
        results=GraphResultStore(),
    )

    GraphPlan(
        label="root",
        graph=graph,
        execution_state=state,
    ).run()

    assert state.results is not None
    assert not state.results.has(StepId((0,)))


def test_graph_plan_run_does_not_record_results_for_failed_steps() -> None:
    def _fail(_run_ctx: StepRunContext) -> object:
        raise RuntimeError("boom")

    graph = Graph.from_wiring(
        label="root",
        wiring=(
            Step(
                label="build",
                action=LeafAction(fn=_fail),
            ).wire(),
        ),
    )

    state = GraphExecutionState(results=GraphResultStore())
    try:
        GraphPlan(
            label="root",
            graph=graph,
            execution_state=state,
        ).run()
    except RuntimeError as exc:
        assert str(exc) == "boom"
    else:
        raise AssertionError("expected RuntimeError")

    assert state.results is not None
    assert not state.results.has(StepId((0,)))


def test_parallel_graph_failure_requests_active_stop() -> None:
    execution_control = GraphExecutionControl()
    stop_seen = threading.Event()

    def _fail(_run_ctx: StepRunContext) -> object:
        raise RuntimeError("boom")

    def _wait_for_stop(_run_ctx: StepRunContext) -> object:
        deadline = time.time() + 2.0
        while time.time() < deadline:
            if execution_control.stop_requested():
                stop_seen.set()
                return None
            time.sleep(0.01)
        raise AssertionError("parallel failure did not request global stop")

    graph = Graph.from_wiring(
        label="root",
        wiring=(
            Step(
                label="fail",
                action=LeafAction(fn=_fail),
            ).wire(),
            Step(
                label="wait",
                action=LeafAction(fn=_wait_for_stop),
            ).wire(),
        ),
    )

    with pytest.raises(RuntimeError, match="boom"):
        GraphPlan(
            label="root",
            graph=graph,
            execution_state=GraphExecutionState(results=GraphResultStore()),
            execution_control=execution_control,
        ).run(options=ThreadGraphRunOptions(workers=2))

    assert stop_seen.is_set()


def test_async_graph_failure_waits_for_started_steps() -> None:
    survivor_started = asyncio.Event()
    survivor_completed = False

    async def _fail() -> None:
        await survivor_started.wait()
        raise RuntimeError("boom")

    async def _survive() -> None:
        nonlocal survivor_completed
        survivor_started.set()
        await asyncio.sleep(0.01)
        survivor_completed = True

    graph = Graph.from_wiring(
        label="root",
        wiring=(
            Step(label="fail", action=_fail).wire(),
            Step(label="survive", action=_survive).wire(),
        ),
    )

    plan = GraphPlan(label="root", graph=graph)
    with pytest.raises(RuntimeError, match="boom"):
        plan.run(options=AsyncGraphRunOptions(concurrency=2))

    assert survivor_completed
    assert plan.execution_control.stop_requested()


@pytest.mark.parametrize(
    "options",
    [
        ThreadGraphRunOptions(workers=1),
        AsyncGraphRunOptions(concurrency=1),
    ],
    ids=["thread", "async"],
)
def test_execution_stop_does_not_dispatch_ready_successor(
    options: ThreadGraphRunOptions | AsyncGraphRunOptions,
) -> None:
    calls: list[str] = []
    execution_control = GraphExecutionControl()

    def _request_stop() -> None:
        calls.append("first")
        execution_control.request_stop()

    first = Step(label="first", action=_request_stop)
    second = Step(label="second", action=lambda: calls.append("second"))
    graph = Graph.from_wiring(
        wiring=(
            first.wire(),
            second.wire(after=(first,)),
        ),
    )

    with pytest.raises(GraphExecutionStopped):
        GraphPlan(
            label="root",
            graph=graph,
            execution_control=execution_control,
        ).run(options=options)

    assert calls == ["first"]


def test_shared_execution_control_stops_following_plan() -> None:
    calls: list[str] = []
    execution_control = GraphExecutionControl()

    def _fail() -> None:
        raise RuntimeError("boom")

    first_plan = GraphPlan(
        label="build",
        graph=Graph.from_wiring(wiring=(Step(action=_fail).wire(),)),
        execution_control=execution_control,
    )
    second_plan = GraphPlan(
        label="package",
        graph=Graph.from_wiring(
            wiring=(Step(action=lambda: calls.append("package")).wire(),),
        ),
        execution_control=execution_control,
    )

    with pytest.raises(RuntimeError, match="boom"):
        first_plan.run()
    with pytest.raises(GraphExecutionStopped):
        second_plan.run()

    assert calls == []


def test_graph_plan_reads_nested_result_by_step_ref() -> None:
    def _nested(_run_ctx: StepRunContext) -> str:
        return "nested-value"

    nested_step = Step(label="nested", action=LeafAction(fn=_nested))
    nested = Graph.from_wiring(
        label="nested",
        wiring=(nested_step.wire(),),
    )
    stage = Step(label="stage", action=nested)
    root = Graph.from_wiring(
        label="root",
        wiring=(stage.wire(),),
    )

    plan = GraphPlan(
        label="root",
        graph=root,
        execution_state=GraphExecutionState(results=GraphResultStore()),
    )
    plan.run()

    assert plan.require_result(nested_step).value == "nested-value"


@pytest.mark.parametrize("mode", ["dynamic", "compiled"])
def test_reused_subgraph_mounts_have_distinct_step_instances(
    mode: GraphMode,
) -> None:
    shared_step = Step(
        label="shared",
        action=lambda run_ctx: str(run_ctx.step_id),
    )
    shared_graph = Graph.from_wiring(
        label="shared",
        wiring=(shared_step.wire(),),
    )
    first = Step(label="first", action=shared_graph)
    second = Step(label="second", action=shared_graph)
    root = Graph.from_wiring(
        label="root",
        wiring=(first.wire(), second.wire()),
    )
    plan = GraphPlan(
        label="root",
        graph=root,
        execution_state=GraphExecutionState(results=GraphResultStore()),
    )

    state = plan.run(
        options=ThreadGraphRunOptions(workers=1),
        mode=mode,
    )

    assert state.results is not None
    assert state.results.require(StepId((0, 0))).value == "0.0"
    assert state.results.require(StepId((1, 0))).value == "1.0"
    with pytest.raises(RuntimeError, match="mounted more than once"):
        plan.require_result(shared_step)


def test_step_run_context_reads_parent_graph_step_results_by_ref() -> None:
    def _prepare(_run_ctx: StepRunContext) -> str:
        return "prepared"

    prepare = Step(label="prepare", action=LeafAction(fn=_prepare))

    def _nested(run_ctx: StepRunContext) -> str:
        return (
            "nested:"
            f"{run_ctx.require_value(ParentGraphStepRef(step=prepare, levels_up=1))}"
        )

    nested_step = Step(label="nested", action=LeafAction(fn=_nested))
    nested = Graph.from_wiring(
        label="nested",
        wiring=(nested_step.wire(),),
    )
    stage = Step(label="stage", action=nested)
    root = Graph.from_wiring(
        label="root",
        wiring=(
            prepare.wire(),
            stage.wire(after=(prepare,)),
        ),
    )

    state = GraphPlan(
        label="root",
        graph=root,
        execution_state=GraphExecutionState(results=GraphResultStore()),
    ).run()

    assert state.results is not None
    assert state.results.require(StepId((1, 0))).value == "nested:prepared"


@pytest.mark.parametrize("mode", ["dynamic", "compiled"])
def test_runtime_condition_true_waits_for_source_step(mode: GraphMode) -> None:
    probe = Step(label="probe", action=LeafAction(fn=lambda _ctx: "miss"))
    setup = Step(label="setup", action=LeafAction(fn=lambda _ctx: "setup"))
    configure = Step(
        label="configure",
        action=LeafAction(fn=lambda _ctx: "configured"),
    )
    build = Step(
        label="build",
        action=LeafAction(
            fn=lambda run_ctx: (
                f"{run_ctx.require_value(probe)}:{run_ctx.require_value(configure)}"
            )
        ),
    )
    graph = Graph.from_wiring(
        label="root",
        wiring=(
            probe.wire(),
            setup.wire(),
            configure.wire(after=(setup,)),
            build.wire(
                after=(
                    probe,
                    ConditionalPrerequisite(
                        step=configure,
                        condition=runtime_condition(
                            when=lambda ctx: ctx.require_value(probe) == "miss",
                            reads=(probe,),
                        ),
                    ),
                )
            ),
        ),
    )

    state = GraphPlan(
        label="root",
        graph=graph,
        execution_state=GraphExecutionState(results=GraphResultStore()),
    ).run(options=ThreadGraphRunOptions(workers=1), mode=mode)

    assert state.results is not None
    assert state.results.require(StepId((3,))).value == "miss:configured"


@pytest.mark.parametrize("mode", ["dynamic", "compiled"])
def test_runtime_step_condition_is_evaluated_once_per_run(mode: GraphMode) -> None:
    evaluations = 0

    def _condition(_ctx: object) -> bool:
        nonlocal evaluations
        evaluations += 1
        return True

    probe = Step(label="probe", action=lambda: "ready")
    first = Step(label="first", action=lambda: None)
    second = Step(label="second", action=lambda: None)
    target = Step(
        label="target",
        when=runtime_condition(reads=(probe,), when=_condition),
        action=lambda: None,
    )
    graph = Graph.from_wiring(
        label="root",
        wiring=(
            probe.wire(),
            first.wire(),
            second.wire(),
            target.wire(after=(first, second)),
        ),
    )

    GraphPlan(label="root", graph=graph).run(
        options=ThreadGraphRunOptions(workers=1),
        mode=mode,
    )

    assert evaluations == 1


@pytest.mark.parametrize("mode", ["dynamic", "compiled"])
def test_runtime_edge_condition_is_evaluated_once_per_run(mode: GraphMode) -> None:
    evaluations = 0

    def _condition(_ctx: object) -> bool:
        nonlocal evaluations
        evaluations += 1
        return True

    probe = Step(label="probe", action=lambda: None)
    first = Step(label="first", action=lambda: None)
    second = Step(label="second", action=lambda: None)
    source = Step(label="source", action=lambda: None)
    target = Step(label="target", action=lambda: None)
    graph = Graph.from_wiring(
        label="root",
        wiring=(
            probe.wire(),
            first.wire(),
            second.wire(),
            source.wire(),
            target.wire(
                after=(
                    first,
                    second,
                    ConditionalPrerequisite(
                        step=source,
                        condition=runtime_condition(
                            reads=(probe,),
                            when=_condition,
                        ),
                    ),
                )
            ),
        ),
    )

    GraphPlan(label="root", graph=graph).run(
        options=ThreadGraphRunOptions(workers=1),
        mode=mode,
    )

    assert evaluations == 1


def test_execution_render_does_not_reevaluate_runtime_condition() -> None:
    evaluations = 0

    def _condition(_ctx: object) -> bool:
        nonlocal evaluations
        evaluations += 1
        return True

    graph = Graph.from_wiring(
        label="root",
        wiring=(
            Step(
                label="target",
                when=runtime_condition(when=_condition),
                action=lambda: None,
            ).wire(),
        ),
    )
    plan = GraphPlan(label="root", graph=graph)

    state = plan.run()
    rendered = render_graph_execution(graph, state, root_label="root")

    assert evaluations == 1
    assert "target [when=true] [executed]" in rendered


@pytest.mark.parametrize("mode", ["dynamic", "compiled"])
def test_runtime_condition_false_does_not_require_source_result(
    mode: GraphMode,
) -> None:
    probe = Step(label="probe", action=LeafAction(fn=lambda _ctx: "hit"))
    setup = Step(label="setup", action=LeafAction(fn=lambda _ctx: "setup"))
    configure = Step(
        label="configure",
        action=LeafAction(fn=lambda _ctx: "configured"),
    )
    build = Step(
        label="build",
        action=LeafAction(fn=lambda run_ctx: run_ctx.require_value(probe)),
    )
    graph = Graph.from_wiring(
        label="root",
        wiring=(
            probe.wire(),
            setup.wire(),
            configure.wire(after=(setup,)),
            build.wire(
                after=(
                    probe,
                    ConditionalPrerequisite(
                        step=configure,
                        condition=runtime_condition(
                            when=lambda ctx: ctx.require_value(probe) == "miss",
                            reads=(probe,),
                        ),
                    ),
                )
            ),
        ),
    )

    state = GraphPlan(
        label="root",
        graph=graph,
        execution_state=GraphExecutionState(results=GraphResultStore()),
    ).run(options=ThreadGraphRunOptions(workers=1), mode=mode)

    assert state.results is not None
    assert state.results.require(StepId((3,))).value == "hit"


@pytest.mark.parametrize("mode", ["dynamic", "compiled"])
def test_runtime_condition_false_does_not_wait_for_source(mode: GraphMode) -> None:
    build_started = threading.Event()

    def _configure() -> str:
        if not build_started.wait(timeout=1):
            raise AssertionError("build incorrectly waited for configure")
        return "configured"

    def _build() -> str:
        build_started.set()
        return "built"

    probe = Step(label="probe", action=lambda: "hit")
    configure = Step(label="configure", action=_configure)
    build = Step(label="build", action=_build)
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
                            when=lambda ctx: ctx.require_value(probe) == "miss",
                            reads=(probe,),
                        ),
                    ),
                )
            ),
        ),
    )

    GraphPlan(
        label="root",
        graph=graph,
        execution_state=GraphExecutionState(results=GraphResultStore()),
    ).run(
        options=ThreadGraphRunOptions(workers=3),
        mode=mode,
    )

    assert build_started.is_set()


@pytest.mark.parametrize("mode", ["dynamic", "compiled"])
def test_parent_waits_for_all_steps_in_conditionally_connected_child(
    mode: GraphMode,
) -> None:
    source_finished = threading.Event()

    def _source() -> None:
        time.sleep(0.1)
        source_finished.set()

    def _package() -> None:
        if not source_finished.is_set():
            raise AssertionError("parent completed before all child steps")

    source = Step(label="source", action=_source)
    target = Step(label="target", action=lambda: None)
    child = Graph.from_wiring(
        label="child",
        wiring=(
            source.wire(),
            target.wire(
                after=(
                    ConditionalPrerequisite(
                        step=source,
                        condition=runtime_condition(when=lambda _ctx: False),
                    ),
                )
            ),
        ),
    )
    child_step = Step(label="child", action=child)
    package = Step(label="package", action=_package)
    graph = Graph.from_wiring(
        label="root",
        wiring=(
            child_step.wire(),
            package.wire(after=(child_step,)),
        ),
    )

    GraphPlan(label="root", graph=graph).run(
        options=ThreadGraphRunOptions(workers=4),
        mode=mode,
    )


@pytest.mark.parametrize("mode", ["dynamic", "compiled"])
def test_parent_prerequisite_blocks_all_conditionally_connected_child_steps(
    mode: GraphMode,
) -> None:
    prepare_finished = threading.Event()

    def _prepare() -> None:
        time.sleep(0.1)
        prepare_finished.set()

    def _target() -> None:
        if not prepare_finished.is_set():
            raise AssertionError("child step bypassed parent prerequisite")

    source = Step(label="source", action=lambda: None)
    target = Step(label="target", action=_target)
    child = Graph.from_wiring(
        label="child",
        wiring=(
            source.wire(),
            target.wire(
                after=(
                    ConditionalPrerequisite(
                        step=source,
                        condition=runtime_condition(when=lambda _ctx: False),
                    ),
                )
            ),
        ),
    )
    prepare = Step(label="prepare", action=_prepare)
    child_step = Step(label="child", action=child)
    graph = Graph.from_wiring(
        label="root",
        wiring=(
            prepare.wire(),
            child_step.wire(after=(prepare,)),
        ),
    )

    GraphPlan(label="root", graph=graph).run(
        options=ThreadGraphRunOptions(workers=4),
        mode=mode,
    )


@pytest.mark.parametrize("mode", ["dynamic", "compiled"])
def test_empty_child_graph_preserves_parent_dependency_boundary(
    mode: GraphMode,
) -> None:
    release_prepare = threading.Event()
    prepare_completed = threading.Event()
    package_started_early = False

    def _prepare() -> None:
        if not release_prepare.wait(timeout=1):
            raise RuntimeError("prepare release timed out")
        prepare_completed.set()

    def _package() -> None:
        nonlocal package_started_early
        package_started_early = not prepare_completed.is_set()

    timer = threading.Timer(0.05, release_prepare.set)
    timer.start()
    try:
        prepare = Step(label="prepare", action=_prepare)
        empty_stage = Step(label="empty_stage", action=Graph(label="empty"))
        package = Step(label="package", action=_package)
        GraphPlan(
            label="root",
            graph=Graph.from_wiring(
                label="root",
                wiring=(
                    prepare.wire(),
                    empty_stage.wire(after=(prepare,)),
                    package.wire(after=(empty_stage,)),
                ),
            ),
        ).run(options=ThreadGraphRunOptions(workers=2), mode=mode)
    finally:
        timer.cancel()

    assert not package_started_early


def test_compiled_skipped_composite_preserves_prerequisite_boundary() -> None:
    prepare_completed = threading.Event()

    def _prepare() -> None:
        time.sleep(0.05)
        prepare_completed.set()

    def _package() -> None:
        if not prepare_completed.is_set():
            raise AssertionError("skipped composite bypassed its prerequisite")

    prepare = Step(label="prepare", action=_prepare)
    stage = Step(label="stage", action=Graph(label="stage"))
    package = Step(label="package", action=_package)
    graph = Graph.from_wiring(
        label="root",
        wiring=(
            prepare.wire(),
            stage.wire(after=(prepare,)),
            package.wire(after=(stage,)),
        ),
    )
    GraphPlan(
        label="root",
        graph=graph,
        execution_state=GraphExecutionState(skipped_steps={StepId((1,))}),
    ).run(
        options=ThreadGraphRunOptions(workers=2),
        mode="compiled",
    )


@pytest.mark.parametrize("mode", ["dynamic", "compiled"])
def test_parent_waits_for_all_steps_in_child_with_disabled_step(
    mode: GraphMode,
) -> None:
    source_finished = threading.Event()

    def _source() -> None:
        time.sleep(0.1)
        source_finished.set()

    def _package() -> None:
        if not source_finished.is_set():
            raise AssertionError("parent completed before disabled-step child")

    source = Step(label="source", action=_source)
    disabled = Step(
        label="disabled",
        when=runtime_condition(when=lambda _ctx: False),
        action=lambda: None,
    )
    target = Step(label="target", action=lambda: None)
    child = Graph.from_wiring(
        label="child",
        wiring=(
            source.wire(),
            disabled.wire(after=(source,)),
            target.wire(after=(disabled,)),
        ),
    )
    child_step = Step(label="child", action=child)
    package = Step(label="package", action=_package)
    graph = Graph.from_wiring(
        label="root",
        wiring=(
            child_step.wire(),
            package.wire(after=(child_step,)),
        ),
    )

    GraphPlan(label="root", graph=graph).run(
        options=ThreadGraphRunOptions(workers=4),
        mode=mode,
    )


@pytest.mark.parametrize("mode", ["dynamic", "compiled"])
@pytest.mark.parametrize(
    "options",
    [
        ThreadGraphRunOptions(workers=1),
        AsyncGraphRunOptions(concurrency=1),
    ],
)
def test_child_graph_observation_is_preserved(
    mode: GraphMode,
    options: ThreadGraphRunOptions | AsyncGraphRunOptions,
) -> None:
    events: list[tuple[str, tuple[str, ...], str]] = []

    class _Observer:
        def emit(self, event: GraphObservationEvent) -> None:
            events.append((event.graph_label, event.graph_label_path, event.phase))

    child = Graph.from_wiring(
        wiring=(Step(label="run", action=lambda: None).wire(),),
        observe=GraphObserve(before="before child", after_success="after child"),
    )
    graph = Graph.from_wiring(
        wiring=(Step(label="child", action=child).wire(),),
        observe=GraphObserve(before="before root", after_success="after root"),
    )

    GraphPlan(label="plan", graph=graph).run(
        options=replace(options, observer=_Observer()),
        mode=mode,
    )

    assert events == [
        ("plan", (), "before"),
        ("child", ("child",), "before"),
        ("child", ("child",), "after_success"),
        ("plan", (), "after_success"),
    ]


def test_serial_executor_runs_compiled_composite() -> None:
    calls: list[str] = []
    graph = Graph.from_wiring(
        label="root",
        wiring=(
            Step(
                label="stage",
                action=Graph.from_wiring(
                    label="stage",
                    wiring=(
                        Step(label="run", action=lambda: calls.append("run")).wire(),
                    ),
                ),
            ).wire(),
        ),
    )
    state = SerialGraphExecutor().execute(
        GraphPlan(label="root", graph=graph).compile()
    )

    assert calls == ["run"]
    assert state.executed_steps == [StepId((0, 0))]
    assert state.completed_composites == {StepId((0,))}


@pytest.mark.parametrize("mode", ["dynamic", "compiled"])
def test_action_returned_graph_preserves_parent_scope(mode: GraphMode) -> None:
    prepare = Step(label="prepare", action=lambda: "prepared")

    def _make_child() -> Graph:
        return Graph.from_wiring(
            label="generated",
            wiring=(
                Step(
                    label="consume",
                    action=lambda ctx: ctx.require_value(
                        ParentGraphStepRef(step=prepare, levels_up=1)
                    ),
                ).wire(),
            ),
        )

    stage = Step(label="stage", action=_make_child)
    graph = Graph.from_wiring(
        label="root",
        wiring=(
            prepare.wire(),
            stage.wire(after=(prepare,)),
        ),
    )

    state = GraphPlan(
        label="root",
        graph=graph,
        execution_state=GraphExecutionState(results=GraphResultStore()),
    ).run(
        options=ThreadGraphRunOptions(workers=1),
        mode=mode,
    )

    assert state.results is not None
    assert state.results.require(StepId((1, 0))).value == "prepared"


@pytest.mark.parametrize("mode", ["dynamic", "compiled"])
def test_live_render_uses_runtime_path_for_action_returned_graph(
    mode: GraphMode,
) -> None:
    stage = Step(
        label="stage",
        action=lambda: Graph.from_wiring(
            label="generated",
            wiring=(Step(label="consume", action=lambda: None).wire(),),
        ),
    )
    graph = Graph.from_wiring(label="root", wiring=(stage.wire(),))
    state = GraphExecutionState(results=GraphResultStore())

    GraphPlan(label="root", graph=graph, execution_state=state).run(
        options=ThreadGraphRunOptions(workers=1),
        mode=mode,
    )

    assert render_live_graph_execution(graph, state, root_label="root") == (
        "graph=root mode=dynamic running=idle last=stage/consume remaining=0"
    )


@pytest.mark.parametrize("mode", ["dynamic", "compiled"])
def test_async_runtime_condition_false_does_not_wait_for_source(
    mode: GraphMode,
) -> None:
    build_started = asyncio.Event()

    async def _configure() -> None:
        await asyncio.wait_for(build_started.wait(), timeout=1)

    async def _build() -> None:
        build_started.set()

    probe = Step(label="probe", action=lambda: "hit")
    configure = Step(label="configure", action=_configure)
    build = Step(label="build", action=_build)
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
                            when=lambda ctx: ctx.require_value(probe) == "miss",
                            reads=(probe,),
                        ),
                    ),
                )
            ),
        ),
    )

    GraphPlan(
        label="root",
        graph=graph,
        execution_state=GraphExecutionState(results=GraphResultStore()),
    ).run(
        options=AsyncGraphRunOptions(concurrency=3),
        mode=mode,
    )

    assert build_started.is_set()


@pytest.mark.parametrize("mode", ["dynamic", "compiled"])
def test_async_executor_runs_independent_sync_steps_concurrently(
    mode: GraphMode,
) -> None:
    release_waiter = threading.Event()

    def _wait_for_peer() -> None:
        if not release_waiter.wait(timeout=1):
            raise RuntimeError("independent sync step did not run concurrently")

    def _release_peer() -> None:
        release_waiter.set()

    graph = Graph.from_wiring(
        label="root",
        wiring=(
            Step(label="wait", action=_wait_for_peer).wire(),
            Step(label="release", action=_release_peer).wire(),
        ),
    )

    GraphPlan(label="root", graph=graph).run(
        options=AsyncGraphRunOptions(concurrency=2),
        mode=mode,
    )

    assert release_waiter.is_set()


@pytest.mark.parametrize("mode", ["dynamic", "compiled"])
@pytest.mark.parametrize(
    "options",
    [
        ThreadGraphRunOptions(workers=1),
        AsyncGraphRunOptions(concurrency=1),
    ],
)
def test_returned_graph_inherits_component_log_path(
    tmp_path: Path,
    mode: GraphMode,
    options: ThreadGraphRunOptions | AsyncGraphRunOptions,
) -> None:
    component_log_path = tmp_path / "component.log"
    observed_paths: list[Path | None] = []
    action_log = _RecordingActionLog()

    def _make_child() -> Graph:
        return Graph.from_wiring(
            label="child",
            wiring=(
                Step(
                    label="write",
                    action=lambda: observed_paths.append(action_log.active_path),
                ).wire(),
            ),
        )

    component = Step(label="component", action=_make_child)
    plan = GraphPlan(
        label="root",
        graph=Graph.from_wiring(
            label="root",
            wiring=(component.wire(),),
        ),
        action_log=action_log,
    )
    plan.bind_component_log_path(component, component_log_path)
    plan.run(options=options, mode=mode)

    assert observed_paths == [component_log_path]


@pytest.mark.parametrize("mode", ["dynamic", "compiled"])
def test_static_component_graph_inherits_bound_log_path(
    tmp_path: Path,
    mode: GraphMode,
) -> None:
    component_log_path = tmp_path / "component.log"
    observed_paths: list[Path | None] = []
    action_log = _RecordingActionLog()
    component = Step(
        label="component",
        action=Graph.from_wiring(
            label="component-graph",
            wiring=(
                Step(
                    label="build",
                    action=lambda: observed_paths.append(action_log.active_path),
                ).wire(),
            ),
        ),
    )
    plan = GraphPlan(
        label="root",
        graph=Graph.from_wiring(
            label="root",
            wiring=(component.wire(),),
        ),
        action_log=action_log,
    )

    plan.bind_component_log_path(component, component_log_path)
    plan.run(mode=mode)

    assert observed_paths == [component_log_path]


@pytest.mark.parametrize("mode", ["dynamic", "compiled"])
def test_run_freezes_static_conditions_before_execution(mode: GraphMode) -> None:
    prepare = Step(label="prepare", action=LeafAction(fn=lambda _ctx: "prepare"))
    android_shim = Step(
        label="android_shim",
        action=LeafAction(fn=lambda _ctx: "shim"),
    )
    archive = Step(
        label="archive",
        action=LeafAction(
            fn=lambda run_ctx: (
                run_ctx.require_value(prepare),
                run_ctx.require_value(android_shim),
            )
        ),
    )
    graph = Graph.from_wiring(
        label="root",
        wiring=(
            prepare.wire(),
            android_shim.wire(),
            archive.wire(
                after=(
                    prepare,
                    ConditionalPrerequisite(
                        step=android_shim,
                        condition=_condition("android"),
                    ),
                )
            ),
        ),
        static_condition_eval=_ConditionEvaluator(active=frozenset({"android"})),
    )

    state = GraphPlan(
        label="root",
        graph=graph,
        execution_state=GraphExecutionState(results=GraphResultStore()),
    ).run(options=ThreadGraphRunOptions(workers=1), mode=mode)

    assert state.results is not None
    assert state.results.require(StepId((2,))).value == ("prepare", "shim")


@pytest.mark.parametrize("mode", ["dynamic", "compiled"])
def test_step_when_false_skips_step_and_allows_downstream_continue(
    mode: GraphMode,
) -> None:
    probe = Step(label="probe", action=LeafAction(fn=lambda _ctx: False))
    optional = Step(
        label="optional",
        when=runtime_condition(
            reads=(probe,),
            when=lambda ctx: bool(ctx.require_value(probe)),
        ),
        action=LeafAction(fn=lambda _ctx: "optional"),
    )
    package = Step(
        label="package",
        action=LeafAction(fn=lambda _ctx: "packaged"),
    )
    graph = Graph.from_wiring(
        label="root",
        wiring=(
            probe.wire(),
            optional.wire(after=(probe,)),
            package.wire(after=(optional,)),
        ),
    )

    state = GraphPlan(
        label="root",
        graph=graph,
        execution_state=GraphExecutionState(results=GraphResultStore()),
    ).run(options=ThreadGraphRunOptions(workers=1), mode=mode)

    assert state.executed_steps == [StepId((0,)), StepId((2,))]
    assert state.results is not None
    assert not state.results.has(StepId((1,)))
    assert state.results.require(StepId((2,))).value == "packaged"


def test_dynamic_step_when_false_skips_graph_step() -> None:
    nested_calls: list[str] = []
    probe = Step(label="probe", action=LeafAction(fn=lambda _ctx: False))
    configure = Step(
        label="configure",
        action=LeafAction(fn=lambda _ctx: nested_calls.append("configure")),
    )
    stage = Step(
        label="stage",
        when=runtime_condition(
            reads=(probe,),
            when=lambda ctx: bool(ctx.require_value(probe)),
        ),
        action=Graph.from_wiring(
            label="stage",
            wiring=(configure.wire(),),
        ),
    )
    package = Step(label="package", action=LeafAction(fn=lambda _ctx: "packaged"))
    graph = Graph.from_wiring(
        label="root",
        wiring=(
            probe.wire(),
            stage.wire(after=(probe,)),
            package.wire(after=(stage,)),
        ),
    )

    state = GraphPlan(
        label="root",
        graph=graph,
        execution_state=GraphExecutionState(results=GraphResultStore()),
    ).run(options=ThreadGraphRunOptions(workers=1))

    assert nested_calls == []
    assert state.executed_steps == [StepId((0,)), StepId((2,))]
    assert state.results is not None
    assert not state.results.has(StepId((1, 0)))
    assert state.results.require(StepId((2,))).value == "packaged"


def test_compiled_runtime_when_false_matches_dynamic_for_graph_step() -> None:
    probe = Step(label="probe", action=LeafAction(fn=lambda _ctx: False))
    configure = Step(label="configure", action=LeafAction(fn=lambda _ctx: "cfg"))
    stage = Step(
        label="stage",
        when=runtime_condition(
            reads=(probe,),
            when=lambda ctx: bool(ctx.require_value(probe)),
        ),
        action=Graph.from_wiring(label="stage", wiring=(configure.wire(),)),
    )
    package = Step(label="package", action=LeafAction(fn=lambda _ctx: "packaged"))
    graph = Graph.from_wiring(
        label="root",
        wiring=(
            probe.wire(),
            stage.wire(after=(probe,)),
            package.wire(after=(stage,)),
        ),
    )

    state = GraphPlan(
        label="root",
        graph=graph,
        execution_state=GraphExecutionState(results=GraphResultStore()),
    ).run(options=ThreadGraphRunOptions(workers=1), mode="compiled")

    assert state.executed_steps == [StepId((0,)), StepId((2,))]
    assert state.results is not None
    assert not state.results.has(StepId((1, 0)))
    assert state.results.require(StepId((2,))).value == "packaged"


def test_compiled_runtime_when_true_activates_composite() -> None:
    probe = Step(label="probe", action=LeafAction(fn=lambda _ctx: "prepared"))
    configure = Step(
        label="configure",
        action=LeafAction(
            fn=lambda ctx: ctx.require_value(
                ParentGraphStepRef(step=probe, levels_up=1)
            )
        ),
    )
    stage = Step(
        label="stage",
        when=runtime_condition(
            reads=(probe,),
            when=lambda ctx: bool(ctx.require_value(probe)),
        ),
        action=Graph.from_wiring(label="stage", wiring=(configure.wire(),)),
    )
    graph = Graph.from_wiring(
        label="root",
        wiring=(
            probe.wire(),
            stage.wire(after=(probe,)),
        ),
    )

    state = GraphPlan(
        label="root",
        graph=graph,
        execution_state=GraphExecutionState(results=GraphResultStore()),
    ).run(options=ThreadGraphRunOptions(workers=1), mode="compiled")

    assert state.executed_steps == [
        StepId((0,)),
        StepId((1, 0)),
    ]
    assert state.entered_composites == {StepId((1,))}
    assert state.completed_composites == {StepId((1,))}
    assert state.results is not None
    assert state.results.require(StepId((1, 0))).value == "prepared"


def test_graph_plan_run_logs_step_skip_by_condition(
    caplog: LogCaptureFixture,
) -> None:
    caplog.set_level(logging.DEBUG, logger="rivetoo_graph.runtime.compiled")
    probe = Step(label="probe", action=LeafAction(fn=lambda _ctx: True))
    build = Step(
        label="build",
        when=runtime_condition(
            reads=(probe,),
            when=lambda ctx: not bool(ctx.require_value(probe)),
        ),
        action=LeafAction(fn=lambda _ctx: "built"),
    )
    graph = Graph.from_wiring(
        label="root",
        wiring=(
            probe.wire(),
            build.wire(after=(probe,)),
        ),
    )

    GraphPlan(
        label="root",
        graph=graph,
        execution_state=GraphExecutionState(results=GraphResultStore()),
    ).run(options=ThreadGraphRunOptions(workers=1))

    assert "Condition: skip build (reads=(0,); value=false)" in caplog.text


def test_graph_plan_run_writes_execution_render_to_run_log_dir(tmp_path: Path) -> None:
    probe = Step(label="probe", action=LeafAction(fn=lambda _ctx: True))
    build = Step(
        label="build",
        when=runtime_condition(
            reads=(probe,),
            when=lambda ctx: not bool(ctx.require_value(probe)),
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

    GraphPlan(
        label="root",
        graph=graph,
        execution_state=GraphExecutionState(results=GraphResultStore()),
        execution_output=TreeFileExecutionOutput(tmp_path),
    ).run(options=ThreadGraphRunOptions(workers=1))

    text = (tmp_path / "graph-execution.root.dynamic.tree").read_text(encoding="utf-8")
    assert "execution: root (mode=dynamic)" in text
    assert "probe [executed] [result=true]" in text
    assert "build [when=false] [skipped] [after: probe]" in text
