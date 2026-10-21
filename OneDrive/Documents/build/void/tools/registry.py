"""Tool registry + AST-based discovery.

Each tool is a plain Python file in void/tools/ that calls `registry.register(...)` at
module top level. Discovery parses each file's AST first and imports ONLY files that
contain a top-level register call — so partial files mid-edit and helper modules are
never imported. Toolsets are named groups (with recursion); dispatch runs sync or
async handlers and always returns a JSON-serializable dict.
"""
from __future__ import annotations

import ast
import asyncio
import importlib
import json
import threading
import traceback
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable


@dataclass
class ToolDef:
    name: str
    description: str
    parameters: dict            # JSON Schema subset: {"type":"object","properties":{...},"required":[...]}
    handler: Callable           # fn(args: dict) -> dict (or awaitable)
    toolset: str = "general"


@dataclass
class ToolContext:
    """Everything a tool handler may need. Set per-thread via set_context()."""
    config: Any = None
    state: Any = None
    db: Any = None
    memory: Any = None
    session_id: str | None = None
    depth: int = 0
    on_event: Callable | None = None
    approval_hook: Callable | None = None
    cancel_token: Any = None
    agent_factory: Callable | None = None   # (prompt, toolsets, depth, label) -> child agent
    cli: Any = None

    def emit(self, event: dict) -> None:
        if self.on_event:
            try:
                self.on_event(event)
            except Exception:
                pass


_tls = threading.local()


def set_context(ctx: ToolContext | None) -> None:
    _tls.ctx = ctx


def get_context() -> ToolContext:
    ctx = getattr(_tls, "ctx", None)
    if ctx is None:
        ctx = ToolContext()
        _tls.ctx = ctx
    return ctx


class ToolRegistry:
    def __init__(self) -> None:
        self._tools: dict[str, ToolDef] = {}
        self._toolsets: dict[str, list[str]] = {}
        self._lock = threading.Lock()
        self._register_default_toolsets()

    def _register_default_toolsets(self) -> None:
        self.register_toolset("terminal", ["terminal"])
        self.register_toolset("file", ["read_file", "write_file", "edit_file", "list_files", "search_files"])
        self.register_toolset("web", ["web_search", "web_fetch"])
        self.register_toolset("code", ["code_exec"])
        self.register_toolset("delegate", ["delegate"])
        self.register_toolset("cron", ["cron"])
        self.register_toolset("webhook", ["webhook"])
        self.register_toolset("memory", ["memory"])
        self.register_toolset("kanban", ["kanban_add", "kanban_move", "kanban_list"])
        self.register_toolset("session", ["session_export", "session_prune", "session_stats"])
        # gateway toolset intentionally NOT part of "all": void has no messaging gateway
        self.register_toolset("gateway", [])

    def register(self, name: str, schema: dict, handler: Callable, toolset: str = "general",
                 description: str | None = None) -> None:
        with self._lock:
            self._tools[name] = ToolDef(
                name=name,
                description=description or str(schema.get("description", "")),
                parameters=schema.get("parameters") or {"type": "object", "properties": {}},
                handler=handler,
                toolset=toolset,
            )

    def register_toolset(self, name: str, includes: list[str]) -> None:
        self._toolsets[name] = list(includes)

    def get(self, name: str) -> ToolDef | None:
        return self._tools.get(name)

    def names(self) -> list[str]:
        return sorted(self._tools.keys())

    def all_schemas(self) -> list[dict]:
        return [self.to_openai_schema(t.name) for t in self._tools.values()]

    def to_openai_schema(self, name: str) -> dict:
        t = self._tools[name]
        return {"type": "function", "function": {
            "name": t.name, "description": t.description, "parameters": t.parameters}}

    def toolsets_summary(self) -> dict[str, list[str]]:
        out: dict[str, list[str]] = {}
        for name in self._toolsets:
            expanded: set[str] = set()
            self._expand(name, expanded)
            out[name] = sorted(t for t in expanded if t in self._tools)
        return out

    # ---- toolset expansion ----------------------------------------------------------
    def _expand(self, toolset: str, acc: set[str], seen: set[str] | None = None) -> None:
        seen = seen or set()
        if toolset in seen:
            return
        seen.add(toolset)
        if toolset == "all":
            for t in self._tools:
                acc.add(t)
            return
        includes = self._toolsets.get(toolset)
        if includes is None:
            if toolset in self._tools:
                acc.add(toolset)  # a bare tool name
            return
        if not includes and toolset in self._tools:
            # toolset (e.g. 'terminal') that is a 1:1 alias for a same-named tool
            acc.add(toolset)
            return
        for sub in includes:
            if sub in self._tools and sub not in self._toolsets:
                acc.add(sub)
            elif sub in self._tools:
                # name is both a tool and a toolset: include the tool AND recurse
                acc.add(sub)
                self._expand(sub, acc, seen)
            elif sub in self._toolsets:
                self._expand(sub, acc, seen)
            else:
                acc.add(sub)  # unknown for now; filtered out by expand_toolsets()


    def expand_toolsets(self, enabled: list[str] | None, disabled: list[str] | None = None) -> list[str]:
        acc: set[str] = set()
        for t in (enabled or ["all"]):
            self._expand(t, acc)
        for d in (disabled or []):
            dacc: set[str] = set()
            self._expand(d, dacc)
            acc -= dacc
        return sorted(t for t in acc if t in self._tools)

    # ---- discovery (AST-gated import) -------------------------------------------------
    def discover(self, tools_dir: Path, package: str = "void.tools") -> int:
        tools_dir = Path(tools_dir)
        imported = 0
        for path in sorted(tools_dir.glob("*.py")):
            if path.name.startswith("_"):
                continue
            try:
                tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
            except SyntaxError:
                continue
            if not _has_toplevel_register(tree):
                continue  # partial file / helper module / conditional registration: skip
            try:
                importlib.import_module(f"{package}.{path.stem}")
                imported += 1
            except Exception:
                traceback.print_exc()
        return imported

    # ---- dispatch ---------------------------------------------------------------------
    def dispatch(self, name: str, args: dict) -> dict:
        tool = self._tools.get(name)
        if tool is None:
            return {"ok": False, "error": f"Unknown tool: {name}"}
        if not isinstance(args, dict):
            args = {}
        try:
            result = tool.handler(args)
            if asyncio.iscoroutine(result):
                result = asyncio.run(result)
            if isinstance(result, dict):
                return result
            return {"ok": True, "result": result}
        except Exception as exc:  # tool errors must never kill the agent loop
            return {"ok": False, "error": f"{type(exc).__name__}: {exc}",
                    "traceback": traceback.format_exc(limit=4)}


def _has_toplevel_register(tree: ast.Module) -> bool:
    """True if the module has a top-level `registry.register(...)` / `register(...)` call."""
    def call_matches(call: ast.Call) -> bool:
        f = call.func
        if isinstance(f, ast.Attribute) and f.attr == "register":
            return True
        if isinstance(f, ast.Name) and f.id == "register":
            return True
        return False

    for node in tree.body:
        if isinstance(node, ast.Expr) and isinstance(node.value, ast.Call) and call_matches(node.value):
            return True
        if isinstance(node, ast.Assign) and isinstance(node.value, ast.Call) and call_matches(node.value):
            return True
    return False


def result_to_json(result: dict, max_chars: int) -> str:
    try:
        text = json.dumps(result, ensure_ascii=False, default=str)
    except (TypeError, ValueError):
        text = json.dumps({"ok": bool(result.get("ok")), "error": "unserializable tool result"},
                          ensure_ascii=False)
    if len(text) > max_chars:
        text = text[:max_chars] + f"\n...[truncated {len(text) - max_chars} chars]"
    return text


registry = ToolRegistry()

