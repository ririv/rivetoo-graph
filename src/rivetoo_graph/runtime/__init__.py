from __future__ import annotations

from importlib import import_module
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .action_log import (
        NO_GRAPH_ACTION_LOG,
        GraphActionLog,
        NoGraphActionLog,
    )
    from .async_executor import AsyncGraphExecutor
    from .control import GraphExecutionControl, GraphExecutionStopped, StopHandler
    from .parallel_executor import ParallelGraphExecutor
    from .run import AsyncGraphRunOptions, GraphRunOptions, ThreadGraphRunOptions
    from .serial_executor import SerialGraphExecutor
    from .state import GraphExecutionSnapshot, GraphExecutionState

__all__ = [
    "GraphExecutionState",
    "GraphExecutionSnapshot",
    "GraphExecutionControl",
    "GraphExecutionStopped",
    "StopHandler",
    "GraphActionLog",
    "NoGraphActionLog",
    "NO_GRAPH_ACTION_LOG",
    "SerialGraphExecutor",
    "ParallelGraphExecutor",
    "AsyncGraphExecutor",
    "GraphRunOptions",
    "ThreadGraphRunOptions",
    "AsyncGraphRunOptions",
]

_EXPORT_MODULES = {
    "GraphActionLog": ".action_log",
    "NoGraphActionLog": ".action_log",
    "NO_GRAPH_ACTION_LOG": ".action_log",
    "AsyncGraphExecutor": ".async_executor",
    "GraphExecutionControl": ".control",
    "GraphExecutionStopped": ".control",
    "StopHandler": ".control",
    "ParallelGraphExecutor": ".parallel_executor",
    "GraphRunOptions": ".run",
    "ThreadGraphRunOptions": ".run",
    "AsyncGraphRunOptions": ".run",
    "SerialGraphExecutor": ".serial_executor",
    "GraphExecutionState": ".state",
    "GraphExecutionSnapshot": ".state",
}


def __getattr__(name: str) -> object:
    """Load only the runtime component requested by the public API."""

    try:
        module_name = _EXPORT_MODULES[name]
    except KeyError:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}") from None
    module = import_module(module_name, __name__)
    value = getattr(module, name)
    globals()[name] = value
    return value


def __dir__() -> list[str]:
    """Expose lazy public symbols to interactive API discovery."""

    return sorted({*globals(), *__all__})
