"""Context compression: track tokens, and when over threshold, summarize the middle
of the conversation with a cheap auxiliary model.

Strategy: split the conversation into segments (each starts at a user message).
Protect the first `protect_first` and last `protect_last` segments; summarize the
middle into a single user message.
"""
from __future__ import annotations

from typing import Callable

from .logging_setup import get_logger

log = get_logger("compression")

CHARS_PER_TOKEN = 4


def estimate_message_tokens(message: dict) -> int:
    content = message.get("content")
    if isinstance(content, list):  # vision-style parts
        content = " ".join(str(p.get("text", "")) if isinstance(p, dict) else str(p) for p in content)
    n = len(str(content or "")) // CHARS_PER_TOKEN
    for tc in message.get("tool_calls") or []:
        n += len(str(tc.get("arguments", ""))) // CHARS_PER_TOKEN + 20
    return max(1, n)


def estimate_conversation_tokens(messages: list[dict], tools: list[dict] | None = None) -> int:
    total = sum(estimate_message_tokens(m) for m in messages)
    if tools:
        total += len(str(tools)) // CHARS_PER_TOKEN
    return total


class ContextCompressor:
    def __init__(self, config, router):
        self.config = config
        self.router = router

    # ---- config ---------------------------------------------------------------
    @property
    def enabled(self) -> bool:
        return bool(self.config.get("compression.enabled", True))

    def context_window(self) -> int:
        return int(self.config.get("compression.context_window", 128000) or 128000)

    def threshold(self) -> float:
        try:
            return float(self.config.get("compression.threshold", 0.85))
        except (TypeError, ValueError):
            return 0.85

    # ---- check ------------------------------------------------------------------
    def should_compress(self, messages: list[dict], tools: list[dict] | None = None) -> bool:
        if not self.enabled:
            return False
        budget = int(self.context_window() * self.threshold())
        used = estimate_conversation_tokens(messages, tools)
        return used >= budget

    # ---- segmentation ---------------------------------------------------------------
    @staticmethod
    def _segments(messages: list[dict]) -> list[list[int]]:
        """Group message indices: each segment starts at a user message (or is leading system)."""
        segments: list[list[int]] = []
        current: list[int] = []
        for i, m in enumerate(messages):
            if m.get("role") == "user" and current:
                segments.append(current)
                current = [i]
            else:
                current.append(i)
        if current:
            segments.append(current)
        return segments

    def compress(self, messages: list[dict], stream_cb: Callable | None = None) -> tuple[list[dict], dict]:
        """Return (new_messages, info). On summary failure returns the original messages."""
        protect_first = int(self.config.get("compression.protect_first", 3))
        protect_last = int(self.config.get("compression.protect_last", 4))
        segments = self._segments(messages)
        if len(segments) <= protect_first + protect_last + 1:
            return messages, {"compressed": False, "reason": "too few segments"}

        head = [i for seg in segments[:protect_first] for i in seg]
        tail = [i for seg in segments[-protect_last:]] if protect_last else []
        middle = []
        stop = len(segments) - protect_last if protect_last else len(segments)
        for seg in segments[protect_first:stop]:
            middle.extend(seg)

        dump_parts: list[str] = []
        for i in middle:
            m = messages[i]
            role = m.get("role", "?")
            content = m.get("content")
            if isinstance(content, list):
                content = " ".join(str(p.get("text", "")) if isinstance(p, dict) else str(p) for p in content)
            if role == "tool":
                dump_parts.append(f"[tool {m.get('name') or m.get('tool_call_id')}]: {str(content)[:2000]}")
            elif role == "assistant" and m.get("tool_calls"):
                calls = ", ".join(tc.get("name", "?") for tc in m["tool_calls"])
                dump_parts.append(f"[assistant called tools: {calls}] {str(content)[:800]}")
            else:
                dump_parts.append(f"[{role}]: {str(content)[:4000]}")
        transcript = "\n".join(dump_parts)[-100_000:]

        summary = self._summarize(transcript)
        if not summary:
            return messages, {"compressed": False, "reason": "summary call failed"}

        summary_msg = {
            "role": "user",
            "content": ("[context compressed] Summary of the earlier conversation:\n\n" + summary +
                        "\n\n(Continue from here. Older tool output was summarized away; "
                        "re-read files or re-run commands if you need exact contents.)"),
        }
        new_messages = [messages[i] for i in head] + [summary_msg] + [messages[i] for i in tail]
        return new_messages, {"compressed": True, "summarized_messages": len(middle),
                              "tokens_before": estimate_conversation_tokens(messages),
                              "tokens_after": estimate_conversation_tokens(new_messages)}

    def _summarize(self, transcript: str) -> str:
        try:
            provider, model = self.router.route("compression")
        except Exception as exc:
            log.warning("no compression model available: %s", exc)
            return ""
        prompt = (
            "Summarize this working transcript for an AI agent that must continue the task. "
            "Keep: the user's goal, decisions made, file paths, commands run and their outcomes, "
            "open questions, and next steps. Be terse — bullet points, under 500 words.\n\n"
            "TRANSCRIPT:\n" + transcript)
        try:
            resp = provider.chat_complete([{"role": "user", "content": prompt}], model,
                                          tools=None, max_tokens=900, timeout=120)
            return str(resp.get("content") or "").strip()
        except Exception as exc:
            log.warning("compression summary failed: %s", exc)
            return ""

