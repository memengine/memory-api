# MemoryOS delivery roadmap

This is the public delivery summary, not a commitment to dates or new features.
The current target is the [local developer beta](beta/README.md).

## Available foundation

- Backend ingestion/jobs, source attribution, history and governed retrieval.
- Pending service-correction visibility and explicit operator HTTP resolution.
- Isolated local setup with synthetic writer credentials.
- Published Python 0.1.2 and TypeScript 0.1.3 clients with visibility status.

Implementation and bounded checks do not establish every combination of these
paths; verify the complete integration before relying on it.

## Next delivery work

1. Verify the exact fresh-clone and published-SDK local integration instructions.
2. Complete one bounded ingestion-to-current-retrieval journey, with explicit
   provider-spend authorization where needed and first-failure evidence retained.
3. Obtain developer feedback about setup, custom policy code and operational
   effort; compare with an adequate SQL/application-logic implementation.
4. Decide pilot scope from those observations. Establish production operating
   requirements before considering hosted deployment.

No additional broad benchmark round, interpretation rollout, domain expansion or
framework adapter is a prerequisite by default. Address a reproducible blocker
or a specific user need before adding work. No demand, unique advantage or
production readiness is inferred from a synthetic demo.
