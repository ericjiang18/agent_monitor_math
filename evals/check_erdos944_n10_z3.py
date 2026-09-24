#!/usr/bin/env python3
"""Independent finite SAT cross-check for the order-10 exclusion in #944.

Let H be the complement of a hypothetical target graph G.  The structural
argument gives max_degree(H) <= 3, H is not partitionable into three cliques,
and H-v is partitionable into three cliques for every v.  This script asks Z3
whether any labelled ten-vertex H satisfies those exact constraints.

UNSAT corroborates only the finite n=10 lemma.  It is not a certificate for
the unrestricted open existence problem and is not a formal proof trace.
"""
from __future__ import annotations

import hashlib
import itertools
import json
from pathlib import Path

from z3 import Bool, If, Implies, Int, Or, Solver, Sum, unsat


ORDER = 10
COLORS = range(3)


def main() -> int:
    solver = Solver()
    edges = {
        (i, j): Bool(f"e_{i}_{j}")
        for i in range(ORDER)
        for j in range(i + 1, ORDER)
    }

    def edge(i: int, j: int):
        return edges[tuple(sorted((i, j)))]

    degree_constraints = 0
    for vertex in range(ORDER):
        solver.add(
            Sum(
                [If(edge(vertex, other), 1, 0) for other in range(ORDER) if other != vertex]
            )
            <= 3
        )
        degree_constraints += 1

    # A K4 would be a connected component because max_degree(H) <= 3.
    # Replacing that component by K3 after deleting one of its vertices leaves
    # the clique-partition number unchanged, contradicting the combination of
    # "H is not three-partitionable" and "every H-v is".  Encode this derived
    # lemma explicitly so the solver need not rediscover it repeatedly.
    no_k4_constraints = 0
    for vertices in itertools.combinations(range(ORDER), 4):
        solver.add(
            Or(
                [~edge(i, j) for i, j in itertools.combinations(vertices, 2)]
            )
        )
        no_k4_constraints += 1

    deletion_partition_constraints = 0
    deletion_domain_constraints = 0
    for deleted in range(ORDER):
        colors = {
            vertex: Int(f"c_{deleted}_{vertex}")
            for vertex in range(ORDER)
            if vertex != deleted
        }
        for color in colors.values():
            solver.add(color >= 0, color <= 2)
            deletion_domain_constraints += 2
        if deleted == ORDER - 1:
            # With no K4, a three-clique partition of these nine vertices has
            # three parts of size three.  Vertex and color permutation symmetry
            # therefore lets us fix one such partition without loss.
            for vertex in range(ORDER - 1):
                solver.add(colors[vertex] == vertex // 3)
        remaining = sorted(colors)
        for offset, left in enumerate(remaining):
            for right in remaining[offset + 1 :]:
                solver.add(Implies(colors[left] == colors[right], edge(left, right)))
                deletion_partition_constraints += 1

    # Forbid every possible partition of H into at most three cliques.  For
    # each color assignment, at least one same-color pair must be a nonedge.
    forbidden_three_partitions = 0
    for assignment in itertools.product(COLORS, repeat=ORDER):
        blockers = [
            ~edge(i, j)
            for i in range(ORDER)
            for j in range(i + 1, ORDER)
            if assignment[i] == assignment[j]
        ]
        solver.add(Or(blockers))
        forbidden_three_partitions += 1

    result = solver.check()
    script_sha256 = hashlib.sha256(Path(__file__).read_bytes()).hexdigest()
    payload = {
        "schema_version": 1,
        "problem": "Erdos 944 order-10 complement exclusion",
        "order": ORDER,
        "edge_variables": len(edges),
        "degree_constraints": degree_constraints,
        "no_k4_constraints": no_k4_constraints,
        "deletion_color_domain_constraints": deletion_domain_constraints,
        "deletion_partition_constraints": deletion_partition_constraints,
        "forbidden_three_partitions": forbidden_three_partitions,
        "result": str(result),
        "expected": "unsat",
        "scope": (
            "labelled H with maximum degree <= 3, no K4 (a derived necessary "
            "condition), every H-v partitionable into three cliques, H not "
            "partitionable into three cliques, and one WLOG fixed H-9 partition"
        ),
        "script_sha256": script_sha256,
        "unrestricted_problem_solved": False,
    }
    print(json.dumps(payload, indent=2, sort_keys=True))
    return 0 if result == unsat else 1


if __name__ == "__main__":
    raise SystemExit(main())
