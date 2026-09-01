# Deterministic importance feature-recall diagnosis

Scope: original development artifact only. Holdout was not loaded. No provider calls were made and the scorer was not changed.

## Method

Each matched provider memory was compared with its canonical expected proposition using the same supporting conversation turns. Both were passed through the frozen deterministic feature extractor. This separates provider-normalization loss from feature-model/rubric coverage. Because the dataset contains importance ranges rather than explicit feature labels, canonical feature signals are a diagnostic proxy, not ground-truth feature annotations.

## Results

- Matched memories: 29
- Canonical-proposition range accuracy: 37.93%
- Provider-normalized range accuracy: 34.48%
- Within range: 10
- Feature-model or annotation gap: 18
- Provider normalization/category loss: 1
- Mixed failures: 0

Only goal commitment lost a canonical signal during normalization (one case). When canonical signals existed, temporal scope, identity breadth, and consequence-of-forgetting had 100% exact preservation. The dominant failure is therefore feature derivation/formula coverage, not extraction paraphrasing.

The canonical extractor produced no non-neutral expertise-maturity, procedure-durability, or preference-scope signals in the original set. Failures concentrate in legacy cases (16 of 20 legacy matches fail versus 3 of 9 modern matches).

## Failure patterns

1. Stable profile facts are insufficiently recognized: education stage, leadership role, company role, and executive identity remain near the neutral score.
2. Durable preferences without explicit words such as “always” or “across every project” receive no preference-scope signal.
3. Goal commitment misses deadlines, “within the next year,” imminent exams, and business ownership phrased without the narrow active/committed vocabulary.
4. Operational facts and short-lived needs lack a general low-consequence/expiry representation, so they remain near 5.
5. Detected temporal signals are sometimes too weak to reach rubric extremes; a today-only preference scores 4 despite an expected 1–1.5 range.
6. Evidence is turn-level rather than proposition-level. A composite turn can attach temporary cues to a durable memory, as seen in the generalization pack’s home-location miss.

## Recommendation

Do not integrate or run shadow mode. The next isolated experiment should improve deterministic feature derivation only, leaving weights and final formula frozen. Add general phrase families for stable identity, declared durable preferences, commitment/deadline state, recurring procedures, and operational expiry. Also derive local evidence clauses around the memory proposition rather than treating an entire multi-memory turn as one feature context. Predefine success as higher canonical-proposition coverage and original-set accuracy without reducing the frozen generalization result below its acceptance threshold. Weight calibration should be a separate later experiment.
