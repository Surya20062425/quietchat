"""Anthropic provider — direct anthropic SDK, messages translated to/from OpenAI wire format."""
from __future__ import annotations

import json

from . import register_provider
from .base import Provider, ProviderError, StreamCb


@register_provider
class AnthropicProvider(Provider):
    name = "anthropic"
    display = "Anthropic"
    env_keys = ["ANTHROPIC_API_KEY"]
    default_models = ["claude-sonnet-4-20250514", "claude-3-5-haiku-20241022", "claude-3-5-sonnet-20241022"]

    def _make_client(self):
        try:
            import anthropic
        except ImportError as exc:
            raise ProviderError("anthropic SDK not installed: pip install anthropic") from exc
        key = self.api_key or self.key_from_env()
        if not key:
            raise ProviderError("ANTHROPIC_API_KEY not set in ~/.void/.env", status=401)
        return anthropic.Anthropic(api_key=key, max_retries=1)

    def _raise_for(self, exc: Exception) -> ProviderError:
        status = getattr(exc, "status_code", None)
        return ProviderError(f"Anthropic: {exc}", status=status,
                             retryable=status in (408, 429, 500, 502, 503, 504, 529))

    def list_models(self, timeout: float = 10.0) -> list[str]:
        try:
            client = self._make_client()
            models = sorted({m.id for m in client.models.list(timeout=timeout).data})
            return models or self.fallback_models()
        except ProviderError:
            raise
        except Exception as exc:
            raise self._raise_for(exc)

    def fallback_models(self) -> list[str]:
        return list(self.default_models)

    # ---- translation ------------------------------------------------------------
    @staticmethod
    def _to_anthropic(messages: list[dict]) -> tuple[str, list[dict]]:
        """Returns (system, converted messages)."""
        system_parts: list[str] = []
        out: list[dict] = []
        for m in messages:
            role = m.get("role", "user")
            content = m.get("content") or ""
            if role == "system":
                system_parts.append(str(content))
                continue
            if role == "assistant":
                blocks: list[dict] = []
                if content:
                    blocks.append({"type": "text", "text": str(content)})
                for tc in m.get("tool_calls", []):
                    args = tc.get("arguments")
                    if not isinstance(args, dict):
                        try:
                            args = json.loads(str(args) or "{}")
                        except json.JSONDecodeError:
                            args = {}
                    blocks.append({"type": "tool_use", "id": tc.get("id") or "call_0",
                                   "name": tc.get("name", ""), "input": args})
                out.append({"role": "assistant", "content": blocks or [{"type": "text", "text": ""}]})
            elif role == "tool":
                out.append({"role": "user", "content": [{
                    "type": "tool_result",
                    "tool_use_id": m.get("tool_call_id") or "call_0",
                    "content": str(content)[:100_000],
                }]})
            else:
                out.append({"role": "user", "content": str(content)})
        return "\n\n".join(system_parts), out

    @staticmethod
    def _tools_to_anthropic(tools: list[dict] | None) -> list[dict]:
        out = []
        for t in tools or []:
            fn = t.get("function", t)
            out.append({"name": fn["name"], "description": fn.get("description", ""),
                        "input_schema": fn.get("parameters") or {"type": "object", "properties": {}}})
        return out

    def chat_complete(self, messages: list[dict], model: str, tools: list[dict] | None = None,
                      stream_cb: StreamCb | None = None, temperature: float | None = None,
                      max_tokens: int | None = None, timeout: float | None = None) -> dict:
        try:
            client = self._make_client()
        except ProviderError:
            raise
        system, conv = self._to_anthropic(messages)
        kwargs: dict = {
            "model": model, "messages": conv,
            "max_tokens": max_tokens or 8192,
            "tools": self._tools_to_anthropic(tools) or None,
        }
        if system:
            kwargs["system"] = system
        if temperature is not None:
            kwargs["temperature"] = temperature
        if timeout:
            kwargs["timeout"] = timeout
        kwargs = {k: v for k, v in kwargs.items() if v is not None}
        try:
            if stream_cb is None:
                resp = client.messages.create(**kwargs)
                return self._format(resp, model)
            return self._stream(client, kwargs, stream_cb, model)
        except ProviderError:
            raise
        except Exception as exc:
            raise self._raise_for(exc)

    def _format(self, resp, model: str) -> dict:
        content_parts: list[str] = []
        thinking_parts: list[str] = []
        tool_calls = []
        for block in resp.content:
            btype = getattr(block, "type", "")
            if btype == "text":
                content_parts.append(getattr(block, "text", ""))
            elif btype == "thinking":
                thinking_parts.append(getattr(block, "thinking", ""))
            elif btype == "tool_use":
                tool_calls.append({"id": getattr(block, "id", "") or f"call_{len(tool_calls)}",
                                   "name": block.name, "arguments": dict(getattr(block, "input", {}) or {})})
        u = getattr(resp, "usage", None)
        return {"content": "".join(content_parts), "tool_calls": tool_calls,
                "reasoning": "".join(thinking_parts) or None,
                "usage": {"prompt_tokens": getattr(u, "input_tokens", 0) or 0,
                          "completion_tokens": getattr(u, "output_tokens", 0) or 0},
                "finish_reason": str(getattr(resp, "stop_reason", "") or ""), "model": model}

    def _stream(self, client, kwargs: dict, stream_cb: StreamCb, model: str) -> dict:
        content_parts: list[str] = []
        thinking_parts: list[str] = []
        tools_by_id: dict[str, dict] = {}
        usage = {"prompt_tokens": 0, "completion_tokens": 0}
        finish_reason = ""
        with client.messages.stream(**kwargs) as stream:
            for event in stream:
                etype = getattr(event, "type", "")
                if etype == "content_block_delta":
                    delta = event.delta
                    dtype = getattr(delta, "type", "")
                    if dtype == "text_delta":
                        chunk = getattr(delta, "text", "")
                        content_parts.append(chunk)
                        stream_cb(chunk)
                    elif dtype == "thinking_delta":
                        thinking_parts.append(getattr(delta, "thinking", ""))
                elif etype == "content_block_start":
                    block = getattr(event, "content_block", None)
                    if getattr(block, "type", "") == "tool_use":
                        tools_by_id[block.id] = {"id": block.id, "name": block.name, "arguments": {}}
                elif etype == "message_delta":
                    if getattr(event.delta, "stop_reason", None):
                        finish_reason = str(event.delta.stop_reason)
                    u = getattr(event, "usage", None)
                    if u and getattr(u, "output_tokens", None):
                        usage["completion_tokens"] = u.output_tokens
            final = stream.get_final_message()
            u = getattr(final, "usage", None)
            if u:
                usage["prompt_tokens"] = getattr(u, "input_tokens", 0) or 0
                usage["completion_tokens"] = getattr(u, "output_tokens", 0) or 0
        return {"content": "".join(content_parts), "tool_calls": list(tools_by_id.values()),
                "reasoning": "".join(thinking_parts) or None, "usage": usage,
                "finish_reason": finish_reason, "model": model}

    def test_connection(self, model: str | None = None) -> tuple[bool, str]:
        try:
            client = self._make_client()
            msg = client.messages.create(
                model=model or self.fallback_models()[-1], max_tokens=10,
                messages=[{"role": "user", "content": "Reply with exactly: ok"}])
            text = "".join(getattr(b, "text", "") for b in msg.content).strip()
            return True, f"model responded: {text[:60]!r}"
        except ProviderError as exc:
            return False, str(exc)
        except Exception as exc:
            return False, f"{type(exc).__name__}: {exc}"

