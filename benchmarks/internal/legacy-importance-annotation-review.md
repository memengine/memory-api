# Legacy development importance annotation review

Scope: the 16 private legacy development cases in `tests/evals/general_extraction_cases`. Holdout was not read or changed.

The annotations are normalized to the canonical rubric in `docs/extraction_spec.md` rather than to observed provider output:

- 1: temporary or almost worthless beyond the current session.
- 3: occasionally useful context.
- 5: consistently useful primary context.
- 7: identity-level context or an immediate priority that shapes most relevant responses.
- 9: foundational identity that must almost always be retrieved.

## Reviewed changes

| Case / memory | Old | New | Rubric rationale |
|---|---:|---:|---|
| borderline language switching | 6 | 3 | Tentative preference, occasionally useful. |
| possible future data-role goal | 5 | 3 | Uncommitted future consideration. |
| short replies today | 4.5 | 1 | Explicitly temporary and session-scoped. |
| class 12 identity | 8 | 7 | Core educational context. |
| weak organic chemistry mechanisms | 8 | 7 | Directly shapes tutoring responses. |
| board exam in 34 days | 8.5 | 7 | Immediate high-priority context, not foundational identity. |
| leads integrations pod | 7.5 | 7 | Stable role that shapes work context. |
| team owns Slack/webhooks | 7 | 5 | Consistently useful tool/ownership context. |
| reduce onboarding drop-off | 8 | 7 | Immediate primary work goal. |
| billing failure after upgrade | 7.5 | 3 | Temporary support context that should decay after resolution. |
| workspace integrations | 6.5 | 5 | Consistently useful product context. |
| invoice fix before review | 8 | 3 | Temporary operational need. |
| learning async Python | 6.5 | 5 | Useful current learning context, not identity-level. |
| product-manager role | 8 | 7 | Stable professional identity. |
| data-scientist career goal | 9 | 7 | Strong long-term goal, but not foundational identity. |
| concise Python-first explanations | 7.5 | 7 | Durable preference that directly shapes responses. |
| morning deep-work schedule | 7 | 3 | Matches the rubric's score-3 work-pattern example. |
| manager Maya | 7.5 | 7 | Important named professional relationship. |

Unchanged after review: FastAPI/SQLAlchemy expertise (7), onboarding/activation work focus (7), plus all negative cases with no expected memories.

These remain point annotations because the legacy dataset contract stores a single score. The internal benchmark converts each point to a ±0.5 acceptance interval; broadening that evaluator rule is intentionally outside this annotation-only cleanup.

## Rescore impact

The three accepted post-extraction evidence runs were rescored locally without provider calls. Importance-range accuracy changed from a 30.25% mean to 46.51% (run values: 44.83%, 46.43%, 48.28%). The normalized result contains 40 within-range, 29 under-scored, and 17 over-scored matched observations. This confirms that annotation inflation explained part, but not all, of the weakness.
