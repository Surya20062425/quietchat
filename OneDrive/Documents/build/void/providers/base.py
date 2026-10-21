"""Provider interface. All providers speak OpenAI-compatible message dicts internally.

chat_complete(messages, model, tools, stream_cb) -> {
    "content": str,                      # assistant text ("" if tool calls only)
    "tool_calls": [{"id","name","arguments"}...],
    "reasoning": str | None,             # thinking text if the model exposed it
    "usage": {"prompt_tokens","completion_tokens"},
    "finish_reason": str, "raw": ...}
"""
from __future__ import annotations

from typing import Any, Callable


class ProviderError(Exception):
    """Raised for auth/rate-limit/network failures; carries a provider-agnostic message."""

    def __init__(self, message: str, status: int | None = None, retryable: bool = False):
        super().__init__(message)
        self.status = status
        self.retryable = retryable


StreamCb = Callable[[str], None]   # called with text deltas as they arrive


class Provider:
    """Base class. Subclasses set `name`, `display`, `env_keys` and implement the methods."""

    name = "base"
    display = "Base"
    env_keys: list[str] = []          # accepted env var names for the API key (first match wins)
    base_url: str | None = None

    def __init__(self, api_key: str | None = None, base_url: str | None = None,
                 config: Any = None, model_override: str | None = None):
        self.api_key = api_key or ""
        self.base_url = base_url or self.base_url
        self.config = config
        self.model_override = model_override

    # ---- helpers -------------------------------------------------------------
    @classmethod
    def key_from_env(cls) -> str:
        import os
        for name in cls.env_keys:
            val = os.environ.get(name, "")
            if val and val.lower() != "none":
                return val
        return ""

    @classmethod
    def env_key_names(cls) -> list[str]:
        return list(cls.env_keys)

    # ---- required interface -----------------------------------------------------
    def list_models(self, timeout: float = 10.0) -> list[str]:
        """Live model list (may hit the network)."""
        raise NotImplementedError

    def fallback_models(self) -> list[str]:
        """Static list used when list_models fails/times out."""
        raise NotImplementedError

    def chat_complete(self, messages: list[dict], model: str, tools: list[dict] | None = None,
                      stream_cb: StreamCb | None = None,
                      temperature: float | None = None,
                      max_tokens: int | None = None,
                      timeout: float | None = None) -> dict:
        raise NotImplementedError

    def test_connection(self, model: str | None = None) -> tuple[bool, str]:
        """Tiny real call to verify the key + model work."""
        try:
            resp = self.chat_complete(
                [{"role": "user", "content": "Reply with exactly: ok"}],
                model or self.model_override or self.fallback_models()[0],
                tools=None, max_tokens=10, timeout=30)
            text = (resp.get("content") or "").strip()
            return True, f"model responded: {text[:60]!r}"
        except ProviderError as exc:
            return False, str(exc)
        except Exception as exc:
            return False, f"{type(exc).__name__}: {exc}"
