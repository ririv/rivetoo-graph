import pytest

from rivetoo_graph import (
    MultipleStaticBranchesSelectedError,
    NoStaticBranchSelectedError,
    StaticBranch,
    select_one,
)


def test_select_one_returns_original_value() -> None:
    selected = object()

    assert (
        select_one(
            StaticBranch(active=False, value=object()),
            StaticBranch(active=True, value=selected),
        )
        is selected
    )


def test_select_one_rejects_no_active_branch() -> None:
    with pytest.raises(NoStaticBranchSelectedError):
        select_one(StaticBranch(active=False, value="inactive"))


def test_select_one_rejects_multiple_active_branches() -> None:
    with pytest.raises(MultipleStaticBranchesSelectedError):
        select_one(
            StaticBranch(active=True, value="first"),
            StaticBranch(active=True, value="second"),
        )
