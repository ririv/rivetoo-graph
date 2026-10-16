from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import TYPE_CHECKING, Protocol

if TYPE_CHECKING:
    from .model import StepId, StepLabelPath


class StepExecutionPhase(StrEnum):
    STARTED = "started"
    COMPLETED = "completed"
    FAILED = "failed"
    BLOCKED = "blocked"
    SKIPPED = "skipped"


@dataclass(frozen=True, slots=True)
class StepExecutionEvent:
    """One committed step transition, independent of presentation messages."""

    step_id: StepId
    label_path: StepLabelPath
    phase: StepExecutionPhase


class StepEventSink(Protocol):
    def emit(self, event: StepExecutionEvent) -> None: ...


class GraphEventDeliveryError(RuntimeError):
    """An event consumer failed after a runtime transition was committed."""
