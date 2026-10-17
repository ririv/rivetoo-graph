from __future__ import annotations

from dataclasses import dataclass, field
from types import MappingProxyType
from typing import TYPE_CHECKING

from .errors import GraphError
from .static_condition import (
    StaticCondition,
    StaticConditionEval,
    evaluate_static_condition,
)

if TYPE_CHECKING:
    from collections.abc import Mapping

    from .model import Graph, GraphWiring, Step


type StaticTarget = Step | tuple[Step, Step]


@dataclass(frozen=True, slots=True)
class BoundStaticCondition:
    """A static expression paired with its evaluator."""

    condition: StaticCondition
    evaluator: StaticConditionEval = field(repr=False)

    def evaluate(self) -> bool:
        return evaluate_static_condition(
            self.condition, static_condition_eval=self.evaluator
        )


@dataclass(frozen=True, slots=True, kw_only=True)
class StaticConditions:
    """Static graph selections interpreted by one explicit evaluator."""

    evaluator: StaticConditionEval = field(repr=False)
    conditions: Mapping[StaticTarget, StaticCondition] = field(default_factory=dict)

    def __post_init__(self) -> None:
        object.__setattr__(self, "conditions", MappingProxyType(dict(self.conditions)))

    def _append(
        self, target: StaticTarget, condition: StaticCondition
    ) -> StaticConditions:
        if target in self.conditions:
            raise GraphError("static condition is already declared for this target")
        return StaticConditions(
            evaluator=self.evaluator,
            conditions={**self.conditions, target: condition},
        )

    def when(self, condition: StaticCondition, component: Step | Graph) -> GraphWiring:
        from .model import Graph, GraphWiring

        mounted = component.step() if isinstance(component, Graph) else component
        return GraphWiring(steps=(mounted,), static=(self._append(mounted, condition),))

    def edge(
        self, source: Step, target: Step, *, condition: StaticCondition
    ) -> GraphWiring:
        from .model import GraphWiring, UnconditionalEdge

        return GraphWiring(
            steps=(source, target),
            edges=(UnconditionalEdge(source=source, target=target),),
            static=(self._append((source, target), condition),),
        )


type StaticConditionsInput = StaticConditions | tuple[StaticConditions, ...]


def static_groups(value: StaticConditionsInput) -> tuple[StaticConditions, ...]:
    return (value,) if isinstance(value, StaticConditions) else value


def resolve_static_bindings(
    groups: tuple[StaticConditions, ...],
    *,
    steps: tuple[Step, ...],
    edge_pairs: set[tuple[Step, Step]],
) -> Mapping[StaticTarget, BoundStaticCondition]:
    # V 个节点、E 条边、S 条静态规则：索引与校验总时间 O(V + E + S)。
    if not groups:
        return MappingProxyType({})
    known = set(steps)
    bindings: dict[StaticTarget, BoundStaticCondition] = {}
    for group in groups:
        for target, condition in group.conditions.items():
            if isinstance(target, tuple):
                if target not in edge_pairs:
                    raise GraphError(
                        "static condition references an unknown graph edge"
                    )
            elif target not in known:
                raise GraphError(
                    "static condition references a step outside this graph"
                )
            if target in bindings:
                raise GraphError("static condition is already declared for this target")
            bindings[target] = BoundStaticCondition(condition, group.evaluator)
    return MappingProxyType(bindings)
