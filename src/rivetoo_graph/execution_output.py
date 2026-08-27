from __future__ import annotations

import re
from dataclasses import dataclass
from typing import TYPE_CHECKING, Protocol

from .errors import GraphError
from .render.execution import render_graph_execution

if TYPE_CHECKING:
    from pathlib import Path

    from .model import Graph, GraphMode
    from .runtime import GraphExecutionState


class GraphExecutionOutput(Protocol):
    """消费一次 Graph 执行结果的显式输出端口。"""

    def write(
        self,
        graph: Graph,
        state: GraphExecutionState,
        *,
        root_label: str,
        mode: GraphMode,
    ) -> None: ...


@dataclass(frozen=True, slots=True)
class NoGraphExecutionOutput:
    """独立运行 Graph 时不产生外部文件的显式缺省输出。"""

    def write(
        self,
        graph: Graph,
        state: GraphExecutionState,
        *,
        root_label: str,
        mode: GraphMode,
    ) -> None:
        del graph, state, root_label, mode


NO_GRAPH_EXECUTION_OUTPUT = NoGraphExecutionOutput()


def _sanitize_root_label(root_label: str) -> str:
    sanitized = re.sub(r"[^A-Za-z0-9._-]+", "_", root_label)
    return sanitized.strip("._-") or "graph"


@dataclass(frozen=True, slots=True)
class TreeFileExecutionOutput:
    """把最终 Graph 执行状态写成指定目录下的文本树文件。"""

    output_dir: Path

    def __post_init__(self) -> None:
        if not self.output_dir.is_dir():
            raise GraphError(
                f"graph execution output directory does not exist: {self.output_dir}"
            )

    def write(
        self,
        graph: Graph,
        state: GraphExecutionState,
        *,
        root_label: str,
        mode: GraphMode,
    ) -> None:
        output_path = self.output_dir / (
            f"graph-execution.{_sanitize_root_label(root_label)}.{mode}.tree"
        )
        output_path.write_text(
            render_graph_execution(
                graph,
                state,
                root_label=root_label,
                mode=mode,
            )
            + "\n",
            encoding="utf-8",
        )
