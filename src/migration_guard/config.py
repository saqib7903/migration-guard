"""Central configuration. Everything is env-overridable; nothing secret is hard-coded."""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]


def _env(name: str, default: str) -> str:
    v = os.environ.get(name)
    return v if v not in (None, "") else default


@dataclass
class Settings:
    # ---- LLM ----
    # provider: "claude_cli" | "anthropic_api" | "replay" | "stub"
    provider: str = field(default_factory=lambda: _env("MG_LLM_PROVIDER", "replay"))
    model: str = field(default_factory=lambda: _env("MG_MODEL", "claude-sonnet-4-5"))
    temperature: float = field(default_factory=lambda: float(_env("MG_TEMPERATURE", "0")))
    max_tokens: int = field(default_factory=lambda: int(_env("MG_MAX_TOKENS", "4000")))
    # record every live call into cassettes/ so the whole run replays offline & free
    record: bool = field(default_factory=lambda: _env("MG_RECORD", "1") == "1")
    cassette_dir: Path = field(default_factory=lambda: Path(_env("MG_CASSETTE_DIR", str(REPO_ROOT / "cassettes"))))

    # ---- Shadow database (sandbox where migrations are actually executed) ----
    shadow_dsn: str = field(
        default_factory=lambda: _env("MG_SHADOW_DSN", "postgresql://postgres@127.0.0.1:5432/postgres")
    )
    shadow_enabled: bool = field(default_factory=lambda: _env("MG_SHADOW_ENABLED", "1") == "1")
    # rows seeded into each table of the shadow schema, to make rewrites/locks observable
    seed_rows: int = field(default_factory=lambda: int(_env("MG_SEED_ROWS", "20000")))
    statement_timeout_ms: int = field(default_factory=lambda: int(_env("MG_STATEMENT_TIMEOUT_MS", "30000")))

    # ---- Agent loop ----
    max_tool_iterations: int = field(default_factory=lambda: int(_env("MG_MAX_TOOL_ITERS", "6")))
    enable_verifier: bool = field(default_factory=lambda: _env("MG_ENABLE_VERIFIER", "1") == "1")
    enable_rewriter: bool = field(default_factory=lambda: _env("MG_ENABLE_REWRITER", "1") == "1")

    # ---- Output ----
    runs_dir: Path = field(default_factory=lambda: Path(_env("MG_RUNS_DIR", str(REPO_ROOT / "runs"))))
    reports_dir: Path = field(default_factory=lambda: Path(_env("MG_REPORTS_DIR", str(REPO_ROOT / "reports"))))

    def ensure_dirs(self) -> None:
        for p in (self.cassette_dir, self.runs_dir, self.reports_dir):
            p.mkdir(parents=True, exist_ok=True)


SETTINGS = Settings()
