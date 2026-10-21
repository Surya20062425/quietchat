"""AIAgent — one class, one loop, many entry points.

REPL, `chat -q`, cron, webhooks, kanban dispatcher, delegate and MCP all construct this
class (directly or via agent_factory) and call run_conversation().
"""
from __future__ import annotations

import json
import threading

from ..context import build_system_prompt as _assemble_prompt
from ..logging_setup import get_logger
from ..model_tools import ModelRouter
from ..tools.registry import registry
from .conversation_loop import make_tool_context, run_conversation


class AIAgent:
    """Synchronous ReAct agent. run_conversation() lives in conversation_loop.py."""

    run_conversation = run_conversation

    def __init__(self, config, state, db=None, cli=None, session_id: str | None = None):
        self.config = config
        self.state = state
        self.db = db
        self.cli = cli
        self.log = get_logger("agent")
        self.system_extra = ""

        self.router = ModelRouter(config, state)
        self.provider, self.model = self.router.route()
        self.last_provider, self.last_model = self.provider.name, self.model
        self.last_latency = 0.0

        self.memory = self._build_memory()
        self.compressor = self._build_compressor()
        self.checkpoints = self._build_checkpoints()

        self.tools_schemas = self._load_tool_schemas()
        self.messages: list[dict] = [{"role": "system", "content": self.build_system_prompt()}]

        if session_id:
            self.state.session_id = session_id
            self.load_history(session_id)

    # ---- construction -----------------------------------------------------------
    def _load_tool_schemas(self) -> list[dict]:
        enabled = self.state.enabled_toolsets or self.config.get("toolsets.enabled") or ["all"]
        disabled = list(self.config.get("toolsets.disabled") or []) + list(self.state.disabled_toolsets or [])
        active = registry.expand_toolsets(enabled, disabled)
        if self.state.depth > 0:
            active = [t for t in active if t not in ("delegate", "cron", "webhook")]
        self.state.active_tools = active
        return [registry.to_openai_schema(t) for t in active]

    def _build_memory(self):
        if not self.config.get("memory.enabled", True):
            self.state.memory_enabled = False
            return None
        from ..memory import MemoryStores
        return MemoryStores(self.config)

    def _build_compressor(self):
        if not self.config.get("compression.enabled", True):
            return None
        from ..compression import ContextCompressor
        return ContextCompressor(self.config, self.router)

    def _build_checkpoints(self):
        if not self.config.get("sessions.checkpoint.enabled"):
            return None
        try:
            from ..checkpoints import CheckpointManager
        except ImportError:
            return None
        return CheckpointManager(self.config)

    # ---- system prompt ------------------------------------------------------------
    def build_system_prompt(self) -> str:
        return _assemble_prompt(self.config, self.state, self.memory,
                                self.state.active_tools, registry)

    # ---- streaming / events --------------------------------------------------------
    def streaming_enabled(self) -> bool:
        return bool(self.config.get("display.streaming", True)) and bool(self.state.stream_enabled)

    def stream_cb(self, text: str) -> None:
        if self.on_event:
            self.on_event({"type": "token", "text": text})

    def on_event(self, event: dict) -> None:
        handler = self.state.on_event
        if handler:
            try:
                handler(event)
            except Exception:
                pass

    def approval_hook(self, text: str) -> bool:
        hook = self.state.approval_hook
        if hook:
            try:
                return bool(hook(text))
            except Exception:
                return False
        return bool(self.state.yolo)  # non-interactive: only yolo can auto-approve

    # ---- persistence ---------------------------------------------------------------
    def _persist_assistant(self, message: dict) -> None:
        if self.db is None or not self.state.session_id:
            return
        tool_calls = message.get("tool_calls") or []
        self.db.append_message(self.state.session_id, "assistant",
                               message.get("content") or "",
                               meta={"tool_calls": tool_calls} if tool_calls else None)

    def _append_tool_result(self, call_id: str, name: str, result: dict) -> None:
        content = result.get("_json") or json.dumps(result, ensure_ascii=False, default=str)
        self.messages.append({"role": "tool", "tool_call_id": call_id, "name": name, "content": content})
        if self.db is not None and self.state.session_id:
            self.db.append_message(self.state.session_id, "tool", content,
                                   tool_call_id=call_id, name=name)
            sess = self.db.get_session(self.state.session_id) or {}
            self.db.touch_session(self.state.session_id, turns=int(sess.get("turns", 0) or 0) + 1)

    def load_history(self, session_id: str) -> None:
        """Rebuild self.messages from the DB for --resume / --continue."""
        if self.db is None:
            return
        rows = self.db.get_messages(session_id)
        self.messages = [{"role": "system", "content": self.build_system_prompt()}]
        for row in rows:
            meta = {}
            if row.get("meta"):
                try:
                    meta = json.loads(row["meta"])
                except (json.JSONDecodeError, TypeError):
                    meta = {}
            role = row["role"]
            content = row["content"]
            if meta.get("images"):
                from .conversation_loop import build_user_message
                self.messages.append(build_user_message(self, content, meta["images"]))
            elif role == "assistant":
                msg: dict = {"role": "assistant", "content": content}
                if meta.get("tool_calls"):
                    msg["tool_calls"] = meta["tool_calls"]
                self.messages.append(msg)
            elif role == "tool":
                self.messages.append({"role": "tool", "tool_call_id": row.get("tool_call_id"),
                                      "name": row.get("name"), "content": content})
            else:
                self.messages.append({"role": role, "content": content})
        sess = self.db.get_session(session_id) or {}
        self.state.session_id = session_id
        self.state.session_title = sess.get("title")
        self.state.turn_count = int(sess.get("turns", 0) or 0)
        self.state.resumed_from = sess.get("title") or session_id

    # ---- delegation / background ----------------------------------------------------
    def agent_factory(self, prompt: str, toolsets: list[str] | None = None,
                      depth: int | None = None, label: str = "child") -> "AIAgent":
        """Spawn a child agent: shares config/db/toolsets, isolated conversation history."""
        from ..state import RuntimeState
        new_depth = depth if depth is not None else self.state.depth + 1
        if new_depth > 2:
            raise ValueError("max delegation depth (2) reached")
        child_state = RuntimeState()
        child_state.depth = new_depth
        child_state.session_id = self.state.session_id
        child_state.model_override = self.state.model_override
        child_state.provider_override = self.state.provider_override
        child_state.enabled_toolsets = toolsets or ["terminal", "file", "web"]
        child_state.disabled_toolsets = list(self.state.disabled_toolsets)
        child_state.yolo = self.state.yolo
        child_state.on_event = self.state.on_event
        child_state.approval_hook = self.state.approval_hook
        child_state.stream_enabled = False   # child output must not fight the parent UI
        child_state.loaded_skills = dict(self.state.loaded_skills)
        child = AIAgent(self.config, child_state, db=self.db, cli=self.cli)
        child.log = self.log
        return child

    def run_background(self, prompt: str, toolsets: list[str] | None = None) -> threading.Thread:
        """/background: run one agent turn on a daemon thread; report via on_event."""
        def _runner() -> None:
            try:
                child = self.agent_factory(prompt, toolsets=toolsets, label="background")
                text = child.run_conversation(prompt)
                self.log.info("background task finished: %.300s", text)
                self.on_event({"type": "background_done", "text": text})
            except Exception as exc:
                self.log.error("background task failed: %s", exc)
                self.on_event({"type": "background_done", "text": f"(background task failed: {exc})"})
        th = threading.Thread(target=_runner, daemon=True, name="void-background")
        th.start()
        return th

    def flush_memory(self) -> None:
        if self.memory is not None:
            self.memory.flush_if_needed(self.state.turn_count)

