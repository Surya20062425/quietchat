"""webhook tool — subscribe/list/remove/test webhook routes (see void/webhooks.py)."""
from __future__ import annotations

from .registry import get_context, registry


def _mod():
    import importlib
    return importlib.import_module("void.webhooks")


def webhook_handler(args: dict) -> dict:
    ctx = get_context()
    if ctx.config is None or ctx.db is None:
        return {"ok": False, "error": "webhooks require config + session DB context"}
    action = str(args.get("action") or "list")
    try:
        mod = _mod()
        if action == "list":
            return {"ok": True, "routes": mod.list_routes(ctx.config, ctx.db)}
        if action == "subscribe":
            name = str(args.get("name") or "").strip()
            if not name:
                return {"ok": False, "error": "name is required"}
            route = str(args.get("route") or f"/webhook/{name}")
            skills = [str(s) for s in (args.get("skills") or [])]
            route_cfg = mod.subscribe(ctx.config, ctx.db, name, route,
                                      str(args.get("prompt") or ""), skills)
            return {"ok": True, "route": route_cfg}
        name = str(args.get("name") or "").strip()
        if action == "remove":
            ok = mod.remove(ctx.config, ctx.db, name)
            return {"ok": ok} if ok else {"ok": False, "error": f"no such route: {name}"}
        if action == "test":
            return mod.test_route(ctx.config, ctx.db, name)
        return {"ok": False, "error": f"unknown action: {action}"}
    except Exception as exc:
        return {"ok": False, "error": f"webhook {action} failed: {exc}"}


registry.register(
    "webhook",
    {"description": "Manage event-driven triggers. A webhook route maps an HTTP path to a prompt; when "
                    "the local server receives a POST, it starts a new agent turn with that prompt plus "
                    "any subscribed skills. Start the receiver with `void webhook serve`.",
     "parameters": {"type": "object",
                    "properties": {
                        "action": {"type": "string", "enum": ["list", "subscribe", "remove", "test"]},
                        "name": {"type": "string", "description": "Route name."},
                        "route": {"type": "string", "description": "URL path, e.g. /webhook/github."},
                        "prompt": {"type": "string", "description": "Prompt run when the route is hit."},
                        "skills": {"type": "array", "items": {"type": "string"},
                                   "description": "Skills to preload for the triggered turn."}},
                    "required": ["action"]}},
    webhook_handler, toolset="webhook")
