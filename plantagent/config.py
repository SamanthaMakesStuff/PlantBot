"""Settings, read from the .env file (no extra packages needed)."""
from __future__ import annotations

import os
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def load_env(path: Path | None = None) -> None:
    path = Path(path) if path else ROOT / ".env"
    if not path.exists():
        return
    found = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        found[key.strip()] = value.strip().strip('"').strip("'")  # if a setting appears twice, the last one wins
    for key, value in found.items():
        os.environ.setdefault(key, value)


class Config:
    def __init__(self) -> None:
        load_env()
        g = os.environ.get
        self.brain = g("BRAIN", "anthropic").lower()
        self.fallback_brain = g("FALLBACK_BRAIN", "").lower()
        self.anthropic_key = g("ANTHROPIC_API_KEY", "")
        self.anthropic_model = g("ANTHROPIC_MODEL", "claude-haiku-5-5")
        self.anthropic_effort = g("ANTHROPIC_EFFORT", "").lower()
        self.gemini_key = g("GEMINI_API_KEY", "")
        self.gemini_model = g("GEMINI_MODEL", "gemini-3.1-flash-lite")
        self.gemini_thinking = g("GEMINI_THINKING", "").lower()
        self.ollama_url = g("OLLAMA_URL", "http://localhost:11434")
        self.ollama_model = g("OLLAMA_MODEL", "gemma3:4b")
        self.daily_call_budget = int(g("DAILY_CALL_BUDGET", "150"))
        self.daily_token_budget = int(g("DAILY_TOKEN_BUDGET", "2000000"))
        self.max_reply_tokens = int(g("MAX_REPLY_TOKENS", "700"))
        self.thinking_allowance = int(g("THINKING_ALLOWANCE", "8000"))
        # Wikipedia: off unless you turn it on with WIKIPEDIA=on. No daily limit unless one is set
        # (the daily token budget still applies).
        self.wikipedia = g("WIKIPEDIA", "off").strip().lower() in ("on", "yes", "true", "1")
        self.wikipedia_daily_limit = int(g("WIKIPEDIA_DAILY_LIMIT", "0") or 0)
        self.data_dir = Path(g("DATA_DIR", str(ROOT / "data")))
        # The web page and timed thoughts
        self.web_password = g("WEB_PASSWORD", "")
        self.web_host = g("WEB_HOST", "0.0.0.0")
        self.web_port = int(g("WEB_PORT", "8000"))
        self.think_every_minutes = int(g("THINK_EVERY_MINUTES", "60"))
        self.awake_start = int(g("AWAKE_START_HOUR", "8"))
        self.awake_end = int(g("AWAKE_END_HOUR", "22"))
