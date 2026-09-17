# Erdős #944: exact exclusion of order 9

This is an independent bounded verification artifact for the open `k=4, r=1`
case. It is not novel progress: Skottova--Steiner,
[arXiv:2508.08703](https://arxiv.org/abs/2508.08703), Proposition 5.1 proves
the stronger known bound `|V(G)| >= 11`. The argument below is retained because
it is short, independently executable, and useful for harness regression. It
does not construct a witness or settle the unrestricted problem.

Assume that a finite simple graph `G` has chromatic number four, every vertex
is critical, and no edge is critical. The independently reproduced local
coloring argument gives `δ(G) ≥ 6`: in every proper three-coloring of `G-v`,
each color must appear at least twice in `N_G(v)`, or deleting the edge from
`v` to a uniquely colored neighbor would make that edge critical.

## Proposition

Such a graph has at least ten vertices.

## Proof

The degree bound first disposes of the smaller orders. If `|V(G)| ≤ 6`, then
`δ(G) ≥ 6` is impossible. If `|V(G)| = 7`, then `G = K_7`, contradicting
`χ(G) = 4`. If `|V(G)| = 8`, its complement `H` has maximum degree at most
one, so it is a matching plus isolated vertices. Writing `e` for its number of
edges gives `θ(H) = 8-e`. The equality `θ(H) = χ(G) = 4` would force `e=4`,
so `H` would be a perfect matching. Deleting any vertex then leaves three
edges and one isolate, whose clique-partition number is still four, contrary
to `χ(G-v)=3`.

It remains to give the self-contained order-nine exclusion. Suppose that
`|V(G)| = 9`, and put
`H = complement(G)`. Since `δ(G) ≥ 6`, every vertex of
`H` has degree at most two. Hence every connected component of `H` is an
isolated vertex, a path, or a cycle.

Let `θ(H)` be the minimum number of cliques whose vertex sets partition
`V(H)`. A proper coloring of `G` is exactly a partition of `V(H)` into
cliques, so

`θ(H) = χ(G) = 4`.

Likewise, vertex criticality gives `θ(H-v) = χ(G-v) = 3` for every vertex
`v`. Clique-partition number is additive over connected components of `H`.
Consequently, every component `C` of `H` must satisfy

`θ(C-v) = θ(C) - 1` for every `v ∈ V(C)`.

We classify the path and cycle components with this property.

- For a path `P_m`, `θ(P_m) = ceil(m/2)`. The one-vertex path works. If
  `m ≥ 2`, deleting a suitable endpoint (when `m` is even) or the second
  vertex (when `m` is odd) leaves the clique-partition number unchanged.
  Thus no `P_m` with `m ≥ 2` works.
- The triangle has clique-partition number one, and deleting a vertex leaves
  an edge, also of clique-partition number one, so `C_3` does not work.
- For `m ≥ 4`, `θ(C_m) = ceil(m/2)`, while deleting any vertex leaves
  `P_{m-1}`. Therefore every deletion lowers `θ` exactly when `m` is odd.

Thus every component of `H` is either an isolated vertex or an odd cycle of
length at least five. With nine total vertices, the only possible component
size multisets are:

- nine isolated vertices, contributing `θ = 9`;
- `C_5` plus four isolated vertices, contributing `θ = 3 + 4 = 7`;
- `C_7` plus two isolated vertices, contributing `θ = 4 + 2 = 6`;
- `C_9`, contributing `θ = 5`.

None has `θ(H) = 4`, a contradiction. Therefore `|V(G)| ≥ 10`. ∎

## Independent finite audit

[`erdos944_small_order.py`](erdos944_small_order.py) enumerates one
representative of all 70 component multisets for nine-vertex graphs of maximum
degree at most two, complements each graph, and checks the original chromatic,
vertex-deletion, and edge-deletion quantifiers by exhaustive DSATUR
backtracking. Seven complements are four-chromatic, none is vertex-critical,
and no witness exists. The coloring routine was separately cross-checked
against brute-force assignments for every graph on at most five vertices.
