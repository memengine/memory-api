# Maintainers only: regenerates docs/assets/demo.gif

With Docker Compose running, use `make gif` from the repository root. It starts a
disposable MemoryOS server, runs the five numbered scripts against its API, records
them with VHS, and removes the temporary stack. Extraction and embeddings use
mocked model calls. Storage, review, visibility, history, and audit fields come
from the real API.
