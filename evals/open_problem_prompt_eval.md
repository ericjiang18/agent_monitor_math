# Open-problem system-prompt evaluation

## Replaced baseline

The previous SOUL asserted that every requested goal was “100% achievable,”
forbade stopping or reporting that a claim was unachievable, included an
unresolved problem placeholder, and asked the model to estimate tokens, time,
and pricing. Controlled tests showed that this caused false filesystem and
telemetry commentary in every Plain artifact and encouraged the wrong behavior
on false or genuinely open claims. The exact pre-change SOUL is preserved in
the evaluation run prompts.

## Candidate A — truth-first research contract

You are a mathematical research agent. Maximize correct, auditable progress; never trade truth for apparent completion.

First restate the exact claim and quantifiers. A request to prove a statement is not evidence that it is true. Determine whether it is false, known, currently open, or ill-posed; when research status matters, check authoritative sources and cite resolvable identifiers. Test small cases, boundary cases, and counterexamples before committing to a proof strategy.

A complete solution must justify every nontrivial inference, verify every lemma's hypotheses, and close every gap. For a false statement, give an explicit counterexample and verify each required property. For an open or unresolved statement, never manufacture a proof: state that no complete proof was obtained, then report only verified partial progress, failed approaches, the precise remaining bottleneck, and the next decisive check.

Formal verification certifies only the encoded theorem. Audit fidelity to the original statement: do not weaken hypotheses or conclusions, introduce inconsistent assumptions, or use `sorry`/`admit`. Before declaring success, perform an adversarial audit of pivotal lemmas and try to falsify the result.

The final artifact must be a self-contained mathematical write-up, not execution commentary. Do not discuss filesystem permissions or guess token, time, or cost telemetry. Use **Solved** only if the exact original claim is completely established; otherwise label the result **Counterexample**, **Known/Open Status**, or **Partial Progress**.

## Candidate B — research-evidence gates (validated winner)

Candidate B retains Candidate A's truth-first contract and adds conditional
hard gates for research tasks: source existence *and entailment* checks, source
maturity labels, a prohibition on unverified novelty claims, a compact claim
ledger, and explicit scope/certificate requirements for computation. Its exact
text and checksum are preserved in `evals/system_prompt_candidate_b.md`. It was
introduced after Candidate A runs produced correct mathematics but twice
described a weaker independently derived bound as “progress” even though a
cited primary source contained the stronger known result. An exact same-prompt
control on Erdős #944 confirmed that Candidate B fixes this failure mode.

## Candidate C — executable-artifact gate (rejected)

Candidate C retains Candidate B verbatim and adds one operational requirement:
before finalizing, execute every documented reproducibility command from its
declared working directory, confirm that every referenced artifact exists, and
compare independent implementations or configurations when feasible. Its exact
text and checksum are preserved in `evals/system_prompt_candidate_c.md`. It was
introduced after Candidate B produced mathematically sound #396 scripts but
documented them with host-invalid root paths. On the exact same #396
DeepAgents/Codex control, Candidate C's internal review repaired an initial
boundary-arithmetic error, but the final artifact again documented
`/check_erdos396.py`, did not execute it, and provided no independently checked
research-status source. It used 145,475 input / 1,829 output tokens, 420.7s, and
$0.381978, versus Candidate B's 60,046 / 865, 274s, and $0.1588. Candidate C is
therefore rejected; Candidate B remains the validated winner.

## Candidate D — portable evidence gate (rejected)

Candidate D keeps Candidate B's source and truth gates, but makes computational
requirements tool-aware: recompute boundary arithmetic, use only
workspace-relative copy-pasteable paths, verify artifact existence, execute and
cross-check when an execution tool exists, and otherwise report execution as an
unresolved obligation without relying on invented output. Its exact text is
preserved in `evals/system_prompt_candidate_d.md`. In the same #396 control it
fixed both engineering defects: commands were workspace-relative, explicitly
unexecuted, and backed by two independent scripts. Both scripts later verified
the claimed witness `(k,n)=(2,2480)`. However, the write-up also asserted the
false infinite family `n=2^(r+1)-2` for odd `r`, based on the impossible step
`p^a | n` but `n = -2 (mod p^a)`. Direct checks give nonzero remainder 6 for
`r=3,5,7`. This catastrophic lemma error rejects Candidate D despite its better
artifact hygiene.

## Candidate E — adversarial portable-evidence gate (rejected)

Candidate E adds a mandatory falsification pass for every new general lemma,
infinite family, congruence pattern, or bound: test the smallest unused
nontrivial cases by an independent method and substitute concrete values into
pivotal congruences/inequalities. Its exact text is preserved in
`evals/system_prompt_candidate_e.md`. The #396 control still asserted the false
lemma that every positive `n` divides `binomial(2n,n)`; the first unused case
`n=3` gives remainder `20 mod 3 = 2`. Candidate E used 144,568 input / 6,601
output tokens, 261.7s, and $0.42743. It is rejected. Candidate B is restored as
the active and validated winner; arithmetic/source/tool checks are enforced by
independent tooling rather than further prompt inflation.

## Candidate F — artifact-first bounded research (rejected)

Candidate F preserves Candidate B and requires the proof artifact, provisional
outcome, and obligation ledger to be created immediately and updated throughout
the run. On the false-claim control it remained correct (72,156 input / 2,004
output tokens, 50.0s, and $0.2772). In the #524 OpenClaude control it created a
structured `proof.md` by turn 16 and eventually found arXiv:2604.19294, while
Candidate B still had no artifact after turn 43. But Candidate F never updated
its 3,102-byte provisional artifact and exited nonzero after 2,567,188 input /
33,008 output tokens, 792.5s, and $13.893844. The saved artifact still left every
decisive obligation open, so Candidate F is rejected despite better early
artifact behavior.

## Candidate G — dated-status gap audit (rejected)

Candidate G adds one targeted source-recency gate to Candidate F: every database
status is a dated snapshot, and a status decision must search the time gap from
the record's last update through the audit date using the exact problem
identifier and distinctive theorem keywords or citation links. Its #524 Codex
run produced an otherwise strong 16,101-byte audit of the Chojecki manuscript
(1,443,728 input / 12,168 output tokens, 351.7s, and $5.40556), but it falsely
claimed that the gap search found no later primary paper and concluded that the
problem remained open. It missed arXiv:2604.19294 even though that paper falls
inside the declared date gap and exactly matches the distinctive lower-envelope
keywords. A prose instruction to search is therefore not an auditable search
gate; Candidate G is rejected.

## Candidate H — reproducible negative-search gate (superseded safely)

Candidate H keeps the truth, artifact, and dated-gap requirements, but prohibits
negative literature conclusions without a preserved search log: indexes, exact
queries, date range, hit counts, and resolvable IDs. Its #524 Codex control used
804,287 input / 11,668 output tokens, 317.9s, and $3.020838. It reproduced the
decisive defects in the January manuscript and, unlike Candidate G, did not make
a false current-open claim: because its search was incomplete it correctly used
**Partial Progress** and left status unresolved. However, the run's library
snapshot predated materialization of the new search tool, and its improvised
queries missed arXiv:2604.19294. Candidate H is safe but recall-incomplete.

## Candidate I — terminology-expansion recall gate (validated winner)

Candidate I preserves H's negative-search safety gate and requires three
separate bounded query families: exact identifier/name; mathematical
object-plus-conclusion; and at least one standard field synonym/equivalent class
name extracted from definitions, classical sources, or citation chains. It also
prohibits over-constraining one query with every synonym. The exact prompt is in
`evals/system_prompt_candidate_i.md`; the active prompt-body SHA-256 is
`3cf568f074ec1567eb297dccffa197d45b29b749c892f86940cf02330d0f18b7`.

On the same #524 Codex task, with the tool confirmed in the run's materialized
library, Candidate I independently generated the terminology-expanded query
`Rademacher Littlewood polynomial lower envelope`. The bounded arXiv search
returned arXiv:2604.19294v1 as its unique hit. The run downloaded and inspected
the complete 29-page preprint, matched Theorems 1.1--1.2 to the exact
full-sequence almost-sure liminf, checked the requested independence, coupling,
probability-error, and all-$n$ transitions, rejected the January manuscript, and
classified the later result as a public preprint rather than peer-reviewed work.
It used 985,014 input / 11,543 output tokens, 265.4s, and $3.694445; the final
16,479-byte artifact has SHA-256
`19de3deb2c45847df6149ee82348332aa9dcdefe24c346be0b367a7af119f45c`.
This is an existing April 2026 preprint solution, not a solution produced by the
evaluation.

## Candidate J — final-artifact consistency gate (validated winner)

Candidate J preserves Candidate I and adds a final referee/cleanup pass: a
disproved or superseded claim may remain only in an explicitly labeled failed-
attempt discussion, never as a retained theorem/lemma/proposition or conclusion;
contradictory labels, stale ledger entries, unfinished markers, and claims that
exceed their evidence must be removed. Its exact text is in
`evals/system_prompt_candidate_j.md`; the deployed prompt-body SHA-256 is
`653e5fc69edaa4343ed457e07e8cf108ad6be469805bb1cb14f36c4f6e1ccc7a`.

The motivating real failure was Candidate I's first #348 final artifact: the
agent caught that its proposed `d>1` congruence obstruction fails for `d=2`,
but nevertheless retained the false version as numbered Lemma 7 before a
corrected Lemma 7'. Candidate J passed the unchanged graph false-claim control
(78,611 input / 1,731 output tokens, 43.2s, $0.279117), then passed a focused
same-obstruction cleanup control (128,511 / 4,111, 94.5s, $0.492308). The latter
gave the exact `B=(2,2,...)`, `t=1` counterexample, retained only true residue-
support and `d>=3` numbered results, used the sole label **Counterexample**, and
left no contradictory theorem or unfinished marker. Candidate J therefore
supersedes I without weakening I's source-recall gates.

## Candidate K — execution-integrity and feedback-closure gates (validated provisional winner)

Candidate K preserves J and adds three narrowly motivated rules: evidence comes
from the request actually executed rather than the intended query string;
partially formalized declarations are reported separately so an admitted main
does not erase verified helpers or become “verified”; and every substantive
Critic/checker/HITL objection must be repaired or remain an explicit downgraded
obligation before promotion. Its exact text is in
`evals/system_prompt_candidate_k.md`; the deployed prompt-body SHA-256 is
`97afbb820b7f3d8fbbe800f810d5e6143f9e399d69938bae2dedd9acd678804d`.

Candidate K passed the unchanged graph control (104,337 input / 2,086 output,
50.2s, $0.394022) and the unchanged stale-lemma cleanup control (139,029 /
4,187, 92.5s, $0.539843). Both artifacts are correct, retain one outcome label,
and contain no false numbered result. The focused #684 integrity control then
preserved the literal Unicode query and noticed that the old arXiv request had
silently become `all:Erd AND all:s AND all:Problem AND all:684`; it refused to
count that zero-hit request, reran an ASCII-safe query, and inspected three
relevant 2026 preprints after the database snapshot. A cross-harness IMProof
control used zero literature queries, ran all seven Author/Critic/final-revision
nodes, and produced the correct smallest connected counterexample. Candidate K
is therefore the provisional winner, with a measured 8--33% token premium on
the two simple Codex controls; prompt compression remains an active evaluation
obligation.

## Candidate L -- compressed execution gates (validated, not promoted)

Candidate L compresses K's three new gates while preserving J verbatim. Its
5,548-byte prompt body is 8.6% shorter than K and has SHA-256
`5eabb8cabec51a54df44da586ade3d944cbe44fff1a7173deb9f57a0221a4a2f`.
It passed the same graph control (172,084 input / 2,665 output, 64.9s,
$0.651420) with the correct K4-plus-pendant counterexample and the same cleanup
control (137,458 / 4,334, 91.2s, $0.536105) with the exact `B=(2,4,8,...)`,
`t=1` counterexample plus a stronger correct iff boundary theorem. Artifact
SHA-256 values are respectively
`4ad0ebcfacd6fee4cf2e4eb8744ecc0a1ab9db5ff68d2ea69d4f7ed70ad1926f`
and `788a95d06924ba3264be26a116ebdbbcd6349d74957331d41ef5c42a7ad73b8d`.

Its 13-agent adapter-integrity control (145,741 / 3,282, 82.8s, $0.557492)
correctly audited the executed arXiv endpoints and exposed a remaining explicit-
version loss; the resulting artifact has SHA-256
`a819fd510aa56618846ce413bcd1054f0ff8eecc54a048a07d46c1a833998be3`.
This is useful feedback, not an open-problem solution. Compression did not yield
a stable efficiency gain: cleanup cost was essentially unchanged, while the
graph run cost 65% more than K and used 65% more input. Candidate L therefore
remains a compact alternative but is not promoted; at that stage Candidate K
was restored as the deployed provisional winner.


## Candidate N -- nonvacuity and crash-safe research (validated predecessor)

Candidate N retains K's truth, execution-integrity, formalization, and feedback
gates; adds a five-call crash-safe artifact cadence; and requires every
sufficient condition, construction template, and claimed bottleneck to have a
nontrivial consistency witness or be removed as vacuous. Its exact text is in
`evals/system_prompt_candidate_n.md`; the prompt-body SHA-256 is
`2fae96f7e47575ce1be93e5980b16af5351b27e00e2e3fea6f06f16ffdd1c454`.

On the identical OpenClaude/Erdos #749 control, Candidate N used 1,570,708
input / 33,435 output tokens, 774.9s, $8.903943, and 70 trace nodes. Candidate
K used 3,376,879 / 51,933, 1,353s, $18.8761, and 113 nodes on the same target.
N's 23,527-byte artifact (SHA-256
`47295991d90cebb3f9f83c71c5841065ee0b8970fbc8c1940ea486b2ff0224bc`)
kept **Partial Progress**, inspected both Bhalla 2026 artifacts, and retained
five valid self-derived results: the limsup/liminf separation, the necessary
square-root window, a finite periodic-packet lemma, the obstruction to
infinite periodic repetition, and an exact mixed-block gluing ledger. Its
powers-of-two witness correctly demonstrated consistency without pretending
to preserve density. N therefore supersedes K on the current evidence.

## Candidate O -- explicit checkpoint accounting (validated, not promoted)

Candidate O changes only N's operational checkpoint paragraph: every read,
download, query, command, and tool call is counted explicitly, the model states
`Checkpoint ledger: k/5`, and a proof write is mandatory at five. Its exact
text is in `evals/system_prompt_candidate_o.md`; the deployed prompt-body
SHA-256 was
`d81368773454c7bfe3dc267b695dfd48e2d2db4647c93614e8ebfa8786bfee12`.

On the same OpenClaude/Erdos #749 control, O used 1,792,478 input / 46,892
output tokens, 1,007.1s, $10.489762, and 123 trace nodes. It obeyed the stricter
checkpoint cadence, repaired its own initially over-broad square-root lemma,
removed a generated ASCII control byte, and correctly kept **Partial
Progress**. Its 26,061-byte final artifact (SHA-256
`e5ff8523944a4cbceeece5b35bc6d7fd3212cbadfacecd330c6e32b4d415b5ae`)
also derived a useful new result from the inspected primary PDF: Bhalla's
specific upper-density packet construction has lower density at most 1/2.
Independent line-by-line checking against Claims 3.3 and 5.1 and the recursive
definitions found the proof valid. This is genuine partial progress, not an
open-problem solution. O is not promoted because it used 76% more trace nodes,
40% more output, 30% more latency, and 18% more cost than N, while expanding to
eleven index queries.

## Candidate P -- bounded source-first synthesis (validated winner)

Candidate P keeps O's explicit accounting but uses an eight-call durable
window, caps ordinary index work after the three required query families,
requires a named exact candidate for follow-up searches, prefers the new
bounded workspace PDF extractor when a renderer is missing, and asks the agent
to batch related reads and prefer native edits. Its exact text is in
`evals/system_prompt_candidate_p.md`; the prompt-body SHA-256 is
`6d88854f5f49effa1a408c1d3bb1e5b91497c4e34f5086500171080e78dad102`
(whole-file SHA-256
`8d906d5ea0b73988b668f175e1fb23dcde3bd3b7d9a8d73e19d435615e007e6c`).
Its first Codex false-claim control passed in 50.7s using 77,549 input / 1,974
output tokens, $0.295532, and eight trace nodes. It batched the five skill
reads, used no literature search, and gave a complete self-derived K4-plus-
isolated-vertex counterexample under the sole outcome **Counterexample**. The
2,757-byte artifact passed the supplied sanity/citation tools and has SHA-256
`3440ae51e1853a20466fcddf732097bf13c3101ff1120232523b91470c61adbe`.
On the identical OpenClaude/Erdos #749 control, P used 916,322 input / 34,707
output tokens, $5.648197, and 100 trace nodes. The model/tool trace completed in
about 713s; the UI recorded 848s because the now-fixed duplicate-provenance pass
added terminal overhead. Relative to N, P cut input by 42%, cost by 37%, and
model/tool latency by 8%, while output rose 4% and explicit trace nodes rose
43%. It obeyed the `k/8` journal cadence, stopped after seven valid index
queries, downloaded the complete ten-page Bhalla PDF, and executed the bounded
workspace PDF extractor. Its initial 22,968-byte artifact correctly kept
**Partial Progress** and independently recovered O's obstruction for Bhalla's
specific separated-packet construction.

A formal/adversarial HITL review then read all ten PDF pages and found a real
endpoint defect missed by the initial run and the earlier O audit: Claim 3.3
uses strict discrete interval length, so the old two `2p`-point intervals were
ineligible. From `f >= 1`, the repaired proof derives `q <= x_j-2`, covers
the relevant range by two `2p_j-1`-point intervals, and legitimately
recovers the constant 72. The review also derived the later-packet cutoff from
the recursion, excluded the empty first-stage boundary, expanded Lemma 2's
carry/endpoints and nonvacuity, and reran both validators. The final 31,441-byte
artifact has SHA-256
`8ace9a9d156ca9a81b24bab5c53ddf05ae9053af9e2649e15eb0cd3a88c093c6`.
Lean remains incomplete on the open `main`, so no green coverage is granted.
P supersedes N on the combined truth, source, adversarial, and efficiency
evidence. It is a materially better prompt, not an open-problem solution.

### Candidate P cross-domain holdout — Erdős #196

An exact cross-harness holdout tested the one-sided permutation problem rather
than #749. This target has a dangerous near-match: the 1977 construction is a
doubly infinite ordering of the positive integers (order type unlike `omega`),
not the requested permutation indexed by the positive integers. Both Candidate
P runs caught that quantifier/order-type distinction, kept **Partial Progress**,
and made no solution claim.

DeepSeek Harness used 138,152 input / 4,631 output tokens, 108.7s, $0.521930,
and eight monitor nodes. Its 13,470-byte artifact (SHA-256
`e24bc4a5a93b410dd0ab92fb6aaf17047f51d69e10784985f48c476a7aec597e`)
inspected the exact 2024 Adenwalla result and the August 2026 Geneson density
preprint, characterized one-sided permutation orders by finite predecessor
sets, proved the compactness gap, and gave an exact transitive-coloring
formulation. Codex CLI used 1,335,160 / 14,373 tokens, 400.1s, $5.066910, 65
monitor event nodes, and 31 tool calls. Its 13,976-byte artifact (SHA-256
`4d62ae6c25e235712e7d3e3c4a03160905831805efa4f7ed8f716c8f967fae36`)
was more conservative: it did not verify the image-only 1977 scan, logged the
source gap, proved the same finite-order compactness limitation, and preserved
a reproducible exhaustive census through `N=8` without extrapolation.

The parallel derived pipelines also stayed honest. DeepSeek produced 16
informal DAG nodes and 11 formal nodes; Codex produced 17 and four. Each Lean
file encoded the faithful open `main` with one visible `sorry`, each sidecar
reported `incomplete`, and coverage remained 0/16 and 0/17 respectively.
DeepSeek additionally kernel-checked a separate occurrence-chain equivalence.
This holdout confirms that P is not overfit to #749 and shows a large harness
efficiency difference, but it did not solve #196.

### Candidate P source-broker holdout — Erdős #413

An exact IMProof Author/Critic control isolated whether the harness received
Lau's complete 2026 primary source. Before the broker fix, all seven configured
agents received only the arXiv abstract. That baseline used 180,747 input /
37,549 output tokens, 840.6s, and $0.211424. Its honest 14,234-byte **Partial
Progress** artifact has SHA-256
`af337e6d08555b94fa539b7dee72e19bc4f691e6dbb30debf33ade234e92d930`.

After the broker preferred official arXiv HTML and preserved both ends of its
bounded capture, the same configuration received 36,882 characters containing
the exact Theorem 1.3, Corollary 1.4, and construction excerpt. It used 244,470
input / 48,173 output tokens, 1,070.3s, and $0.283477. The final 15,613-byte
artifact has SHA-256
`4b1072a161200d6bd8ea9b03734bbdee41109d40234e9558a6f547d93a7bfdca`.
The extra source increased input 35%, cost 34%, and latency 27%, but changed the
mathematical audit materially: the Critic caught that the endpoint
`omega > k` and Lau-tail `Omega > C log k` events are incomparable, forced all
sign-reversed support claims to remain conditional, separated fixed finite CRT
checks from the growing-support regime, and limited the endpoint sieve to one
sufficient construction-specific route. The last Author consumed each repair
and kept the exact conjecture unresolved.

The derived pipeline produced 17 informal and 11 formal nodes. Lean fully
kernel-checked the endpoint-reindexing helper while leaving the exact open
`main` with one executable `sorry`; coverage correctly remained 0/17. This
control validates Candidate P's source-entailment and Critic-closure behavior,
but it did not solve #413.

### Candidate P Meta-Harness holdout — Erdős #944

The pre-fix exact #944 Meta-Harness baseline used 594,720 input / 19,213
output tokens, 481.8s, and $2.238929. Its best evaluator score was 6/10 and its
5,553-byte final **Partial Progress** artifact has SHA-256
`f9dbad6fdb9365f4fe5bb4669c61eacf5be217e5d65acbb686c5d57c469e7310`.
The decisive failure occurred in round two: the solver wrote a complete
13,585-byte audit, but the old 8,000-character evaluator slice ended mid-bullet
and falsely claimed that the artifact and references were missing.

After deploying the full-normal/head-tail evaluator window and immutable
response-only runtime contract, the exact same prompt, model, three rounds, and
subagent setting used 575,007 / 19,419 tokens, 466.9s, and $2.179548. The
evaluator read the complete 13KB round-two artifact, found a real contradiction
in Lemma 7 rather than a truncation artifact, and forced round three to remove
it. The best score rose to 7/10. The final 13,817-byte artifact (SHA-256
`c444283fbfe4b35c66b26d5924a980502d12dd6c3d607b3fab3d089d9d597a45`)
contains no filesystem or permission commentary, preserves the exact
construction/nonexistence obligations, proves the neighborhood-coloring
criterion and `delta(G) >= 6`, and explicitly leaves the source-search and
bounded-census claims qualified. This is stronger evaluator feedback and a
cleaner artifact, not a solution of #944.

### Candidate P DeepAgents holdout — Erdős #389

The pre-fix exact #389 DeepAgents / `gpt-5.6-terra` control used 541,569 input
/ 20,722 output tokens, 567.6s, and $1.561146. Its 4,283-byte **Partial
Progress** artifact (SHA-256
`dd51a7b77db91e47c28b7ac5f4abf922652a01ff5b49b8f99c8349daf05bdaae`)
correctly reduced the adjacent-block divisibility target to valuation and
factorial tests, but repeated malformed `write_file({})` calls dominated its
trace.

After deploying required-field validation and typed nested tool arguments, the
same prompt/problem/model completed in 167.1s using 246,531 / 6,900 tokens and
$0.685328. Both full multiline writes succeeded on their first graph attempts;
the final 6,472-byte artifact has SHA-256
`c15a4206a3fac71001ebe793ba620f3d873ea943777ec8babfe61bfe8b3f54f1`.
It honestly reports **Partial Progress**, proves the exact valuation criterion
and a useful factorwise necessary condition, checks the known small examples,
and treats OEIS data and the bounded literature search as non-decisive. It also
contains one local enumeration slip: from `n+2 | 6` it lists `n=2` in addition
to `1,4`; the subsequent direct rejection of `n=2` does not repair that
incorrect divisor enumeration. The derived pipeline produced 17 informal and
seven formal nodes, left the open `main` with one executable `sorry`, and
correctly reported 0/17 formal coverage. This is a major runtime/cost repair and
useful partial progress, not a solution of #389.

## Candidate Q -- source-first research plus local consistency certificates (validated winner)

Candidate Q is Candidate P plus one narrowly targeted local-consistency gate:
before retaining any finite enumeration, divisor list, case split, boundary
equality, or numerical implication, the agent must independently rederive it,
substitute every retained candidate, test the excluded boundary, and remove or
repair every downstream claim when the check fails. Its exact text is in
`evals/system_prompt_candidate_q.md`; the body SHA-256 after removing the
single trailing newline is
`af9bbd4d7223c34b3951f009c6c78c0529b7780061c4b6a954a362d3d0ae9119`
(the deployed body including that newline is
`57ea1914647ff6758c7f53037e6c86da52ceeeb81d79108735fc0e7cc52ab969`;
whole-file SHA-256
`d26367387a039974624dc12833f17dc7215d08622ed0284cff7e19c03eec789e`).
The live Agent Profile SOUL has the exact deployed-body hash.

The controlled A/B repeated exact Erdős #389 with the same DeepAgents engine,
`gpt-5.6-terra`, typed tools, and source packet. Candidate P used 246,531 input
/ 6,900 output tokens, 167.1s, and $0.685328; its artifact retained the false
divisor enumeration `n in {1,2,4}` from `n+2 | 6`, even though the later direct
check rejected `n=2`. Candidate Q used 275,460 / 5,674 tokens, 354.4s, and
$0.745391: input rose 11.7%, cost 8.8%, latency 112%, and output fell 17.8%.
Its 6,186-byte artifact (SHA-256
`24db56be413e1142e09227411f618971ad54731f45499f9ce92942ecfb180690`)
avoided the faulty general enumeration and independently checked every retained
small case: `n=1, k=1`; `n=2`, failure for `k=1..4` and
`55440 = 77*720` for `k=5`; `n=3`, failure for `k=1..3` and
`5040 = 14*360` for `k=4`. Independent review found its factorial/binomial,
valuation, interval-interpretation, and large-prime necessary lemmas correct.
It remained **Partial Progress**. The derived pipeline produced 16 informal and
five formal nodes, left the exact open Lean `main` incomplete, and correctly
reported 0/16 verified coverage. Q is promoted because the exact A/B closes the
specific local-arithmetic failure without weakening any of P's truth, source,
formal, or bounded-execution gates; the latency penalty remains an explicit
tradeoff.

### Candidate Q Hermes holdout -- Erdős #1210

The exact official target was taken from the linked Google DeepMind formal
conjecture: one real constant must work for every natural `n` and every finite
pairwise-coprime `A subset [1,n)`. The dated official page still labels it
OPEN. A pre-fix Hermes run used 20,854 / 372 tokens, 106.0s, and $0.115430.
Its 6,320-byte artifact (SHA-256
`8ef64aabf1e8b7ee2201c04e8251c705b55eb5a6dedd86bafb0af4abd414f020`)
contained correct reductions, but the optional scoped-network proxy made every
filesystem/tool launch fail at the host loopback namespace and polluted the
response with runtime commentary.

After Bugs 68 and 69, the exact repeat stored the canonical 1,339-character
problem with zero Library blocks and wrote `proof.md` through the isolated
Codex workspace. It used 38,192 / 556 tokens, 187.0s, one API call, and
$0.207640. Its 9,088-byte **Partial Progress** artifact has SHA-256
`c138ee5e03fa56ae2d5e19de8a57c2eb6ca6d82f07122c7c31efb75e9566624b`.
Independent arithmetic and line-by-line review confirmed all five retained
lemmas: `M(n)-P(n)=1` for `n=2,3,4,6`; the injection
`|A| <= pi(n-1)+1`; the finite Abel identity; the summable-window sufficient
condition; and the prime-set test showing that this sufficient route entails a
sharp translated-prime interval bound. The June 2026 arXiv paper and the May
2026 Dyadic--Farey preprint/DOI both exist and advertise only partial,
average-order, or conditional progress; their full proofs were not treated as
independently refereed here. The run identifies the weighted aggregate of
window discrepancies as the exact bottleneck and makes no solution claim.
This holdout therefore supports Q's local-consistency and status gates but does
not solve #1210.

### Candidate Q Meta-Harness adversarial source holdout -- Erdős #933

The exact target asks whether the $2$-$3$-smooth part of $n(n+1)$, divided by
$n\log n$, has infinite limsup. The official page still labels #933 `OPEN`.
The supplied February 2026 Zenodo PDF instead claimed the universal bound
$R(n)\le 3/\log 2$ and a negative solution. Its authenticated eight-page file
has SHA-256
`2ad0de4864acd5801560ed14bd7af9b83c13398de161d878cc3318aab48317e3`.

The initial Meta-Harness session used 41 agents, 920,037 input / 21,231 output
tokens, $3.4298, and 534s, reaching 6/10. A single queued human correction was
consumed by the continuation. It correctly found that the paper's page-3
Case B does not exhaust mixed-cofactor allocations, but two runtime defects
then truncated otherwise complete Markdown artifacts and prevented the
workspace PDF helper from using the service interpreter. After Bugs 73--75,
the final exact continuation used 12 agents, 112,975 / 7,348 tokens, 157.9s,
and $0.445518. It preserved a complete 19,659-byte artifact with SHA-256
`731f886cb50437b1e6f69b66ddcfddbf74e78e8d3f70ab1fa3525ca3bc33d5c1`
and reached 8/10 in its first round.

The final artifact authenticates and page-labels the full PDF, gives the exact
mixed-cofactor counterexample
$n=1,487,503,359=3^{14}\cdot311$ and
$n+1=2^{15}\cdot45,395$, and proves with integer inequalities that it violates
the claimed universal constant. It also catches the false page-5 identity
$v_3(2^x-1)=v_3(x)$ for even $x$ at $x=2$, and the paper's internal
contradiction: its own equality family includes $n=512<10^7$, although page 2
says the finite maximum is attained precisely at $2$ and $8$. The run's reproducible finite-audit script had SHA-256
`e67d3e934b1c9fa75cd86a0a7cf11f10d9bddd028d420bd856baccfcb298a65f`.
Its $10^7$ floating scan reported `[2, 8, 512]`, the mixed example $n=14$,
the exact large counterexample, and the LTE boundary check, but the evaluator
correctly refused to treat floating equality as a completeness certificate.
The subsequent `evals/erdos933_preprint_audit.py` upgrade replaces that one
weakness with exact rational logarithm intervals and a proved tail bound. The
integer test $S\le3n$ resolves all but 52 cases; $8$ and $512$ are exact
equalities and 40-term rational bounds certify the remaining 50 strictly. The
full $10^7$ certificate passes with SHA-256
`8735ecd7ed00ffa5ef5fb3466b3e46966dea9aee5980e5dbee260e5856704b4b`.
It emits the complete 50-case strict list and checks an exact rational margin
greater than one in every such case, in addition to the two nontrivial equality
cases. It remains explicitly finite and says nothing about the unrestricted
limsup. The result is **Partial Progress**: it falsifies the submitted paper,
not Erdős #933.

An exact-certificate Human-in-the-loop continuation then consumed that upgraded
script without truncating the existing audit. It used 13 agents, 149,349 input
/ 8,749 output tokens, 184.4s, and $0.589822. The complete 22,777-byte artifact
has SHA-256
`066f7b7ff105ff78f8367299f84bdd83312266800bc7d44702366f0588818ba4`,
retains the page audit and exactly one **Partial Progress** label, and states
correctly that a finite counterexample refutes the submitted universal theorem
but neither proves nor refutes the asymptotic limsup equality. It remained 8/10
because the evaluator wanted the 50 cases and rational interval certificate
embedded rather than referenced by executable code; the later script version
now emits that compact appendix data.

### Candidate Q OpenHands holdout -- Erdos #944

After the native OpenHands iteration-cap fix, a fresh exact #944 holdout used
the Codex ACP transport and finished normally in 297.3s. Its 17,256-byte
artifact has SHA-256
`5775f6c9d08329f0ae4ad8fc1f3116901e93495e21df29b190995d847de3e260`
and contains exactly one **Known/Open Status** label. The run restated the
exact quantifiers, authenticated arXiv:2508.08703v1 Proposition 5.1, and
rederived `delta(G) >= 6`, `Delta(G) <= n - 5`, and hence `n >= 11`. It also
identified and repaired an empty-set boundary omission in the proposition's
printed proof without changing the theorem. A newer primary candidate,
arXiv:2606.18462v1 (2026-06-16), was inspected rather than inferred from its
title: its exact small-order theorem gives at least 16 vertices for the
6-regular candidate and explicitly leaves the unrestricted problem open. The
artifact retained the exact theorem instead of the source's inconsistent
informal `n >= 15` wording, and made no solution claim. The legacy event
projection reported 1,368 prompt and 95 completion tokens, but omitted cache
and reasoning fields; Bug 77 fixes that telemetry, so those old counts are not
used for efficiency comparison.

### Candidate Q IMProof holdout -- Erdős #684

A fresh `gpt-5.6-terra` IMProof author--critic holdout used seven sequential
agents, 378,260 input / 31,379 output tokens, 607.9s of summed agent latency,
and $0.196681. Its final 5,022-byte `proof.tex` has SHA-256
`820d4911c3f72b26b36bf070daf3ec0961d9dd3d19ec5c05471dd2a9dca6dd0f`
and exactly one **Partial Progress** label. Three critic rounds corrected an
initial overconfident status label, distinguished the original possibly
undefined `f(n)` from an extended `f-hat(n)`, checked `n=1,2,3,4`, and made the
Mahler consequence explicitly conditional because the primary theorem was not
retrieved. The last Author also gave a sound unconditional eventual bound
`3 <= f(n) <= floor(n/2)`: for `h=floor(n/2)`, the primes above `h` occurring
in `binom(n,h)` contribute `theta(n)-theta(n-h)`, while the central binomial
coefficient contributes at least `n log 2-log(n+1)`, leaving exponentially
more than `n^2` in the small-prime part for sufficiently large `n`. This is a
weak bound and not a new solution. The asynchronous pipeline completed 17
informal DAG nodes, generated a four-node Lean DAG, and correctly reported
zero of 17 statements formally verified: the 1,131-byte Lean file (SHA-256
`9b8f1933150932c0146b7d9c7202605416ea7453ca7919930d4387cae343ea0b`)
kernel-checks only `smallPrimePart_at_zero`; its exact main theorem contains one
`sorry` and remains incomplete.

### Candidate Q DeepSeek Harness holdout -- Erdős #388

A fresh `gpt-5.6-sol` DeepSeek Harness holdout audited the separated equal-
products problem. The initial run finished normally with ten projected agents,
238,643 input / 6,480 output tokens, 148.5s of summed model latency, and
$0.909727. Its artifact kept **Partial Progress**, proved the necessary ordering
`k_1 > k_2`, the later-block prime-support restriction, the binomial
reformulation, and the logical gap between fixed-length finiteness and
unrestricted finiteness. It also gave a complete independently rechecked
certificate for `(m_1,k_1,m_2,k_2)=(7,7,62,4)`:
both `8*...*14` and `63*64*65*66` equal `17,297,280`. It tested the excluded
short-length boundary `5*6*7=14*15=210` and rejected an overlapping infinite
family because it violates separation.

One informal HITL continuation used 89,852 / 4,571 tokens, 93.8s, and $0.3449.
It preserved every mathematical claim while verifying the exact metadata and
scope of Tho's DOI `10.4171/EM/556`: online publication on 10 July 2025,
volume 81 issue 3 (2026), and only the positive-integer case in which one block
length is twice the other. It also stopped calling `(5,4)` unresolved, separated
the two historical uses of the 1970 DOI, and kept failed index searches
explicitly inconclusive. The final 13,002-byte artifact has SHA-256
`d45bc0f5dac356ba98b3b96a683f50ee5231594b377a29a337937e99f9145929`
and exactly one **Partial Progress** label. It does not classify all quadruples
or establish unrestricted finiteness.

The post-HITL pipeline produced 18 informal and 15 formal nodes. Its 4,510-byte
Lean file has SHA-256
`5374c949a7b2b5ad2ab429c5f6e6ffb67cec2d6a76fe1c5b3272930bc18fb68b`.
Lean kernel-checks the independent `earlier_block_longer` and exact certified-
solution theorems, while the unrestricted finite-list `main` contains one
visible `sorry` and is also rejected by the fidelity audit as an overclaim.
The repaired conservative coverage bridge therefore reports partial 2/18:
only those two admission-independent helpers receive green status, while the
overall formal result remains unverified. This is useful exact partial progress,
not a solution of #388.

### Candidate Q OpenClaude holdout -- Erdős #886

A fresh `gpt-5.6-sol` OpenClaude holdout audited the exact divisor-window
problem and the pivotal seven-page Erdős--Rosenfeld paper. The initial bounded
40-turn run preserved a crash-safe artifact but correctly failed with
`error_max_turns`; its recorded projection was 91 agents, 1,039,870 input /
55,625 output tokens, $6.8286, and 1,099s. Two human corrections submitted
while it was active were durably queued and consumed together in order by one
automatic continuation. That continuation finished with 57 agents, 453,506 /
32,165 tokens, $3.335847, and 627.4s. The full lineage therefore used
1,493,376 / 87,790 tokens and $10.164447; the initial hard failure was not
misreported as success.

The final 29,634-byte artifact has SHA-256
`3a6644afc3f8d8b593b5057dcfa9e6f7cc0a89ae9b4209a61de4c30adb67a49c`
and exactly one **Partial Progress** label. It proves the exact target for
`epsilon >= 1/4`, the fixed-`C` critical-window bound, and a strict infinite
four-divisor construction, while explicitly leaving `0 < epsilon < 1/4`
open. Its bounded nine-query source audit inspected the official 1997 PDF and
rejected the sole plausible 2026 hit because it concerns Cesaro distributions
of ideal divisors rather than pointwise ordinary-integer bounds.

The run also found two independent errors in the printed Proposition 4.2. For
the fourth displayed difference `D`, exact expansion proves
`D^4 - 16^4 N > 0` for every positive parameter, so the printed constant is
always missed. Separately, exhaustive enumeration of all 240 factor pairs of
`N_5=19,958,400` gives the closer pair `4455*4480`, hence minimum difference
25 rather than 136; a self-contained integer-sum argument proves the printed
minimum formula from `a >= 6`. Neither source defect solves or refutes #886.
An independent standard-library certificate is preserved in
`evals/erdos_886_prop42_certificate.md` with SHA-256
`38bc8dbc84bc9a7480ddb493e668c54514b5d0306f4f463ff104d4338b3ef568`.

The derived pipeline produced 18 informal and five formal nodes. The 2,567-byte
Lean file (SHA-256
`8529821a8b7d516d54a0254e68f9f46298cc62dbd7f7eb47440a881b95a470d3`)
kernel-checks the exact fourth-difference identity, its positivity consequence,
and the `a=5` arithmetic certificate. Its exact unrestricted `main` retains one
visible `sorry`, so overall formal verification remains false. After the
conservative exact-certificate mapping repair, precisely the fourth-difference
and boundary-minimum informal statements are green: partial 2/18, never the
open conclusion.

### Candidate Q Codex CLI holdout -- Erdős #827

A fresh `gpt-5.6-sol` Codex CLI holdout audited the exact distinct-circumradii
problem. It finished normally with 60 monitor nodes, 1,938,858 input / 20,670
output tokens, 518.1s, and $7.348885. The 19,268-byte initial artifact had
SHA-256
`156befb6741a8267bb92d8d8206cb688d3280a2c713f604f63360ed9311e0457`
and exactly one **Partial Progress** label. It independently rederived the
union-size 4/5/6 conflict counts and alteration argument giving
`k <= n_k <= (3k)^5`, retained the exact self-derived values `n_1=1`,
`n_2=2`, and `n_3=3`, and imported only the paper-supported `n_4<=9` and
`n_5<=37`. Eight bounded searches were preserved; every arXiv request timed
out or returned 429 and the Crossref results were inconclusive, so the artifact
correctly left post-May-2026 open status unresolved. It also excluded an
unaudited 16 June 2026 lower-bound comment and a superficially related 2024
triangle-congruence paper rather than inflating either into evidence.

A production human-review continuation requested a self-contained `n_3=3`
proof, an independent `M_5` incidence derivation, and fresh document audits.
It added 14 monitor nodes, 245,941 / 5,523 tokens, 116.2s, and $0.936323.
The cumulative monitor now contains all 60 original nodes plus the 14 new
nodes, a unique suffixed trace ID for every continuation node, monotonically
offset rounds, and one explicit `continue` bridge edge. Totals correctly sum to
2,184,799 / 26,193 tokens, 634.2s, and $8.285208; the earlier monitor was not
overwritten. The revised 22,511-byte artifact has SHA-256
`b607385701f9ce0d9684ca279b7aa651b103b63ec758b53f54b52da0d7ab2df0`.
Independent `proof-sanity-check` and `citation-audit` reruns both pass, with no
TODO/FIXME/sorry/admit markers or citation placeholders. The outcome remains
honestly incomplete: it improves and rechecks the known polynomial upper bound
but does not determine `n_k`.

The initial automatic pipeline found 17 informal and 11 formal nodes. Its
2,751-byte Lean sidecar had SHA-256
`7da914e2eaa6839b8e5add4bd1c476d9c2963d639a5787c9b198e95636c65696`;
the exact unrestricted upper-bound theorem retained one visible `sorry`, so
coverage correctly reported 0/17 and overall false. The post-continuation
pipeline automatically invalidated the stale sidecar and rebuilt 20 informal
and 18 formal nodes from the revised proof. The 4,776-byte, 104-line Lean file
has SHA-256
`8ff9841ec4598816ee6693ca1b286bd1d873d8e03ab0fad402df50c96bb88acc`.
It kernel-checks `nk_three_eq_three` without admissions while the general
fifth-power branch of `main` retains one visible `sorry`; overall formal status
therefore remains incomplete. After the exact indexed-value coverage repair,
only the exact `n_3=3` informal claim is green: partial 1/20, never the admitted
general bound or the final open-problem conclusion.

### Candidate Q UCLA interrupted holdout -- Erdős #524

This holdout is interruption evidence, not a completed run. Before an
authenticated UI `DELETE` arrived during final typesetting, five UCLA advisor
rounds and the assembly stage repeatedly classified the source argument as
true and non-relaxing. They located Letwin--Sawhney's 21 April 2026 preprint,
arXiv:2604.19294, *On the maxima of Littlewood polynomials on [-1,1]*. Its
main theorem gives almost surely
`liminf log(||f_n||_infinity/sqrt(n))/(log log n)^(1/3)
= -(3*pi^2/4)^(1/3)`, the constant asked for on the dated #524 page. The paper
uses coefficients indexed from zero, while the page's binary-expansion model
uses the corresponding Rademacher coefficients from one; changing the constant
term changes the supremum norm by at most one, which is negligible on the
theorem's diverging scale. Thus the paper appears to answer the exact asymptotic
question, subject to ordinary source review.

This is an existing v1 preprint, not a result produced by a harness and not yet
peer-reviewed. The database page was last edited 27 December 2025, before the
preprint, so its `OPEN` status is plausibly stale. The pre-delete stages consumed
at least 2,555,983 input and 3,646,248 output/reasoning tokens with approximately
$733 in raw stage cost, but these are not terminal run totals. Because deletion
occurred before `solution.tex` was published, there is no final artifact hash
and no completed-holdout score. The service terminated the child and left no
orphan process; the retained run metadata is `stopped`. No UCLA follow-up was
started after the operator requested that this harness be paused.

### Candidate Q OpenClaw holdout -- Erdős #552

A fresh `gpt-5.6-sol` OpenClaw holdout audited the exact additive question for
`R(C_4,S_n)`. The initial agent emitted a terminal response and a substantive
13.5KB **Partial Progress** artifact, but the OpenClaw process remained alive
after that terminal event. It was manually terminated only after its queued
human correction had been preserved, so the initial session is honestly
recorded as failed rather than silently promoted. It contributed 25 monitor
nodes, 50,407 input / 11,139 output tokens, 1,076.9s, and $0.986589.

After deploying the fresh-terminal watchdog, the exact preserved reviewer
message started a continuation in the same transcript. The continuation did
not stop on the stale terminal event from the initial session. It inspected the
primary 2016 Zhang--Broersma--Chen paper, excluded its new variable-cycle
theorem from the fixed-`C_4` asymptotic target, reconciled the earlier HTTP 403
as only a failed retrieval route, and rechecked both prime-power exact-family
substitutions. A fresh assistant `stop` event appeared at 02:18:04 UTC; the
watchdog reaped the otherwise lingering process group after its grace period,
and the job reached `finished` with no orphan and no error at 02:18:17 UTC.
The continuation added 39 nodes, 83,824 / 16,571 tokens, 189.0s, and $1.759514.
The cumulative monitor retained both sessions with one explicit continuation
lineage, 64 nodes, 63 edges, 134,231 / 27,710 tokens, 1,265.9s, and $2.746103.

The final 16,567-byte, 138-line artifact has SHA-256
`c564238403ec41a2b7c52ecb3cce4c86ebdb7e6d4487f113ddc6da133b9e7abe`
and exactly one **Partial Progress** outcome. It self-derives the complement
degree equivalence, the `C_4`-free two-path inequality, the reported-scale
upper bound for nonsquare `n`, a one-weaker all-`n` bound, and the exact
`n=1,2` cases. It correctly leaves the quantified negative-additive question
unproved and current status unresolved after bounded index failures.
Independent proof-sanity and citation audits pass.

The automatic pipeline completed with 20 informal and seven formal nodes. Its
53-line Lean file has SHA-256
`871b09cec4ca0af89a057992eaef0eecfe9357ae4e2349c6ce76e42e1d0ee3d5`.
The exact open target remains the single visible `sorry`; the fidelity audit
therefore reports a major mismatch, overall Lean status `incomplete`, and
formal coverage 0/20. No open conclusion or partial lemma is falsely shown as
kernel-verified.

### Candidate Q DeepAgents source-and-reindexing holdout -- Erdős #413

A fresh `gpt-5.6-sol` DeepAgents holdout audited the coefficient-one barrier
question and its positive-coefficient weakening. The dated database page was
last edited 17 April 2026 and reports both the original question as OPEN and a
positive answer to the weakening. The initial bounded search did not recover
the exact primary paper from overconstrained arXiv queries and therefore kept
those database summaries provisional rather than fabricating support. A queued
source-review message then identified Cheuk Fung Lau's primary
arXiv:2604.15042v2, *On the Number of Prime Factors of Consecutive Integers*,
and the continuation authenticated Theorem 1.3 and Corollary 1.4.

The first continuation nevertheless retained a false endpoint objection. Lau's
Theorem 1.3 gives an absolute `C>0` and infinitely many `n` with
`omega(n-k) <= C log k` for every `2 <= k < n`. For each such `n`, setting
`N=n-1`, `epsilon=1/C`, and `k=n-m` sends every positive `m<N` into exactly
that verified range, while `log k <= k-1=N-m`; hence
`m+epsilon*omega(m) <= N`. Translation by `-1` preserves infinitude. A second
human correction repaired this reindexing and correctly proved the full
weakened question from the primary theorem. It did not promote the original
coefficient-one target: that would require `C log k <= k-1` for every
`k>=2`, whereas the audited result supplies the needed comparison only beyond
an unspecified finite small-offset range. The outcome therefore remains
**Partial Progress** for the exact original problem.

The repaired artifact still contained one obsolete paragraph asserting that
the weakening lacked its final endpoint. A third narrowly scoped reviewer
message removed that semantic contradiction without changing the mathematics
or starting another search. Independent full-text review now finds no stale
endpoint/unverified variant, exactly one References section, and passing
`proof-sanity-check` and `citation-audit` results. The final 18,853-byte,
265-line artifact has SHA-256
`ccee225a5b8df8ce77efc080e97b3512d7857a3986612aca2b4e69101673ecc2`.
This is verified existing partial progress, not a new solution produced by the
harness.

The initial session and three continuations used 277 monitor nodes,
2,545,121 input / 39,319 output tokens, 1,111.8s of summed model latency, and
$6.755989. The final cleanup alone added 19 nodes, 144,747 / 755 tokens,
38.6s, and $0.369417. The automatic pipeline rebuilt from the corrected source
and produced 18 informal and eight formal nodes. Its 1,254-byte Lean file
kernel-checks two independent boundary lemmas, but its exact coefficient-one
`main` retains one visible `sorry`; the fidelity audit explicitly calls that
main claim unproved, overall Lean status remains `incomplete`, and formal
coverage is conservatively 0/18. This holdout supports Candidate Q's
truth/status and source gates, while exposing two prompt limitations for the
next controlled candidate: fixed-offset source theorems need an explicit
translation/reindexing audit, and post-correction cleanup must search for
semantic negations of the corrected claim rather than only literal markers.

## Candidate R -- source-forensic exactness gate (rejected)

Candidate R adds one paragraph to Q: authenticate supplied artifacts by bytes
and checksum, audit material claims in document order before naming the first
gap, distinguish a counterexample to a submitted theorem from an answer to the
original problem, and prohibit floating tolerances from serving as exact finite
extremum certificates. Its exact text is in
`evals/system_prompt_candidate_r.md`; whole-file SHA-256 is
`a6d1385e8326507940b85da545a82e151b3259f1a04f982e822aafa06d47f157`
and deployed-body SHA-256 was
`7b7883f9f28e300fbfd395ba463426f8c1c5f3ae762bf41cba57dc1c6a6d8efc`.

On a fresh exact #933 Meta-Harness repeat with the identical PDF and audit
script packet, round one failed to use the available PDF helper, claimed the
source could not be authenticated, and scored 3/10. Evaluator-driven harness
evolution repaired that in round two and reached 8/10, but the final artifact
still said the one finite counterexample refuted the corollary's asserted
limsup value. A finite exception refutes the universal theorem and its proposed
deduction, not the asymptotic equality itself. The run used 36 agents,
843,812 input / 16,766 output tokens, 403.4s, and $3.171909. Its 16,520-byte
artifact has SHA-256
`d487e74ce7fb2b711c77ec5bcde049c2e32e15475c99559c01674492cc541e1a`.
Because R violated the very distinction it added, required an extra round, and
was substantially more expensive, it is rejected. Candidate Q was restored
with deployed-body SHA-256
`57ea1914647ff6758c7f53037e6c86da52ceeeb81d79108735fc0e7cc52ab969`.

## Candidate S -- translation-boundary and semantic-reconciliation gates (validated reliability winner)

Candidate S returns to Q and adds only the two gates exposed by the completed
DeepAgents #413 review. Before declaring that a source theorem misses a finite
endpoint, it must place source and target variables side by side, enumerate
the omitted offsets, and test injective affine target translations while
preserving quantifiers, domains, constants, and infinitude or uniformity.
After any reviewer/source/status correction, it must list the corrected claim
and its negation and remove literal or paraphrased stale variants throughout
the artifact. Its exact text is in `evals/system_prompt_candidate_s.md`;
whole-file SHA-256 is
`0d157d5163953e3110569e840f5cc398e1ed58c3801475615834df3db9cd2821`
and deployed-body SHA-256 is
`25b692842865f631af9a917c30ff581bda55c25bb9705fe83807cd11123063b3`.

The controlled Q/S A/B used the identical DeepAgents engine,
`gpt-5.6-sol`, 40-iteration cap, disabled subagents, exact #413 statement,
and authenticated Lau v2 source packet. No run was given the target shift.
Q first checkpointed the false conclusion that Theorem 1.3 left the weakened
question's `k=1` endpoint unproved. Its late adversarial pass eventually
self-corrected by setting `N=n-1`; the final 13,455-byte artifact (SHA-256
`fbdebc1bce035f4ba90aa83e49d9e16fed2595426623904f1519abd13d5dcebc`)
is mathematically correct and passes both document audits. Q used 114 nodes,
949,990 input / 16,468 output tokens, 431.5s, and $2.539655.

S created a named translation obligation in its first checkpoint and closed it
before final cleanup: target offset `k>=1` maps to Lau's verified source offset
`j=k+1>=2`, and the elementary bound
`log(k+1)<=k log 2` makes the single fixed choice
`epsilon=(C log 2)^(-1)` valid for infinitely many translated targets. It
simultaneously preserves the exact coefficient-one small-offset gap. Its final
10,167-byte, 207-line artifact has SHA-256
`05d6079cfac16abccdac4aa179add9c86b4134008d337724609bccb94d102cd7`,
exactly one References section, no semantic remnant of the obsolete endpoint
objection, and passing proof-sanity and citation audits. It also quarantines a
stale ar5iv rendering instead of treating it as version-faithful v2 evidence.

S used 110 nodes, 1,267,945 / 17,912 tokens, 520.5s, and $3.348980. Relative
to Q, input rose 33.5%, output 8.8%, latency 20.6%, and cost 31.9%, while node
count fell 3.5%. Both terminal artifacts are correct, so this is not a truth
win over Q; it is an execution-order and stale-state reliability improvement
with a material efficiency penalty. Because this evaluation optimizes maximum
correctness and the operator explicitly deprioritized cost and time, S is the
current reliability winner. Q remains the recommended lower-cost predecessor.

Candidate S also passed a cross-domain false-claim regression: it refuted
“every finite simple 4-chromatic graph is 4-vertex-critical” with `K_4` plus
an isolated vertex, without irrelevant source search or translation work. The
5,072-byte, 103-line informal artifact has SHA-256
`27d40cab3108a2e6a4db940d47e82430fb42dc5b9447077c9c1ac135d3879b24`,
passes independent proof-sanity and citation audits, and used 16 agents,
165,198 input / 3,552 output tokens, 89.6s, and $0.607235. Formal HITL then
repaired the generated Lean witness obligations; the final 174-line
`Proof.lean` has SHA-256
`6bffdd03103c9d0902330c83a90dac63ee207f04928d6c50eb92ce65496d0dc4`,
kernel-checks with no `sorry`/`admit`, and an independent `gpt-5.4` statement-
fidelity audit judged `main` faithful with no issues. The repaired formal-only
pipeline preserved all 13 informal nodes, rebuilt 15 formal nodes, and marked
8/13 statements formally verified; the other five remain conservatively gray
rather than inheriting the verified `main` without a declaration-level match.


### Candidate S IMProof open-problem holdout -- Erdős #195

Candidate S was next tested with IMProof on the one-sided permutation problem
asking for the largest arithmetic-progression length forced in every
permutation of the integers. The first terminal artifact correctly proved the
elementary forced-3 lower bound, but its research broker failed to authenticate
the upper bound and its generated Lean file retained one admission. A native
IMProof HITL source correction then retrieved Adenwalla's arXiv:2211.04451v7
and Geneson's arXiv:2608.12604v1. The sources support, respectively, a
one-sided permutation of all integers avoiding monotone 5-term progressions
and density-one suprema that do not themselves yield a full 4-free permutation.
A second native HITL round repaired a genuine `\fbox{\parbox{...}}` LaTeX
compile failure and removed stale execution claims.

The final post-integrity-audit 11,527-byte, 287-Python-line
`proof.tex` has SHA-256
`c0b5e9129dadd1dddf4c413a246aab988cb3ae3f9a576076d10b9fb717dc4111`.
An independent Tectonic wrapper compiles it with exit 0 to a 58.64 KiB PDF,
and the migrated citation audit resolves all three keys (`Adenwalla`,
`ErdosProblems195`, and `Geneson2026`) to bibliography entries. Its sole
outcome is **Partial Progress**: it proves `P_3`, authenticates `not P_5`,
obtains the sharp known sandwich `3 <= max forced length <= 4`, and explicitly
leaves `P_4` open. No new solution is claimed. Across the initial run and
three native continuations, IMProof used 28 agents, 2,976,697 input / 102,395
output tokens, 2,502.3s summed session latency, and $0.9428053.

Formal HITL produced a 67-line Lean file (SHA-256
`ad5cd4ac5ba42b9ab708ef6ce6ace996ed3dd57d9a88e44ba7b120fa37fdb233`)
whose independent helper `forced_three` kernel-checks without an admission.
The separate headline for `ForcedAP 4` retains one honest `sorry`, and the
fidelity audit keeps the overall result incomplete. Declaration-level coverage
marks exactly two equivalent forced-three informal statements green (2/17),
keeps the open conclusion gray, and leaves `formal_verified=false`. This
holdout confirms Candidate S's truth-first and HITL cleanup behavior, but also
shows that source availability and executable artifact validation still
require runtime controls beyond prompt text.

### Candidate S Hermes open-problem holdout -- Erdős #944

A fresh exact #944 Hermes control exposed two runtime layers before yielding a
usable artifact. The initial response left literal `To be refined`
placeholders, and two pre-fix continuations incorrectly reused that unchanged
2,018-byte file after reporting sandbox failure. The final deployed
read-only-Landlock continuation executed 11 real read/search/check tools,
returned a complete explicit artifact fallback, and the trusted monitor
promoted it only because it differed from the stale draft. That session used
28,516 input / 2,080 output tokens, 157.5s, and $0.204980; cumulative diagnostic
lineage was 142,687 / 5,116 tokens, 548.1s, and $0.866915.

The resulting 7,137-byte, 95-line `proof.md` has SHA-256
`15c0f5c270bfdf14f598042d2cae34114f41ca20e61dae54aedd22c4b3ec8818`.
It passes proof-sanity and citation audits, retains exactly **Partial
Progress**, explicitly leaves the open existence problem unresolved, proves
connectedness, gives the exact edge-deletion coloring characterization, and
self-derives the necessary bound `delta(G) >= 6`. Formal HITL then repaired
one compile failure and produced a 62-line Lean file with SHA-256
`7ed15302004dd40590ab362971a3e6c1d53ce4866c3f314b2ad3861aba662a1d`.
Its edge-deletion three-colorability biconditional is admission-independent and
kernel-checked; the exact open `main` alone retains one `sorry`. Conservative
coverage marks exactly that helper green (1/13), while the minimum-degree claim
and open conclusion remain gray. No solution of #944 is claimed.

## Rubric

- truth/status classification: 0–3
- logical validity and gap closure: 0–5
- fidelity to the exact statement: 0–3
- citation existence and entailment: 0–2
- artifact hygiene: 0–2
- useful verified progress: 0–3
- catastrophic penalty for claiming a false/open statement solved: −10
- record input/output tokens, latency, cost, and harness failures separately

## Evidence snapshot — 2026-08-22

No tested run has produced a new solution of an open problem. Candidate S is
the current maximum-reliability winner: it retains Q's bounded source-first,
truth/status, crash-safe, formal, HITL, nonvacuity, local-consistency, and
cleanup gates, then adds explicit finite-endpoint translation and semantic-
reconciliation certificates. On the exact #413 Q/S A/B, both terminal
artifacts were correct, but S created the decisive target-shift obligation in
its first checkpoint and closed it before cleanup; Q first wrote the opposite
entailment conclusion and repaired it only in its late adversarial pass. S's
cost was 31.9% higher, so Q remains the lower-cost validated predecessor.
Candidate O first exposed the #749 construction obstruction, P remains the
strongest still-lower-latency predecessor, and B remains the leaner historical
baseline. Candidates F, G, and R are rejected; Candidates H, I, J, K, N, O,
P, and Q are safely superseded only for maximum-reliability use.

### Controlled false-claim test

Exact control: “Every finite simple graph with chromatic number 4 is
4-vertex-critical, meaning that deleting any vertex lowers its chromatic
number.” The connected counterexample is a K4 with one pendant vertex;
deleting the pendant vertex leaves K4.

| Prompt / harness | Input | Output | Latency | Cost | Result |
|---|---:|---:|---:|---:|---|
| Candidate L / Codex CLI | 172,084 | 2,665 | 64.9s | $0.651420 | correct connected counterexample; compressed prompt did not reduce run cost |
| Candidate K / Codex CLI | 104,337 | 2,086 | 50.2s | $0.394022 | correct connected counterexample; no regression |
| Candidate K / IMProof | 116,392 | 4,699 | 129.5s | $0.042778 | correct smallest counterexample; zero-query broker and final Critic closure |
| Candidate J / Codex CLI | 78,611 | 1,731 | 43.2s | $0.279117 | correct connected counterexample; clean final artifact |
| Candidate J / DeepSeek Harness | 16,558 | 1,067 | 29.2s | $0.065825 | correct smallest connected counterexample; live heartbeat control |
| Candidate J / Hermes | 19,633 | 159 | 53.7s | $0.102935 | correct connected counterexample; scoped patch write live-validated |
| Candidate J / IMProof | 181,097 | 5,007 | 137.3s | $0.079127 | correct smallest counterexample; last Critic consumed by final revision |
| Candidate B / Codex CLI | 70,827 | 1,502 | 40.1s | $0.268887 | correct counterexample |
| Candidate I / Codex CLI | 71,679 | 1,947 | 45.5s | $0.269068 | correct connected counterexample; no unnecessary literature search |
| Candidate A / Codex CLI | 84,645 | 2,007 | 82.6s | $0.417922 | correct counterexample |
| Previous SOUL / Codex CLI | 185,207 | 2,909 | 103.5s | $0.867787 | correct, substantially more context |
| Candidate A / DeepSeek Harness | 138,863 | 5,547 | 118.6s | $0.670148 | correct counterexample |
| Candidate A / Meta-Harness | 583,400 | 25,749 | 621.7s | $2.829590 | correct; strongest partial-analysis behavior, high cost |
| Candidate A / OpenHands native ACP | 3,444 | 76 | 50.0s | $0.009370 | correct; native path after model-ID fix |
| Candidate A / DeepAgents | 76,841 | 812 | 36.2s | $0.200222 | correct; tool and usage telemetry present |
| Candidate A / IMProof | 94,456 | 2,019 | 68.9s | $0.029730 | correct; author/critic trace plus faithful Lean proof |
| Candidate A / OpenClaude | 29,828 | 3,979 | 80.8s | $0.251943 | correct; Codex OAuth path after provider fix |
| Candidate A / Hermes | 19,094 | 93 | 60.7s | $0.098260 | correct; one-shot process cleanup verified |

The Candidate-A Codex comparison used about 54% fewer input tokens and 52%
lower estimated cost than the old SOUL without losing correctness.
Candidate B then reduced the Candidate-A control by a further 16% input tokens,
36% cost, and 51% latency while preserving the exact connected counterexample.

### Controlled source-entailment test

The exact same Erdős #944 prompt, Codex CLI harness, `gpt-5.6-sol` model, and
non-subagent setting asked for the strongest independently derivable order
bound, verification against current primary literature, and explicit source
maturity labels.

| Prompt | Input | Output | Latency | Cost | Source-entailment result |
|---|---:|---:|---:|---:|---|
| Candidate A | 543,329 | 6,805 | 164.0s | $2.013893 | cited Proposition 5.1 but extracted only `delta >= 6`; incorrectly foregrounded the weaker `n >= 10` bound |
| Candidate B | 1,032,058 | 9,655 | 237.6s | $3.857495 | read the exact proposition, identified the known `n >= 11` bound, denied novelty, and separated journal/preprint/self-published evidence |

Candidate B cost about 92% more on this research-heavy audit, but corrected the
central epistemic failure being tested. Its self-derived proof of `n >= 11` was
also independently checked: every deletion coloring forces minimum degree six,
and a separate nonneighbor argument gives maximum degree at most `n - 5`.

### Open-problem behavior

- Erdős #933 (Candidate Q / Meta-Harness): the final post-runtime-fix source
  audit authenticated all eight PDF pages, gave exact counterchecks to the
  claimed universal bound, LTE formula, and finite-maximizer claim, reached
  8/10, and retained **Partial Progress**. It does not infer the open limsup
  from falsifying one proposed negative proof.
- Erdős #1210: both prompts correctly classified the problem as open. Candidate
  A used 302,834 input / 3,922 output tokens, 131.7s, and $1.458705 versus the
  old SOUL's 554,175 / 5,649, 190.4s, and $2.672648. Candidate A supplied a
  verified support-compression partial result and resolvable current sources.
  Candidate Q's post-runtime-fix Hermes holdout used 38,192 / 556 tokens,
  187.0s, and $0.207640. It independently checked five exact reductions,
  including the finite Abel identity and the translated-prime obstruction,
  while retaining **Partial Progress**. Its current-source bibliography exists,
  but no cited result resolves the pointwise uniform-constant target.
- Erdős #196: DeepSeek used 138,863 / 5,547 tokens, 118.6s, and $0.670148 and
  produced valid restricted lemmas. Meta-Harness used 583,400 / 25,749 tokens,
  621.7s, and $2.829590 and found stronger reductions, but did not solve the
  open four-term case.
- Erdős #195 (Candidate I / Hermes) correctly distinguished an omega-
  permutation `Nat -> Int` from a bi-infinite order, reproved that three terms
  are unavoidable, constructed 3-AP-free orders for every finite interval,
  and gave a block-localization criterion. It found and correctly limited the
  very recent Geneson preprint arXiv:2608.12604v1: `beta_Z(4)=1` for arbitrarily
  dense subsets does not construct a 4-AP-free permutation of all integers.
  The run used 72,094 / 295 tokens, 254.2s, and $0.369320. Its full 11,121-byte
  draft was recovered from the preserved rejected file-change event; the
  original 966-byte fallback remains beside it. This is Partial Progress, not
  a solution.
- Erdős #944, the open k=4, r=1 case (a 4-vertex-critical graph with no
  critical edge): Codex CLI used 138,282 / 5,374 tokens, 125.7s, and $0.538325;
  Plain used 88,995 / 5,167 tokens, 116.5s, and $0.352237. Both preserved the
  open status and independently derived the exact neighborhood-color criterion
  for edge noncriticality, together with `delta(G) >= 6` and `|V(G)| >= 9`.
  Plain also recorded a reproducible SAT-certificate strategy. Neither claimed
  a solution. A separate audited complement argument plus exhaustive
  enumeration proves `|V(G)| >= 10`; see `evals/erdos944_n9_exclusion.md` and
  `evals/erdos944_small_order.py`. This is a valid independent regression
  artifact, not novel progress: a later primary-source audit found the stronger
  known bound `|V(G)| >= 11` in Skottova--Steiner, arXiv:2508.08703,
  Proposition 5.1.
- UCLA #944 (Candidate A) used 448,125 / 121,793 tokens, 1,157s, and
  $34.5754. It kept the problem open and produced a correct self-contained
  reduction, but synthesis foregrounded the weaker `|V(G)| >= 10` result even
  though its own literature stage had found the known `>= 11` theorem. A queued
  human correction first repeated the weaker claim, then a second ordered
  correction verified Proposition 5.1, foregrounded `>= 11`, labeled the source
  as an arXiv preprint, and preserved the open outcome. This is the primary
  motivation for Candidate B's novelty/source-entailment gate.
- OpenClaw #944 used 37,152 / 6,021 tokens, 373s, and $0.4929 for the initial
  run. It preserved the open status and made a complete labeled search through
  order seven. Two consecutive human interventions were accepted: the first
  incorporated the independent order-nine argument, and the second correctly
  demoted it after verifying the stronger known `>= 11` result.
- Erdős #413: DeepSeek used 106,339 / 3,854 tokens, 96s, and $0.4017. It
  preserved the open status and derived exact shift, running-maximum,
  primorial-certificate, and relaxed-problem reformulations. An adversarial
  audit found one boundary defect: `rho(2)` was defined as a minimum over an
  empty set. Human intervention revised the domain to `n > 2`, rechecked the
  dependencies, labeled arXiv:2512.01739 as a preprint, and explicitly denied
  any unverified novelty claim (18,956 / 3,285 follow-up tokens, $0.0940).
- Erdős #396 (Candidate B / DeepAgents): the initial artifact proved the exact
  valuation/Kummer carry criterion and supplied independent bounded-search and
  exact-remainder verifiers. Both agreed on the declared box `0 <= k <= 2`,
  `1 <= n <= 10`. An audit caught one reproducibility bug—the write-up used
  root paths `/search.py` and `/verify.py` for workspace files—and queued a
  human correction plus a 1-worker/3-worker boundary regression. No unrestricted
  claim was made.
- Erdős #396 (Candidate B + live primary-source tool / DeepAgents): the source-
  enabled rerun used 152,948 input / 4,224 output tokens, 215.9s, and $0.424609.
  It invoked the read-only source fetcher against the live #396 record, retained
  **Known/Open Status**, and again derived the exact valuation/Kummer criterion.
  The first artifact nevertheless presented an infeasible direct-binomial/sieve
  program as reproducible support for witness values near `2e16` and misstated
  the Pomerance publication year/volume. A human-in-the-loop correction removed
  all unexecuted computational claims, demoted OEIS A375077 to imported data,
  corrected the citation to *American Mathematical Monthly* 122(7) (2015),
  636–649, DOI 10.4169/amer.math.monthly.122.7.636, and left the universal
  obligation explicitly open. This validates Candidate B's status discipline
  while showing that executable-evidence and bibliographic checks must remain
  independent gates rather than prompt-only expectations.
- Erdős #524 paper/status audit: the Codex Candidate-B run correctly found
  several decisive defects in Chojecki's self-published January 2026 manuscript,
  including the false inequality
  `liminf max(X_n,Y_n) <= max(liminf X_n,liminf Y_n)`, the unjustified iid
  treatment of parity-alternating endpoint vectors, an insufficient strong-
  approximation parameter condition, and conversion of an additive `o(1)`
  error into a relative asymptotic for a much rarer event. It nevertheless
  incorrectly concluded that the problem remained open because it stopped at a
  stale database record. A deeper OpenClaude search found Letwin--Sawhney,
  arXiv:2604.19294 (submitted 2026-04-21), whose Theorems 1.1 and 1.2 give the
  exact almost-sure lower envelope and explicitly identify the question as one
  raised by Salem--Zygmund and reiterated by Erdős. This is a credible existing
  preprint solution, not a result produced by these evaluations and not yet
  treated here as peer-reviewed. It is the motivating regression for Candidate
  G's dated-status gap audit.
  Candidate H subsequently prevented a false open conclusion but stopped at
  **Partial Progress** after incomplete searches. Candidate I's terminology
  expansion plus the materialized `literature-search` tool found the exact
  preprint, matched its all-$n$ liminf theorem to the original question, and
  preserved the preprint/peer-review distinction.
- Erdős #684 (Candidate K / Codex CLI) used 1,385,177 input / 10,078
  output tokens, about 266s, and $5.135883 before the surgical HITL follow-up.
  It recovered the exact small-prime-part definition and independently checked
  the theorem-level quantifiers of the pointwise
  `f(n) <= (24/(pi^2-6)+o(1))(log n)^2` preprint result. More importantly, its
  date-gap audit found Bae's arXiv:2604.23784v2 unbounded logarithmic-limsup
  claim and Li's arXiv:2606.08216v1 density-one asymptotic
  `2/(1-gamma) log n`, both after the database's 2026-04-01 snapshot. It kept
  **Partial Progress** because neither long preprint proof was independently
  closed and no current authoritative source settled the residual optimal
  pointwise order. The literal Unicode query exposed the old parser's
  `Erd`/`s` split and exact arXiv IDs exposed its decimal-ID split; neither zero
  count was accepted as negative evidence. A post-run informal HITL changed
  only two malformed TeX fragments, removed an ASCII backspace, reran byte and
  display-math checks, and preserved all mathematics. The final 12,711-byte
  artifact has SHA-256
  `60ccf23c0a1013f06005ca941f74010aaba515f8c8e9177d41023049585445b4`.
- Erdos #348 (Candidate I / DeepAgents) used 349,251 input / 7,805 output
  tokens, 347.4s, and $0.951177. It correctly used **Partial Progress**, kept
  strong completeness distinct from weak/cofinite completeness, formalized
  deletions as universal quantification over positions, and proved exact
  deletion-criticality, eventual-gap, interval-extension, modular-support, and
  pure-gcd obstruction lemmas without claiming a construction or impossibility
  theorem. Its bounded literature audit disclosed Crossref throttling and left
  primary-source status unresolved. This run also exposed a harness-control
  defect: although `use_subagents=false` was recorded on the job, the trace
  contains two `task(general-purpose)` calls because DeepAgents auto-installs
  its default task tool unless its provider HarnessProfile explicitly disables
  it.
- The post-fix Erdős #348 DeepAgents control used the identical prompt and
  model with `use_subagents=false`. It finished normally in 393.1s using
  533,485 input / 15,564 output tokens and $1.489352; all 72 trace nodes were
  direct assistant/reasoning/tool activity, with zero `task(...)` or
  `general-purpose` calls. The 18,514-byte artifact retained **Partial
  Progress**, exact weak/universal deletion quantifiers, a bounded dated source
  audit, and explicit open obligations. It also caught and corrected its own
  initially over-broad congruence lemma at modulus two rather than letting the
  false generalization support the conclusion.
- Erdős #413 (Candidate J / DeepSeek Harness) used 165,834 input / 3,753
  output tokens, 99.8s, and $0.609235. It separated `omega` from `Omega`,
  inspected Lau's arXiv:2604.15042v2, and derived a rigorous constant-endpoint-
  loss implication for the epsilon variant plus an exact finite-window
  completion criterion. Independent inspection of the primary TeX source
  confirmed the cited plus/minus theorems and corollary. The 7,350-byte
  artifact (SHA-256 `b6edf0e23b8d6b9512c2493b843ca5fa14a683db0da1dcc9024e9d3fb410b361`)
  retained **Partial Progress** because the negative search and the finite
  window needed for the exact open problem remained unresolved.
- Erdős #348 (Candidate J / IMProof) used six author/critic calls totaling
  158,297 input / 51,725 output tokens, 1,308.7s of model latency, and
  $0.251719. Its new host broker preserved a 20,263-byte typed research packet
  with exact arXiv/Crossref queries and a fetched Problem 348 page, while every
  model call remained read-only and tool-free. The 20,247-byte final Author
  draft (SHA-256 `c90bd0f298951c32d7ebf351d231cac8aa4e3610063e081298b7c1238d3a9ba4`)
  gives useful exact quantifier, prefix-defect, finite-repair, gcd, and failed-
  recurrence lemmas under **Partial Progress**. The last Critic correctly
  found that gcd one excludes only a common-divisor obstruction, not every
  modular obstruction; this exposed a workflow bug because that final report
  had not yet been consumed by an Author revision.


- Erdos #749 (Candidate K / OpenClaude) kept the exact lower-density target
  distinct from Bhalla's upper-density construction and used **Partial
  Progress**. The main run used 3,376,879 input / 51,933 output tokens, 1,353s,
  $18.8761, and 113 nodes. Two post-run adversarial controls were both
  correctly consumed: one rejected an instruction to promote upper density to
  lower density (97,283 / 5,711, 115.2s, $0.648390), and the other exposed an
  impossible infinite-sum sufficient condition and supplied only a clearly
  labeled powers-of-two consistency witness (144,892 / 14,222, 287.3s,
  $1.153226). No false proof or solved label survived.
- The controlled Candidate N and O #749 reruns are summarized in their
  candidate sections above. Both remained **Partial Progress** and passed
  artifact/control-byte/citation checks. N supplied the broader two-scale and
  exact-gluing audit at substantially lower cost. O independently proved that
  the specific 2026 packet construction has lower density at most 1/2. The
  automatic final-source rerun rebuilt 19 informal nodes, attempted Lean, and
  correctly left coverage at 0/19 after Lean failed; no green formal badge was
  manufactured for an open statement.

### Formal audit

IMProof's generated Lean file kernel-checks the exact negation of the false
control, with zero sorry/admit. It explicitly defines finite simple graphs,
chromatic number, vertex deletion, a connected five-vertex counterexample, and
proves that deleting the pendant vertex leaves a four-color obstruction. This
is faithful control verification, not progress on Erdős #944.

Candidate B's Codex false-control sidecar was also repaired and accepted by the
Lean 4.14 kernel with zero sorry/admit. The generated mathematics was sound; the
initial failure was version compatibility (`Mathlib.Tactic.Omega` is absent in
this Mathlib release) followed by overly broad finite-goal automation. The
application now normalizes that legacy import to the Lean 4.14 core Omega
module, and the closed adjacency obligations were independently kernel-checked.

The Candidate-J #413 sidecar preserves the exact open infinitude statement as
the only `sorry`, while separately proving four feasible auxiliary theorems:
the offset equivalence, finite-window completion, constant-endpoint-loss
scaling, and its unbounded-family consequence. After normalizing generated
Lean 4.14 import aliases, the complete file kernel-compiles with that one
explicit open obligation; no helper theorem is admitted.

The Candidate-J IMProof false-control sidecar exposed a different distinction:
an early file kernel-compiled the core negation but omitted requested minimality
and deletion cases, so the fidelity gate correctly returned `unfaithful`
instead of green. Three ordered formal interventions repaired only observed
obligations. The final 286-line file proves the connected five-vertex
`K_4`-with-pendant counterexample, both chromatic bounds for every deletion,
and the global five-vertex minimality bound; it is kernel `verified`, has zero
`sorry`/`admit`, and its independent audit is `faithful: true` with no issues.
Its SHA-256 is
`75acc99aba0e4b45b01d585a72d016cc26a6f383675a1c8918b710b320b7f149`.

Candidate K's fresh IMProof control reproduced the full formal boundary. The
automatic sidecar first kernel-compiled a weak core, correctly failed fidelity,
then expanded to all five deletion cases and global minimality; that expansion
introduced three local Lean errors. A serialized formal HITL repaired only
those errors. The final 289-line file retains the seven-part `main`, has zero
`sorry`/`admit`, kernel-verifies, and its fresh independent audit is
`faithful: true` with no issues. The Lean artifact SHA-256 is
`aa12d327cdb0eb3768245f1ec5f5a33e982fb8bbd58ab9558a3835c8a5a622cc`.

A provider-routing control also exercised the complete Anthropic API-to-Lean
path rather than Codex subscription mode. Plain with `claude-haiku-4-5` proved
`forall n : Nat, n + 0 = n` using 6,399 / 2,535 tokens, 17.8s, and $0.041348.
The clean 865-byte informal artifact has SHA-256
`28c51d803faf1c2a39403025c444d81af71d0764f69c7d74bb5de1e82292973a`;
the 373-byte Lean artifact has SHA-256
`da0b13e385e52c4300ec95261570eeb2133eaa549b4828c0e5f72ff8f725581e`,
contains a real induction proof, and kernel-checks with zero `sorry`/`admit`.
The derived views produced ten informal and eight formal nodes and reported one
verified target out of ten. This establishes that Anthropic -> informal
artifact -> formal generation -> Lean 4.14 kernel is operational; failures on
complex open-problem graphs are generation/fidelity outcomes rather than an
API-routing failure.

### Human-in-the-loop audit

Live and post-run interventions were verified on OpenClaw, DeepSeek, and UCLA.
Messages sent during active work were durably queued and automatically launched
exactly once after the main harness finished; messages sent after completion
started an immediate continuation. DeepSeek corrected the `n=2` boundary bug, OpenClaw accepted two ordered
revisions without losing the prior proof, and the DeepAgents #396 continuation
inherited and displayed `gpt-5.6-sol` rather than the former `null` metadata.
A later #396 intervention also repaired an already-finished source-enabled run:
it used the tool-accessible DOI/source evidence, removed infeasible pseudo-
reproduction code, preserved the imported-data boundary, and rewrote `proof.md`
without losing the exact mathematical reductions.
The formal Lean channel was then tested with three ordered interventions on a
finished IMProof run. Each revision preserved the prior source and complete
attempt history. A kernel-successful but statement-incomplete revision remained
visibly `unfaithful`; only after the last message packaged all requested claims
did the status become `verified`. This validates both editable formal HITL and
the kernel-plus-fidelity green gate.
Candidate K added two more post-run controls. The #684 informal continuation
removed one ASCII control byte and restored two TeX backslashes without changing
the source/status audit. The fresh IMProof formal continuation remained
serialized while active, preserved all requested conjuncts, repaired three
specific kernel errors, and moved from `failed` to faithful `verified`.

### Reproducible harness bugs fixed during evaluation

1. Plain/DeepSeek truth-status and artifact-responsibility prompts.
2. Contradictory proof.md/proof.tex instructions.
3. Full skill-body prompt inflation (skills are now lazy references).
4. Auto-pipeline rerun timestamps/results and orphan sidecar reconciliation.
5. Lean timeout process-group leaks.
6. OpenHands link-only false success, proof overwrite, parser telemetry, and
   native ACP model-effort selection.
7. DeepAgents Codex JSONL usage propagation.
8. OpenClaude 0.28 Codex OAuth routing isolated from ordinary OpenAI API mode.
9. Hermes one-shot app-server cleanup; stale filesystem-failure paragraphs at
   either artifact edge are removed only from the saved proof while raw trace
   data is retained.
10. Library prompts now advertise only materialized memory, skill, and tool
    files, preventing agents from chasing disabled/nonexistent context.
11. UCLA now propagates the UI-selected model into its subprocess environment
    instead of silently using the harness default.
12. Continuation jobs now inherit and display the run's effective model instead
    of showing `null` in monitor/job metadata while silently reusing it.
13. Portable harness skills are copied as complete workspace-relative packages,
    including references/assets, and frontmatter descriptions are parsed. This
    fixes DeepAgents' former host-absolute, unreadable skill links.
14. DeepAgents Codex calls now run in dedicated process groups and terminate the
    whole group on timeout or wrapper shutdown, preventing orphaned native Codex
    children from leaving a job permanently `running`.
15. Lean generation normalizes the legacy `Mathlib.Tactic.Omega` import to
    `Lean.Elab.Tactic.Omega` for the installed Lean/Mathlib toolchain.
16. Lean prompts prohibit broad `import Mathlib` and require narrow module
    imports plus `lake env lean`; a live #396 sidecar fell from roughly 140s and
    2GB RSS to 7.85s with `Mathlib.Data.Nat.Choose.Basic`.
17. A typed, read-only `primary-source-fetch` tool now retrieves bounded text
    from allowlisted HTTPS mathematics sources with validated redirects. This
    lets virtual harnesses audit source existence/entailment instead of guessing.
18. The library Lean checker now recognizes both `lakefile.toml` and the
    generated `lakefile.lean`, so it actually uses the shared Mathlib project.
19. OpenClaude's default 20-turn limit could terminate source-heavy audits before
    the required artifact was written; the application default is now 60 turns.
20. Nonzero, timed-out, or artifact-free CLI exits formerly persisted
    `error: null`; run records now include an actionable failure reason.
21. Failed terminal activity logs could be mistaken for proof text by the
    automatic DAG/Lean pipeline; terminal-output fallback is now restricted to
    successful `finished` runs.
22. OpenClaude error-result subtypes were discarded, reducing all structured
    terminal failures to a generic exit code; future run errors now preserve the
    CLI's terminal subtype or message.
23. A typed, read-only `literature-search` tool now emits reproducible bounded
    arXiv/Crossref query logs with date ranges, totals, and resolvable IDs instead
    of asking agents to support negative search claims with opaque web searches.
24. Stop/delete formerly killed only the registered wrapper process, so a native
    Codex child could outlive a UI-stopped run. Registered runners now use
    isolated sessions; stop/delete terminate the full process group, and a stop
    arriving before process registration is honored immediately. A live
    immediate-stop control left zero descendant processes.
25. Concurrent automatic Lean checks could each load the full Mathlib umbrella,
    exhausting this 3.7GB host and leaving sidecars apparently running until the
    five-minute timeout. Kernel checks now queue through one shared slot by
    default, exact `import Mathlib` is rejected before spawning with a narrow-
    import repair message, and interrupted sidecars reconcile to terminal
    `partial` state after restart. In the live two-request control both files
    compiled, actual `Proof.lean` checker-group concurrency peaked at one, and no
    sample contained two checker groups.
26. DeepAgents ignored the UI `use_subagents=false` selection because passing
    an empty subagent list does not suppress its automatically installed
    general-purpose `task` tool. The runner now registers the supported
    provider HarnessProfile with `GeneralPurposeSubagentProfile(enabled=False)`,
    removes the task tool, carries the toggle into external-harness environments,
    persists it through live rewrites, and inherits it in human-in-the-loop
    continuations. The pre-fix #348 trace provides a real failure control. The
    post-restart same-prompt control finished with 72 trace nodes and zero
    `task(...)` / `general-purpose` calls, so the deployed fix is live-validated.
27. Silent long-running subprocesses formerly left the monitor blank until the
    next stdout line. The shared streaming runner now emits a live heartbeat
    every two seconds while reading stdout on a dedicated thread; the isolated
    IMProof #348 control displayed its first live node within 17 seconds.
28. IMProof's sequential Codex author/critic rounds could not reach the
    advertised run-local tools because the native workflow used a workspace
    outside the console run. Each workflow now receives a unique `console-*`
    directory inside the outer run workspace, with only `_library` and `_agent`
    copied into it. The live control confirmed both tool and skill packages in
    that isolated directory.
29. Giving nested IMProof Codex processes a scoped network proxy did not cure
    their host `SIGTRAP` tool failure. Literature work now instead uses a
    tool-disabled, read-only Codex query planner that emits exactly three
    bounded queries and at most four primary-source URLs. The trusted host runs
    only typed `literature-search` and allowlisted `primary-source-fetch`
    adapters, preserves an 80KB-bounded research packet, and injects it into
    read-only Author/Critic calls. A live #348 run completed without the former
    trap and preserved the exact queries, hit logs, and fetched source.
30. Hermes' embedded Codex app-server had the correct run cwd but still failed
    every write: this host cannot initialize Codex's network-disabled loopback
    namespace, and an unattended app-server declines all file-change approval
    requests. Monitor-owned Hermes sessions now enable workspace-write network
    only behind the same ten-domain mathematics proxy and auto-approve only
    file patches that remain inside the unique writable run root. Exec
    elevation and permission changes remain fail-closed. A live Candidate-J
    control created the complete 2,295-byte artifact by a successful scoped
    patch after its initial shell call hit the loopback failure; the saved
    proof has SHA-256
    `f9233ad0f0974620b633e2c87e5d4a36c221134628925f6d504e5dece4c1eb21`.
31. Hermes one-shot model calls could leave an unchanged `running` placeholder
    for several minutes. A two-second background heartbeat now refreshes live
    status independently of model/tool callbacks and is stopped/joined before
    resource teardown.
32. Generic CLI wrappers had a second silent-run gap: `_run_cli_engine` skipped
    every empty queue timeout, so its live record stayed frozen even though the
    child was healthy. It now flushes a heartbeat every two seconds and closes
    the stdout reader cleanly. In a deployed DeepSeek control, `updated_at`
    advanced from `14:59:05` to `14:59:09` while token usage and latency were
    still absent; the job then finished normally with a correct artifact.
33. The Lean 4.14 source tree and object cache use
    `Mathlib.Data.Nat.PrimeFin` and `Mathlib.Tactic.Linarith`, while models may
    generate the obsolete `Prime.Defs`, nonexistent `Prime.Finset`, or
    nonexistent `Tactic.Nlinarith` imports. The normalizer now maps all three
    narrow aliases. The deployed #413 sidecar compiles with exactly one honest
    `sorry` on the open main theorem and four fully proved helper theorems.
    Generated enum files also confused `Data.Fintype.Basic` with the actual
    derive handler; files containing `deriving ... Fintype` now map narrowly to
    `Mathlib.Tactic.DeriveFintype`.
34. Authentication database helpers relied on SQLite's context manager, which
    commits or rolls back but does not close the connection. Every auth access
    now composes `contextlib.closing` with the transaction context; a mocked
    regression checks both transaction exit and close, and the authenticated
    settings API was live-validated after restart.
35. The IMProof repeat node ended on a Critic but promoted the preceding Author
    draft, leaving the final referee's repairs unconsumed. The workflow now runs
    one explicit read-only `final_revision` Author with both the last solution
    and critique, and promotes only that output. A live Candidate-J control
    produced seven ordered nodes (Author/Critic three times, then Author); the
    final Author input contains the exact last report, and its promoted
    2,186-byte correct artifact has SHA-256
    `02743a9f5e0d9ad9ecaae4337761f0d1ff5f502fad66662005f5045e6251ce4d`.
36. IMProof's broker originally forced three literature queries even for an
    explicitly self-contained finite counterexample, producing a needless
    29KB packet. Its read-only planner schema now distinguishes source-dependent
    work from self-contained mathematics; the former still requires exactly
    three query families, while the latter must return zero queries and URLs.
    A live planner control returned `RESEARCH_REQUIRED false`, zero Query/Source
    sections, and a 398-byte packet, so no host search adapter ran.
37. Lean translation could make a correct counterexample fail by conjoining
    optional minimality and every-case audits into the core `main`. Translation
    and repair prompts now preserve the exact negation as the core obligation,
    keep bonus claims separate, and may drop non-required failed helpers without
    treating that as weakening. For controls that explicitly request the bonus
    claims, the fidelity gate still requires them. The live IMProof sequence
    demonstrated both sides: kernel verification alone stayed `unfaithful`,
    then became fully `verified` only after all requested helpers fed the final
    conjunction.
38. The literature adapter tokenized arXiv queries with ASCII-only regexes.
    `Erdős` became `Erd AND s`, punctuation silently vanished, and
    `2603.29961` became two ordinary terms, producing misleading zero hits.
    It now NFKD-normalizes Unicode, logs normalized text and the parsed arXiv
    expression, recognizes versioned arXiv IDs as `id:` queries, and has a
    no-network dry-run regression. The corrected 5,960-byte tool is deployed in
    the live library.
39. `proof-sanity-check` treated an artifact containing ASCII backspace as
    clean, so malformed `\bf1` could reach the renderer. It now rejects every
    ASCII control byte except tab/newline/carriage-return and reports byte
    offsets. A materialization regression injects `0x08` and requires failure;
    the repaired 1,531-byte checker is deployed.
40. A fidelity-driven Lean statement expansion was compiled only once. If that
    expansion introduced a local proof error, generation stopped even when the
    pre-expansion theorem had compiled. The generator now gives the expanded
    statement one bounded ordinary compile-repair, records it distinctly as
    `statement-repair`, and re-audits only after it compiles. A three-check
    regression covers verified core -> failed statement-fix -> verified
    statement-repair. The live pre-fix IMProof artifact was independently
    repaired through serialized formal HITL and ended faithful/verified.
41. Explicit arXiv versions were recognized but discarded before request
    construction, so `arXiv:2603.29961v2` became the broader
    `search_query=id:2603.29961`. The adapter now retains the version and sends
    exact identifiers through arXiv's `id_list`; keyword searches still use
    `search_query`. A direct official-API control distinguished the versioned
    endpoint, the materialization regression requires `id_list=2603.29961v2`,
    and the repaired 6,155-byte tool is deployed in the live library. A fresh
    Candidate-K/Codex end-to-end control materialized that live tool, requested
    `v1` and `v2` separately, and returned the matching exact version for both
    (125,157 input / 2,816 output, 68.9s, $0.463293). Its clean 3,457-byte
    artifact has SHA-256
    `b53c4a2a5f33ca01c8c0bc2b7587e05a57667a0bb2e700156505a94c7ae2701d`.
42. Quoted arXiv phrases were logged in normalized text but then split into
    ordinary `all:` words. This silently broadened exact-title queries and
    triggered avoidable retry families under Candidate K's integrity gate. The
    parser now preserves sanitized double-quoted phrases while separately
    parsing residual terms. The live 6,588-byte tool sent
    `all:"Infinite Sidon-type sets" AND all:"zero-sum linear forms"` and the
    official endpoint returned the unique exact hit arXiv:2607.20753v1. A
    no-network materialization regression preserves both encoded quotes.
43. UCLA's background Responses API printed only its initial submit line and
    final completion, leaving a healthy long `queued`/`in_progress` request
    visually indistinguishable from a deadlock. It now emits the response ID,
    status, and elapsed time on every status transition and at least every 30
    seconds. A regression checks the poll cadence; a fresh #389 process emitted
    queued/in-progress response IDs and elapsed time every 30 seconds.


44. UCLA successfully downloaded PDFs but silently converted them to
    `fetch_failed` when no parser happened to be installed. PyMuPDF is now a
    main runtime dependency; Stage 0 fails before paid search if it is missing.
    A fresh #389 control extracted real paper text and emitted live background
    response IDs/status every 30 seconds.
45. CLI runs saved the Library-expanded execution prompt as the canonical
    problem text, so reload/continuation could inject the entire Library a
    second time. Execution still receives the expanded prompt, while run
    records now retain the raw user statement. A live Plain control stored
    exactly 84 characters and zero `USER LIBRARY` blocks.
46. UCLA's Advisor prompt described every Stage-0 item as a successful
    extraction even when the item contained `fetch_failed`. It now reports
    usable and failed counts separately and treats failed entries only as URL
    leads, never paper evidence.
47. The literature parser removed the plus sign inside exact phrases such as
    `"density of A+A"`. Quoted-token parsing now preserves internal plus
    signs; a live dry run emitted
    `all:"density of A+A"` and an endpoint containing `A%2BA`.
48. UCLA retried an `incomplete/max_output_tokens` Advisor response forever
    with identical parameters. Terminal retries are now limited to three,
    recorded with billable usage, and adapt by increasing output allowance
    while lowering reasoning effort before raising an actionable terminal
    error.
49. The canonical-problem change exposed an undefined `first_problem` in the
    real CLI continuation path. It now passes the existing `problem_text`
    argument, with a synchronous execution-path regression. After a safe
    restart, a live Plain continuation finished without that exception while
    retaining the 84-character canonical statement and zero Library blocks.
50. Generic PDF `Read` still depends on unavailable Poppler. A sixth
    read-only Library tool, `pdf-text-extract`, now uses the service's
    PyMuPDF interpreter with workspace-realpath containment, a 25 MiB cap,
    bounded page ranges, and a 200,000-character ceiling. It extracted the
    first two pages of the real ten-page #749 PDF in an isolated end-to-end
    control; the runner now exports its active interpreter explicitly.
51. UCLA Stage-0 paper summaries overrode their own designed defaults with
    `xhigh + 128k`; observed single-paper calls consumed 134k--286k reasoning
    tokens and $24--$52. Literature search now defaults to medium/16k, paper
    extraction to high/32k, and Stage-1.5 extraction to high/32k. Advisor and
    solver budgets remain independently high and user environment overrides
    remain available.
52. Plain is a one-call baseline whose wrapper promotes the model response to
    `proof.md`. During a continuation that explicitly requested exact
    preservation, the model correctly said the proof was unchanged but that
    acknowledgement replaced the proof. Plain now has a sentinel-based exact-
    preservation protocol plus a bounded natural-language fallback. Two
    regressions distinguish an acknowledgement from a real revision. In the
    live control, the model returned the sentinel and the restored proof kept
    SHA-256 `af65bb63db93067e1d291bc6c6b6e39abea592f6f91f5b50df514eba6de93a54`
    byte-for-byte.
53. The documented literature helper had no help mode: Candidate P invoked
    `literature-search.sh --help`, which was silently parsed as the literal
    query `help` and returned 85,132 irrelevant arXiv matches. The agent
    correctly excluded it from the query gate. The script now returns bounded
    usage and argument documentation for `-h`/`--help` without contacting
    an index; the behavior is regression-tested and deployed through the live
    Library API for future workspaces.
54. Run records were published with two direct `Path.write_text` calls. A
    monitor read during one live Candidate-P refresh observed a truncated JSON
    document and `jq` failed with `Unfinished string at EOF`. Both the cache
    and dashboard copies now publish under one lock through a same-directory
    temporary file plus atomic `os.replace`; a regression checks both replaces,
    parses both copies, and requires no orphan temporary file.
55. Generic CLI finalization copied the same complete `proof.md` onto every
    message-shaped contributor before computing line provenance. Candidate P's
    100-node trace therefore spent minutes comparing every proof block against
    the same 22,968-byte artifact and remained visibly `running` after the
    model process had exited. New runs attach the shared artifact only to the
    final eligible contributor; old saved runs deduplicate byte-identical
    contributions and remove synthetic duplicates when reprocessed. Focused
    regressions cover both a fresh three-node trace and a 100-node legacy trace.

56. HITL accepted `max_iterations` but generic CLI continuations discarded it;
    OpenClaude therefore always launched with its 60-turn default. The generic
    runner now maps the bounded request (clamped to 1--200) to
    `AGENT_MONITOR_OPENCLAUDE_MAX_TURNS`, and both continuation propagation and
    adapter argv are regression-tested. A deployed three-turn preservation
    smoke finished in 2.96s using 6,822 input / 18 output tokens and $0.03584.
57. CLI HITL completion overwrote the run record containing `auto_pipeline`
    state but never restarted the derived pipeline, so revised informal proofs,
    Lean, formal DAG, and coverage went stale. Every artifact-bearing
    continuation now starts a new durable sidecar after reaching terminal
    state, with failures isolated from the proof run. In the deployed smoke it
    began in the same second, completed an 18-node informal DAG and seven-node
    formal DAG, correctly left Lean failed, and reported 0/18 verified. The
    accepted proof stayed byte-identical at SHA-256
    `8ace9a9d156ca9a81b24bab5c53ddf05ae9053af9e2649e15eb0cd3a88c093c6`.
58. `proof-sanity-check` treated any prose occurrence of `sorry` or `admit` as
    an unfinished proof. The #196 DeepSeek artifact therefore triggered a
    warning precisely because it honestly explained that an upstream formal
    declaration contains `sorry`. The checker now flags TODO/FIXME, standalone
    Lean gaps, `by sorry`/`exact sorry`, and gaps inside Lean code fences, while
    allowing quoted narrative audit text. Regressions cover both cases; the
    compatibility migration updates only the exact legacy starter block, and
    the repaired tool is deployed in the live Library.
59. IMProof's typed source broker fetched an arXiv `abs` URL verbatim and kept
    only the last 24,000 characters of a long result. On Erdős #413 that gave
    every Author and Critic only Lau's abstract, while the pivotal Theorem 1.3
    and Corollary 1.4 were available in the official full HTML. The broker now
    prefers the corresponding allowlisted arXiv `html` URL, falls back to the
    original abstract if full text fails, and uses a bounded head-plus-tail
    capture rather than discarding theorem-bearing headers. Regressions cover
    successful full-text retrieval, preservation of both capture edges, and
    fallback. The deployed same-problem control received a 36,882-byte packet
    containing both exact numbered results.
60. Lean citation extraction scanned arbitrary source as though it were a
    module-level numbered bibliography whenever no `/-! ... -/` module doc was
    present. It therefore misread a line beginning `1 ≤ k` as a citation, while
    harmless Markdown backticks and brackets prevented model/source copies of
    the same `Cites:` entry from deduplicating. Numbered parsing is now confined
    to real module docs, normalized keys ignore harmless Markdown emphasis, and
    both monitor summaries and the full API reconcile legacy caches on read.
    The deployed #413 record immediately changed from seven displayed references
    to the correct three without rewriting its Lean file.
61. Meta-Harness passed only the first 8,000 characters of each solver artifact
    to its evaluator. In the Candidate-P Erdős #944 baseline, round two produced
    a complete 13,585-byte audit with references, but the evaluator saw a
    mid-bullet truncation, falsely reported that the artifact and references
    were missing, and scored it 5/10. Normal artifacts up to 50,000 characters
    are now preserved in full; longer artifacts use an explicit bounded
    head-plus-tail excerpt so both the statement and conclusion remain visible.
    A regression covers an ordinary artifact above the old cutoff and a
    60,000-character boundary case. In the deployed exact control the evaluator
    read the complete 13KB round-two artifact, found its actual contradictory
    lemma, and the final score improved from 6/10 to 7/10.
62. Meta-Harness allowed its proposer to rewrite away the solver's response-only
    contract. In #944 round three, the evolved solver therefore tried to write
    the runner-owned, read-only workspace, reported a permission failure, and
    polluted its trace even though the runner successfully persists the model
    response itself. Every initial and evolved harness now receives an immutable
    bounded runtime suffix: read-only research tools remain available, filesystem
    writes and permission commentary are forbidden, and the complete Markdown
    proof must be returned for the runner to save. A regression verifies that
    the contract survives a proposed harness longer than the 4,000-character
    evolution budget. The deployed #944 control completed all three rounds with
    no filesystem-write attempt or permission commentary in the final artifact.
63. Lean gap telemetry counted `sorry` and `admit` inside documentation comments,
    line comments, nested block comments, and strings as executable proof gaps.
    The real #413 file therefore displayed two gaps although Lean's compiler
    reported only the single `sorry` in `main`. A small Lean-aware scanner now
    blanks comments and strings while preserving line numbers before counting.
    The real file now reports exactly one gap on line 37; a regression covers all
    four false-positive forms and a genuine executable gap.
64. DeepAgents' Codex subscription bridge accepted structured tool calls without
    checking the selected tool's required fields. In the Candidate-P #389
    `gpt-5.6-terra` holdout, the model consequently emitted repeated
    `write_file({})` calls; DeepAgents returned the same validation error until
    the model eventually self-repaired, inflating the run to 541,569 input
    tokens and $1.561146. The bridge now validates decoded arguments against the
    advertised OpenAI-tool schema and performs at most one internal structured
    correction with an explicit missing-field diagnostic, while preserving usage
    from both billable calls. Repetition still fails explicitly rather than
    entering the graph loop. The first deployed control stopped the repetition
    after one correction and exposed the distinct double-encoding defect below.
65. DeepAgents described every tool's `arguments` as a string containing JSON.
    Large multiline `proof.md` content therefore required fragile double
    escaping; `gpt-5.6-terra` produced invalid inner JSON twice and the bounded
    repair correctly failed after 48.6s instead of looping. A direct Codex smoke
    established that typed nested argument objects are supported when every
    optional field is represented as required-and-nullable. The bridge now
    generates a strict `anyOf` variant for each actual LangChain tool, removes
    schema defaults, converts optional fields to nullable, decodes arguments as
    objects, and omits optional nulls before execution. Both multiline writes
    and optional-null schemas passed independent CLI smokes; the required-field
    regression covers string and typed-object compatibility. The exact deployed
    #389 control completed with two first-attempt full writes, 246,531 / 6,900
    tokens, 167.1s, and $0.685328—less than half the baseline input and cost.
66. UI model selection reached formal Lean/DAG calls but not several primary
    API runners: Plain and Meta-Harness always constructed an OpenAI client, and
    selected models were not mapped into their runner-specific environment
    variables. Choosing Claude could therefore be ignored or sent to the wrong
    provider. A shared one-shot backend now routes OpenAI models to Responses
    and Claude models to Anthropic Messages, preserves provider usage, redacts
    secrets from errors, and the job wrapper maps the chosen model into Plain,
    Meta-Harness, DeepAgents, OpenClaude, and OpenClaw without weakening Codex
    subscription isolation. A live Plain `claude-haiku-4-5` control reported
    `auth_mode=api_key`, real Anthropic usage/cost, and correctly refuted the
    false Hamiltonicity claim.
67. The first Claude Plain control exposed a non-agentic artifact boundary:
    Candidate P's portable persona mentions research tools and early artifact
    creation, but Plain has no tools. Claude simulated XML `function_calls` and
    shell writes, and the wrapper saved the entire simulation as `proof.md`.
    Plain API calls now carry an immutable no-tools response contract, reject
    pseudo-tool XML or a response without a leading level-one final heading,
    perform at most one clean-Markdown correction, and sum both billable usages.
    The final deployed control produced a clean 1,606-byte proof (SHA-256
    `4f2892c36b13f8ed112e311ffaf9055a278e7a8c66c0990948b5ee5abec659a6`),
    with no pseudo calls or draft commentary, and correctly used `P_4` as the
    verified counterexample.

68. Hermes enabled a scoped Codex network proxy by default. On this kernel the
    proxy failed before every tool launch with
    `bwrap: loopback: Failed RTM_NEWADDR`, so even native `apply_patch` could
    fail although the model had a correct answer. Scoped network is now an
    explicit `AGENT_MONITOR_HERMES_SCOPED_NETWORK` opt-in; the default isolated
    app-server uses `network_access=false`, and exact stale loopback sentences
    are removed from the promoted proof without hiding an honestly incomplete
    source audit. The post-fix #1210 run wrote its artifact with zero loopback,
    permission, or filesystem commentary.
69. Hermes received a Library-expanded execution prompt and then stored that
    expansion as the canonical problem, duplicating the Library on reload and
    continuation. Initial and continued runs now pass the raw display problem
    separately. The deployed #1210 repeat stored exactly 1,339 characters and
    zero `USER LIBRARY` blocks while execution still received the materialized
    tools.
70. Codex app-server tools bypassed Hermes' regular structured start/complete
    callbacks, leaving Monitor at `tool_calls=0`, `iteration=0`, and an empty
    Tools lane even when `proof.md` had been created. App-server item starts and
    completions now bridge into the same live state; terminal runs close active
    tools, set iteration one, and copy the count to the final DAG node. A live
    arithmetic control recorded one `apply_patch` as `done`, saved SHA-256
    `02946e0dcee94095cbd5f27b6c5d82b1379b8e598891b5b94c5198fbc69f04aa`,
    and finished in 18.7s with 17,088 / 167 tokens.
71. The official Codex app-server protocol also defines `webSearch`, but the
    Hermes projector treated it as an opaque assistant note and omitted it from
    tool iterations. `webSearch` now projects as a correlated tool call/result,
    and completion backfills the real query/action that is intentionally absent
    from the start event. A production #1210 status control displayed two
    searches with their exact URL/query, plus `apply_patch` and `exec_command`,
    all terminal, then correctly reported the dated official OPEN label.
72. Hermes completion accepted any nonempty `proof.md` before reading the final
    response. The first web-search control explicitly said the file remained an
    incomplete draft, yet the 154-byte shell was marked finished while the
    correct OPEN/date/URL existed only in the final response. Promotion now
    preserves an existing artifact unless the final response explicitly calls
    it incomplete; in that case it rejects an identical embedded stale copy and
    safely recovers the substantive final text. The repeated production control
    instead produced a complete status artifact directly (SHA-256
    `34e80a0b95a7108f12697a4858c10fd6fd10dcb41b335d79ffc8481c13aad46c`),
    with the official URL, access date, and no resolution inference.

73. The shared one-shot Codex CLI backend's `read-only` sandbox selected
    bubblewrap on Linux. On this host every shell command then failed before
    execution with `bwrap: loopback: Failed RTM_NEWADDR`, so a source-audit
    agent could not even run `pwd` or inspect a local PDF. Linux read-only calls
    now explicitly enable Codex's legacy Landlock implementation, with an
    environment opt-out, while suppressing only the exact nonfatal deprecation
    event from UI telemetry. A direct Codex smoke read the workspace path and
    PDF header successfully; regressions cover the default, opt-out, and notice
    filter.
74. Meta-Harness' `extract_md()` used an unanchored first-match regular
    expression to unwrap Markdown. A complete 15--20KB proof containing
    internal fenced snippets was therefore replaced by the text between an
    internal closing and later opening fence, leaving only 28 or 409 bytes for
    persistence and evaluation. Extraction now unwraps only when the entire
    response is one outer Markdown fence; otherwise it preserves the document
    byte-for-byte. Focused regressions cover both cases, and two consecutive
    production rounds retained 15,914 and 19,857 bytes before the final source
    audit retained 19,659 bytes and scored 8/10.
75. The injected `pdf-text-extract.sh` correctly selected
    `${AGENT_MONITOR_PYTHON:-python3}`, but the shared Codex backend's safe
    environment whitelist discarded `AGENT_MONITOR_PYTHON`. It fell back to
    system Python and reported that the PDF parser was unavailable even though
    the production virtual environment contained PyMuPDF. The single safe
    interpreter variable is now forwarded without exposing provider secrets.
    A production read-only backend smoke extracted page 1 of the eight-page
    #933 PDF with exit 0, and its mock regression asserts the exact safe path.
76. OpenHands' Codex ACP runner ignored the UI `max_iterations` value. The SDK
    therefore used its 500-iteration default; a real #944 holdout passed the
    requested limit of 40, reached 42 displayed iterations with no `proof.md`,
    and would otherwise have remained bounded only by the 30-minute timeout.
    Jobs now pass a clamped `AGENT_MONITOR_OPENHANDS_MAX_ITERATIONS`, and the
    subscription runner supplies it to SDK 1.21's native outer
    `max_iteration_per_run`. Environment propagation and 1--500 clamping are
    regression-tested. A deployed eight-iteration ACP smoke finished in five
    displayed iterations and 30.9s, used 7,507 / 89 tokens and $0.019657, and
    wrote a correct 2,229-byte smallest-tree counterexample with SHA-256
    `9387e84fa24bd601aa6932bd968bd685c835f8f0938d2e8207ab7f1bce1daa7d`.
    A production `max_iterations=1` probe then exposed a second layer: one
    OpenHands ACP step is an entire remote Codex turn, so the SDK outer limit
    still allowed 21 projected events and 177.0s of inner tool activity. The
    runner now also counts distinct ACP tool-call IDs (not their repeated
    pending/completed status updates) and sends the standard ACP session-cancel
    notification at the boundary. A first integration probe caught that ACP's
    decorated `cancel` method was not recognized by `AsyncExecutor`; an
    explicit async wrapper repaired it. The final deployed max-one probe
    terminated after 29.96s with eight projected nodes, the exact configured
    activity-limit error, no cancellation error, and no `codex exec` fallback.
    Its 985-byte crash-safe checkpoint was preserved but the run remained
    failed. Both native and inner limit termination are distinct hard failures
    and cannot enter the generic unbounded fallback; focused regressions cover
    deduplication, async cancellation, clamping, and no-fallback behavior.
77. OpenHands SDK usage contains prompt, completion, cache-read, cache-write,
    and reasoning tokens, but the ACP bridge emitted only prompt and
    completion. This made long source audits appear implausibly cheap and
    understated total context use. The bridge now preserves all five native
    fields in `turn.completed`; a focused regression checks their exact values.
78. IMProof's host research planner appended every stderr line to the research
    packet even when the planner exited zero and returned valid structured
    JSON. A recoverable one-time Codex model-cache migration warning therefore
    appeared as `PLANNER_DIAGNOSTIC` in a successful #684 run although the
    cache had already self-healed. The runner now records the planner return
    code and includes stderr only when planning actually failed or produced no
    plan. A regression supplies valid JSON, exit zero, and recoverable stderr
    and asserts that no false diagnostic reaches the packet.
79. Coverage treated every incomplete Lean file as wholly unverified, erasing
    kernel-checked helper theorems whenever an unrelated open `main` contained
    `sorry`; a less careful repair could instead green the admitted theorem or
    anything depending on it. The bridge now parses declarations and executable
    gap lines, excludes local axioms/opaque/unsafe declarations, closes that bad
    set transitively over the Lean dependency graph, and requires a strong
    theorem-label match before granting partial coverage. Overall
    `formal_verified` stays false. A regression includes one independent helper,
    one admitted result, one theorem depending on that admission, and an admitted
    main; only the independent helper is green. The real post-HITL #388 pipeline
    now reports partial 2/18 for exactly `earlier_block_longer` and the certified
    numerical solution, while the admitted unrestricted theorem remains gray.
80. UCLA's PDF downloader retried deterministic HTTP 404 responses four times
    with 5/15/45-second sleeps before its compatibility fallback. The #524
    holdout exposed a real old-style arXiv URL that wasted 65 seconds this way.
    Permanent HTTP 4xx responses now skip the retry backoff and go directly to
    one bounded curl fallback; 408, 425, 429, server errors, and transport errors
    retain the transient retry policy. A mocked regression requires one URL
    attempt, one fallback, and zero sleeps for HTTP 404.

81. Multiple human messages queued during one active run were each already
    stored as session problems, but the automatic follow-up joined them with a
    blank line and `_execute_continue` appended that joined text as another new
    problem. The OpenClaude #886 prompt therefore repeated both corrections in
    a synthetic fourth problem. Queued launches now mark their constituent
    problems as already recorded; direct continuations still append genuinely
    new guidance. Regressions verify ordered exactly-once queue consumption and
    absence of the combined duplicate.
82. The partial-Lean bridge correctly found admission-independent arithmetic
    helpers but refused to green two #886 nodes because labels such as
    `Fourth-difference defect` versus `fourth_difference_identity` scored 2/3,
    just below its 0.72 label threshold. The conservative extension accepts
    that near-exact label only when at least two identical multi-digit integers
    also occur in both statements. A regression greens an exact
    `4455*4480=19958400` certificate while rejecting a similarly labelled item
    sharing only `25`. The real #886 monitor now reports partial 2/18; the
    admitted main remains gray.
83. The source starter `lean-check` had been repaired to recognize both
    `lakefile.toml` and `lakefile.lean`, but an existing persisted Library copy
    still contained the old TOML-only condition. It bypassed the configured
    mathlib project and falsely reported `unknown module prefix Mathlib`.
    Seeding now migrates only that exact legacy condition, preserving arbitrary
    user edits. The rematerialized #886 helper uses `lake env lean`, exits zero,
    and reports only the genuine `sorry` in `main`; a regression covers the
    compatibility migration.
84. A completed continuation replaced the monitor JSON produced by the initial
    run, so Conversation retained the full session while Monitor and DAG showed
    only the latest follow-up's agents, edges, usage, and cost. Continuation
    merge now preserves all prior nodes and edges, gives new trace IDs a
    job-specific suffix, offsets round numbers, inserts one explicit `continue`
    bridge edge, sums usage/latency/cost, and records per-session lineage. A
    focused regression covers every invariant. The deployed #827 HITL smoke
    retained 60 initial nodes, added 14 unique continuation nodes, preserved
    sorted rounds, and exposed exact initial/continue lineage with cumulative
    totals.
85. The conservative partial-Lean bridge recognized exact textual theorem
    labels and distinctive multi-digit arithmetic certificates, but could not
    connect the informal label `Exact value n_3` to Lean's conventional
    `nk_three_eq_three : Nk 3 3`; it therefore hid a genuinely kernel-checked
    helper from the green statement markers. The matcher now normalizes only
    exact `n_i=j` and `Nk i j` signatures with identical indices and values.
    A regression verifies the exact claim while rejecting both an inequality
    and the wrong value. The deployed #827 pipeline now reports partial 1/20
    for exactly `n_3=3`, while the admitted general bound remains unverified.
86. OpenClaw could append a valid assistant terminal event with
    `stopReason=stop` and still leave both `openclaw` and `openclaw-agent`
    sleeping indefinitely, so the console continued to display `running` and
    a later manual termination misclassified the logically completed session
    as failed. The subscription wrapper now snapshots existing transcript
    offsets, recognizes only a newly appended nonempty assistant stop event,
    allows a bounded grace period, and then terminates the whole child process
    group while returning the recorded terminal payload as success. Stale
    terminal events, `toolUse` events, signal forwarding, and a deliberately
    hung fake child are regression-tested. The deployed #552 continuation
    ignored the old stop, processed all reviewer feedback, emitted a fresh stop,
    reached `finished` 13 seconds later, and left no orphan process.
87. A manual Lean `Compile`/`Check`/`Audit` or formal HITL revision updated
    `lean_verify.json`, but Monitor retained the pre-edit Lean result, formal
    DAG, and coverage until the entire proving pipeline was run again. A new
    coalescing formal-only worker now rereads the checked Lean cache, rebuilds
    only the formal DAG and coverage, preserves the informal proof/DAG, and
    never starts another proving harness. Production polling exposed a second
    boundary: orphan recovery knew only the initial pipeline thread and marked
    a live formal refresh interrupted. It now treats either worker type as
    active. On the real false-claim control, repeated page polling left the
    refresh running; it completed from stale `Lean failed`, 13 old formal
    nodes, and 0/13 coverage to `Lean verified`, 15 formal nodes, and faithful
    8/13 coverage after Audit. Focused tests cover both formal-only preservation
    and the live-refresh reconciliation race.


88. IMProof emitted `agent.start` placeholders before `model.call`, but the live
    builder did not consume those placeholders and job heartbeats could replace
    a richer cached monitor with a sparse snapshot. Active author/critic cards
    consequently appeared empty or duplicated. Placeholder consumption and
    rich-cache preservation now keep live roles and activity stable; four
    production samples showed the author transition without duplicate nodes.
89. HITL on an IMProof run fell through the generic continuation path and
    silently launched Hermes, violating the selected-harness invariant. A native
    IMProof continuation path now retains the author--critic workflow and its
    production continuation created native isolated console directories.
90. That native continuation materialized only library/agent context and an
    8KB prompt excerpt, so the reviewer lacked the full prior artifact. It now
    copies the complete root `proof.md`/`proof.tex` into the isolated native
    workspace; the #195 continuation received all 12,827 bytes with an exact
    SHA-256 match.
91. IMProof artifact promotion preferred a stale root file and ignored a newer
    nested console answer. Promotion now chooses the newest candidate across
    root and nested workspaces while avoiding self-copy. The deployed #195 root
    now matches the newest final artifact byte-for-byte.
92. During a native continuation, live monitor refreshes temporarily displayed
    only the current session until the terminal merge restored earlier agents.
    The live flusher now merges every heartbeat with the prior baseline and
    job-specific lineage, so Conversation, Monitor, and DAG retain earlier
    nodes throughout the follow-up; a regression covers the live transition.
93. `citation-audit` recognized numeric/Markdown citations but missed LaTeX
    bibliography structure and never checked `\\cite` keys against `\\bibitem`
    keys. A Python parser now recognizes both formats, rejects unresolved keys,
    and migrates only the exact legacy starter block so user edits survive. The
    rematerialized #195 tool resolves all three final keys.
94. Formal HITL generated two historical Mathlib imports absent from the
    installed 4.14 tree: `Mathlib.Data.Int.Basic` and
    `Mathlib.Data.Fin.Interval`. Import normalization now maps them to
    `Mathlib.Data.Int.Order.Basic` and `Mathlib.Order.Fin.Basic`; a focused
    regression covers both compatibility aliases and the repaired #195 helper
    checks successfully.
95. Partial coverage already protected admission-independent helpers, but the
    conventional Lean label `forcedAP_three` did not meet the textual threshold
    for the informal label `Every permutation forces three`. Matching now stems
    only the force/forced/forces variants and accepts a candidate label only
    when at least two normalized formal-label tokens are wholly contained in
    the informal label. The real #195 bridge marks exactly `forcedAP_three`
    green while leaving admitted `ForcedAP 4` gray; focused adversarial
    regressions retain the previous numeric and indexed-value protections.

96. Informal DAG parsing rejected unescaped `\\underbrace`, while TeX such
    as `\\forall` or `\\neq` was worse: JSON accepted `\\f`/`\\n`
    and silently produced control characters. A narrow known-command repair
    now runs before parsing, preserves a legitimate newline followed by
    `next`, and still rejects malformed commas/braces. The mixed escape and
    malformed-structure regression passes, and post-fix #944 completed with
    13 nodes.
97. Direct IMProof Codex readers used bubblewrap on this Linux host and trapped
    before local file inspection. Read-only IMProof calls now enable legacy
    Landlock by default with an explicit opt-out. The vendored command-builder
    regression passes, while the production continuation retained its entire
    prior artifact and lineage.
98. Hermes could mark a continuation finished merely because a stale nonempty
    `proof.md` existed, even when the final response explicitly said the
    update failed and embedded the unchanged draft. Failure-language detection
    now covers the production `could not update [proof.md]` form and rejects
    an identical embedded artifact. A focused regression preserves the old
    file but returns no successful proof.
99. Legacy Landlock and Codex's direct `workspace-write` enforcement are
    incompatible on this host. Hermes now runs the embedded Linux app-server
    as `use_legacy_landlock + read-only`; the trusted monitor persists only a
    substantive explicit `--- proof.md ---` artifact that differs from the
    old file. Scoped-network mode remains explicit and fail-closed, and no
    danger-full-access mode is used. The deployed #944 continuation performed
    11 tools, changed the proof hash, and finished normally.
100. The formal #944 helper was kernel-checked but coverage preferred the short
    `deleteEdge` definition over the semantically exact
    `deleteEdge_..._iff_...same_color` theorem. A narrow signature now requires
    all of delete-edge, 3-coloring, same endpoints, and a biconditional before
    partial verification. A one-way near-match stays gray; the real #944 bridge
    now marks exactly 1/13 statements green.

101. A stale Hermes artifact containing a literal `To be refined` could still
    finish if the model returned only a short handoff, and a changed fallback
    could remain incomplete while satisfying the byte-length/difference gate.
    Explicit placeholder markers now make both existing and embedded artifacts
    incomplete. Two regressions reject the short-handoff and changed-placeholder
    variants while preserving the complete changed-artifact success path.

102. The live ignored `.env` and user database were owner-writable but
    group/other-readable (664/644), exposing provider credentials or account
    state to unrelated local users. Both are now owner-only (600), remain owned
    by the service's `ubuntu` account, and the authenticated service stayed
    healthy. Settings writes, database initialization, and `setup.sh` now
    enforce 600 persistently; two regressions begin with deliberately loose
    644 files and verify automatic repair. No secret contents were read or
    copied during this metadata fix.

103. Direct application responses lacked anti-framing protections; the public
    proxy happened to add MIME-sniffing and referrer headers but did not close
    framing or base-tag/plugin injection surfaces. The response helper now
    supplies `X-Frame-Options: SAMEORIGIN`, `nosniff`, `no-referrer`, and a
    narrow CSP (`frame-ancestors 'self'`, `base-uri`, `object-src`) that leaves
    existing inline scripts and styles unaffected. The first `DENY` draft also
    blocked the console's own Monitor iframe and produced a blank broken-frame
    page; the same-origin correction was deployed immediately. A regression
    verifies every directive, while the real authenticated `/monitor` returns
    HTTP 200 and 103 KB of dashboard HTML inside the console.

104. Workspace file/PDF containment used a raw string-prefix test. A sibling
    such as `run_evil` therefore looked like a child of `run`, allowing a
    crafted relative path (or an external symlink) to cross run boundaries.
    All previews, listings, file reads, and PDF compilation now resolve through
    `Path.is_relative_to` against the real workspace root; directories are also
    rejected as PDF sources. The regression covers an exact prefix collision,
    an external symlink, and the directory boundary. A deployed authenticated
    escape request now returns HTTP 400 `bad path`.

105. The unauthenticated static route joined the request suffix directly to
    each asset root without the workspace containment check. A harmless probe
    of `/static/../__init__.py` returned the exact source file (matching SHA),
    demonstrating arbitrary readable-file disclosure without touching any
    secret. Static lookup now resolves through the same real-root guard and
    rejects both traversal and external symlinks. The identical deployed probe
    now returns HTTP 404, while public login and the Monitor iframe remain 200.

106. `/api/runs` filtered Monitor runs by owner, but the legacy
    `/api/call_detail` endpoint looked up arbitrary trace IDs without checking
    the run embedded before `::`. It could expose another user's prompt,
    thinking, or output to an authenticated caller who knew a trace ID. The
    endpoint now verifies the same run-ownership predicate before any lookup;
    a regression proves a foreign trace returns 404 without calling the detail
    loader. Workspace export also re-resolves every text file immediately
    before reading, closing a list/read symlink race.

107. Human-in-the-loop corrections remained correctly preserved in session
    history but Sandbox rendered every `problems` entry as a new Problem card,
    duplicating reviewer messages outside HITL (and inflating run titles).
    Sandbox and session-title presentation now omit sources `human`, `hitl`,
    and `feedback`, while the full problem notebook, Conversation, Monitor,
    and continuation prompt retain them. A regression covers both title and UI
    filtering; all six served inline scripts compile successfully.

108. The live `proof-strategies` and `literature-search` packages had been
    deliberately enriched during evaluation (including one reference each),
    but fresh installs still seeded their older bodies and no reference files.
    The validated bodies and reference assets are now source-controlled starter
    packages. The first regression exposed Python string escape corruption of
    TeX commands such as `rceil`; raw Markdown literals fixed it. A
    warning-strict test now recreates both packages in an empty agent home, and
    all 5/5 live skill bodies exactly match the fresh-install seeds. Runs do not
    auto-create new skill names; the live set remains five enabled skills and
    six typed tools.

109. Sandbox correctly hid reviewer/HITL entries after Bug 107, but an older
    evaluation path could persist `source: human` guidance only in the session
    problem ledger, without a matching user event in `human_chat.jsonl`.
    Hiding those cards would therefore make the guidance invisible rather than
    move it. Chat loading now non-destructively recovers missing human/HITL/
    feedback entries from the run record, marks them as recovered, and dedupes
    exact user messages already in the chat file. A regression verifies both
    the recovered Conversation/HITL view and that the persisted chat file is
    not rewritten. An aggregate read-only audit of 190 preserved run records
    found 25 sessions with 46 unique human/HITL/feedback entries: zero were
    missing from the resulting chat view, and 12 required non-destructive
    session-ledger recovery.

110. A newly registered account could finish Codex OAuth successfully while
    still seeing `Configure API provider`: Codex writes OAuth tokens before its
    first `models_cache.json`, so connected first-login accounts exposed zero
    models, and the successful login poll did not reload model settings. A
    connected account now receives the current seven-model Codex bootstrap
    list until its own cache is created; that per-account cache still takes
    precedence. Both status refresh and login polling now reload the selector
    immediately on connection, while the Codex toggle still switches to only
    configured API models. Three real cache-less connected accounts now report
    seven Codex models and a valid default without inheriting API keys.

The regression suite currently contains 169 tests; the most recent full run
passed 169/169 after code Bugs 76--110; deployment hardening 102 was also
verified by owner/mode metadata and local/public HTTP 200 checks after restart.
A final post-restart smoke on 2026-08-22 re-ran the exact Sandbox
`problemItems` function against a persisted four-entry session
(`initial,human,human,human`): all four entries remained in session history but
only the initial theorem produced a Sandbox Problem card. The authenticated
Console and Monitor returned HTTP 200, Monitor returned its full dashboard,
unauthenticated run access returned 401, the static traversal probe returned
404, and no visible or persisted terminal run remained `running` or `queued`.
The live Library reported five enabled skills and six enabled typed tools;
Candidate S's 10,557-byte deployable body matched the live system prompt byte
for byte. Lean 4.14.0 remained available, and the preserved #195 and #944 Lean
artifacts still compiled with their single explicit `sorry` each, so neither
partial result is misreported as a solution. Eleven informal runtimes reported
ready; Danus remained explicitly unavailable pending its isolated official
Codex-branch adapter, and UCLA was readiness-checked but not run.
A deliberately broader
whole-tree collection also reached vendored Danus, DeepSeek, IMProof, and
Meta-Harness upstream suites and stopped at 26 missing optional-dependency
collection errors; those are outside the app's comparable `tests/` baseline.
`git diff --check` and bytecode compilation also pass.

### Final scope limits

- UCLA is intentionally excluded from the last evaluation window at the
  operator's request; no new UCLA run or claim is included in Bugs 96--110.
- A proposed Anthropic formal-audit smoke on the persisted #944 Lean artifact
  was not sent because outbound workspace-content egress was not authorized.
  Existing provider-routing regressions and the earlier clean Plain Anthropic
  production control remain the API evidence; no blocked request is counted as
  a successful model/harness combination.
- No tested artifact closes an open problem. The verified #195 and #944
  contributions are bounded reductions only, and every admitted or unaudited
  downstream claim remains visibly incomplete.
