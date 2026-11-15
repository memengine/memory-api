# Annotate the offline demo data shapes

Suggested label: `good first issue`

## Description

The demo helpers pass record and visibility dictionaries between scripts without a typed shape. Editors cannot check field names such as `source_event_id`.

## Files to touch

`examples/demo/scenario.py`

## Acceptance criteria

Add lightweight types for record, pending state, and visibility and annotate extraction and retrieval. Run the demo and confirm its output is unchanged.
