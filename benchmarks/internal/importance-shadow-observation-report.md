# Development importance shadow observation

## Scope

- 40 newly designed development workflows; no benchmark or holdout cases.
- 200 extracted memories: 184 stored candidates and 16 pending candidates.
- All six memory categories represented.
- Existing production extraction path and configured `gpt-4o-mini` provider.
- Deterministic scores were observed only. They were not used for storage, retrieval, ranking, decay, lifecycle, or API output.
- The scorer was not tuned during the window.

The first observation artifact used an invalid derived latency calculation. Its score telemetry was consistent with this run, but its latency is superseded. The authoritative artifact is `importance-shadow-observation-development-v2.json`, which times the observer call directly.

## Aggregate telemetry

| Metric | Result |
|---|---:|
| Exact score agreement | 35.0% |
| Mean absolute score delta | 1.435 |
| Shadow above model | 112 (56.0%) |
| Shadow below model | 18 (9.0%) |
| Exact agreement | 70 (35.0%) |
| Observer mean latency | 1.348 ms |
| Observer median latency | 1.272 ms |
| Observer maximum latency | 3.998 ms |
| Observer failures/fallbacks | 0 |
| Extraction workflow errors | 0 |
| Active model scores unchanged | Yes |
| Provider calls | 40 |
| Provider tokens | 120,300 |
| Estimated provider cost | $0.02413 |

## Category disagreement

| Category | Count | Agreement | Mean absolute delta | Shadow above | Shadow below |
|---|---:|---:|---:|---:|---:|
| Expertise | 39 | 64.1% | 0.359 | 14 | 0 |
| Fact | 30 | 10.0% | 2.267 | 27 | 0 |
| Goal | 40 | 55.0% | 0.600 | 3 | 15 |
| Preference | 40 | 0.0% | 3.250 | 40 | 0 |
| Procedure | 25 | 80.0% | 0.400 | 2 | 3 |
| Relationship | 26 | 0.0% | 1.577 | 26 | 0 |

Pending agreement was 25.0% with mean absolute delta 2.188. Stored-candidate agreement was 35.9% with mean absolute delta 1.370.

The active model concentrated 154 of 200 scores at 5. The deterministic scorer produced a broader distribution but systematically raised facts, preferences, and relationships. Goals were more often lowered. Unlabelled shadow traffic cannot establish which score is correct, so this disagreement must not be treated as an activation win.

## Decision

Continue shadow observation. Do not begin controlled activation yet. Operational safety is strong, but agreement is too low and category-direction bias is too systematic. A subsequent natural-development window should add blinded rubric annotation for a representative disagreement sample, especially preferences, relationships, facts, and pending memories. That validation should compare both scorers to independent labels without changing either scorer during the window.
