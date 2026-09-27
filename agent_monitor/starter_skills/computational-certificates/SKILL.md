---
name: computational-certificates
description: Produce exact, bounded, reproducible computational evidence for mathematical claims. Use for finite searches, boundary arithmetic, divisor or case enumerations, counterexample searches, numerical implications, and independent checks of proof lemmas.
---

# Computational certificates

1. State the claim being checked, the complete finite scope, and why that scope is relevant.
2. Prefer exact integers and rationals. Do not use floating equality as a proof certificate.
3. Use exact-math-certificate for concrete identities and inequalities when available.
4. Use bounded-counterexample-search for a declared finite Cartesian range when available.
5. Record the input, method, checked-case count, result, limitations, and certificate digest.
6. Independently rederive pivotal arithmetic or verify it with a second method.
7. Substitute every retained candidate back into its defining condition and test the first excluded boundary.
8. Rerun downstream obligations after any failed micro-check.

Treat exhaustive finite output as a theorem only about its declared scope. Never extrapolate it to an unrestricted or asymptotic statement.
