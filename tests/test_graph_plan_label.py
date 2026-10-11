from pathlib import Path

import pytest

from rivetoo_graph import (
    Graph,
    GraphPlan,
    GraphRenderOptions,
    Step,
    TreeFileExecutionOutput,
    render_graph_plan,
)


@pytest.mark.parametrize(
    ("label", "display", "filename"),
    [("pipeline", "pipeline", "pipeline"), (None, "<unlabeled>", "unlabeled")],
)
def test_graph_name_is_shared_by_compilation_rendering_and_output(
    tmp_path: Path, label: str | None, display: str, filename: str
) -> None:
    graph = Graph(label=label, steps=(Step(lambda: None, label="run"),))
    plan = GraphPlan(graph=graph, execution_output=TreeFileExecutionOutput(tmp_path))

    assert plan.compile().label == display
    assert (
        render_graph_plan(plan, options=GraphRenderOptions()).splitlines()[0] == display
    )
    assert f"compilation: {display}" in render_graph_plan(
        plan, options=GraphRenderOptions(mode="compiled")
    )
    assert display in render_graph_plan(plan, options=GraphRenderOptions(fmt="mermaid"))

    plan.run()

    output = tmp_path / f"graph-execution.{filename}.dynamic.tree"
    assert display in output.read_text()
