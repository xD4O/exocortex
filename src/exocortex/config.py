from __future__ import annotations

from pathlib import Path
from typing import Literal

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="EXOCORTEX_",
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    data_dir: Path = Field(default=Path("./data"))
    audit_log_path: Path = Field(default=Path("./data/audit.jsonl"))
    memory_db_path: Path = Field(default=Path("./data/memory.db"))

    log_level: Literal["DEBUG", "INFO", "WARNING", "ERROR"] = "INFO"
    log_format: Literal["json", "console"] = "console"

    # Memory chat (RAG over exocortex). Off by default. See docs/memory-chat-plan.md.
    memory_chat_endpoint: str = "http://localhost:11434"
    memory_chat_chat_model: str = ""  # "" = auto-detect via /api/tags
    memory_chat_embedding_model: str = "nomic-embed-text"
    memory_chat_default_top_k: int = 8
    memory_chat_max_tokens: int = 1024
    memory_chat_timeout_seconds: int = 60

    # User profile memory (USER-scope records about the operator themselves).
    profile_user_id: str = "operator"

    @property
    def chat_toggle_path(self) -> Path:
        # Persistent on/off marker — flipped via CLI / UI / MCP, read on every
        # invocation so cross-process state stays coherent without a daemon.
        return self.data_dir / "chat-enabled.flag"

    @property
    def profile_freeze_path(self) -> Path:
        # When this flag-file exists, profile observation writes are blocked.
        return self.data_dir / "profile-frozen.flag"

    def memory_chat_enabled(self) -> bool:
        return self.chat_toggle_path.exists()

    def profile_frozen(self) -> bool:
        return self.profile_freeze_path.exists()

    def ensure_dirs(self) -> None:
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.audit_log_path.parent.mkdir(parents=True, exist_ok=True)
        self.memory_db_path.parent.mkdir(parents=True, exist_ok=True)
