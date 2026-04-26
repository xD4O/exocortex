# Memory chat — natural-language queries over exocortex

> **Status: v0 shipped.** Ollama-backed RAG, off by default. Toggle via
> `precog chat-toggle on` or the header pill on the web UI. CLI:
> `precog chat "<question>"`. MCP: `memory_chat`. Web: `/chat` page (full
> screen) — citation chips, scope filter, tasks sidebar, activity strip.
> v0 uses FTS-only retrieval (alpha=1.0); v1 will re-embed all records via
> Ollama for semantic retrieval — see "Open questions" below.

A retrieval-augmented chat layer over exocortex memory. Operator (or any
agent) asks a question; the system retrieves relevant records, sends them
to a local chat model with the question, returns a grounded answer with
explicit citations. Off by default. Local-first by default.

## Goals

1. Operator can ask "what did we decide about authentication?" and get a
   real answer instead of having to read 12 records by hand.
2. Other agents can ask the same questions via MCP — when they need
   context, they query the memory librarian instead of pasting record
   contents into their own prompt.
3. **No memory writes from chat.** The chat layer reads. If the operator
   says "save that," they (or the agent) make a separate, explicit
   `memory_write` call. Avoids the model hallucinating new "facts" into
   the durable store.
4. **Local-first.** Default config uses Ollama on `localhost:11434`. No
   network egress unless the operator explicitly configures a cloud
   provider. Memory contents never leave the machine in the default path.
5. **Toggle-controlled.** Off by default. Operator flips a single switch
   (CLI or UI button). When off, the MCP tool returns a clear "disabled"
   error instead of silently failing.
6. **Auditable.** Every chat invocation appends a `MEMORY_CHAT` audit
   event with the question, the cited record IDs, the model name, and
   latency. The chat output is recoverable via trace.

## Architecture

```
Question (string, optional scope filter)
   │
   ▼
EmbeddingProvider (Ollama nomic-embed-text by default)
   │  embedding vector
   ▼
HybridRetrieval (existing — FTS + cosine, scope-filtered)
   │  top-K records (default K=8)
   ▼
PromptBuilder
   │  system prompt + cited records + question
   ▼
ChatProvider (Ollama qwen2.5:7b or auto-detected)
   │  answer text
   ▼
Response: {answer, cited_record_ids, model, embedding_model, latency_ms}
   │
   ▼
Audit: emit MEMORY_CHAT event with the above
```

## Components

### 1. `OllamaEmbeddingProvider` (`src/exocortex/memory/llm.py`)

Implements existing `EmbeddingProvider` Protocol.

- `POST {endpoint}/api/embeddings` body `{"model": ..., "prompt": ...}`
- Returns `embedding: list[float]`
- On connection failure: raises `LocalLLMUnavailableError` with a clear
  message ("ollama not reachable at <endpoint> — is `ollama serve` running?")
- Configurable: model name, endpoint, timeout.
- Default model: `nomic-embed-text` (768-dim, ubiquitous on Ollama).

The retrieval layer continues to use `DeterministicEmbeddingProvider` for
internal storage embeddings (16-dim, fast, deterministic) — those are
written when records land. Chat-time queries use the *real* embedding
model, then we project both into the same space for similarity.

> **Open question.** Mixing 16-dim deterministic embeddings (stored) with
> 768-dim Ollama embeddings (query-time) doesn't work directly — different
> spaces. **Resolution for v0:** chat uses FTS-only retrieval (alpha=1.0),
> doesn't use semantic. v1 re-embeds existing records via Ollama in the
> background and stores those alongside; chat uses semantic over the
> Ollama embeddings, the existing constellation continues to use the
> deterministic ones (or migrates).

### 2. `OllamaChatProvider` (`src/exocortex/memory/llm.py`)

- `POST {endpoint}/api/chat` body `{"model": ..., "messages": [...], "stream": false}`
- Returns answer text + token count.
- Auto-detect model: if `model` config is empty, query
  `GET {endpoint}/api/tags`, pick the first non-embedding model (heuristic:
  exclude names containing `embed`).
- Configurable: model, endpoint, temperature, max tokens, timeout.

### 3. `MemoryChatService` (`src/exocortex/memory/chat.py`)

```python
class MemoryChatService:
    def __init__(
        self,
        store: DurableMemoryStore,
        retrieval: HybridRetrieval,
        embedder: EmbeddingProvider,    # for retrieval (deterministic for v0)
        chat: ChatProvider,
        audit: AuditLog,
    ) -> None: ...

    async def ask(
        self,
        *,
        question: str,
        top_k: int = 8,
        scope: MemoryScope | None = None,
        scope_id: str | None = None,
        alpha: float = 1.0,             # FTS-only for v0; tunable for v1
    ) -> ChatResponse: ...
```

Returns `ChatResponse(answer, cited_record_ids, model, embedding_model, latency_ms, retrieved_records)`. Emits `MEMORY_CHAT` audit event.

### 4. `memory_chat` MCP tool

Exposed via `server.py`. Checks `Settings.memory_chat_enabled`. If false,
returns `{"status": "disabled", "error": "memory chat is off — operator must enable it"}`.

Tool signature (Annotated + Field for clarity to agents):
- `question: str`
- `top_k: int = 8`
- `scope: Scope | None = None`
- `scope_id: str | None = None`

### 5. CLI

```bash
precog chat-toggle [on|off|status]      # flip the persistent toggle
precog chat "what did we decide..."     # one-shot terminal chat
```

`chat-toggle` writes `Settings.memory_chat_enabled` to a tiny config file
`./data/chat-enabled.flag` so the toggle persists across CLI invocations
and MCP-server restarts.

### 6. Web UI (subagent's lane)

- Header toggle on every page (`MEMORY CHAT [ON|OFF]`)
- Chat panel on `/memory` when enabled (slide-up, monospace input, citation chips)
- Search bar at top of `/memory` (independent from chat — uses `/api/memory/search`)
- Backend exposes `/api/settings/memory_chat[/toggle]` and `/api/memory/chat`

## Settings

```toml
[exocortex]
memory_chat_enabled = false          # the master toggle
memory_chat_endpoint = "http://localhost:11434"
memory_chat_chat_model = ""          # empty = auto-detect from /api/tags
memory_chat_embedding_model = "nomic-embed-text"
memory_chat_default_top_k = 8
memory_chat_max_tokens = 1024
memory_chat_timeout_seconds = 60
```

Plus the persistent toggle file (`./data/chat-enabled.flag`) for cross-process state.

## Privacy / trust model

- **Off by default.** Operator must explicitly turn on. UI button + CLI both flip the same flag.
- **Local-only by default.** Default endpoint is `localhost:11434`. Memory leaves the machine only if the operator points the endpoint elsewhere — and we surface that in `chat-toggle status` so it's visible.
- **Read-only against memory.** The chat service has no write path. If a future "save this answer" flow is added, it's a separate explicit operator action.
- **Audit-logged.** Every invocation emits `MEMORY_CHAT` with the question, retrieved record IDs, model, and latency. Operator can `precog trace` any chat after the fact.
- **Scope-filterable.** Operator can `precog chat --scope project` to restrict retrieval to project records, never accidentally querying a private session scope.
- **Redaction.** v1 will run a pre-prompt scan for secret patterns (API keys, tokens) and redact before sending to the chat model. v0 trusts the local model + the local store.

## Performance

| Stage | Budget |
|---|---|
| Embed query (Ollama) | ~50ms |
| Hybrid retrieval | ~10ms (1k records) |
| Prompt assembly | ~5ms |
| Chat generation (small model, ~200 tokens) | 2-10s |
| Audit append | ~5ms |

Total: 2-10s for typical questions. Streaming tokens to the UI brings perceived latency down even when generation is slow.

## Sequencing

| Phase | What | Notes |
|---|---|---|
| **v0** (this PR) | Provider + Service + MCP tool + CLI toggle + tests | FTS-only retrieval; deterministic embeddings unchanged |
| **v0.5** (subagent, parallel) | Web UI integration | Toggle, chat panel, search bar |
| **v1** | Real Ollama embeddings for all records | Background reembed job; semantic chat |
| **v1.1** | Streaming token output | Faster perceived latency |
| **v2** | Per-scope chat | Restrict retrieval to a scope at chat time |
| **v3** | "Save this answer as a decision" flow | Explicit, operator-confirmed, audit-logged |
| **v4** | Conversational memory (multi-turn) | Chat history feeds back as context |
| **v5** | Memory-agent persona via dispatch | Long-lived "librarian" agent that other agents talk to |

## Open questions

1. **Embedding-space mismatch.** Stored embeddings are 16-dim deterministic; Ollama embeddings are 768-dim. For v0 we sidestep this with FTS-only retrieval. For v1 we either (a) reembed all records in the background, doubling storage, or (b) keep the deterministic embeddings as the storage truth and use Ollama only for query-time keyword expansion. (a) is more powerful, (b) is more frugal. Defer to whoever runs into retrieval-quality complaints first.

2. **Default chat model.** Auto-detection picks the first non-embedding model from `ollama list`. If the user has only big models (70B), latency is bad. We could surface "current model: qwen2.5:7b — use `precog chat-toggle model gpt-oss:7b` to change" in `status` output.

3. **Cloud opt-in.** Some operators may want to use Anthropic / OpenAI for chat quality. The provider interface is generic; adding a cloud provider is ~50 lines. Tracked as v2 with explicit "leaves your machine" warning in `chat-toggle status`.

4. **Multi-agent contention.** If 3 agents simultaneously call `memory_chat`, Ollama serializes by default. Acceptable for MVP; consider a request queue if it bites.

## What shipped in v0

- `OllamaEmbeddingProvider`, `OllamaChatProvider` with graceful fallback when Ollama unavailable
- `MemoryChatService` (FTS-based retrieval, prompt assembly, citation extraction with hallucinated-id rejection)
- `memory_chat` MCP tool with toggle gate (returns `disabled` / `llm_unavailable` / `ok`)
- `precog chat-toggle [on|off|status]` and `precog chat "<question>"` CLI commands
- Persistent toggle file (`./data/chat-enabled.flag`)
- `MEMORY_CHAT` audit event kind (every query is reproducible via `precog trace`)
- Web routes `/api/settings/memory_chat[/toggle]` and `/api/memory/chat`
- `/chat` web page with: citation chips → `/memory#focus=<id>`, scope dropdown, tasks sidebar (right), activity strip (bottom), localStorage history, Cmd+Enter submit
- Live-thinking animation on `/memory`: when a chat fires (any tab/CLI/agent), the constellation shows a ripple + retrieval pulses on cited records + amber citation beams
- Tests: 16 in `test_memory_chat.py` (citation extraction, mocked Ollama, service end-to-end, handler toggle/llm-unavailable/happy-path)

## Tracked for v1+

- v1: Re-embed all records with Ollama in the background; chat uses semantic + FTS hybrid (sidesteps the embedding-space mismatch documented in §1)
- v1.1: Streaming token output for the `/chat` page
- v2: "Save this answer as a decision" — explicit, operator-confirmed write back
- v2: Multi-turn conversational memory (chat history feeds back as context)
- v3: Memory-agent persona via dispatch (long-lived "librarian" agent that other agents talk to)
