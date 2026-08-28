# LongMemEval Root-Cause Audit

Date: 2026-08-28  
Scope: public-development 30-case sample  
Status: diagnosis only; no production integration decision

## Executive conclusion

The weak preview result is not caused by one bad semantic threshold. It is produced by
four separate boundaries:

1. MemoryOS production memory and LongMemEval measure different memory planes.
2. Flat retrieval does not reliably collect exhaustive multi-session evidence.
3. Correct sessions are often mapped to the wrong conversational chunk.
4. The preview answer prompt is invalid for recommendation/preference tasks and is too
   permissive toward instructions embedded in raw conversation evidence.

Further cutoff, Top-K, or token-cap tuning will not repair these boundaries.

## 1. Product-architecture mismatch

MemoryOS production memory is a governed durable-state system. Extracted memories enter
claims, conflict resolution, versioning, lifecycle, provenance, PostgreSQL, and Qdrant.
LongMemEval asks questions over detailed past conversations, including assistant statements,
completed events, multi-session counts, and temporal comparisons.

MemoryOS currently has no production episodic evidence plane. Extraction-job transcripts
are temporary and are not a durable searchable conversation store. The experimental
episodic index used in this evaluation is benchmark-only and bypasses the production
memory/claim lifecycle.

Therefore the current result cannot be described as the score of the shipping MemoryOS
architecture. It measures an experimental episodic retrieval prototype attached to the
LongMemEval adapter.

## 2. Retrieval-architecture failure

At Top-10, hybrid evidence-session recall reached 98.33%, but the multi-session furniture
case retrieved only two of four required sessions. This is a genuine retrieval miss.

The present algorithm is a flat chunk search followed by session aggregation. It is weak
for exhaustive questions such as counts, lists, comparisons, and temporal chains because:

- one globally ranked list allows similar filler to consume candidate positions;
- a query may require several semantically different events;
- lexical decomposition cannot infer that individual chairs, desks, shelves, or repairs
  are instances of the broader concept "pieces of furniture";
- there is no planned evidence-completeness loop that knows several events are required;
- there is no diversity or per-subquery allocation across sessions.

This is an episodic retrieval architecture gap, not a cutoff problem.

## 3. Session-to-chunk localization failure

The first paid preview used correct-session recall as a proxy for usable context. That was
insufficient:

- mean answer-turn coverage was only 54.70%;
- 18 incorrect answers had all expected sessions present;
- only four incorrect answers had every labelled answer turn in the supplied chunk context.

Bounded semantic-plus-lexical union and same-session neighbor expansion improved mean
answer-turn coverage to 89.05% and complete coverage to 78.57%. Raising the context cap
from 6,000 to 10,000 tokens produced no further improvement.

Five of the six remaining incomplete cases had the correct session in Top-10 but selected
the wrong area of that session:

| Case | Type | Failure |
|---|---|---|
| `cc5ded98` | knowledge update | answer at user turn 0; selected turns 5-9 |
| `c4a1ceb8` | multi-session | answer at user turn 0 in two sessions; later turns selected |
| `06878be2` | preference | required turn 8; selected turns 0-2 and 13-15 |
| `b46e15ee` | temporal | answer at user turn 0; selected turns 9-11 |
| `0bc8ad92` | temporal | answer at user turn 0; selected turns 7-11 |

Verbose assistant discussion and repeated query vocabulary outrank concise autobiographical
user statements. Role-aware chunk boundaries prevent speaker mixing but do not provide
source-role ranking. The architecture needs a second-stage, within-session locator with
strong provenance/speaker awareness; blind neighbor expansion is not sufficient.

## 4. Answer-evaluation harness defects

### Corrected defect: abstention semantics

The first evaluator treated `_abs` as requiring empty retrieval. LongMemEval abstention
cases still contain relevant evidence; the answer layer must inspect it and conclude that
the requested answer cannot be derived. This contract was corrected. The paid preview then
achieved 4/4 abstention accuracy.

### Unresolved defect: preference prompt

The answer prompt says to use only the memory context. Preference questions ask for new
recommendations personalized from remembered tastes. The model must use remembered
preferences plus its general knowledge. Under the existing instruction, saying "cannot be
answered" is defensible. The resulting 0/5 preference accuracy is therefore not clean
evidence of retrieval or product failure.

### Unresolved defect: untrusted raw conversation text

Raw retrieved chunks contain user and assistant instructions, questions, and generated
answers. They are inserted into one user prompt as ordinary text rather than isolated as
untrusted quoted evidence. Irrelevant instructional content can distract or redirect the
answerer. Four direct-answer cases returned "cannot answer" even though the answer-bearing
turn was present, including daily guitar practice and the conversation with Sarah.

### Judge status

The binary model judge is a diagnostic preview, not the upstream official evaluator. The
36.67% preview accuracy must not be used publicly.

## 5. What the experiments actually prove

Confirmed strengths:

- episodic indexing recovered assistant statements and detailed events that durable
  extraction intentionally omits;
- Top-10 hybrid session recall reached 98.33% on the frozen 30-case sample;
- abstention preview accuracy was 100%;
- provenance hashes, session IDs, dates, roles, and turn ranges were retained;
- cross-case leakage remained zero.

Confirmed weaknesses:

- no production episodic evidence architecture exists;
- exhaustive multi-session evidence retrieval is incomplete;
- user assertions are not sufficiently favored over verbose assistant content;
- correct-session retrieval does not guarantee correct-turn localization;
- raw evidence is not safely structured for the answer model;
- the answer prompt does not support preference personalization correctly.

## 6. Root-cause classification

| Boundary | Classification | Risk |
|---|---|---|
| Missing production episodic plane | Architecture gap | High |
| Missing events in exhaustive multi-session queries | Retrieval architecture gap | High |
| Wrong chunk inside correct session | Retrieval/context implementation gap | High |
| Preference answer instruction | Benchmark harness defect | High for score validity |
| Raw conversational instructions in context | Context safety/answer architecture gap | High |
| Earlier empty-abstention scoring | Corrected harness defect | Closed |
| Semantic cutoff and token cap | Not primary causes | Stop tuning |

## 7. Recommended decision

Do not run another paid 30-case evaluation yet, and do not integrate the benchmark episodic
prototype into production.

The next work should be an architecture decision, not another parameter experiment:

1. Decide whether MemoryOS intends to support a separately governed episodic evidence plane.
2. If yes, design its retention, privacy deletion, authorization, provenance, and lifecycle
   contract before implementation.
3. Design hierarchical retrieval: session discovery, within-session user/source-aware
   evidence localization, and multi-event completeness planning.
4. Repair the evaluation answer contract independently: quoted untrusted evidence and
   preference-aware use of general model knowledge.
5. Freeze a fresh evaluation partition before measuring improvements; the current 30 cases
   have now been repeatedly inspected and should be treated as development-only.

If MemoryOS does not intend to store episodic conversation evidence, stop LongMemEval work
and choose a public benchmark aligned with governed durable state, conflict resolution,
provenance, temporal validity, and multi-agent authorization.
