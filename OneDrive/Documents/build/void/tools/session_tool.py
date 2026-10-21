"""session tools — session_export / session_prune / session_stats."""
from __future__ import annotations

import json
from pathlib import Path

from .registry import get_context, registry


def session_export_handler(args: dict) -> dict:
    ctx = get_context()
    if ctx.db is None:
        return {"ok": False, "error": "no session DB"}
    sid = str(args.get("session_id") or ctx.session_id or "")
    if not sid:
        return {"ok": False, "error": "session_id required (or run inside a session)"}
    sess = ctx.db.find_session(sid)
    if not sess:
        return {"ok": False, "error": f"session not found: {sid}"}
    fmt = str(args.get("format") or "markdown")
    out_path = Path(args.get("path") or f"session_{sess['id']}.{'md' if fmt != 'jsonl' else 'jsonl'}")
    if fmt == "jsonl":
        lines = [json.dumps(m, ensure_ascii=False) for m in ctx.db.get_messages(sess["id"])]
        out_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    else:
        out_path.write_text(ctx.db.export_messages_markdown(sess["id"]), encoding="utf-8")
    return {"ok": True, "path": str(out_path), "messages": ctx.db.message_count(sess["id"])}


def session_prune_handler(args: dict) -> dict:
    ctx = get_context()
    if ctx.db is None:
        return {"ok": False, "error": "no session DB"}
    days = args.get("older_than_days")
    keep = args.get("keep_total")
    removed = ctx.db.prune_sessions(int(days) if days is not None else None,
                                    int(keep) if keep is not None else None)
    return {"ok": True, "removed": removed}


def session_stats_handler(args: dict) -> dict:
    ctx = get_context()
    if ctx.db is None:
        return {"ok": False, "error": "no session DB"}
    sid = str(args.get("session_id") or ctx.session_id or "")
    sessions = ctx.db.list_sessions(limit=1000)
    stats = {"total_sessions": len(sessions), "db_bytes": ctx.db.db_size_bytes()}
    if sid:
        sess = ctx.db.find_session(sid)
        if sess:
            stats["session"] = sess
            stats["messages"] = ctx.db.message_count(sess["id"])
    return {"ok": True, "stats": stats}


registry.register(
    "session_export",
    {"description": "Export a session's messages to Markdown or JSONL.",
     "parameters": {"type": "object",
                    "properties": {"session_id": {"type": "string"},
                                   "format": {"type": "string", "enum": ["markdown", "jsonl"]},
                                   "path": {"type": "string", "description": "Output file path."}}}},
    session_export_handler, toolset="session")

registry.register(
    "session_prune",
    {"description": "Delete old sessions: older than N days and/or keep only the newest M.",
     "parameters": {"type": "object",
                    "properties": {"older_than_days": {"type": "integer"}, "keep_total": {"type": "integer"}}}},
    session_prune_handler, toolset="session")

registry.register(
    "session_stats",
    {"description": "Session DB statistics (counts, size, current session info).",
     "parameters": {"type": "object", "properties": {"session_id": {"type": "string"}}}},
    session_stats_handler, toolset="session")
