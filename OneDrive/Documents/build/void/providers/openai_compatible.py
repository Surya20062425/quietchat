"""OpenAI-compatible chat completion using the `openai` SDK. Base for openai/openrouter/custom."""
from __future__ import annotations

import json
from typing import Any

from . import register_provider
from .base import Provider, ProviderError, StreamCb


@register_provider
class OpenAICompatible(Provider):
    """Everything that speaks /v1/chat/completions with tools + streaming."""

    name = "openai"
    display = "OpenAI"
    env_keys = ["OPENAI_API_KEY"]
    base_url = "https://api.openai.com/v1"
    default_models: list[str] = ["gpt-4o", "gpt-4o-mini"]

    def _make_client(self, api_key: str | None = None):
        from openai import OpenAI
        key = api_key if api_key is not None else (self.api_key or self.key_from_env())
        if not key and self.name != "custom":
            raise ProviderError(
                f"{self.display} API key not set (expected env {self.env_keys[0]} in ~/.void/.env)", status=401)
        kwargs: dict[str, Any] = {"api_key": key or "none", "timeout": 180, "max_retries": 1}
        if self.base_url:
            kwargs["base_url"] = self.base_url
        return OpenAI(**kwargs)

    def _raise_for(self, exc: Exception) -> ProviderError:
        status = getattr(exc, "status_code", None)
        msg = str(getattr(exc, "message", None) or exc)
        retryable = status in (408, 409, 429, 500, 502, 503, 504)
        return ProviderError(f"{self.display}: {msg}", status=status, retryable=retryable)

    def list_models(self, timeout: float = 10.0) -> list[str]:
        try:
            client = self._make_client()
            models = sorted({m.id for m in client.models.list(timeout=timeout)})
            return models or self.fallback_models()
        except ProviderError:
            raise
        except Exception as exc:
            raise self._raise_for(exc)

    def fallback_models(self) -> list[str]:
        return list(self.default_models)

    @staticmethod
    def _messages_for_wire(messages: list[dict]) -> list[dict]:
        out = []
        for m in messages:
            role = m.get("role", "user")
            content = m.get("content")
            entry: dict[str, Any] = {"role": role, "content": content if content is not None else ""}
            if role == "assistant" and m.get("tool_calls"):
                entry["tool_calls"] = [
                    {"id": tc["id"], "type": "function",
                     "function": {"name": tc["name"], "arguments": tc["arguments"]}}
                    for tc in m["tool_calls"]]
            if role == "tool":
                entry["tool_call_id"] = m.get("tool_call_id") or ""
                if m.get("name"):
                    entry["name"] = m["name"]
            out.append(entry)
        return out

    def chat_complete(self, messages: list[dict], model: str, tools: list[dict] | None = None,
                      stream_cb: StreamCb | None = None,
                      temperature: float | None = None, max_tokens: int | None = None,
                      timeout: float | None = None) -> dict:
        try:
            client = self._make_client()
        except ProviderError:
            raise
        wire = self._messages_for_wire(messages)
        kwargs: dict[str, Any] = {"model": model, "messages": wire,
                                  "stream": stream_cb is not None}
        if stream_cb is not None:
            kwargs["stream_options"] = {"include_usage": True}
        if tools:
            kwargs["tools"] = tools
            kwargs["tool_choice"] = "auto"
        if temperature is not None:
            kwargs["temperature"] = temperature
        if max_tokens is not None:
            kwargs["max_tokens"] = max_tokens
        if timeout:
            kwargs["timeout"] = timeout

        try:
            if not stream_cb:
                resp = client.chat.completions.create(**kwargs)
                msg = resp.choices[0].message if resp.choices else None
                return self._format_message(msg, getattr(resp, "usage", None),
                                            getattr(resp, "model", model))
            return self._stream(client, kwargs, stream_cb, model)
        except Exception as exc:
            raise self._raise_for(exc)

    def _stream(self, client, kwargs: dict, stream_cb: StreamCb, model: str) -> dict:
        content_parts: list[str] = []
        tool_calls: dict[int, dict] = {}
        reasoning_parts: list[str] = []
        usage = {"prompt_tokens": 0, "completion_tokens": 0}
        finish_reason = ""
        for event in client.chat.completions.create(**kwargs):
            if not getattr(event, "choices", None):
                u = getattr(event, "usage", None)
                if u:
                    usage = {"prompt_tokens": getattr(u, "prompt_tokens", 0) or 0,
                             "completion_tokens": getattr(u, "completion_tokens", 0) or 0}
                continue
            choice = event.choices[0]
            delta = choice.delta
            if getattr(choice, "finish_reason", None):
                finish_reason = str(choice.finish_reason)
            text = getattr(delta, "content", None)
            if text:
                content_parts.append(text)
                stream_cb(text)
            think = getattr(delta, "reasoning_content", None) or getattr(delta, "reasoning", None)
            if think:
                reasoning_parts.append(str(think))
            for tc in (getattr(delta, "tool_calls", None) or []):
                idx = getattr(tc, "index", 0) or 0
                slot = tool_calls.setdefault(idx, {"id": "", "name": "", "arguments": ""})
                if getattr(tc, "id", None):
                    slot["id"] = tc.id
                fn = getattr(tc, "function", None)
                if fn:
                    if getattr(fn, "name", None):
                        slot["name"] = fn.name
                    if getattr(fn, "arguments", None):
                        slot["arguments"] += fn.arguments
        return self._assemble("".join(content_parts), tool_calls, "".join(reasoning_parts),
                              usage, finish_reason, model)

    @staticmethod
    def _assemble(content: str, tool_calls: dict, reasoning: str, usage: dict,
                  finish_reason: str, model: str) -> dict:
        final_tools = []
        for idx in sorted(tool_calls):
            tc = tool_calls[idx]
            try:
                args_obj = json.loads(tc["arguments"]) if str(tc["arguments"]).strip() else {}
            except json.JSONDecodeError:
                args_obj = {"_raw": tc["arguments"]}
            final_tools.append({"id": tc["id"] or f"call_{idx}", "name": tc["name"], "arguments": args_obj})
        return {"content": content, "tool_calls": final_tools, "reasoning": reasoning or None,
                "usage": usage, "finish_reason": finish_reason, "model": model}

    def _format_message(self, msg, usage, model: str) -> dict:
        tool_calls: dict[int, dict] = {}
        for i, tc in enumerate(getattr(msg, "tool_calls", None) or []):
            fn = tc.function
            tool_calls[i] = {"id": getattr(tc, "id", "") or f"call_{i}", "name": fn.name,
                             "arguments": fn.arguments if isinstance(fn.arguments, str) else json.dumps(fn.arguments or {})}
        u = usage
        return self._assemble(
            getattr(msg, "content", None) or "", tool_calls,
            getattr(msg, "reasoning_content", None) or "",
            {"prompt_tokens": getattr(u, "prompt_tokens", 0) or 0,
             "completion_tokens": getattr(u, "completion_tokens", 0) or 0},
            str(getattr(msg, "finish_reason", "") or ""), model)


