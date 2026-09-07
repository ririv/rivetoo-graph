from __future__ import annotations

from importlib import import_module
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from ._native import NativeGraph
    from .capability import (
        AnyProviderRequirement,
        CapabilitiesInput,
        Capability,
        CapabilityBinding,
        CapabilityInterface,
        CapabilityRequirement,
        CapabilitySelection,
        ExactProviderRequirement,
        resolve_capability_requirements,
    )
    from .compile import (
        CompiledComposite,
        CompiledCompositeRef,
        CompiledConditionalDependency,
        CompiledNativeStep,
        CompiledRuntimeCondition,
        CompiledStep,
        CompiledStepRef,
        GraphCompilation,
        compile_graph,
    )
    from .errors import GraphError
    from .execution_output import (
        NO_GRAPH_EXECUTION_OUTPUT,
        GraphExecutionOutput,
        NoGraphExecutionOutput,
        TreeFileExecutionOutput,
    )
    from .model import (
        ConditionalEdge,
        ConditionalPrerequisite,
        Graph,
        GraphEdge,
        GraphMode,
        GraphObservationMessages,
        GraphWiring,
        LeafAction,
        LeafActionCallable,
        RuntimeCondition,
        Step,
        StepAction,
        StepActionInput,
        StepId,
        StepLabelPath,
        StepWiring,
        UnconditionalEdge,
        runtime_condition,
        step,
    )
    from .observe import GraphObservationEvent, GraphObserver
    from .plan import GraphPlan
    from .render import (
        GraphRenderFormat,
        GraphRenderOptions,
        render_graph,
        render_graph_compilation_mermaid,
        render_graph_execution,
        render_graph_mermaid,
        render_graph_plan,
        render_live_graph_execution,
        resolve_graph_render_options,
    )
    from .results import (
        GraphResultStore,
        GraphScope,
        ParentGraphStepRef,
        StepControl,
        StepReadContext,
        StepResult,
        StepRunContext,
    )
    from .runtime import (
        NO_GRAPH_ACTION_LOG,
        AsyncGraphRunOptions,
        GraphActionLog,
        GraphExecutionControl,
        GraphExecutionState,
        GraphExecutionStopped,
        GraphRunOptions,
        NoGraphActionLog,
        SerialGraphExecutor,
        ThreadGraphRunOptions,
    )
    from .static_condition import (
        StaticCondition,
        StaticConditionEval,
        StaticConditionEvalFn,
        StaticConditionEvaluator,
        all_,
        any_,
        evaluate_static_condition,
        not_,
    )
    from .topology import Arc, DAGTopology, DirectedTopology, TopologyCycleError

_EXPORT_GROUPS = (
    ("._native", ("NativeGraph",)),
    (
        ".capability",
        (
            "CapabilitiesInput",
            "CapabilityInterface",
            "Capability",
            "CapabilityRequirement",
            "AnyProviderRequirement",
            "ExactProviderRequirement",
            "CapabilitySelection",
            "CapabilityBinding",
            "resolve_capability_requirements",
        ),
    ),
    (
        ".compile",
        (
            "CompiledStep",
            "CompiledNativeStep",
            "CompiledStepRef",
            "CompiledComposite",
            "CompiledCompositeRef",
            "CompiledRuntimeCondition",
            "CompiledConditionalDependency",
            "GraphCompilation",
            "compile_graph",
        ),
    ),
    (".errors", ("GraphError",)),
    (
        ".execution_output",
        (
            "GraphExecutionOutput",
            "NoGraphExecutionOutput",
            "NO_GRAPH_EXECUTION_OUTPUT",
            "TreeFileExecutionOutput",
        ),
    ),
    (
        ".model",
        (
            "Graph",
            "GraphMode",
            "GraphObservationMessages",
            "GraphWiring",
            "GraphEdge",
            "UnconditionalEdge",
            "ConditionalEdge",
            "ConditionalPrerequisite",
            "RuntimeCondition",
            "runtime_condition",
            "Step",
            "StepAction",
            "StepActionInput",
            "step",
            "StepWiring",
            "LeafAction",
            "LeafActionCallable",
            "StepId",
            "StepLabelPath",
        ),
    ),
    (".observe", ("GraphObservationEvent", "GraphObserver")),
    (".plan", ("GraphPlan",)),
    (
        ".render",
        (
            "render_graph",
            "render_graph_execution",
            "render_live_graph_execution",
            "render_graph_plan",
            "resolve_graph_render_options",
            "render_graph_mermaid",
            "render_graph_compilation_mermaid",
            "GraphRenderFormat",
            "GraphRenderOptions",
        ),
    ),
    (
        ".results",
        (
            "GraphResultStore",
            "GraphScope",
            "ParentGraphStepRef",
            "StepReadContext",
            "StepControl",
            "StepResult",
            "StepRunContext",
        ),
    ),
    (
        ".runtime",
        (
            "GraphExecutionState",
            "GraphExecutionControl",
            "GraphExecutionStopped",
            "SerialGraphExecutor",
            "GraphRunOptions",
            "GraphActionLog",
            "NoGraphActionLog",
            "NO_GRAPH_ACTION_LOG",
            "ThreadGraphRunOptions",
            "AsyncGraphRunOptions",
        ),
    ),
    (
        ".static_condition",
        (
            "StaticCondition",
            "StaticConditionEval",
            "StaticConditionEvalFn",
            "StaticConditionEvaluator",
            "all_",
            "any_",
            "evaluate_static_condition",
            "not_",
        ),
    ),
    (
        ".topology",
        ("Arc", "DirectedTopology", "DAGTopology", "TopologyCycleError"),
    ),
)

__all__ = [
    "CapabilitiesInput",
    "CapabilityInterface",
    "Capability",
    "CapabilityRequirement",
    "AnyProviderRequirement",
    "ExactProviderRequirement",
    "CapabilitySelection",
    "CapabilityBinding",
    "resolve_capability_requirements",
    "Graph",
    "GraphError",
    "GraphMode",
    "GraphObservationMessages",
    "GraphWiring",
    "GraphEdge",
    "UnconditionalEdge",
    "ConditionalEdge",
    "ConditionalPrerequisite",
    "RuntimeCondition",
    "runtime_condition",
    "StaticCondition",
    "StaticConditionEval",
    "StaticConditionEvalFn",
    "StaticConditionEvaluator",
    "all_",
    "any_",
    "evaluate_static_condition",
    "not_",
    "Step",
    "StepAction",
    "StepActionInput",
    "step",
    "StepWiring",
    "LeafAction",
    "LeafActionCallable",
    "NativeGraph",
    "StepId",
    "StepLabelPath",
    "CompiledStep",
    "CompiledNativeStep",
    "CompiledStepRef",
    "CompiledComposite",
    "CompiledCompositeRef",
    "CompiledRuntimeCondition",
    "CompiledConditionalDependency",
    "GraphCompilation",
    "compile_graph",
    "render_graph",
    "render_graph_execution",
    "render_live_graph_execution",
    "render_graph_plan",
    "resolve_graph_render_options",
    "render_graph_mermaid",
    "render_graph_compilation_mermaid",
    "GraphRenderFormat",
    "GraphRenderOptions",
    "GraphPlan",
    "GraphExecutionOutput",
    "NoGraphExecutionOutput",
    "NO_GRAPH_EXECUTION_OUTPUT",
    "TreeFileExecutionOutput",
    "GraphExecutionState",
    "GraphExecutionControl",
    "GraphExecutionStopped",
    "SerialGraphExecutor",
    "GraphRunOptions",
    "GraphActionLog",
    "NoGraphActionLog",
    "NO_GRAPH_ACTION_LOG",
    "ThreadGraphRunOptions",
    "AsyncGraphRunOptions",
    "GraphResultStore",
    "GraphScope",
    "ParentGraphStepRef",
    "StepReadContext",
    "StepControl",
    "StepResult",
    "StepRunContext",
    "GraphObservationEvent",
    "GraphObserver",
    "Arc",
    "DirectedTopology",
    "DAGTopology",
    "TopologyCycleError",
]
_EXPORT_MODULES = {
    name: module_name for module_name, names in _EXPORT_GROUPS for name in names
}


def __getattr__(name: str) -> object:
    """Load only the graph module that owns the requested public symbol."""

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
