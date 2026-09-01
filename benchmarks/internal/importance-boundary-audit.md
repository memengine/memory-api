# Importance scoring boundary audit

Scope: the five remaining formula-calibration failures in the private development set. Holdout was not read. Feature derivation, formula, production code, and provider behavior were not changed.

## Contract decision

Legacy annotations are integer rubric anchors. The loader deliberately converts anchor `n` to the closed interval `[n - 0.5, n + 0.5]`. Metric comparison uses the scorer's numeric output directly; it does not round interval endpoints or widen them to neighboring integers. Therefore integer score 8 is outside a 7-anchor interval of `[6.5, 7.5]`. This preserves exact anchor meaning and avoids silently accepting two adjacent integer anchors.

## Case decisions

| Case | Annotation | Decision |
|---|---:|---|
| Possible future data-role goal | 3 | Keep. It is explicitly uncommitted future consideration. The score-5 provider proposition is a frozen derivation miss. |
| Short replies today | 1 | Keep. It is explicitly session-scoped and matches the canonical temporary/almost-worthless anchor. Formula score 3 remains a real calibration limitation. |
| Product-manager role | 7 | Keep. Stable professional identity is identity-level, not foundational. Integer 8 is correctly outside the interval. |
| Onboarding/activation work focus | 7 | Keep. It is durable primary work context. Integer 8 is correctly outside the interval. |
| Morning deep-work procedure | 3 | Keep. It matches the canonical occasionally useful work-pattern example. Formula score 5 remains a real interaction limitation. |

## Outcome

No annotations or evaluator contracts were normalized because none were inconsistent with the current rubric. The remaining failures must not be removed by widening benchmark ranges. The calibrated scorer remains benchmark-only.
