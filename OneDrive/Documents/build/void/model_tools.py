"""Model routing: resolve provider/model, rotate credential pools, fallback chain, pickers."""
from __future__ import annotations

import os
import sys
import threading
from typing import Any

from .config import Config
from .providers import create_provider, provider_names
from .providers.base import Provider, ProviderError

_ROTATION_LOCK = threading.Lock()
# key signature -> number of failed attempts (401/429). Skipped while > 0; reset on success.
_KEY_FAILURES: dict[str, int] = {}


def _sig(provider_name: str, key: str | None) -> str:
    """Stable, non-reversible signature for a credential (never log raw keys)."""
    import hashlib
    raw = f"{provider_name}\x00{key or ''}".encode("utf-8", errors="replace")
    return f"{provider_name}:{hashlib.sha256(raw).hexdigest()[:16]}"



class ModelRouter:
    """Chooses provider+model for the main agent or auxiliary tasks, rotating API keys."""

    def __init__(self, config: Config, state=None):
        self.config = config
        self.state = state
        # task -> {"key": env key name, "sig": signature} of the key currently in use
        self._current_key: dict[str, str] = {}

    # ---- routing -----------------------------------------------------------------
    def route(self, task: str | None = None) -> tuple[Provider, str]:
        """Returns (provider_instance, model_id). task=None -> main model; else auxiliary.task."""
        provider_name = ""
        model = ""
        if task:
            aux = self.config.get(f"model.auxiliary.{task}") or {}
            provider_name = str(aux.get("provider") or self.config.get("model.provider") or "")
            model = str(aux.get("model") or "")
            base_url = str(aux.get("base_url") or "")
        else:
            provider_name = str(self.state.provider_override or "") if self.state else ""
            model = str(self.state.model_override or "") if self.state else ""
            provider_name = provider_name or str(self.config.get("model.provider") or "")
            model = model or str(self.config.get("model.default") or "")
            base_url = ""
        if not provider_name:
            raise ProviderError("no model configured — run `void model` or `void setup model`")
        provider = create_provider(provider_name, config=self.config)
        if base_url:
            provider.base_url = base_url
        if not model:
            models = provider.list_models()
            model = models[0] if models else ""
        provider.api_key = self._pick_key(provider_name, task)
        return provider, model

    def context_window(self, task: str | None = None) -> int:
        if task:
            aux = self.config.get(f"model.auxiliary.{task}") or {}
            return int(aux.get("context_window") or self.config.get("compression.context_window", 128000))
        return int(self.config.get("model.context_window") or self.config.get("compression.context_window", 128000))

    # ---- credential pool rotation ---------------------------------------------------
    def _pick_key(self, provider_name: str, task: str | None) -> str | None:
        keys = self.config.api_keys(provider_name)
        if not keys:
            self._current_key.pop(task or "main", None)
            return None
        if len(keys) == 1:
            self._current_key[task or "main"] = keys[0][:12]
            return keys[0]
        with _ROTATION_LOCK:
            alive = [k for k in keys if _KEY_FAILURES.get(_sig(provider_name, k), 0) < 3]
            if not alive:
                # all exhausted: reset and start over
                for k in keys:
                    _KEY_FAILURES.pop(_sig(provider_name, k), None)
                alive = keys
            key = alive[0]
            self._current_key[task or "main"] = key[:12]
            return key

    def report_key_result(self, provider_name: str, key: str | None, ok: bool,
                          retryable: bool = False) -> None:
        if not key:
            return
        sig = _sig(provider_name, key)
        with _ROTATION_LOCK:
            if ok:
                _KEY_FAILURES.pop(sig, None)
            elif retryable or True:  # 401/429 both count toward rotation
                _KEY_FAILURES[sig] = _KEY_FAILURES.get(sig, 0) + 1

    def key_status(self) -> dict[str, dict]:
        out: dict[str, dict] = {}
        for name in provider_names():
            keys = self.config.api_keys(name)
            out[name] = {
                "count": len(keys),
                "exhausted": sum(1 for k in keys if _KEY_FAILURES.get(_sig(name, k), 0) >= 3),
                "fingerprints": [k[:7] + "…" + k[-4:] if len(k) > 14 else "****" for k in keys],
            }
        return out


# ---- interactive pickers --------------------------------------------------------------
def pick_model_interactive(config: Config, router: ModelRouter | None = None,
                           show=None, prompt_toolkit=None) -> bool:
    """Full provider -> model -> api key -> test flow. Returns True if config was changed.

    `show(text)` prints feedback (plain print by default so it works from CLI or REPL).
    Falls back to numbered input if prompt_toolkit is unavailable.
    """
    show = show or (lambda s: print(s))
    pt = prompt_toolkit
    if pt is None:
        try:
            import prompt_toolkit as pt  # noqa: PLW0621
        except ImportError:
            pt = None

    def _can_draw_dialog() -> bool:
        """A full-screen dialog needs a real console screen buffer; pipes and CI don't
        have one, and radiolist_dialog raises rather than degrading on its own."""
        if pt is None:
            return False
        try:
            return bool(sys.stdin.isatty() and sys.stdout.isatty())
        except (AttributeError, ValueError):
            return False

    def _choose(title: str, options: list[str], default_idx: int = 0) -> int | None:
        if _can_draw_dialog():
            try:
                from prompt_toolkit.formatted_text import HTML
                from prompt_toolkit.shortcuts import radiolist_dialog
                return radiolist_dialog(
                    title=title, text=HTML(f"<b>{title}</b> (arrow keys, enter to select)"),
                    values=[(i, opt) for i, opt in enumerate(options)],
                    default=default_idx).run()
            except Exception as exc:
                show(f"  (could not draw the dialog: {exc}; using a numbered list)")
        # fallback: numbered list
        show(title)
        for i, opt in enumerate(options):
            marker = "*" if i == default_idx else " "
            show(f"  [{i + 1}] {marker}{opt}")
        show("  [0] cancel")
        while True:
            try:
                raw = input("number> ").strip()
                if not raw:
                    return default_idx
                idx = int(raw)
                if idx == 0:
                    return None
                return idx - 1 if 0 < idx <= len(options) else None
            except (ValueError, EOFError, KeyboardInterrupt):
                return None

    # 1. provider
    prov_names = provider_names()
    labels = {"openrouter": "OpenRouter (400+ models, one key)",
              "anthropic": "Anthropic (Claude)",
              "openai": "OpenAI (GPT)",
              "google-gemini": "Google Gemini",
              "custom": "Custom / open-source (LM Studio, Ollama, vLLM — OpenAI-compatible URL)"}
    current = config.get("model.provider") or ""
    default_idx = prov_names.index(current) if current in prov_names else 0
    idx = _choose("Select a provider", [labels.get(p, p) for p in prov_names], default_idx)
    if idx is None:
        return False
    provider_name = prov_names[idx]

    # 2. base_url for custom
    if provider_name == "custom":
        default_url = config.get("model.custom.base_url") or "http://localhost:11434/v1"
        show(f"Base URL for the OpenAI-compatible endpoint [{default_url}]: ")
        url = input("> ").strip() or default_url
        config.set("model.custom.base_url", url)

    # 3. api key (if missing)
    provider = create_provider(provider_name, config=config)
    key_names = provider.env_key_names()
    if provider_name != "custom" and not config.api_keys(provider_name):
        show(f"No API key found for {provider.display}. Get one from the provider's dashboard.")
        show(f"Enter {key_names[0]} (input hidden; stored in {config.env_path}): ")
        try:
            from getpass import getpass
            key = getpass("> ").strip()
        except Exception:
            key = input("> ").strip()
        if key:
            config.set_env(key_names[0], key)
            show(f"saved to {config.env_path}")
    elif provider_name != "custom":
        show(f"using existing {key_names[0]} from env")

    # 4. model list (live, cached fallback)
    models: list[str] = []
    show(f"fetching models from {provider.display}…")
    try:
        models = provider.list_models(timeout=15.0)
    except Exception as exc:
        show(f"live list failed ({exc}); falling back to static list")
    if not models:
        models = provider.fallback_models()
    if not models:
        show("no models available for this provider")
        return False
    cur_model = config.get("model.default") or ""
    default_idx = models.index(cur_model) if cur_model in models else 0
    if len(models) > 400:
        models = _filter_models_interactive(models, show, _choose)
        if models is None:
            return False
        default_idx = 0
    idx = _choose(f"Select a model ({len(models)} available)", models, default_idx)
    if idx is None:
        return False
    model = models[idx]

    # 5. save + test
    config.set("model.provider", provider_name)
    config.set("model.default", model)
    show(f"testing {provider_name}/{model}…")
    ok, msg = test_provider_model(config, provider_name, model)
    show(("✓ " if ok else "✗ ") + msg)
    if ok:
        show(f"saved: model.provider={provider_name}, model.default={model}")
    return True


def _filter_models_interactive(models: list[str], show, _choose) -> list[str] | None:
    show("Too many models — enter a filter substring (e.g. 'claude', 'free', 'gpt'):")
    try:
        needle = input("filter> ").strip().lower()
    except (EOFError, KeyboardInterrupt):
        return None
    if not needle:
        return models[:100]
    filtered = [m for m in models if needle in m.lower()]
    return filtered[:100] or models[:100]



# ---- test connection ----------------------------------------------------------------
def test_provider_model(config: Config, provider_name: str, model: str, api_key: str | None = None,
                        base_url: str | None = None) -> tuple[bool, str]:
    """One tiny non-streaming call. Returns (ok, message)."""
    try:
        provider = create_provider(provider_name, config=config)
        if base_url:
            provider.base_url = base_url
        if api_key:
            provider.api_key = api_key
        ok, msg = provider.test_connection(model)
        return ok, f"{provider_name}/{model}: {msg}"
    except ProviderError as exc:
        return False, str(exc)
    except Exception as exc:  # noqa: BLE001 — doctor must never crash
        return False, f"{type(exc).__name__}: {exc}"
