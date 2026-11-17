# MemoryOS

**Memory for AI agents that tracks what changed and where it came from.**

Building a chatbot or agent? Use MemoryOS to save useful information from conversations and services, then fetch the current records when your agent needs them. Each record can carry its source and history. When an update needs review, your app can see that before it uses the affected context.

[Quickstart](docs/quickstart.md) · [Python SDK](sdk/python/README.md) · [TypeScript SDK](sdk/typescript/README.md) · [API docs](https://docs.memoryo.dev/)

When CRM corrects the plan Billing recorded, what should a support agent use in its
next chat? The [two-source walkthrough](#try-the-no-key-demo) shows
the current record, its source, and a correction awaiting review.

## Ways to contribute

- **Try a fresh clone.** Report the first setup step that blocked you, with your
  OS, tool versions, and sanitized output.
- **Pick a [good first issue](https://github.com/memengine/memory-api/labels/good%20first%20issue).**
  Start with setup docs, a small SDK example, or a focused regression.
- **Share a memory failure from your agent.** Tell us what went stale, which
  source should win, and how your app handles it today in a [memory failure report](https://github.com/memengine/memory-api/issues/new?template=memory_failure_report.md).

See the [contribution guide](CONTRIBUTING.md) and [development checks](DEVELOPMENT.md)
for work you can verify without a paid model key.

## Try the no-key demo

With Python 3.12+ and Make, run this from the repository root.

```bash
make demo
```

Billing records Growth. CRM proposes Scale. While the correction awaits review, the agent withholds the plan answer. This is a local simulation with no server. It uses a synthetic account, mock extraction and embeddings, and a local state file. It needs no API keys, containers, or paid calls.

For maintainers, `make gif` regenerates this recording by running the real MemoryOS API locally with mocked model calls. See the [recording guide](examples/recording/README.md).

![Terminal recording of the MemoryOS API correction and review flow](docs/assets/demo.gif)

See [examples](examples/README.md) for individual commands and the LangGraph example.

## Quickstart

[Start the API and save a memory with Python or TypeScript](docs/quickstart.md), then [connect your agent](docs/connect-your-agent.md). The Docker beta requires `OPENAI_API_KEY` for extraction and embeddings. For the API version of the two-source scenario, follow the [Billing and CRM walkthrough](beta/README.md#one-two-service-integration-example).

[Architecture](ARCHITECTURE.md) · [Contributing](CONTRIBUTING.md) · [Security](SECURITY.md) · [MIT license](LICENSE)
