from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Callable
from dataclasses import dataclass

from .errors import GraphError


@dataclass(frozen=True, slots=True)
class _AllPayload:
    conditions: tuple[StaticCondition, ...]


@dataclass(frozen=True, slots=True)
class _AnyPayload:
    conditions: tuple[StaticCondition, ...]


@dataclass(frozen=True, slots=True)
class _NotPayload:
    condition: StaticCondition


@dataclass(frozen=True, slots=True)
class StaticCondition:
    """高层图里的静态条件描述。

    graph core 只要求它是显式数据，不内建任何具体业务字段。
    `payload` 的具体结构由上层业务定义；
    `description` 只服务渲染与调试。
    """

    payload: object
    description: str

    def __post_init__(self) -> None:
        if not self.description:
            raise GraphError("StaticCondition description must not be empty")

    def __and__(self, other: StaticCondition) -> StaticCondition:
        return all_(self, other)

    def __or__(self, other: StaticCondition) -> StaticCondition:
        return any_(self, other)

    def __invert__(self) -> StaticCondition:
        return not_(self)

    def __bool__(self) -> bool:
        # Python 对普通对象默认按 True 处理；静态条件必须交给显式 evaluator 求值。
        raise TypeError("Use ~condition, not_(condition), all_(...) or any_(...)")


class StaticConditionEvaluator(ABC):
    """静态条件求值器。

    graph core 不理解 `StaticCondition.payload` 的业务语义；
    freeze 阶段统一委托给显式 evaluator 求值。
    """

    @abstractmethod
    def evaluate(self, condition: StaticCondition) -> bool:
        raise NotImplementedError


type StaticConditionEvalFn = Callable[[StaticCondition], bool]
type StaticConditionEval = StaticConditionEvaluator | StaticConditionEvalFn


def all_(*conditions: StaticCondition) -> StaticCondition:
    if not conditions:
        raise GraphError("all_ requires at least one static condition")
    return StaticCondition(
        payload=_AllPayload(conditions=conditions),
        description="all("
        + ", ".join(condition.description for condition in conditions)
        + ")",
    )


def any_(*conditions: StaticCondition) -> StaticCondition:
    if not conditions:
        raise GraphError("any_ requires at least one static condition")
    return StaticCondition(
        payload=_AnyPayload(conditions=conditions),
        description="any("
        + ", ".join(condition.description for condition in conditions)
        + ")",
    )


def not_(condition: StaticCondition) -> StaticCondition:
    return StaticCondition(
        payload=_NotPayload(condition=condition),
        description=f"not({condition.description})",
    )


def evaluate_static_condition(
    condition: StaticCondition,
    *,
    static_condition_eval: StaticConditionEval,
) -> bool:
    match condition.payload:
        case _AllPayload(conditions=conditions):
            return all(
                evaluate_static_condition(
                    child,
                    static_condition_eval=static_condition_eval,
                )
                for child in conditions
            )
        case _AnyPayload(conditions=conditions):
            return any(
                evaluate_static_condition(
                    child,
                    static_condition_eval=static_condition_eval,
                )
                for child in conditions
            )
        case _NotPayload(condition=child):
            return not evaluate_static_condition(
                child,
                static_condition_eval=static_condition_eval,
            )
    if isinstance(static_condition_eval, StaticConditionEvaluator):
        active = static_condition_eval.evaluate(condition)
    else:
        active = static_condition_eval(condition)
    if not isinstance(active, bool):
        raise GraphError(
            f"static condition eval must return bool: {condition.description}"
        )
    return active
