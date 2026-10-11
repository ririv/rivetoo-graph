from pathlib import Path

import pytest

from rivetoo_graph import (
    Graph,
    GraphExecution,
    GraphRenderOptions,
    Step,
    TreeFileExecutionOutput,
    render_execution_structure,
)


@pytest.mark.parametrize(
    ("label", "display", "filename"),
    [("pipeline", "pipeline", "pipeline"), (None, "<unlabeled>", "unlabeled")],
)
def test_graph_name_is_shared_by_compilation_rendering_and_output(
    tmp_path: Path, label: str | None, display: str, filename: str
) -> None:
    graph = Graph(label=label, steps=(Step(lambda: None, label="run"),))
    plan = GraphExecution(
        graph=graph, execution_output=TreeFileExecutionOutput(tmp_path)
    )

    assert plan.compile().label == display
    assert (
        render_execution_structure(plan, options=GraphRenderOptions()).splitlines()[0]
        == display
    )
    assert f"compilation: {display}" in render_execution_structure(
        plan, options=GraphRenderOptions(mode="compiled")
    )
    assert display in render_execution_structure(
        plan, options=GraphRenderOptions(fmt="mermaid")
    )

    plan.run()

    output = tmp_path / f"graph-execution.{filename}.dynamic.tree"
    assert display in output.read_text()
