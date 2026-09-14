from __future__ import annotations

from dataclasses import dataclass

from .errors import GraphError


class StaticSelectionError(GraphError):
    """静态选择没有得到唯一结果。"""


class NoStaticBranchSelectedError(StaticSelectionError):
    """静态选择没有命中任何分支。"""


class MultipleStaticBranchesSelectedError(StaticSelectionError):
    """静态选择同时命中了多个分支。"""


@dataclass(frozen=True, slots=True)
class StaticBranch[ValueT]:
    """Bind an already evaluated static decision to its original value."""

    active: bool
    value: ValueT


def select_one[ValueT](*branches: StaticBranch[ValueT]) -> ValueT:
    """返回唯一 active 分支持有的原始值。"""

    selected = tuple(branch.value for branch in branches if branch.active)
    match selected:
        case (value,):
            return value
        case ():
            raise NoStaticBranchSelectedError(
                "static selection requires exactly one active branch; none are active"
            )
        case _:
            raise MultipleStaticBranchesSelectedError(
                "static selection requires exactly one active branch; "
                f"{len(selected)} are active"
            )


__all__ = [
    "MultipleStaticBranchesSelectedError",
    "NoStaticBranchSelectedError",
    "StaticBranch",
    "StaticSelectionError",
    "select_one",
]
