from __future__ import annotations

import graphlib
from collections.abc import Hashable, Iterable, Mapping
from dataclasses import dataclass, field
from types import MappingProxyType

from .errors import GraphError


@dataclass(frozen=True, slots=True)
class Arc[NodeT]:
    """A directed connection from one node to another."""

    source: NodeT
    target: NodeT


class TopologyCycleError(GraphError):
    """A directed topology cannot be represented as a DAG."""

    cycle: tuple[object, ...]

    def __init__(self, cycle: tuple[object, ...]) -> None:
        self.cycle = cycle
        super().__init__(
            "directed topology contains cycle: "
            + " -> ".join(str(node) for node in cycle)
        )


@dataclass(frozen=True, slots=True, init=False)
class DirectedTopology[NodeT: Hashable]:
    """The nodes and arcs of a directed graph, without domain behavior."""

    nodes: tuple[NodeT, ...]
    arcs: tuple[Arc[NodeT], ...] = ()
    _successors: Mapping[NodeT, tuple[NodeT, ...]] = field(
        init=False,
        repr=False,
        compare=False,
    )
    _predecessors: Mapping[NodeT, tuple[NodeT, ...]] = field(
        init=False,
        repr=False,
        compare=False,
    )

    def __init__(
        self,
        nodes: Iterable[NodeT],
        arcs: Iterable[Arc[NodeT]] = (),
    ) -> None:
        normalized_nodes = tuple(nodes)
        known_nodes = set(normalized_nodes)
        if len(known_nodes) != len(normalized_nodes):
            raise GraphError("directed topology contains duplicate nodes")

        successors: dict[NodeT, list[NodeT]] = {node: [] for node in normalized_nodes}
        predecessors: dict[NodeT, list[NodeT]] = {node: [] for node in normalized_nodes}
        normalized_arcs: list[Arc[NodeT]] = []
        for arc in arcs:
            if arc.source not in known_nodes:
                raise GraphError(
                    f"directed topology arc has unknown source: {arc.source}"
                )
            if arc.target not in known_nodes:
                raise GraphError(
                    f"directed topology arc has unknown target: {arc.target}"
                )
            normalized_arcs.append(arc)
            successors[arc.source].append(arc.target)
            predecessors[arc.target].append(arc.source)

        object.__setattr__(self, "nodes", normalized_nodes)
        object.__setattr__(self, "arcs", tuple(normalized_arcs))
        object.__setattr__(
            self,
            "_successors",
            MappingProxyType(
                {node: tuple(items) for node, items in successors.items()}
            ),
        )
        object.__setattr__(
            self,
            "_predecessors",
            MappingProxyType(
                {node: tuple(items) for node, items in predecessors.items()}
            ),
        )

    def successors_by_node(self) -> Mapping[NodeT, tuple[NodeT, ...]]:
        return self._successors

    def predecessors_by_node(self) -> Mapping[NodeT, tuple[NodeT, ...]]:
        return self._predecessors

    def reachable_from(self, roots: Iterable[NodeT]) -> frozenset[NodeT]:
        known_nodes = set(self.nodes)
        pending = list(roots)
        unknown_roots = set(pending) - known_nodes
        if unknown_roots:
            unknown = ", ".join(str(node) for node in unknown_roots)
            raise GraphError(f"directed topology has unknown roots: {unknown}")

        successors = self.successors_by_node()
        reachable: set[NodeT] = set()
        while pending:
            node = pending.pop()
            if node in reachable:
                continue
            reachable.add(node)
            pending.extend(successors[node])
        return frozenset(reachable)


@dataclass(frozen=True, slots=True)
class DAGTopology[NodeT: Hashable]:
    """A directed topology whose acyclic invariant is validated at construction."""

    directed: DirectedTopology[NodeT]

    def __post_init__(self) -> None:
        sorter = graphlib.TopologicalSorter(self.directed.predecessors_by_node())
        try:
            sorter.prepare()
        except graphlib.CycleError as exc:
            raise TopologyCycleError(tuple(exc.args[1])) from exc

    def topological_order(self) -> tuple[NodeT, ...]:
        return tuple(
            graphlib.TopologicalSorter(
                self.directed.predecessors_by_node()
            ).static_order()
        )
