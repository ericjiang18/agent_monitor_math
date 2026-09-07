# Erdős #413 relaxed-variant translation audit

Status: independently checked **Partial Progress**, not a solution of the
coefficient-one problem.

## Exact distinction

The original problem asks whether infinitely many positive integers `n` satisfy

`m + omega(m) <= n` for every positive integer `m < n`.

The relaxed question asks whether the same holds for some fixed coefficient
`epsilon > 0` in front of `omega(m)`. The current Erdős Problems page labels the
original question open and says Lau's 2026 result answers the relaxed question
positively:

- https://www.erdosproblems.com/413 (last edited 2026-04-17; inspected
  2026-08-23)

## Primary theorem checked

The official arXiv v2 record and PDF for Cheuk Fung Lau, *On the Number of Prime
Factors of Consecutive Integers*, were independently inspected:

- arXiv:2604.15042v2, submitted 2026-04-16 and revised 2026-06-24;
- Theorem 1.3 gives one constant `C > 0` and infinitely many positive integers
  `N` such that

  `omega(N - j) <= Omega(N - j) <= C log(j)`

  for every integer `1 < j < N`;
- Corollary 1.4 records the corresponding fixed-endpoint-loss progress toward
  Erdős #413.

Primary source: https://arxiv.org/abs/2604.15042

## Endpoint-preserving translation

For every `N > 2` supplied by Theorem 1.3, define

`n = N - 1` and `epsilon = 1 / C`.

Given an arbitrary integer `k` with `1 <= k < n`, put `j = k + 1`. Then
`1 < j < N`, and

`epsilon * omega(n - k)`

`= omega(N - (k + 1)) / C`

`<= log(k + 1)`

`<= k`.

The last inequality follows because `x - log(x + 1)` is zero at `x = 0` and has
derivative `x / (x + 1) >= 0` for `x >= 0`. The substitution therefore covers
the formerly omitted endpoint `k = 1`, uses one `epsilon` independent of `N`,
and preserves infinitude because `N -> N - 1` is injective after discarding at
most finitely many small values. Replacing `k` by `n - m` gives

`m + epsilon * omega(m) <= n`

for every positive `m < n`.

This does not prove the original coefficient-one target: the imported estimate
only gives `omega(n-k) <= C log(k+1)`, and an unspecified fixed `C` cannot be
removed while retaining all small `k`.

## ProvingConsole evidence

- Run:
  `openclaude_audit24h_erdos413_audit_openclaude_020_165536eb75`
- Selected harness/model: OpenClaude / `gpt-5.6-sol`; subagents disabled.
- Outcome: exactly one `Partial Progress` line.
- `global.json`, `decomposed.json`, and `verifier-output.json` independently
  pass the locked verifier-output validator.
- `merge-map.json` uses `ensemble-paper-audit-app.merge-map.v1`; `run.json`
  records isolated serial passes and degraded independence.
- Installed skill SHA-256:
  `e7319d6162d46a1fb0a1cb1c306d873010d232a0100ea40e61a521e4cceb2f58`.

The audit establishes the correctness and scope of this relaxed-variant
translation only. It does not establish novelty, peer review, or a solution to
the original open problem.
