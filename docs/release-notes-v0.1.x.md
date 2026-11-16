# Release notes draft for v0.1.x

| SDK | Version | Registry |
| --- | --- | --- |
| Python | 0.1.2 | [PyPI](https://pypi.org/project/memoryo-sdk/0.1.2/) |
| TypeScript | 0.1.3 | [npm](https://www.npmjs.com/package/memoryo-sdk/v/0.1.3) |

Both SDKs expose write-job status and retrieval visibility in their quickstart
flow. A completed write job can precede searchable context. Check visibility
before using any returned records in an agent answer.

- Python applications use `Memory.wait_for_job()` and inspect
  `RetrieveResult.memory_visibility`. See the [Python changelog](../sdk/python/CHANGELOG.md).
- TypeScript applications use `waitForJob()` and inspect
  `memoryVisibility` on the retrieval result. The package exports ESM and CommonJS.
  See the [TypeScript SDK guide](../sdk/typescript/README.md).

The [quickstart](quickstart.md) covers the Docker beta and both SDK examples.
The [offline demo](../examples/README.md) illustrates what the agent should do
while a CRM correction is pending.
