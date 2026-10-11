from pathlib import Path

import pytest

from rivetoo_graph import (
    AsyncGraphRunOptions,
    Graph,
    GraphExecution,
    GraphExecutionState,
    GraphMode,
    Step,
    StepId,
    StepRunContext,
    ThreadGraphRunOptions,
)


@pytest.mark.parametrize("mode", ["dynamic", "compiled"])
@pytest.mark.parametrize(
    "options", [ThreadGraphRunOptions(workers=2), AsyncGraphRunOptions(concurrency=2)]
)
def test_leaf_uses_constructor_log_path_snapshot(
    tmp_path: Path,
    mode: GraphMode,
    options: ThreadGraphRunOptions | AsyncGraphRunOptions,
) -> None:
    observed: list[Path | None] = []

    def action(ctx: StepRunContext) -> None:
        observed.append(ctx.step_log_path)

    step = Step(action, label="leaf")
    path = tmp_path / "leaf.log"
    bindings = {step: path}
    execution = GraphExecution(graph=Graph(steps=(step,)), step_log_paths=bindings)
    bindings[step] = tmp_path / "changed.log"

    execution.run(options, mode=mode)

    assert observed == [path]


def test_constructor_resolves_all_bindings_in_one_traversal(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    first = Step(lambda: None)
    second = Step(lambda: None)
    traversals: list[Graph] = []
    original = GraphExecution._index_mounted_step_ids

    def index(execution: GraphExecution) -> dict[Step, tuple[StepId, ...]]:
        traversals.append(execution.graph)
        return original(execution)

    monkeypatch.setattr(GraphExecution, "_index_mounted_step_ids", index)
    graph = Graph(steps=(first, second))
    GraphExecution(graph=graph)
    assert traversals == []

    GraphExecution(
        graph=graph,
        step_log_paths={first: tmp_path / "first.log", second: tmp_path / "second.log"},
    )
    assert traversals == [graph]


def test_unmounted_binding_does_not_partially_configure_state(tmp_path: Path) -> None:
    mounted = Step(lambda: None)
    outside = Step(lambda: None)
    state = GraphExecutionState()

    with pytest.raises(RuntimeError, match="is not mounted"):
        GraphExecution(
            graph=Graph(steps=(mounted,)),
            execution_state=state,
            step_log_paths={
                mounted: tmp_path / "first.log",
                outside: tmp_path / "bad.log",
            },
        )

    assert state.get_step_log_path(StepId((0,))) is None


def test_repeated_step_requires_an_unambiguous_binding(tmp_path: Path) -> None:
    shared = Step(lambda: None)
    first = Graph(steps=(shared,)).step()
    second = Graph(steps=(shared,)).step()

    with pytest.raises(RuntimeError, match="mounted more than once"):
        GraphExecution(
            graph=Graph(steps=(first, second)),
            step_log_paths={shared: tmp_path / "ambiguous.log"},
        )
