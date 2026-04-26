"""Local LLM providers (Ollama-flavored) for memory chat.

Two providers:

- `OllamaEmbeddingProvider` — implements the EmbeddingProvider Protocol.
  Used by chat-time retrieval (NOT the storage embedder; storage stays on
  DeterministicEmbeddingProvider for v0).
- `OllamaChatProvider` — generates an answer given a prompt.

Both are thin httpx wrappers around Ollama's HTTP API. If Ollama isn't
running on the configured endpoint, both raise `LocalLLMUnavailableError`
with a clear message — callers degrade gracefully rather than hanging.

Auto-detection: `OllamaChatProvider` queries `/api/tags` if no model is
configured and picks the first non-embedding model. Picking the most
recently pulled model is more likely to match what the operator actually
intends to use.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass

import httpx


class LocalLLMUnavailableError(RuntimeError):
    """Ollama (or whatever local LLM backend) isn't reachable."""


@dataclass(frozen=True)
class OllamaEmbeddingProvider:
    """Pull text embeddings from a local Ollama server."""

    model: str = "nomic-embed-text"
    endpoint: str = "http://localhost:11434"
    timeout_seconds: float = 30.0
    dim: int = 768  # nominal; real size determined at first call

    def embed(self, text: str) -> list[float]:  # sync API for parity
        # The existing EmbeddingProvider Protocol is sync. Wrap an async
        # client in a fresh event loop for the rare sync caller; async
        # users should call `aembed` directly.
        return asyncio.run(self.aembed(text))

    async def aembed(self, text: str) -> list[float]:
        try:
            async with httpx.AsyncClient(timeout=self.timeout_seconds) as client:
                response = await client.post(
                    f"{self.endpoint}/api/embeddings",
                    json={"model": self.model, "prompt": text},
                )
                response.raise_for_status()
                data = response.json()
        except httpx.ConnectError as e:
            raise LocalLLMUnavailableError(
                f"ollama not reachable at {self.endpoint} — "
                f"is `ollama serve` running?"
            ) from e
        except httpx.HTTPStatusError as e:
            raise LocalLLMUnavailableError(
                f"ollama returned {e.response.status_code}: "
                f"{e.response.text[:200]}"
            ) from e
        embedding = data.get("embedding")
        if not isinstance(embedding, list):
            raise LocalLLMUnavailableError(
                f"unexpected ollama response shape: {data!r}"
            )
        return [float(x) for x in embedding]


@dataclass(frozen=True)
class ChatMessage:
    role: str
    content: str

    def to_ollama(self) -> dict[str, str]:
        return {"role": self.role, "content": self.content}


@dataclass(frozen=True)
class ChatCompletion:
    answer: str
    model: str
    input_tokens: int = 0
    output_tokens: int = 0


@dataclass(frozen=True)
class OllamaChatProvider:
    """Generate completions from a local Ollama server."""

    model: str = ""  # empty = auto-detect via /api/tags
    endpoint: str = "http://localhost:11434"
    timeout_seconds: float = 60.0
    temperature: float = 0.2  # low — answers should be grounded, not creative
    max_tokens: int = 1024

    async def list_available_models(self) -> list[str]:
        try:
            async with httpx.AsyncClient(timeout=self.timeout_seconds) as client:
                response = await client.get(f"{self.endpoint}/api/tags")
                response.raise_for_status()
                data = response.json()
        except httpx.ConnectError as e:
            raise LocalLLMUnavailableError(
                f"ollama not reachable at {self.endpoint}"
            ) from e
        models = data.get("models") or []
        return [str(m.get("name", "")) for m in models if m.get("name")]

    async def resolve_model(self) -> str:
        if self.model:
            return self.model
        all_models = await self.list_available_models()
        # Skip embedding models — they don't generate.
        chat_models = [
            m for m in all_models
            if "embed" not in m.lower() and "embedding" not in m.lower()
        ]
        if not chat_models:
            raise LocalLLMUnavailableError(
                "no chat-capable model found via ollama /api/tags. "
                "Install one (e.g. `ollama pull qwen2.5:7b`) or set "
                "EXOCORTEX_MEMORY_CHAT_CHAT_MODEL."
            )
        return chat_models[0]

    async def chat(
        self, messages: list[ChatMessage]
    ) -> ChatCompletion:
        model = await self.resolve_model()
        try:
            async with httpx.AsyncClient(timeout=self.timeout_seconds) as client:
                response = await client.post(
                    f"{self.endpoint}/api/chat",
                    json={
                        "model": model,
                        "messages": [m.to_ollama() for m in messages],
                        "stream": False,
                        "options": {
                            "temperature": self.temperature,
                            "num_predict": self.max_tokens,
                        },
                    },
                )
                response.raise_for_status()
                data = response.json()
        except httpx.ConnectError as e:
            raise LocalLLMUnavailableError(
                f"ollama not reachable at {self.endpoint}"
            ) from e
        except httpx.HTTPStatusError as e:
            raise LocalLLMUnavailableError(
                f"ollama returned {e.response.status_code}: "
                f"{e.response.text[:200]}"
            ) from e

        message = data.get("message") or {}
        answer = str(message.get("content") or "")
        return ChatCompletion(
            answer=answer,
            model=model,
            input_tokens=int(data.get("prompt_eval_count") or 0),
            output_tokens=int(data.get("eval_count") or 0),
        )
