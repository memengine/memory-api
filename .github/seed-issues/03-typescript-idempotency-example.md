# Show event replay in a short TypeScript SDK example

Suggested label: `good first issue`

## Description

The TypeScript contract test verifies that `idempotencyKey` becomes the `Idempotency-Key` header. Add a runnable example isolating that behavior for someone connecting a Billing webhook.

## Files to touch

`sdk/typescript/README.md`, `sdk/typescript/examples/`

## Acceptance criteria

Use a synthetic event and a mocked fetch, run the SDK add call twice with the same event ID, and print the header on both requests. Keep the example free of live credentials.
