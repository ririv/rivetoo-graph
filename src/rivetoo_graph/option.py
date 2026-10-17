from __future__ import annotations

from dataclasses import dataclass
from typing import Literal, Never


@dataclass(frozen=True, slots=True)
class Some[T]:
    """A present value, including a successful result of None."""

    value: T

    def is_some(self) -> Literal[True]:
        return True

    def is_none(self) -> Literal[False]:
        return False

    def unwrap(self) -> T:
        return self.value

    def unwrap_or(self, default: object) -> T:
        return self.value

    def __bool__(self) -> Never:
        raise TypeError("use is_some(), is_none() or unwrap() explicitly")


@dataclass(frozen=True, slots=True)
class Nothing:
    """An absent result, distinct from Some(None)."""

    def is_some(self) -> Literal[False]:
        return False

    def is_none(self) -> Literal[True]:
        return True

    def unwrap(self) -> Never:
        raise ValueError("cannot unwrap an absent result")

    def unwrap_or[T](self, default: T) -> T:
        return default

    def __bool__(self) -> Never:
        raise TypeError("use is_some(), is_none() or unwrap() explicitly")


type Option[T] = Some[T] | Nothing
