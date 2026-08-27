import pytest

from rivetoo_graph import (
    Arc,
    DAGTopology,
    DirectedTopology,
    GraphError,
    TopologyCycleError,
)


def test_directed_topology_allows_cycles_and_resolves_reachability() -> None:
    topology = DirectedTopology(
        nodes=("a", "b", "c", "unreachable"),
        arcs=(
            Arc("a", "b"),
            Arc("b", "c"),
            Arc("c", "a"),
        ),
    )

    assert topology.reachable_from(("a",)) == frozenset({"a", "b", "c"})


def test_dag_topology_rejects_cycle() -> None:
    topology = DirectedTopology(
        nodes=("a", "b"),
        arcs=(Arc("a", "b"), Arc("b", "a")),
    )

    with pytest.raises(TopologyCycleError):
        DAGTopology(topology)


def test_dag_topology_returns_topological_order() -> None:
    topology = DAGTopology(
        DirectedTopology(
            nodes=("prepare", "compile", "install"),
            arcs=(
                Arc("prepare", "compile"),
                Arc("compile", "install"),
            ),
        )
    )

    assert topology.topological_order() == ("prepare", "compile", "install")


def test_directed_topology_rejects_arc_outside_node_set() -> None:
    with pytest.raises(GraphError, match="unknown target: missing"):
        DirectedTopology(nodes=("known",), arcs=(Arc("known", "missing"),))
