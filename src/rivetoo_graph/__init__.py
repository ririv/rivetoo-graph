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
    from .events import (
        GraphEventDeliveryError,
        StepEventSink,
        StepExecutionEvent,
        StepExecutionPhase,
    )
    from .execution import GraphExecution
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
        ResultEdge,
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
    from .option import Nothing, Option, Some
    from .render import (
        GraphRenderFormat,
        GraphRenderOptions,
        render_execution_structure,
        render_graph,
        render_graph_compilation_mermaid,
        render_graph_execution,
        render_graph_mermaid,
        render_live_graph_execution,
        resolve_graph_render_options,
    )
    from .results import (
        GraphResultStore,
        GraphResultView,
        GraphScope,
        StepControl,
        StepRunContext,
    )
    from .runtime import (
        NO_GRAPH_ACTION_LOG,
        AsyncGraphRunOptions,
        GraphActionLog,
        GraphExecutionControl,
        GraphExecutionState,
        GraphExecutionStopped,
        GraphNodeFailure,
        GraphRunOptions,
        NoGraphActionLog,
        SerialGraphExecutor,
        ThreadGraphRunOptions,
    )
    from .selection import (
        MultipleStaticBranchesSelectedError,
        NoStaticBranchSelectedError,
        StaticBranch,
        StaticSelectionError,
        select_one,
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
    from .static_conditions import StaticConditions
    from .topology import Arc, DAGTopology, DirectedTopology, TopologyCycleError

_EXPORT_GROUPS = (
    (".static_conditions", ("StaticConditions",)),
    ("._native", ("NativeGraph",)),
    (
        ".events",
        (
            "GraphEventDeliveryError",
            "StepEventSink",
            "StepExecutionEvent",
            "StepExecutionPhase",
        ),
    ),
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
            "ResultEdge",
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
    (".execution", ("GraphExecution",)),
    (
        ".render",
        (
            "render_graph",
            "render_graph_execution",
            "render_live_graph_execution",
            "render_execution_structure",
            "resolve_graph_render_options",
            "render_graph_mermaid",
            "render_graph_compilation_mermaid",
            "GraphRenderFormat",
            "GraphRenderOptions",
        ),
    ),
    (".option", ("Some", "Nothing", "Option")),
    (
        ".results",
        (
            "GraphResultStore",
            "GraphResultView",
            "GraphScope",
            "StepControl",
            "StepRunContext",
        ),
    ),
    (
        ".runtime",
        (
            "GraphExecutionState",
            "GraphExecutionControl",
            "GraphExecutionStopped",
            "GraphNodeFailure",
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
        ".selection",
        (
            "StaticBranch",
            "StaticSelectionError",
            "NoStaticBranchSelectedError",
            "MultipleStaticBranchesSelectedError",
            "select_one",
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
    "GraphEventDeliveryError",
    "StepEventSink",
    "StepExecutionEvent",
    "StepExecutionPhase",
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
    "ResultEdge",
    "ConditionalEdge",
    "ConditionalPrerequisite",
    "RuntimeCondition",
    "runtime_condition",
    "StaticCondition",
    "StaticConditions",
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
    "render_execution_structure",
    "resolve_graph_render_options",
    "render_graph_mermaid",
    "render_graph_compilation_mermaid",
    "GraphRenderFormat",
    "GraphRenderOptions",
    "GraphExecution",
    "GraphExecutionOutput",
    "NoGraphExecutionOutput",
    "NO_GRAPH_EXECUTION_OUTPUT",
    "TreeFileExecutionOutput",
    "GraphExecutionState",
    "GraphExecutionControl",
    "GraphExecutionStopped",
    "GraphNodeFailure",
    "SerialGraphExecutor",
    "GraphRunOptions",
    "GraphActionLog",
    "NoGraphActionLog",
    "NO_GRAPH_ACTION_LOG",
    "ThreadGraphRunOptions",
    "AsyncGraphRunOptions",
    "StaticBranch",
    "StaticSelectionError",
    "NoStaticBranchSelectedError",
    "MultipleStaticBranchesSelectedError",
    "select_one",
    "GraphResultStore",
    "GraphResultView",
    "GraphScope",
    "StepControl",
    "Some",
    "Nothing",
    "Option",
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
