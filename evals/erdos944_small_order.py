"""Exact small-order audit for the k=4,r=1 case of Erdős problem 944.

The prompt-evaluation runs prove that every witness has minimum degree at
least six.  At order nine its complement therefore has maximum degree two,
so every complement component is a path, a cycle, or an isolated vertex.
This script enumerates exactly one representative of every such component
multiset and checks the original coloring quantifiers by exhaustive DSATUR
backtracking.  It is a bounded audit, not a solver for the open problem.
"""
from __future__ import annotations

import json
from collections.abc import Iterator


Component = tuple[str, int]


def component_multisets(n: int) -> Iterator[tuple[Component, ...]]:
    types = sorted(
        [("P", size) for size in range(1, n + 1)]
        + [("C", size) for size in range(3, n + 1)],
        key=lambda item: (item[1], item[0]),
    )

    def visit(remaining: int, start: int, chosen: list[Component]):
        if remaining == 0:
            yield tuple(chosen)
            return
        for index in range(start, len(types)):
            component = types[index]
            if component[1] > remaining:
                break
            chosen.append(component)
            yield from visit(remaining - component[1], index, chosen)
            chosen.pop()

    yield from visit(n, 0, [])


def complement_components(components: tuple[Component, ...]) -> list[int]:
    n = sum(size for _, size in components)
    adjacency = [0] * n
    offset = 0
    for kind, size in components:
        for local in range(size - 1):
            u, v = offset + local, offset + local + 1
            adjacency[u] |= 1 << v
            adjacency[v] |= 1 << u
        if kind == "C":
            u, v = offset + size - 1, offset
            adjacency[u] |= 1 << v
            adjacency[v] |= 1 << u
        offset += size
    return adjacency


def graph_complement(adjacency: list[int]) -> list[int]:
    n = len(adjacency)
    mask = (1 << n) - 1
    return [(mask ^ (1 << vertex) ^ adjacency[vertex]) for vertex in range(n)]


def colorable(adjacency: list[int], colors_available: int, removed: int | None = None) -> bool:
    n = len(adjacency)
    active = [v for v in range(n) if v != removed]
    colors = [-1] * n

    def search(uncolored: set[int]) -> bool:
        if not uncolored:
            return True
        vertex = max(
            uncolored,
            key=lambda v: (
                len({colors[u] for u in active if colors[u] >= 0 and adjacency[v] >> u & 1}),
                sum(1 for u in uncolored if adjacency[v] >> u & 1),
            ),
        )
        forbidden = {
            colors[u]
            for u in active
            if colors[u] >= 0 and adjacency[vertex] >> u & 1
        }
        rest = set(uncolored)
        rest.remove(vertex)
        for color in range(colors_available):
            if color in forbidden:
                continue
            colors[vertex] = color
            if search(rest):
                return True
        colors[vertex] = -1
        return False

    return search(set(active))


def delete_edge(adjacency: list[int], u: int, v: int) -> list[int]:
    result = list(adjacency)
    result[u] &= ~(1 << v)
    result[v] &= ~(1 << u)
    return result


def is_witness(graph: list[int]) -> bool:
    n = len(graph)
    if colorable(graph, 3) or not colorable(graph, 4):
        return False
    if any(not colorable(graph, 3, removed=v) for v in range(n)):
        return False
    for u in range(n):
        for v in range(u + 1, n):
            if graph[u] >> v & 1 and colorable(delete_edge(graph, u, v), 3):
                return False
    return True


def chromatic_number(graph: list[int]) -> int:
    if not graph:
        return 0
    return next(k for k in range(1, len(graph) + 1) if colorable(graph, k))


def deletion_critical_component(kind: str, size: int) -> bool:
    component = complement_components(((kind, size),))
    cover = chromatic_number(graph_complement(component))
    return all(
        chromatic_number(graph_complement([
            sum(
                (1 << (u - (u > vertex)))
                for u in range(size)
                if u != vertex and component[v] >> u & 1
            )
            for v in range(size)
            if v != vertex
        ])) == cover - 1
        for vertex in range(size)
    )


def main() -> None:
    n = 9
    tested = 0
    four_chromatic = 0
    vertex_critical = 0
    witnesses: list[tuple[Component, ...]] = []
    for components in component_multisets(n):
        complement = complement_components(components)
        assert max((row.bit_count() for row in complement), default=0) <= 2
        graph = graph_complement(complement)
        tested += 1
        if colorable(graph, 3) or not colorable(graph, 4):
            continue
        four_chromatic += 1
        if any(not colorable(graph, 3, removed=v) for v in range(n)):
            continue
        vertex_critical += 1
        if is_witness(graph):
            witnesses.append(components)

    critical_components = [
        (kind, size)
        for size in range(1, n + 1)
        for kind in (("P",) if size < 3 else ("P", "C"))
        if deletion_critical_component(kind, size)
    ]
    report = {
        "order": n,
        "scope": "all complements of maximum degree at most two, up to isomorphism",
        "component_multisets_tested": tested,
        "four_chromatic": four_chromatic,
        "four_vertex_critical": vertex_critical,
        "erdos944_witnesses": [list(item) for item in witnesses],
        "deletion_critical_path_cycle_components": critical_components,
    }
    assert tested == 70
    assert critical_components == [("P", 1), ("C", 5), ("C", 7), ("C", 9)]
    assert vertex_critical == 0
    assert not witnesses
    print(json.dumps(report, indent=2))


if __name__ == "__main__":
    main()
