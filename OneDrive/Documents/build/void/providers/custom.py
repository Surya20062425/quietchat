"""Custom provider — any OpenAI-compatible endpoint (LM Studio, Ollama, vLLM, local)."""
from __future__ import annotations

import json
import urllib.request

from . import register_provider
from .base import Provider, ProviderError
from .openai_compatible import OpenAICompatible


@register_provider
class CustomProvider(OpenAICompatible):
    name = "custom"
    display = "Custom (OpenAI-compatible)"
    env_keys = ["CUSTOM_API_KEY"]
    base_url = ""  # must come from config model.custom.base_url
    default_models = ["local-model"]

    def _endpoint(self) -> str:
        url = self.base_url or ""
        if self.config is not None:
            url = url or str(self.config.get("model.custom.base_url") or "")
        if not url:
            raise ProviderError("custom provider needs model.custom.base_url in config.yaml "
                                "(e.g. http://localhost:11434/v1)", status=400)
        return url.rstrip("/")

    def list_models(self, timeout: float = 10.0) -> list[str]:
        """Query <base_url>/models live; fall back to whatever is in config."""
        url = self._endpoint()
        req = urllib.request.Request(url + "/models", headers={"User-Agent": "void-cli/1.0"})
        key = self.api_key or self.key_from_env()
        if key:
            req.add_header("Authorization", f"Bearer {key}")
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                data = json.loads(resp.read(2_000_000).decode("utf-8", errors="replace"))
            models = [m.get("id", "") for m in data.get("data", []) if m.get("id")]
            if models:
                return models
        except Exception:
            pass
        configured = []
        if self.config is not None:
            configured = [str(self.config.get("model.default") or "")]
        return [m for m in configured if m] or self.fallback_models()

    def fallback_models(self) -> list[str]:
        if self.config is not None and self.config.get("model.default"):
            return [str(self.config.get("model.default"))]
        return list(self.default_models)

    def _make_client(self, api_key: str | None = None):
        """Always route to the configured endpoint.

        OpenAICompatible only passes base_url when it is non-empty, and CustomProvider's
        class-level base_url is "" (ModelRouter fills it per-instance for auxiliary tasks
        only). Without this override, provider=custom would silently fall through to
        api.openai.com and send the user's key to the wrong host.
        """
        self.base_url = self._endpoint()
        return super()._make_client(api_key)
