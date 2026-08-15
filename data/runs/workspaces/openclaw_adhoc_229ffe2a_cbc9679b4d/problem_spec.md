# Canonical Problem Specification

## Exact claim

Prove the closed equality

$$1+1=2$$

in the natural numbers $\mathbb N$.

## Quantifiers and dependencies

There are no free variables and hence no nontrivial quantifiers. The symbols $0,1,2,+$ are interpreted in the standard recursively defined natural-number arithmetic described below.

## Definitions and conventions

Let $0$ be the initial natural number and let $S$ denote successor. Define

- $1:=S(0)$;
- $2:=S(1)$;
- addition recursively in its second argument by $a+0:=a$ and $a+S(b):=S(a+b)$ for all $a,b\in\mathbb N$.

No appeal to decimal notation or cardinal arithmetic is intended beyond these definitions.

## Permitted assumptions

The defining equations above for the natural numbers and addition are permitted. Equality permits substitution of equals for equals.

## Standard for completion

A complete solution must derive $1+1=2$ by finitely many applications of the stated definitions, with no appeal to the desired equality.

## Weaker statements that do not solve the task

- Numerical verification by a calculator.
- A proof only modulo some integer.
- A proof for cardinalities without connecting cardinal addition to the stated natural-number addition.
- Treating $1+1=2$ as an axiom.

## Consistency and small-case checks

The recursion gives $1+0=1$. Since $1=S(0)$, it then gives $1+1=S(1+0)=S(1)=2$.

## Verification checklist

- [x] Interpret all symbols in $\mathbb N$.
- [x] State the recursive clauses for addition.
- [x] Verify that the second summand $1$ has the form $S(0)$.
- [x] Apply the successor clause exactly once.
- [x] Apply the zero clause exactly once.
- [x] Identify $S(1)$ with $2$ by definition.
- [x] Check no division, limiting process, hidden positivity assumption, or converse is used.
