"""OpenRouter provider — OpenAI-compatible API with a live model catalog."""
from __future__ import annotations

import json
import urllib.request

from .openai_compatible import OpenAICompatible
from . import register_provider
from .base import ProviderError


def _fetch_catalog(timeout: float = 8.0) -> list[str]:
    req = urllib.request.Request("https://openrouter.ai/api/v1/models",
                                 headers={"User-Agent": "void-cli/1.0"})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        data = json.loads(resp.read(2_000_000).decode("utf-8", errors="replace"))
    return [m.get("id", "") for m in data.get("data", []) if m.get("id")]


@register_provider
class OpenRouterProvider(OpenAICompatible):
    name = "openrouter"
    display = "OpenRouter"
    env_keys = ["OPENROUTER_API_KEY"]
    base_url = "https://openrouter.ai/api/v1"
    default_models = [
        "anthropic/claude-3.5-sonnet", "openai/gpt-4o", "openai/gpt-4o-mini",
        "google/gemini-2.0-flash-001", "meta-llama/llama-3.3-70b-instruct",
        "deepseek/deepseek-chat", "qwen/qwen-2.5-72b-instruct",
    ]

    def list_models(self, timeout: float = 10.0) -> list[str]:
        """Live catalog; on failure fall back to a static list (picker handles timeouts)."""
        try:
            models = _fetch_catalog(timeout)
            if models:
                return models
        except Exception:
            pass
        return self.fallback_models()

    def fallback_models(self) -> list[str]:
        return list(self.default_models)
