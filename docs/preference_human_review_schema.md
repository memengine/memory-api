# Human preference review schema v1

This is an offline development-review format, not a model reply, memory write,
authority grant or production API. Its authoritative models are
`benchmarks/internal/preference_review_schema.py`: `ReviewWorksheet`,
`ReviewAnnotation` and `HumanFinding`. Evidence attribution, offered references,
input identity and completion are checked by
`benchmarks/internal/preference_review.py`. Unknown fields are rejected at every
model level. Nullable required fields must be present with `null`, not omitted.

Print the generated JSON Schema without loading any dataset:

```powershell
.venv\Scripts\python.exe -X utf8 -m benchmarks.internal.preference_review --schema
```

JSON Schema describes field shapes, but does not encode all cross-field or
source-context checks. Use the worksheet validator below, not JSON Schema alone.
The frozen model contract remains unchanged. Human judgments may describe
meanings that contract cannot express; descriptions never become quoted evidence.

## One non-null annotation

Every annotation requires these eight fields:

| Field | Type / allowed values | Rule |
| --- | --- | --- |
| `findings` | Array of 0–16 `HumanFinding` objects | Empty means no preference findings, not unreviewed. |
| `no_findings_reason` | `null` or enum below | Required non-null exactly when findings are empty. |
| `representability` | `representable`, `unrepresentable` | Whether the judgment fits the frozen model contract. |
| `representability_reason` | `null` or text | Required non-null exactly when unrepresentable. |
| `ambiguity` | `unambiguous`, `ambiguous` | Distinguish uncertainty from a definite interpretation. |
| `ambiguity_reason` | `null` or text | Required non-null exactly when ambiguous. |
| `language_quality` | `natural`, `needs_revision` | Record wording/naturalness problems separately from meaning. |
| `language_notes` | `null` or text | Required non-null exactly when needs_revision. |

Optional annotation fields:

- `alternative_interpretations`: array of distinct text strings; defaults to `[]`.
  Ambiguous judgments require 2–8 entries; unambiguous judgments require none.
- `notes`: text or `null`; defaults to `null`.

Text fields are trimmed, nonempty and at most 1,000 characters when non-null.
`no_findings_reason` allows exactly:
`not_preference`, `untrusted_source`, `ambiguous_reference`, `unknown_property`,
`unknown_scope`, `insufficient_evidence`, `unrepresentable`.
The reason `unrepresentable` also requires `representability: "unrepresentable"`.
Exact duplicate findings are rejected. A human can record partial findings in an
unrepresentable case instead of hiding recognized meaning in an empty array.

## One semantic finding

A `HumanFinding` requires all six fields:

| Field | Type / allowed values |
| --- | --- |
| `property_key` | `null` or 1–120 characters matching `[a-z][a-z0-9._-]*` |
| `scope_ref` | `null` or 1–64 characters matching `[a-z][a-z0-9_-]*` |
| `target_ref` | `null` or a reference with the same pattern as scope_ref |
| `stance` | `asserted`, `replacement`, `current_choice_unresolved`, `tentative_interest`, `reaffirmed`, `rejected_alternative`, `historical`, `unclear` |
| `evidence` | Array of 1–8 evidence objects |
| `value` | One evidence object or `null` |

Three optional fields default to `null`:

- `property_description`: required when property_key is null or is not offered
  by the case's catalog.
- `scope_description`: required when scope_ref is null. A non-null scope_ref
  must be an offered reference; describe an unsupported scope instead of
  inventing a reference.
- `value_description`: required for asserted/replacement when value is null.
  Record the interpreted meaning here when the user did not literally quote it.

Descriptions follow the same trimmed 1–1,000 character text rule. Non-null
target_ref must match a displayed target's property and scope, even for
unrepresentable judgments. Do not guess a target when the user is ambiguous.

Each evidence object has exactly two required fields and no optional fields:

```json
{"turn_index": 1, "quote": "Python"}
```

`turn_index` is a strict integer from 0 through 63 (not a boolean), indexing the
case's displayed turns. `quote` is an exact, nonempty 1–1,000 character substring
of the cited source text. Each evidence quote must occur uniquely in that turn;
duplicate evidence entries are rejected. Only typed eligible user turns can be
evidence: user role with kind `direct_user_input` or `client_assertion`.
Assistant/tool/document text and embedded claims such as `role=user` are not
eligible evidence. This is source attribution, not certification of user identity
or authority. A value quote must be contained within a cited evidence quote at
the same turn index; it cannot be copied from an assistant message.

For `representable` findings, the validator additionally requires an offered
property/scope, a target when one exists for that identity, at most one finding
per property/scope, and compatibility with the frozen `Assessment` model.
Asserted/replacement require a non-null quoted value of at most 256 characters;
replacement/current_choice_unresolved/reaffirmed require a target. At least one
evidence quote must satisfy the model's single-quote value rule. These checks
cannot prove that the stance or quoted value is semantically correct.

For `unrepresentable` judgments, the reviewer may use descriptive fields,
partial bindings and multi-turn evidence without forcing the meaning into an
`Assessment`. Source quotes, value containment and any supplied target/reference
must still pass attribution checks. Never invent quotes to make a case fit.

## Complete illustrative annotations

These examples show the format, not adjudicated ground truth. They are not
inserted into either blank worksheet. Judge every actual case independently.

### Asserted preference

This shape is source-valid for development case `d0001`, whose user turn 1
explicitly names Python:

```json
{
  "findings": [
    {
      "property_key": "examples.language",
      "scope_ref": "global",
      "target_ref": null,
      "stance": "asserted",
      "evidence": [
        {
          "turn_index": 1,
          "quote": "The second option, Python, should be my default for future programming examples."
        }
      ],
      "value": {"turn_index": 1, "quote": "Python"}
    }
  ],
  "no_findings_reason": null,
  "representability": "representable",
  "representability_reason": null,
  "ambiguity": "unambiguous",
  "ambiguity_reason": null,
  "language_quality": "natural",
  "language_notes": null
}
```

### No preference findings

Use a non-null object with an empty array and an appropriate reason. This
example's `not_preference` reason applies to an ordinary non-preference turn;
it must not be copied onto preference cases just to achieve completion.

```json
{
  "findings": [],
  "no_findings_reason": "not_preference",
  "representability": "representable",
  "representability_reason": null,
  "ambiguity": "unambiguous",
  "ambiguity_reason": null,
  "language_quality": "natural",
  "language_notes": null
}
```

### Ambiguous and unrepresentable

This shape is source-valid for `d0271`. It preserves a partial interpretation
without inventing a value quote or deciding the ambiguous scope:

```json
{
  "findings": [
    {
      "property_key": "examples.language",
      "scope_ref": null,
      "target_ref": null,
      "stance": "asserted",
      "evidence": [{"turn_index": 1, "quote": "Yes, use that as my default."}],
      "value": null,
      "scope_description": "General or Atlas, unspecified.",
      "value_description": "The setting referred to as 'that', without a quoted value."
    }
  ],
  "no_findings_reason": null,
  "representability": "unrepresentable",
  "representability_reason": "The adopted value and target scope are not identified in the user quote.",
  "ambiguity": "ambiguous",
  "ambiguity_reason": "Both general and Atlas entries were discussed.",
  "language_quality": "natural",
  "language_notes": null,
  "alternative_interpretations": [
    "Keep the general default.",
    "Keep the Atlas default."
  ]
}
```

## Worksheet metadata and completion

All eight top-level fields are required:

- `schema_version`: exactly `preference-human-review-v1`.
- `pack_id`: exactly `preference-semantic-development-v1`.
- `inputs_sha256_utf8_lf`: the manifest's 64-character lowercase hex input hash.
  Preserve it; do not replace it with a hash of the worksheet.
- `review_pass`: `a` or `b`, matching the template used.
- `reviewer_identity`: `null` while unassigned, otherwise a stable actual human
  reviewer identifier, trimmed 1–120 characters. A pseudonymous reviewer ID is
  acceptable; it must resolve to an actual reviewer in your review records.
- `reviewer_is_human`: JSON `null`, `true` or `false`, not a string. Set `true`
  only for a human-performed pass; an AI session must not claim to be human.
- `status`: `pending`, `in_progress` or `completed`.
- `annotations`: object containing exactly every one of the 360 development case
  IDs (`d0001` through `d0360`), each mapping to `null` or a ReviewAnnotation.

`pending` requires all annotations null. `in_progress` requires some filled and
some null. `completed` requires every annotation non-null. Both non-pending
statuses require nonempty reviewer_identity and reviewer_is_human exactly true.
An unrepresentable, ambiguous or needs_revision case is still a completed human
judgment, not an automatically accepted input for evaluation.

Older copies without schema_version must add that field before validation. Do
not mark unreviewed rows as no-preference, manufacture reviewer identities or
change input hashes to satisfy a check. Keep the committed templates blank.
Store result copies under `artifacts/preference-review/`, for example
`review-a.completed.json` and `review-b.completed.json`.

## Read-only validation commands

Run from `D:\memoryos\memory-api`. A completed single worksheet:

```powershell
.venv\Scripts\python.exe -X utf8 -m benchmarks.internal.preference_review --worksheet artifacts/preference-review/review-a.completed.json
```

An unfinished copy (does not pass completion):

```powershell
.venv\Scripts\python.exe -X utf8 -m benchmarks.internal.preference_review --worksheet artifacts/preference-review/review-a.in-progress.json --allow-incomplete
```

A completed a/b pair:

```powershell
.venv\Scripts\python.exe -X utf8 -m benchmarks.internal.preference_review --worksheet artifacts/preference-review/review-a.completed.json --other-worksheet artifacts/preference-review/review-b.completed.json
```

The pair check requires one pass a and one pass b, matching inputs/cases and
different declared reviewer identities (case-insensitive after trimming). It
does not compare their answers, adjudicate disagreements or establish independent
work. Keep answers private between reviewers until both passes finish.

Success exits 0; invalid shape, completion, input identity or attribution exits 1.
Worksheet files must be JSON under the repository's `artifacts/preference-review`
directory, at most 2 MiB. UTF-8 with or without BOM is accepted; duplicate JSON
keys are rejected. Private holdout paths are rejected before reading. Validation
loads only the fixed public development pack and does not write files, score
outputs, call a provider or alter production.

The report deliberately leaves human_identity_verified,
semantic_correctness_verified, review_independence_verified,
adjudication_complete, provider_execution_approved and
production_enablement_allowed false. Structural validity and declared human
completion are not authenticated identity, correct labels or release approval.
