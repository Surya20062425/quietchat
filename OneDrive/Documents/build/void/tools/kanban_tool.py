"""kanban tools — kanban_add / kanban_move / kanban_list (durable board in the session DB)."""
from __future__ import annotations

from .registry import get_context, registry


def _db():
    ctx = get_context()
    if ctx.db is None:
        return None
    return ctx.db


def kanban_add_handler(args: dict) -> dict:
    db = _db()
    if db is None:
        return {"ok": False, "error": "no session DB"}
    title = str(args.get("title") or "").strip()
    if not title:
        return {"ok": False, "error": "title is required"}
    card_id = db.kanban_add(title, str(args.get("description") or ""))
    return {"ok": True, "id": card_id, "title": title, "status": "todo"}


def kanban_move_handler(args: dict) -> dict:
    db = _db()
    if db is None:
        return {"ok": False, "error": "no session DB"}
    try:
        card_id = int(args.get("id"))
    except (TypeError, ValueError):
        return {"ok": False, "error": "id must be an integer"}
    status = str(args.get("column") or args.get("status") or "")
    if db.kanban_move(card_id, status):
        return {"ok": True, "id": card_id, "status": status}
    return {"ok": False, "error": f"invalid id or column (columns: todo, in_progress, done)"}


def kanban_list_handler(args: dict) -> dict:
    db = _db()
    if db is None:
        return {"ok": False, "error": "no session DB"}
    status = args.get("column") or args.get("status")
    cards = db.kanban_list(str(status) if status else None)
    return {"ok": True, "cards": cards, "stats": db.kanban_stats()}


registry.register(
    "kanban_add",
    {"description": "Add a card to the kanban board (column: todo).",
     "parameters": {"type": "object",
                    "properties": {"title": {"type": "string"}, "description": {"type": "string"}},
                    "required": ["title"]}},
    kanban_add_handler, toolset="kanban")

registry.register(
    "kanban_move",
    {"description": "Move a kanban card to another column (todo, in_progress, done).",
     "parameters": {"type": "object",
                    "properties": {"id": {"type": "integer"}, "column": {"type": "string"}},
                    "required": ["id", "column"]}},
    kanban_move_handler, toolset="kanban")

registry.register(
    "kanban_list",
    {"description": "List kanban cards with per-column stats.",
     "parameters": {"type": "object",
                    "properties": {"column": {"type": "string", "enum": ["todo", "in_progress", "done"]}}}},
    kanban_list_handler, toolset="kanban")
