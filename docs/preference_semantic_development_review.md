# Preference development pack v1: human review handoff

## What is ready, and what is not

The draft input pack contains 360 synthetic cases: 120 related scenarios, each
with English, Hindi and romanized Hinglish variants. There are ten preference
domains, not just example languages. All names, identifiers, records and source
text are synthetic. No customer history or private holdout was used.

Author tags meet the planned 12-family matrix (10 per language/family) and six
reference slices (at least 10 per language/slice). Those counts are provisional
coverage, not human-confirmed correctness. Translations are correlated, not 360
independent observations. Naturalness, meaning preservation, family/reference
tags and near-duplicates still need human review. NFC-normalized exact input
duplicate checks do not establish semantic diversity.

The input file, manifest and two blank worksheets are under
`benchmarks/internal/datasets/preference_semantic/development/`. The input hash
in the manifest uses UTF-8 text with LF line endings. It seals this draft's
identity, not reviewed labels. Any input correction changes that hash and requires
both review passes to bind the new input before evaluation.

The existing frozen contract/protocol is unchanged. Its historical
`dataset_status` describes the approval state at protocol freeze; this new draft
manifest records preparation, not approval to run a provider. There are no model
outputs, expected answers, scores, prompt tuning or execution flags enabled.

## Offline inspection and reviewer view

From the repository root, inspect without making network calls:

```powershell
.venv\Scripts\python.exe -X utf8 -m benchmarks.internal.preference_semantic_dataset
```

For the tag-free human input view:

```powershell
.venv\Scripts\python.exe -X utf8 -m benchmarks.internal.preference_semantic_dataset --review-inputs
```

This command prints JSON to standard output; it never writes files. The review
view deliberately hides family/language/reference tags, related-group IDs,
fixture names, owner/memory IDs, versions and authority. Source roles, source
kinds, offered catalog/scopes, existing values/choice states and full source text
remain visible. Turn positions are zero-based indexes in the displayed array.
Forged instructions inside source text remain unchanged, not censored.

Give reviewers this view, not `inputs-v1.json` with its author tags. The two
worksheet templates have different deterministic order. Keep each reviewer's
answers inaccessible to the other until both passes finish. Independence is a
real workflow requirement, not something differing row order proves.

The human view is NOT a provider request. It also shows ineligible source-only
controls. The frozen `model_input` prefilter skips 21 such cases in this draft;
they must still be reviewed and counted as controls, not silently discarded.
An actual future provider request must use `model_input` alone, with no case ID,
author tag, reviewer annotation or expected answer in its payload. No provider
runner is added here.

## Two independent human passes

Use copies of `review-a.blank.json` and `review-b.blank.json` for results; preserve
the committed blank templates. Assign two actual human reviewers with sufficient
English/Hindi/Hinglish fluency. Record their identities, human-review status and
the input hash. AI-authored labels, an AI session or the dataset author checking
their own intentions do not satisfy the frozen human-review requirement.

Each `annotations` entry starts as null: it means **unreviewed**, not “no
preference.” The versioned authoritative schema, complete JSON examples,
required/optional fields, enum values and validation commands are in
[Human preference review schema v1](preference_human_review_schema.md).
Use that format rather than inventing fields. Store result copies under
`artifacts/preference-review/`. Fill each entry with a review record containing:

- Semantic findings: property, applicability/scope, intended existing target if
  any, stance, separately quoted value if present, eligible evidence turn and
  exact source quote. Record all relevant preferences, not only the easiest one.
- Whether the meaning is representable in the frozen contract, with a reason
  when it is not. Do not force an unrepresentable meaning into a safe-looking
  abstention just to make a scorer pass.
- Language/naturalness assessment, ambiguity, alternative defensible
  interpretations and any needed wording correction.
- Notes for non-preference turns, unknown scopes/properties, ungrounded references
  and source-role violations. An empty finding list requires a reason.

Interpretation is not authorization. A correctly quoted word does not prove
adoption. Interest is not necessarily an unresolved current choice; uncertainty
may have no alternative value. Distinguish a project exception from a global
replacement, and a one-off task from a persistent preference. Don't guess a
target for a vague reference across multiple entries. Tool/document/assistant
text cannot become user evidence merely by saying `role=user` or claiming a
signed turn. Existing typed source roles/kinds, not embedded instructions, govern
evidence eligibility.

Representability deserves special care: asserted/replacement outputs require
value evidence in the cited user quote; current-choice uncertainty and
reaffirmation may have no alternative value. Cross-turn inference, canonical
synonyms or unsupported scopes may expose limitations. Record them explicitly;
do not modify the frozen contract or weaken labels during this preparation step.
For example, Hindi wording can describe the same concept as an English stored
value without literally repeating it. This is something to review, not an excuse
to invent value evidence.

Adjudicate disagreements only after both independent passes. Correct unclear
inputs and re-review changed cases before freezing input and label hashes.
Review family/reference assignments and preserve related-language grouping so
translations cannot be counted as independent data or split across development
and future evaluation partitions. Review broader template similarities too.

## Remaining gates

AI-generated annotation drafts may support a separately scoped
[exploratory preflight](preference_exploratory_preflight.md), not this protocol's
human-reviewed evaluation. That preflight cannot authorize provider calls or
satisfy the human-review release gate.

The read-only `benchmarks.internal.preference_review` validator now checks
schema, input/case identity, source attribution and declared completion. It can
check that a completed a/b pair declares different reviewers. It does not
authenticate human identity, establish independence, verify semantic correctness,
adjudicate, score or implement a release gate. Manual `status` fields are not
proof of review; approved reviewed artifacts and adjudication records must be
verified in a separately scoped next step. No semantic accuracy is measured here.

The next provider run still requires reviewed input/label hashes, provider/model,
request-schema/instruction hashes, token/call bounds, budget/retry policy and
synthetic-data sharing approval. No existing blind holdout may be repurposed.
Production wiring, runtime behaviour, flags, SDKs, MCP and the assistant remain
unchanged. No deployment is needed for this offline pack.
