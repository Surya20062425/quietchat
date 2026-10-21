"""void.providers — model provider registry (openai-compatible wire format)."""
from __future__ import annotations

from typing import Any

from .base import Provider, ProviderError

_REGISTRY: dict[str, type[Provider]] = {}


def register_provider(cls: type[Provider]) -> type[Provider]:
    _REGISTRY[cls.name] = cls
    return cls


def get_provider_class(name: str) -> type[Provider] | None:
    return _REGISTRY.get(name)


def provider_names() -> list[str]:
    return sorted(_REGISTRY.keys())


def create_provider(name: str, config: Any = None, api_key: str | None = None,
                    base_url: str | None = None, model_override: str | None = None) -> Provider:
    cls = _REGISTRY.get(name)
    if cls is None:
        raise ProviderError(f"unknown provider '{name}'. Available: {', '.join(provider_names())}")
    return cls(api_key=api_key, base_url=base_url, config=config, model_override=model_override)


# import built-ins so register_provider side effects run
from . import openai_compatible, openrouter, anthropic, gemini, custom  # noqa: E402,F401
