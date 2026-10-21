"""Google Gemini provider — google-generativeai SDK, OpenAI wire format translation."""
from __future__ import annotations

import json

from . import register_provider
from .base import Provider, ProviderError, StreamCb


@register_provider
class GeminiProvider(Provider):
    name = "google-gemini"
    display = "Google Gemini"
    env_keys = ["GEMINI_API_KEY", "GOOGLE_API_KEY"]
    default_models = ["gemini-2.0-flash", "gemini-1.5-flash", "gemini-1.5-pro"]

    def _configure(self):
        try:
            import google.generativeai as genai
        except ImportError as exc:
            raise ProviderError("google-generativeai not installed: pip install google-generativeai") from exc
        key = self.api_key or self.key_from_env()
        if not key:
            raise ProviderError("GEMINI_API_KEY not set in ~/.void/.env", status=401)
        genai.configure(api_key=key)
        return genai

    def _raise_for(self, exc: Exception) -> ProviderError:
        status = getattr(exc, "code", None)
        if status is None and hasattr(exc, "response"):
            status = getattr(exc.response, "status_code", None)
        return ProviderError(f"Gemini: {exc}", status=status,
                             retryable=status in (408, 429, 500, 503) or status in ("UNAVAILABLE", "RESOURCE_EXHAUSTED"))

    def list_models(self, timeout: float = 10.0) -> list[str]:
        genai = self._configure()
        try:
            models = []
            for m in genai.list_models(timeout=timeout):
                name = m.name.removeprefix("models/")
                supported = getattr(m, "supported_generation_methods", []) or []
                if "generateContent" in supported:
                    models.append(name)
            return sorted(models) or self.fallback_models()
        except ProviderError:
            raise
        except Exception as exc:
            raise self._raise_for(exc)

    def fallback_models(self) -> list[str]:
        return list(self.default_models)

    @staticmethod
    def _to_gemini(messages: list[dict]) -> tuple[str, list[dict]]:
        system_parts: list[str] = []
        out: list[dict] = []
        for m in messages:
            role = m.get("role", "user")
            content = m.get("content") or ""
            if role == "system":
                system_parts.append(str(content))
            elif role == "assistant":
                parts: list[dict] = []
                if content:
                    parts.append({"text": str(content)})
                for tc in m.get("tool_calls", []):
                    parts.append({"function_call": {"name": tc.get("name", ""), "args": tc.get("arguments") or {}}})
                out.append({"role": "model", "parts": parts or [{"text": ""}]})
            elif role == "tool":
                out.append({"role": "user", "parts": [{"function_response": {
                    "name": m.get("name") or m.get("tool_call_id") or "tool",
                    "response": {"result": str(content)[:60_000]}}}]})
            else:
                out.append({"role": "user", "parts": [{"text": str(content)}]})
        return "\n\n".join(system_parts), out

    def _tools_to_gemini(self, tools: list[dict] | None) -> list[dict]:
        decls = []
        for t in tools or []:
            fn = t.get("function", t)
            params = fn.get("parameters") or {"type": "object", "properties": {}}
            decls.append({"name": fn["name"], "description": fn.get("description", ""),
                          "parameters": params})
        return [{"function_declarations": decls}] if decls else []

    def chat_complete(self, messages: list[dict], model: str, tools: list[dict] | None = None,
                      stream_cb: StreamCb | None = None, temperature: float | None = None,
                      max_tokens: int | None = None, timeout: float | None = None) -> dict:
        genai = self._configure()
        system, conv = self._to_gemini(messages)
        gm = genai.GenerativeModel(model, system_instruction=system or None,
                                   tools=self._tools_to_gemini(tools) or None)
        gen_kwargs: dict = {}
        if temperature is not None:
            gen_kwargs["temperature"] = temperature
        if max_tokens:
            gen_kwargs["max_output_tokens"] = max_tokens
        try:
            if stream_cb is None:
                resp = gm.generate_content(conv, generation_config=gen_kwargs or None)
                return self._format(resp, model)
            return self._stream(gm, conv, gen_kwargs, stream_cb, model)
        except ProviderError:
            raise
        except Exception as exc:
            raise self._raise_for(exc)

    def _format(self, resp, model: str) -> dict:
        content_parts: list[str] = []
        tool_calls = []
        thought_parts: list[str] = []
        for cand in getattr(resp, "candidates", []) or []:
            for part in getattr(cand.content, "parts", []) or []:
                text = getattr(part, "text", None)
                if text:
                    content_parts.append(text)
                if getattr(part, "thought", None):
                    thought_parts.append(text or "")
                fc = getattr(part, "function_call", None)
                if fc is not None and getattr(fc, "name", None):
                    tool_calls.append({"id": f"call_{len(tool_calls)}", "name": fc.name,
                                       "arguments": dict(fc.args or {})})
        u = getattr(resp, "usage_metadata", None)
        return {"content": "".join(content_parts), "tool_calls": tool_calls,
                "reasoning": "".join(thought_parts) or None,
                "usage": {"prompt_tokens": getattr(u, "prompt_token_count", 0) or 0,
                          "completion_tokens": getattr(u, "candidates_token_count", 0) or 0},
                "finish_reason": "", "model": model}

    def _stream(self, gm, conv, gen_kwargs: dict, stream_cb: StreamCb, model: str) -> dict:
        content_parts: list[str] = []
        tool_calls = []
        usage = {"prompt_tokens": 0, "completion_tokens": 0}
        stream = gm.generate_content(conv, generation_config=gen_kwargs or None, stream=True)
        for chunk in stream:
            u = getattr(chunk, "usage_metadata", None)
            if u:
                usage = {"prompt_tokens": getattr(u, "prompt_token_count", 0) or 0,
                         "completion_tokens": getattr(u, "candidates_token_count", 0) or 0}
            for cand in getattr(chunk, "candidates", []) or []:
                for part in getattr(cand.content, "parts", []) or []:
                    text = getattr(part, "text", None)
                    fc = getattr(part, "function_call", None)
                    if text and not getattr(part, "thought", None):
                        content_parts.append(text)
                        stream_cb(text)
                    if fc is not None and getattr(fc, "name", None):
                        tool_calls.append({"id": f"call_{len(tool_calls)}", "name": fc.name,
                                           "arguments": dict(fc.args or {})})
        return {"content": "".join(content_parts), "tool_calls": tool_calls, "reasoning": None,
                "usage": usage, "finish_reason": "", "model": model}

