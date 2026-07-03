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

    # --- Web server hardening (A2) -------------------------------------
    # The operator UI trusts a local browser. These close cross-site
    # request forgery + cross-site WebSocket hijack without breaking
    # loopback use or non-browser clients (CLI/curl send no Origin).
    #
    # Extra browser origins allowed to call the API / open the event
    # socket, comma-separated (e.g. "http://192.168.1.5:8756"). Loopback
    # origins (localhost / 127.0.0.1 / ::1, any port) are always allowed.
    web_allowed_origins: str = ""
    # When set, every /api/* request and the event WebSocket must present
    # this token (header `X-Exocortex-Token` or `?token=`). Empty = no
    # token (pure loopback trust).
    web_token: str = ""

    # --- Tool sandbox for ad-hoc MCP fs/shell calls (A1) ---------------
    # fs_read / fs_list / shell_exec from an attached agent are confined
    # to this root and audited through the policy engine. Defaults to the
    # process working directory (typically the project the operator
    # launched the server in). Widen deliberately if agents need more.
    tool_sandbox_root: Path = Field(default=Path("."))
    # When true, an out-of-policy fs write / shell exec is auto-denied
    # rather than auto-approved. Kept false by default so autonomous
    # dispatch keeps working, but the choice is now explicit + audited
    # instead of a silent masquerade (A4).
    dispatch_auto_approve_tools: bool = True

    @property
    def tool_sandbox_root_resolved(self) -> Path:
        return self.tool_sandbox_root.expanduser().resolve()

    def web_allowed_origin_set(self) -> set[str]:
        return {o.strip() for o in self.web_allowed_origins.split(",") if o.strip()}

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
