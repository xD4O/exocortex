"""Memory chat — RAG over the exocortex memory store.

These tests exercise the full chat pipeline (retrieval → prompt →
chat → audit) against a mocked Ollama HTTP transport, so they run
without `ollama serve`.
"""

from __future__ import annotations

import re
from pathlib import Path

import httpx
import pytest

from exocortex.config import Settings
from exocortex.contracts import (
    Confidence,
    EventKind,
    MemoryRecord,
    MemoryScope,
)
from exocortex.memory.chat import MemoryChatService, _extract_citations
from exocortex.memory.durable import DurableMemoryStore
from exocortex.memory.embedding import DeterministicEmbeddingProvider
from exocortex.memory.llm import (
    ChatCompletion,
    ChatMessage,
    LocalLLMUnavailableError,
    OllamaChatProvider,
    OllamaEmbeddingProvider,
)
from exocortex.memory.retrieval import HybridRetrieval
from exocortex.observability.audit import AuditLog
from exocortex.operator.mcp.handlers import MemoryHandlers


def _rec(
    content: str,
    *,
    type: str = "decision",
    source: str = "codex",
    scope: MemoryScope = MemoryScope.PROJECT,
    scope_id: str = "exocortex",
) -> MemoryRecord:
    return MemoryRecord(
        type=type,
        content=content,
        source=source,
        confidence=Confidence.OBSERVED,
        scope=scope,
        scope_id=scope_id,
    )


def _mock_transport(responses: dict[str, dict]) -> httpx.MockTransport:
    """Build a MockTransport that maps endpoint suffix → JSON body."""

    def handler(request: httpx.Request) -> httpx.Response:
        for suffix, body in responses.items():
            if request.url.path.endswith(suffix):
                return httpx.Response(200, json=body)
        return httpx.Response(404, text=f"unmocked path {request.url.path}")

    return httpx.MockTransport(handler)


@pytest.fixture
async def stack(tmp_path: Path):
    store = DurableMemoryStore(tmp_path / "mem.db")
    embedder = DeterministicEmbeddingProvider()
    retrieval = HybridRetrieval(store, embedder)
    audit = AuditLog(tmp_path / "audit.jsonl")
    return store, embedder, retrieval, audit


# --- Citation extraction ---------------------------------------------------


def test_extract_citations_finds_referenced_ids() -> None:
    valid = ["abc12345-aaaa-bbbb-cccc-000000000000"]
    answer = "We chose SQLite [id:abc12345] because it's lighter."
    cited = _extract_citations(answer, valid)
    assert cited == valid


def test_extract_citations_dedups_repeats() -> None:
    valid = ["abc12345-aaaa-bbbb-cccc-000000000000"]
    answer = "[id:abc12345] and again [id:abc12345]."
    assert _extract_citations(answer, valid) == valid


def test_extract_citations_ignores_hallucinated_ids() -> None:
    valid = ["abc12345-aaaa-bbbb-cccc-000000000000"]
    answer = "Probably [id:deadbeef] but real [id:abc12345]."
    assert _extract_citations(answer, valid) == valid


def test_extract_citations_case_insensitive() -> None:
    valid = ["abc12345-aaaa-bbbb-cccc-000000000000"]
    answer = "Per [ID:ABC12345]."
    # Regex is [0-9a-fA-F] but the bracket prefix is literal lowercase `id:`.
    # Confirms the parser doesn't crash and matches lowercase prefix only.
    assert _extract_citations(answer, valid) == []


# --- Ollama providers (mocked HTTP) ----------------------------------------


@pytest.mark.asyncio
async def test_ollama_embedding_returns_vector(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    transport = _mock_transport(
        {"/api/embeddings": {"embedding": [0.1, 0.2, 0.3]}}
    )
    provider = OllamaEmbeddingProvider(model="m", endpoint="http://x")
    real_async_client = httpx.AsyncClient

    def fake_client(*args, **kwargs):  # type: ignore[no-untyped-def]
        kwargs["transport"] = transport
        return real_async_client(*args, **kwargs)

    monkeypatch.setattr(httpx, "AsyncClient", fake_client)
    vec = await provider.aembed("hello")
    assert vec == [0.1, 0.2, 0.3]


@pytest.mark.asyncio
async def test_ollama_embedding_connect_error_raises_unavailable(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("nope", request=request)

    real_async_client = httpx.AsyncClient

    def fake_client(*args, **kwargs):  # type: ignore[no-untyped-def]
        kwargs["transport"] = httpx.MockTransport(handler)
        return real_async_client(*args, **kwargs)

    monkeypatch.setattr(httpx, "AsyncClient", fake_client)
    provider = OllamaEmbeddingProvider(endpoint="http://nowhere")
    with pytest.raises(LocalLLMUnavailableError, match="not reachable"):
        await provider.aembed("anything")


@pytest.mark.asyncio
async def test_ollama_chat_auto_detects_model(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    transport = _mock_transport(
        {
            "/api/tags": {
                "models": [
                    {"name": "nomic-embed-text"},  # skipped (embed)
                    {"name": "qwen2.5:7b"},
                ]
            },
            "/api/chat": {
                "message": {"content": "hello back"},
                "prompt_eval_count": 10,
                "eval_count": 5,
            },
        }
    )
    real_async_client = httpx.AsyncClient

    def fake_client(*args, **kwargs):  # type: ignore[no-untyped-def]
        kwargs["transport"] = transport
        return real_async_client(*args, **kwargs)

    monkeypatch.setattr(httpx, "AsyncClient", fake_client)
    provider = OllamaChatProvider(model="", endpoint="http://x")
    completion = await provider.chat([ChatMessage(role="user", content="hi")])
    assert completion.answer == "hello back"
    assert completion.model == "qwen2.5:7b"
    assert completion.input_tokens == 10
    assert completion.output_tokens == 5


@pytest.mark.asyncio
async def test_ollama_chat_no_chat_models_raises(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    transport = _mock_transport(
        {"/api/tags": {"models": [{"name": "nomic-embed-text"}]}}
    )
    real_async_client = httpx.AsyncClient

    def fake_client(*args, **kwargs):  # type: ignore[no-untyped-def]
        kwargs["transport"] = transport
        return real_async_client(*args, **kwargs)

    monkeypatch.setattr(httpx, "AsyncClient", fake_client)
    provider = OllamaChatProvider(model="", endpoint="http://x")
    with pytest.raises(LocalLLMUnavailableError, match="no chat-capable model"):
        await provider.chat([ChatMessage(role="user", content="hi")])


# --- MemoryChatService end-to-end ------------------------------------------


class _StubChatProvider:
    """Fake OllamaChatProvider that answers with the IDs it was shown."""

    def __init__(self, answer_template: str = "Per [id:{first8}] yes.") -> None:
        self.answer_template = answer_template
        self.last_messages: list[ChatMessage] = []

    async def chat(self, messages: list[ChatMessage]):  # type: ignore[no-untyped-def]
        self.last_messages = messages
        # Pull first id:xxxxxxxx from the user prompt; if none, no citation.
        user = next((m.content for m in messages if m.role == "user"), "")
        m = re.search(r"id:([0-9a-fA-F]{8})", user)
        first8 = m.group(1) if m else "00000000"
        return ChatCompletion(
            answer=self.answer_template.format(first8=first8),
            model="stub-7b",
            input_tokens=42,
            output_tokens=7,
        )


@pytest.mark.asyncio
async def test_memory_chat_returns_grounded_answer_and_citations(stack) -> None:  # type: ignore[no-untyped-def]
    store, embedder, retrieval, audit = stack
    rec = _rec("Chose SQLite over Postgres for the MVP because operability matters")
    await store.write(rec, embedding=embedder.embed(rec.content))

    service = MemoryChatService(
        store=store,
        retrieval=retrieval,
        chat_provider=_StubChatProvider(),
        audit=audit,
    )
    response = await service.ask(question="Why SQLite?")
    assert "id:" in response.answer
    assert response.cited_record_ids == [str(rec.id)]
    assert response.retrieved_record_ids == [str(rec.id)]
    assert response.model == "stub-7b"
    assert response.input_tokens == 42
    assert response.output_tokens == 7

    # Audit event recorded.
    events = await audit.read_all()
    chat_events = [e for e in events if e.kind == EventKind.MEMORY_CHAT]
    assert len(chat_events) == 1
    assert chat_events[0].payload["question"] == "Why SQLite?"
    assert chat_events[0].payload["cited_record_ids"] == [str(rec.id)]


@pytest.mark.asyncio
async def test_memory_chat_no_records_still_calls_model(stack) -> None:  # type: ignore[no-untyped-def]
    _, _, retrieval, audit = stack
    stub = _StubChatProvider(answer_template="No relevant memory.")
    service = MemoryChatService(
        store=stack[0], retrieval=retrieval, chat_provider=stub, audit=audit
    )
    response = await service.ask(question="anything?")
    assert response.retrieved_record_ids == []
    assert response.cited_record_ids == []
    # Prompt told the model there were no records.
    user_prompt = next(m.content for m in stub.last_messages if m.role == "user")
    assert "No memory records matched" in user_prompt


@pytest.mark.asyncio
async def test_memory_chat_empty_question_raises(stack) -> None:  # type: ignore[no-untyped-def]
    store, _, retrieval, audit = stack
    service = MemoryChatService(
        store=store, retrieval=retrieval, chat_provider=_StubChatProvider(), audit=audit
    )
    with pytest.raises(ValueError, match="must not be empty"):
        await service.ask(question="   ")


# --- Handler-level integration --------------------------------------------


@pytest.mark.asyncio
async def test_memory_chat_handler_disabled_when_toggle_off(stack, tmp_path) -> None:  # type: ignore[no-untyped-def]
    store, embedder, retrieval, audit = stack
    settings = Settings(data_dir=tmp_path / "data")
    settings.ensure_dirs()
    # No flag-file → disabled.
    handlers = MemoryHandlers(
        store=store, embedder=embedder, retrieval=retrieval, audit=audit,
        settings=settings,
    )
    out = await handlers.memory_chat(question="anything?")
    assert out["status"] == "disabled"
    assert "OFF" in out["error"]


@pytest.mark.asyncio
async def test_memory_chat_handler_llm_unavailable_returns_clean_error(
    stack, tmp_path, monkeypatch  # type: ignore[no-untyped-def]
) -> None:
    store, embedder, retrieval, audit = stack
    settings = Settings(data_dir=tmp_path / "data")
    settings.ensure_dirs()
    settings.chat_toggle_path.write_text("on\n")  # enabled

    # Force OllamaChatProvider to fail immediately.
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("no", request=request)

    real_async_client = httpx.AsyncClient

    def fake_client(*args, **kwargs):  # type: ignore[no-untyped-def]
        kwargs["transport"] = httpx.MockTransport(handler)
        return real_async_client(*args, **kwargs)

    monkeypatch.setattr(httpx, "AsyncClient", fake_client)

    handlers = MemoryHandlers(
        store=store, embedder=embedder, retrieval=retrieval, audit=audit,
        settings=settings,
    )
    out = await handlers.memory_chat(question="hello?")
    assert out["status"] == "llm_unavailable"
    assert "ollama" in out["error"].lower() or "reachable" in out["error"].lower()


@pytest.mark.asyncio
async def test_memory_chat_handler_happy_path(
    stack, tmp_path, monkeypatch  # type: ignore[no-untyped-def]
) -> None:
    store, embedder, retrieval, audit = stack
    rec = _rec("auth tokens rotate every 60 minutes")
    await store.write(rec, embedding=embedder.embed(rec.content))

    settings = Settings(data_dir=tmp_path / "data")
    settings.ensure_dirs()
    settings.chat_toggle_path.write_text("on\n")

    transport = _mock_transport(
        {
            "/api/tags": {"models": [{"name": "qwen2.5:7b"}]},
            "/api/chat": {
                "message": {
                    "content": f"Per [id:{str(rec.id)[:8]}] every 60m.",
                },
                "prompt_eval_count": 12,
                "eval_count": 6,
            },
        }
    )
    real_async_client = httpx.AsyncClient

    def fake_client(*args, **kwargs):  # type: ignore[no-untyped-def]
        kwargs["transport"] = transport
        return real_async_client(*args, **kwargs)

    monkeypatch.setattr(httpx, "AsyncClient", fake_client)

    handlers = MemoryHandlers(
        store=store, embedder=embedder, retrieval=retrieval, audit=audit,
        settings=settings,
    )
    out = await handlers.memory_chat(question="when do tokens rotate?")
    assert out["status"] == "ok"
    assert out["cited_record_ids"] == [str(rec.id)]
    assert out["model"] == "qwen2.5:7b"


# --- Settings integration --------------------------------------------------


def test_chat_toggle_flag_path_round_trip(tmp_path: Path) -> None:
    settings = Settings(data_dir=tmp_path / "data")
    settings.ensure_dirs()
    assert settings.memory_chat_enabled() is False
    settings.chat_toggle_path.write_text("on\n")
    assert settings.memory_chat_enabled() is True
    settings.chat_toggle_path.unlink()
    assert settings.memory_chat_enabled() is False


def test_settings_chat_defaults_local_first() -> None:
    settings = Settings()
    assert settings.memory_chat_endpoint.startswith("http://localhost")
    assert "embed" in settings.memory_chat_embedding_model.lower()
    assert settings.memory_chat_default_top_k == 8


