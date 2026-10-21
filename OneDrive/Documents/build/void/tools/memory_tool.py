"""memory tool — add/replace/remove/list entries in agent memory (notes) and user memory (profile)."""
from __future__ import annotations

from .registry import get_context, registry


def memory_handler(args: dict) -> dict:
    ctx = get_context()
    if ctx.memory is None:
        return {"ok": False, "error": "memory system not initialized"}
    action = str(args.get("action") or "list")
    store = str(args.get("store") or "agent")
    if store not in ("agent", "user"):
        return {"ok": False, "error": "store must be 'agent' or 'user'"}

    if action == "list":
        return {"ok": True, "store": store, "content": ctx.memory.read(store),
                "tokens": ctx.memory.token_count(store)}

    if action == "add":
        text = str(args.get("text") or "").strip()
        if not text:
            return {"ok": False, "error": "text is required for add"}
        return ctx.memory.add(store, text)

    if action == "replace":
        old = str(args.get("old") or "")
        new = str(args.get("new") or "")
        return ctx.memory.replace(store, old, new)

    if action == "remove":
        needle = str(args.get("text") or "")
        return ctx.memory.remove(store, needle)

    if action == "clear":
        ctx.memory.clear(store)
        return {"ok": True, "cleared": store}

    return {"ok": False, "error": f"unknown action: {action}"}


registry.register(
    "memory",
    {
        "description": "Persist useful facts for future sessions. store=agent for notes about how to do "
                       "things / preferences / patterns; store=user for facts about the user and their "
                       "environment. Keep entries short — both stores have small token caps.",
        "parameters": {
            "type": "object",
            "properties": {
                "action": {"type": "string", "enum": ["list", "add", "replace", "remove", "clear"]},
                "store": {"type": "string", "enum": ["agent", "user"]},
                "text": {"type": "string", "description": "add: the fact to store; remove: substring to find."},
                "old": {"type": "string", "description": "replace: text to find."},
                "new": {"type": "string", "description": "replace: replacement text."},
            },
        },
    },
    memory_handler,
    toolset="memory",
)
