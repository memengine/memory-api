# Security policy

## Report privately

Please do **not** disclose suspected vulnerabilities, cross-tenant leaks,
credentials or real customer data in a public issue or pull request.

Email **support@memoryo.dev** with the subject `MemoryOS security report`.
If GitHub's private vulnerability reporting is enabled for this repository,
you may use **Security → Report a vulnerability** instead. This document does
not imply that repository setting is enabled.

Include:

- Affected commit/version and component.
- Expected trust boundary and the observed violation.
- Minimal synthetic reproduction and necessary configuration, without secrets.
- Impact, prerequisites and whether any real data may have been exposed.
- A safe way to contact you for follow-up.

Send only the minimum necessary information. Redact tokens, authorization
headers, DSNs, personal data and private evaluation cases. Do not send database
dumps, executable payloads or live credentials. If a credential is exposed,
revoke/rotate it through its issuer; deleting a GitHub run is not remediation.

## Scope and expectations

This is an early-stage project. There is no published supported-release matrix,
security-response SLA or bug-bounty commitment. Acknowledge uncertainty rather
than assuming a demonstration or passing test establishes production safety.

Tenant isolation, credential handling, authority escalation, unauthorized state
changes and evidence/provenance corruption are relevant security concerns.
A tenant service key is administrative within its own tenant; the documented
customer-application identity boundary still applies.

Investigate only systems and data you are authorized to access. Use local,
synthetic tenants and do not test a hosted endpoint without permission. Coordinate
disclosure privately before publishing exploit details.
