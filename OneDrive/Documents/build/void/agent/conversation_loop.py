"""agent/conversation_loop.py — the ONE core loop.

run_conversation() is attached to AIAgent (run_agent.py). Everything (REPL, chat -q, cron,
webhooks, kanban dispatcher, delegate, MCP) funnels through it.
"""
from __future__ import annotations

import json
import time

from ..config import Config
from ..logging_setup import get_logger
from ..providers.base import ProviderError
from ..tools.registry import ToolContext, registry, result_to_json, set_context

log = get_logger("agent")

MAX_TOOL_RESULT_CHARS_DEFAULT = 20000


def call_model_with_retry(agent, messages: list[dict], tools: list[dict] | None) -> dict:
    """One model call with credential-pool rotation on auth/rate-limit failures."""
    router = agent.router
    provider, model = router.route()
    attempts = 0
    last_error: ProviderError | None = None
    while attempts < 3:
        if agent.state.cancel_token.cancelled():
            raise ProviderError("cancelled by user", status=499)
        attempts += 1
        try:
            t0 = time.time()
            resp = provider.chat_complete(
                messages, model, tools=tools,
                stream_cb=agent.stream_cb if agent.streaming_enabled() else None,
                temperature=agent.config.get("agent.temperature"),
                max_tokens=agent.config.get("agent.max_tokens"),
                timeout=agent.config.get("agent.request_timeout", 180))
            usage = resp.get("usage") or {}
            agent.state.add_usage(usage.get("prompt_tokens", 0), usage.get("completion_tokens", 0))
            agent.last_latency = time.time() - t0
            router.report_key_result(provider.name, provider.api_key, ok=True)
            agent.last_provider, agent.last_model = provider.name, model
            return resp
        except ProviderError as exc:
            last_error = exc
            retryable = bool(getattr(exc, "retryable", False)) or getattr(exc, "status", None) in (401, 429)
            router.report_key_result(provider.name, provider.api_key, ok=False, retryable=retryable)
            if retryable and attempts < 3:
                keys = agent.config.api_keys(provider.name)
                if len(keys) > 1:
                    log.warning("rotating API key for %s after %s", provider.name, exc)
                    provider, model = router.route()  # re-pick skips exhausted keys
                    continue
            raise
    raise last_error or ProviderError("model call failed")


def build_user_message(agent, user_message: str, images: list[str] | None) -> dict:
    """user role message; images become vision-style content parts."""
    if images:
        import base64
        import mimetypes
        parts: list[dict] = [{"type": "text", "text": user_message or "Describe this image."}]
        for path in images:
            fp = str(path)
            if not fp:
                continue
            mime = mimetypes.guess_type(fp)[0] or "image/png"
            with open(fp, "rb") as f:
                b64 = base64.b64encode(f.read()).decode("ascii")
            parts.append({"type": "image_url", "image_url": {"url": f"data:{mime};base64,{b64}"}})
        return {"role": "user", "content": parts}
    return {"role": "user", "content": user_message}

# ---- tool dispatch ---------------------------------------------------------------
def make_tool_context(agent, depth: int = 0) -> ToolContext:
    return ToolContext(
        config=agent.config, state=agent.state, db=agent.db, memory=agent.memory,
        session_id=agent.state.session_id, depth=depth,
        on_event=agent.on_event, approval_hook=agent.approval_hook,
        cancel_token=agent.state.cancel_token, agent_factory=agent.agent_factory,
        cli=agent.cli)


def dispatch_tool(agent, name: str, args: dict, tool_ctx: ToolContext, depth: int = 0) -> dict:
    """Dispatch one tool call with security approval + redaction + event emission."""
    max_chars = int(agent.config.get("tools.max_result_chars", MAX_TOOL_RESULT_CHARS_DEFAULT) or 20000)
    tool_ctx.emit({"type": "tool_start", "name": name, "args": args})

    # security gate for shell commands
    if name == "terminal" and isinstance(args, dict):
        cmd = str(args.get("command") or "")
        from ..security import check_command
        dangerous, reason = check_command(cmd, agent.config.get("security.dangerous_patterns") or [])
        if dangerous and not agent.state.yolo and agent.config.get("security.approvals.enabled", True):
            allowed = bool(agent.approval_hook(f"Dangerous command ({reason}):\n  {cmd}")) \
                if agent.approval_hook else False
            if not allowed:
                tool_ctx.emit({"type": "warning", "text": f"command blocked: {reason}"})
                res = {"ok": False, "error": f"approval denied: dangerous command ({reason}). "
                                             "Ask the user to approve or run /yolo to bypass."}
                res["_json"] = result_to_json(res, max_chars)
                return res

    set_context(tool_ctx)
    try:
        result = registry.dispatch(name, args)
    finally:
        set_context(None)

    if agent.config.get("security.redaction.enabled", True):
        result = _redact_deep(result, agent.config.get("security.redaction.patterns") or [])
    tool_ctx.emit({"type": "tool_result", "name": name, "ok": bool(result.get("ok", True)),
                   "preview": result_to_json(result, 400)})
    result["_json"] = result_to_json(result, max_chars)
    return result


def _redact_deep(obj, patterns: list[str]):
    if isinstance(obj, dict):
        return {k: _redact_deep(v, patterns) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_redact_deep(v, patterns) for v in obj]
    if isinstance(obj, str):
        from ..security import redact
        return redact(obj, patterns, enabled=True)
    return obj


BLOCKED_IN_CHILDREN = {"delegate", "cron", "webhook"}

# ---- the loop --------------------------------------------------------------------
def run_conversation(self, user_message: str, images: list | None = None,
                     max_iterations: int | None = None) -> str:
    """ReAct loop: model → tool calls → dispatch → append results → repeat.

    Returns the final assistant text. Cancellation is checked every iteration.
    """
    max_iter = int(max_iterations or self.config.get("tools.max_iterations", 90) or 90)
    self.state.iteration = 0
    self.state.last_error = ""
    self.state.stream_enabled = True

    # memory is injected at session start (rule 3: opt-in context, not per-turn)
    if self.memory is not None and len(self.messages) <= 1:
        self.messages[0]["content"] = self.build_system_prompt()

    user_msg = build_user_message(self, user_message, images)
    self.messages.append(user_msg)
    if self.db is not None and self.state.session_id:
        self.db.append_message(self.state.session_id, "user", user_message,
                               meta={"images": [str(i) for i in images]} if images else None)
        self.state.turn_count += 1

    tool_ctx = make_tool_context(self)
    final_text = ""

    while self.state.iteration < max_iter:
        if self.state.cancel_token.cancelled():
            self.state.last_error = "cancelled"
            return final_text or "(cancelled)"
        self.state.iteration += 1
        try:
            resp = call_model_with_retry(self, self.messages, self.tools_schemas)
        except ProviderError as exc:
            if getattr(exc, "status", None) == 499:
                self.state.last_error = "cancelled"
                return final_text or "(cancelled)"
            self.state.last_error = str(exc)
            raise
        except KeyboardInterrupt:
            self.state.last_error = "cancelled"
            return final_text or "(cancelled by user)"

        content = str(resp.get("content") or "")
        reasoning = resp.get("reasoning")
        if reasoning and self.config.get("display.show_reasoning") and self.on_event:
            self.on_event({"type": "reasoning", "text": str(reasoning)})

        raw_tool_calls = resp.get("tool_calls") or []
        assistant_msg: dict = {"role": "assistant", "content": content}
        if raw_tool_calls:
            assistant_msg["tool_calls"] = [
                {"id": tc.get("id") or f"call_{i}", "name": tc.get("name") or "",
                 "arguments": tc.get("arguments") if isinstance(tc.get("arguments"), str)
                 else json.dumps(tc.get("arguments") or {})}
                for i, tc in enumerate(raw_tool_calls)]
        self.messages.append(assistant_msg)
        self._persist_assistant(assistant_msg)

        if not raw_tool_calls:
            final_text = content
            self.state.last_response = final_text
            if self.checkpoints is not None:
                self.checkpoints.maybe_checkpoint(self.state.turn_count, force=True)
            return final_text

        for tc in raw_tool_calls:
            if self.state.cancel_token.cancelled():
                break
            call_id = str(tc.get("id") or f"call_{len(self.messages)}")
            name = str(tc.get("name") or "")
            args = tc.get("arguments")
            if isinstance(args, str):
                try:
                    args = json.loads(args) if args.strip() else {}
                except json.JSONDecodeError as exc:
                    result = {"ok": False, "error": f"invalid tool args JSON: {exc}"}
                    args = {}
                    self._append_tool_result(call_id, name, result)
                    continue
            elif not isinstance(args, dict):
                args = {}

            if self.state.active_tools and name not in self.state.active_tools:
                result = {"ok": False, "error": f"tool '{name}' is not enabled in this session"}
            elif name in BLOCKED_IN_CHILDREN and self.state.depth > 0:
                result = {"ok": False, "error": f"tool '{name}' is blocked in child agents"}
            else:
                result = dispatch_tool(self, name, args, tool_ctx, self.state.depth)
            self._append_tool_result(call_id, name, result)

        if self.checkpoints is not None:
            self.checkpoints.maybe_checkpoint(self.state.turn_count, force=False)
        if self.compressor is not None and self.compressor.should_compress(self.messages, self.tools_schemas):
            new_messages, info = self.compressor.compress(self.messages)
            if info.get("compressed"):
                self.messages = new_messages
                if self.on_event:
                    self.on_event({"type": "info",
                                   "text": f"context compressed ({info.get('summarized_messages')} messages "
                                           f"summarized, ~{info.get('tokens_before')}→{info.get('tokens_after')} tokens)"})
        # loop again: the model must see the tool results

    final_text = final_text or "(max iterations reached without a final answer)"
    log.warning("max iterations (%d) hit", max_iter)
    self.state.last_error = "max_iterations"
    return final_text


