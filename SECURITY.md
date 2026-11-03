# Security policy

## Report privately

Send suspected vulnerabilities to **support@memoryo.dev**, subject
`MemoryOS security report`. If this repository offers **Security → Report a
vulnerability**, that private channel is also suitable. Its availability is a
repository setting, not established by this document.

Do not open public issues containing exploit details, credentials, customer data
or evidence of an unauthorized disclosure. Include:

- Affected commit/package version and component.
- Expected trust boundary, observed behavior and impact.
- Minimal synthetic reproduction, prerequisites and sanitized configuration.
- Whether real data may have been exposed and a safe follow-up contact.

Redact tokens, authorization headers, DSNs and personal data. Do not send database
dumps, live credentials or private acceptance inputs. If a secret is exposed,
revoke/rotate it with its issuer; removing a log or commit does not revoke it.

## Release scope

The current offering is a local developer beta, with published Python 0.1.2 and
TypeScript 0.1.3 clients. There is no production security certification,
supported-release maintenance matrix, response SLA or bug-bounty commitment.
Report issues against older versions too, identifying the exact version.
Passing tests do not establish that every trust boundary is safe. Report observed
security violations with the affected version and a minimal reproduction.

Relevant concerns include cross-tenant disclosure, credential theft, authority
escalation, unauthorized mutation and corruption of provenance/revision history.

## Deployment and application boundary

- The beta uses public local test passwords. Keep its API on loopback; do not
  expose it as a hosted deployment or reuse its keys outside disposable testing.
- Keep API/provider keys server-side. Never commit `.env`, bootstrap credentials,
  database state or tokens, and do not place admin keys in browser/agent tools.
- Keys have tenant scope and specific permissions. An admin operator key differs
  from a read/write service key; neither replaces customer user authentication.
- The application authorizes owner/project access. Do not assume every tenant
  endpoint supplies per-human isolation, or that a model's claimed role is trusted.
- Honor visibility holds before model/tool execution. Stored provenance does not
  prove input truth; memory governance is not general prompt-injection protection.

Investigate only systems/data you are authorized to access. Use local synthetic
identities and do not probe hosted systems without permission. Coordinate private
disclosure before publishing a reproduction. Deployment definitions under
`.github/archived-workflows/` are historical, not authorization to run AWS.
