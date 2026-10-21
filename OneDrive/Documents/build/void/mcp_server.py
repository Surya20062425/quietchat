"""MCP server mode: `void mcp serve` exposes the tool registry over STDIO (JSON-RPC 2.0).

The stdio transport is newline-delimited JSON-RPC. Tools registered in void.tools.registry
are advertised automatically via tools/list and executed via tools/call.
"""
from __future__ import annotations

import json
import sys
from typing import Any

from . import __version__
from .config import Config
from .tools.registry import registry, set_context
from .tools.registry import ToolContext

PROTOCOL_VERSION = "2024-11-05"


class MCPServer:
    def __init__(self, config: Config, db=None):
        self.config = config
        self.db = db
        self.initialized = False

    # ---- JSON-RPC plumbing -----------------------------------------------------------
    def _send(self, payload: dict) -> None:
        sys.stdout.write(json.dumps(payload, ensure_ascii=False, default=str) + "\n")
        sys.stdout.flush()

    def _result(self, req_id: Any, result: dict) -> None:
        self._send({"jsonrpc": "2.0", "id": req_id, "result": result})

    def _error(self, req_id: Any, code: int, message: str) -> None:
        self._send({"jsonrpc": "2.0", "id": req_id, "error": {"code": code, "message": message}})

    # ---- MCP methods -------------------------------------------------------------------
    def handle(self, message: dict) -> None:
        method = message.get("method")
        req_id = message.get("id")
        params = message.get("params") or {}

        if method == "initialize":
            self.initialized = True
            self._result(req_id, {
                "protocolVersion": PROTOCOL_VERSION,
                "capabilities": {"tools": {"listChanged": False}},
                "serverInfo": {"name": "void", "version": __version__},
            })
            return
        if method in ("notifications/initialized", "initialized"):
            return
        if method == "ping":
            self._result(req_id, {})
            return
        if method == "tools/list":
            self._result(req_id, {"tools": self._tool_list()})
            return
        if method == "tools/call":
            self._call(req_id, params)
            return
        if method == "resources/list":
            self._result(req_id, {"resources": []})   # minimal: tools only
            return
        if method == "prompts/list":
            self._result(req_id, {"prompts": []})
            return
        if req_id is not None:
            self._error(req_id, -32601, f"method not found: {method}")

    def _tool_list(self) -> list[dict]:
        tools = []
        for name in registry.names():
            tool = registry.get(name)
            tools.append({"name": tool.name, "description": tool.description,
                          "inputSchema": tool.parameters or {"type": "object", "properties": {}}})
        return tools

    def _call(self, req_id: Any, params: dict) -> None:
        name = params.get("name") or ""
        args = params.get("arguments") or {}
        if registry.get(name) is None:
            self._error(req_id, -32602, f"unknown tool: {name}")
            return
        ctx = ToolContext(config=self.config, db=self.db, depth=0)
        set_context(ctx)
        try:
            result = registry.dispatch(name, args if isinstance(args, dict) else {})
        finally:
            set_context(None)
        text = json.dumps(result, ensure_ascii=False, default=str)
        self._result(req_id, {
            "content": [{"type": "text", "text": text}],
            "isError": not bool(result.get("ok", True)),
        })


def serve(config: Config, db=None) -> int:
    """Blocking STDIO loop. Reads one JSON-RPC message per line from stdin."""
    server = MCPServer(config, db)
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            message = json.loads(line)
        except json.JSONDecodeError:
            server._error(None, -32700, "parse error")
            continue
        if isinstance(message, list):  # batch
            for item in message:
                server.handle(item)
        else:
            server.handle(message)
    return 0
