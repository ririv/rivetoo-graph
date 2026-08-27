from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import TYPE_CHECKING, Protocol

if TYPE_CHECKING:
    from collections.abc import Mapping

    from .model import Graph, Step, StepId, StepLabelPath
    from .results import GraphResultStore, StepResult


class GraphObservationPhase(StrEnum):
    BEFORE = "before"
    AFTER_SUCCESS = "after_success"


@dataclass(frozen=True, slots=True)
class GraphObservationEvent:
    phase: GraphObservationPhase
    message: str
    source_graph: Graph
    graph_label: str
    graph_label_path: StepLabelPath
    graph_step_ids: Mapping[Step, StepId]
    results: GraphResultStore | None = None

    def result(self, step: Step) -> StepResult | None:
        """按声明 step 引用读取当前观察快照中的结果。"""

        step_id = self.graph_step_ids.get(step)
        if step_id is None:
            raise RuntimeError(f"step '{step.label}' is not declared in observed graph")
        if self.results is None:
            return None
        return self.results.get(step_id)


class GraphObserver(Protocol):
    def emit(self, event: GraphObservationEvent) -> None: ...
