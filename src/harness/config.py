import os
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class LLMConfig:
    api_key: str | None = None
    model: str = "openai/gpt-6-luna"  # $0.10 in / $0.50 out per 1M tokens
    base_url: str = "https://openrouter.ai/api/v1"
    max_tokens: int | None = None
    temperature: float | None = None
    # Reasoning tokens bill as output, so keep this low on a cheap model. None = don't send.
    reasoning_effort: str | None = "medium"  # "off", "low", "medium", "high"
    timeout: float = 120.0

    @classmethod
    def from_env(cls) -> "LLMConfig":
        d = cls()
        effort = os.environ.get("HARNESS_REASONING_EFFORT", d.reasoning_effort)
        return cls(
            api_key=os.environ.get("OPENROUTER_API_KEY"),
            model=os.environ.get("HARNESS_MODEL", d.model),
            base_url=os.environ.get("HARNESS_BASE_URL", d.base_url),
            max_tokens=_opt("HARNESS_MAX_TOKENS", int),
            temperature=_opt("HARNESS_TEMPERATURE", float),
            reasoning_effort=None if effort in (None, "", "off") else effort,
            timeout=float(os.environ.get("HARNESS_TIMEOUT", d.timeout)),
        )


def workspace_root() -> Path:
    """The only folder the file tools may touch. Defaults to where you started the harness."""
    return Path(os.environ.get("HARNESS_WORKSPACE", ".")).expanduser().resolve()


def sessions_dir() -> Path:
    """Global, so you can resume a session from any directory."""
    return Path(os.environ.get("HARNESS_HOME", "~/.ai-harness")).expanduser() / "sessions"


def _opt(name: str, cast):
    value = os.environ.get(name)
    return cast(value) if value else None
