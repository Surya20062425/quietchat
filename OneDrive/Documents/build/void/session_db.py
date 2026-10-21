"""Session/state database: SQLite (WAL) with FTS5 full-text search.

Holds: sessions, messages (+FTS), cron jobs, webhook routes, kanban cards.
All mutations run inside transactions. One DB file per profile (state.db).
"""
from __future__ import annotations

import json
import sqlite3
import time
import uuid
from pathlib import Path
from typing import Any

_SCHEMA = """
CREATE TABLE IF NOT EXISTS sessions (
    id TEXT PRIMARY KEY,
    title TEXT DEFAULT '',
    created_at TEXT,
    updated_at TEXT,
    platform TEXT DEFAULT 'cli',
    turns INTEGER DEFAULT 0,
    model TEXT DEFAULT '',
    provider TEXT DEFAULT ''
);
CREATE TABLE IF NOT EXISTS messages (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id TEXT NOT NULL,
    role TEXT NOT NULL,
    content TEXT DEFAULT '',
    tool_call_id TEXT,
    name TEXT,
    meta TEXT,
    created_at TEXT
);
CREATE INDEX IF NOT EXISTS idx_messages_session ON messages(session_id);
CREATE VIRTUAL TABLE IF NOT EXISTS messages_fts USING fts5(content, content='messages', content_rowid='id');
CREATE TRIGGER IF NOT EXISTS messages_fts_ai AFTER INSERT ON messages BEGIN
    INSERT INTO messages_fts(rowid, content) VALUES (new.id, new.content); END;
CREATE TRIGGER IF NOT EXISTS messages_fts_ad AFTER DELETE ON messages BEGIN
    INSERT INTO messages_fts(messages_fts, rowid, content) VALUES ('delete', old.id, old.content); END;
CREATE TRIGGER IF NOT EXISTS messages_fts_au AFTER UPDATE OF content ON messages BEGIN
    INSERT INTO messages_fts(messages_fts, rowid, content) VALUES ('delete', old.id, old.content);
    INSERT INTO messages_fts(rowid, content) VALUES (new.id, new.content); END;

CREATE TABLE IF NOT EXISTS cron_jobs (
    id TEXT PRIMARY KEY,
    name TEXT,
    schedule TEXT,
    prompt TEXT,
    toolsets TEXT DEFAULT '',
    no_agent INTEGER DEFAULT 0,
    enabled INTEGER DEFAULT 1,
    last_run TEXT,
    last_status TEXT,
    last_output TEXT,
    created_at TEXT
);
CREATE TABLE IF NOT EXISTS webhook_routes (
    name TEXT PRIMARY KEY,
    route TEXT UNIQUE,
    prompt TEXT,
    skills TEXT DEFAULT '',
    created_at TEXT,
    last_hit TEXT
);
CREATE TABLE IF NOT EXISTS kanban_cards (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    title TEXT,
    description TEXT DEFAULT '',
    status TEXT DEFAULT 'todo',
    assignee_agent TEXT DEFAULT '',
    created_at TEXT,
    finished_at TEXT
);
"""

COLUMNS = ["todo", "in_progress", "done"]


def now_iso() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime())


def new_session_id() -> str:
    return uuid.uuid4().hex[:12]


class SessionDB:
    def __init__(self, path: Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(str(self.path), check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA synchronous=NORMAL")
        try:
            self._conn.executescript(_SCHEMA)
        except sqlite3.OperationalError as exc:  # FTS5 missing in exotic builds
            if "fts5" in str(exc).lower():
                base = _SCHEMA.split("CREATE VIRTUAL TABLE")[0]
                self._conn.executescript(base)
            else:
                raise
        self._conn.commit()

    # ---- helpers ----------------------------------------------------------------
    def _exec(self, sql: str, params: tuple = ()) -> sqlite3.Cursor:
        cur = self._conn.execute(sql, params)
        self._conn.commit()
        return cur

    def _one(self, sql: str, params: tuple = ()) -> sqlite3.Row | None:
        return self._conn.execute(sql, params).fetchone()

    def _all(self, sql: str, params: tuple = ()) -> list[sqlite3.Row]:
        return self._conn.execute(sql, params).fetchall()

    def close(self) -> None:
        try:
            self._conn.commit()
            self._conn.close()
        except sqlite3.Error:
            pass

    # ---- sessions ---------------------------------------------------------------
    def create_session(self, title: str = "", model: str = "", provider: str = "", platform: str = "cli") -> str:
        sid = new_session_id()
        self._exec(
            "INSERT INTO sessions (id, title, created_at, updated_at, platform, turns, model, provider) "
            "VALUES (?,?,?,?,?,?,?,?)",
            (sid, title or "untitled", now_iso(), now_iso(), platform, 0, model, provider),
        )
        return sid

    def get_session(self, session_id: str) -> dict | None:
        row = self._one("SELECT * FROM sessions WHERE id = ?", (session_id,))
        return dict(row) if row else None

    def touch_session(self, session_id: str, turns: int | None = None, model: str | None = None,
                      provider: str | None = None, title: str | None = None) -> None:
        sets, params = ["updated_at = ?"], [now_iso()]
        if turns is not None:
            sets.append("turns = ?"); params.append(turns)
        if model is not None:
            sets.append("model = ?"); params.append(model)
        if provider is not None:
            sets.append("provider = ?"); params.append(provider)
        if title:
            sets.append("title = ?"); params.append(title)
        params.append(session_id)
        self._exec(f"UPDATE sessions SET {', '.join(sets)} WHERE id = ?", tuple(params))

    def find_session(self, id_or_title: str) -> dict | None:
        """Exact id, then exact title, then prefix-of-id, then LIKE title."""
        row = self._one("SELECT * FROM sessions WHERE id = ?", (id_or_title,))
        if row:
            return dict(row)
        row = self._one("SELECT * FROM sessions WHERE title = ? ORDER BY updated_at DESC", (id_or_title,))
        if row:
            return dict(row)
        row = self._one("SELECT * FROM sessions WHERE id LIKE ? ORDER BY updated_at DESC", (id_or_title + "%",))
        if row:
            return dict(row)
        row = self._one("SELECT * FROM sessions WHERE title LIKE ? ORDER BY updated_at DESC", (f"%{id_or_title}%",))
        return dict(row) if row else None

    def recent_session(self) -> dict | None:
        row = self._one("SELECT * FROM sessions ORDER BY updated_at DESC LIMIT 1")
        return dict(row) if row else None

    def list_sessions(self, limit: int = 50) -> list[dict]:
        rows = self._all(
            "SELECT s.id, s.title, s.created_at, s.updated_at, s.platform, s.turns, s.model, s.provider, "
            "(SELECT COUNT(*) FROM messages m WHERE m.session_id = s.id) AS message_count "
            "FROM sessions s ORDER BY s.updated_at DESC LIMIT ?", (limit,))
        return [dict(r) for r in rows]

    def rename_session(self, session_id: str, title: str) -> bool:
        cur = self._exec("UPDATE sessions SET title = ?, updated_at = ? WHERE id = ?", (title, now_iso(), session_id))
        return cur.rowcount > 0

    def delete_session(self, session_id: str) -> bool:
        self._exec("DELETE FROM messages WHERE session_id = ?", (session_id,))
        cur = self._exec("DELETE FROM sessions WHERE id = ?", (session_id,))
        return cur.rowcount > 0

    def prune_sessions(self, older_than_days: int | None = None, keep_total: int | None = None) -> int:
        removed = 0
        if older_than_days is not None:
            cutoff = time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime(time.time() - older_than_days * 86400))
            rows = self._all("SELECT id FROM sessions WHERE updated_at < ?", (cutoff,))
            for r in rows:
                removed += 1 if self.delete_session(r["id"]) else 0
        if keep_total is not None:
            rows = self._all("SELECT id FROM sessions ORDER BY updated_at DESC")
            for r in rows[keep_total:]:
                removed += 1 if self.delete_session(r["id"]) else 0
        return removed

    # ---- messages ---------------------------------------------------------------
    def append_message(self, session_id: str, role: str, content: Any, tool_call_id: str | None = None,
                       name: str | None = None, meta: dict | None = None) -> int:
        stored = content if isinstance(content, str) else json.dumps(content, ensure_ascii=False)
        cur = self._exec(
            "INSERT INTO messages (session_id, role, content, tool_call_id, name, meta, created_at) "
            "VALUES (?,?,?,?,?,?,?)",
            (session_id, role, stored, tool_call_id, name,
             json.dumps(meta, ensure_ascii=False) if meta else None, now_iso()),
        )
        return int(cur.lastrowid or 0)

    def get_messages(self, session_id: str) -> list[dict]:
        rows = self._all("SELECT * FROM messages WHERE session_id = ? ORDER BY id ASC", (session_id,))
        return [dict(r) for r in rows]

    def message_count(self, session_id: str) -> int:
        row = self._one("SELECT COUNT(*) AS c FROM messages WHERE session_id = ?", (session_id,))
        return int(row["c"]) if row else 0

    def search_messages(self, query: str, limit: int = 20) -> list[dict]:
        """FTS5 search; falls back to LIKE if the fts table is unavailable."""
        try:
            rows = self._all(
                "SELECT m.* FROM messages_fts f JOIN messages m ON m.id = f.rowid "
                "WHERE messages_fts MATCH ? ORDER BY rank LIMIT ?", (query, limit))
            if rows:
                return [dict(r) for r in rows]
        except sqlite3.OperationalError:
            pass
        rows = self._all("SELECT * FROM messages WHERE content LIKE ? ORDER BY id DESC LIMIT ?",
                         (f"%{query}%", limit))
        return [dict(r) for r in rows]

    # ---- cron jobs --------------------------------------------------------------
    def cron_create(self, name: str, schedule: str, prompt: str, toolsets: list[str] | None = None,
                    no_agent: bool = False) -> str:
        jid = uuid.uuid4().hex[:8]
        self._exec(
            "INSERT INTO cron_jobs (id, name, schedule, prompt, toolsets, no_agent, enabled, created_at) "
            "VALUES (?,?,?,?,?,?,1,?)",
            (jid, name, schedule, prompt, ",".join(toolsets or []), int(no_agent), now_iso()),
        )
        return jid

    def cron_list(self, include_disabled: bool = True) -> list[dict]:
        sql = "SELECT * FROM cron_jobs" + ("" if include_disabled else " WHERE enabled = 1") + " ORDER BY created_at"
        return [dict(r) for r in self._all(sql)]

    def cron_get(self, job_id: str) -> dict | None:
        row = self._one("SELECT * FROM cron_jobs WHERE id = ? OR name = ?", (job_id, job_id))
        return dict(row) if row else None

    def cron_update(self, job_id: str, **fields) -> bool:
        allowed = {"name", "schedule", "prompt", "toolsets", "no_agent", "enabled", "last_run", "last_status", "last_output"}
        sets, params = [], []
        for k, v in fields.items():
            if k in allowed:
                sets.append(f"{k} = ?"); params.append(int(v) if k in ("no_agent", "enabled") else v)
        if not sets:
            return False
        params.extend([job_id, job_id])   # matches "id = ? OR name = ?"
        cur = self._exec(f"UPDATE cron_jobs SET {', '.join(sets)} WHERE id = ? OR name = ?", tuple(params))
        return cur.rowcount > 0


    def cron_remove(self, job_id: str) -> bool:
        cur = self._exec("DELETE FROM cron_jobs WHERE id = ? OR name = ?", (job_id, job_id))
        return cur.rowcount > 0

    # ---- webhooks ---------------------------------------------------------------
    def webhook_add(self, name: str, route: str, prompt: str, skills: list[str] | None = None) -> bool:
        try:
            self._exec("INSERT INTO webhook_routes (name, route, prompt, skills, created_at) VALUES (?,?,?,?,?)",
                       (name, route, prompt, ",".join(skills or []), now_iso()))
            return True
        except sqlite3.IntegrityError:
            return False

    def webhook_list(self) -> list[dict]:
        return [dict(r) for r in self._all("SELECT * FROM webhook_routes ORDER BY name")]

    def webhook_get(self, name: str) -> dict | None:
        row = self._one("SELECT * FROM webhook_routes WHERE name = ?", (name,))
        return dict(row) if row else None

    def webhook_get_by_route(self, route: str) -> dict | None:
        row = self._one("SELECT * FROM webhook_routes WHERE route = ?", (route,))
        return dict(row) if row else None

    def webhook_remove(self, name: str) -> bool:
        cur = self._exec("DELETE FROM webhook_routes WHERE name = ?", (name,))
        return cur.rowcount > 0

    def webhook_touch(self, name: str) -> None:
        self._exec("UPDATE webhook_routes SET last_hit = ? WHERE name = ?", (now_iso(), name))

    # ---- kanban -----------------------------------------------------------------
    def kanban_add(self, title: str, description: str = "") -> int:
        cur = self._exec("INSERT INTO kanban_cards (title, description, status, created_at) VALUES (?,?,?,?)",
                         (title, description, "todo", now_iso()))
        return int(cur.lastrowid or 0)

    def kanban_move(self, card_id: int, status: str) -> bool:
        if status not in COLUMNS:
            return False
        finished = now_iso() if status == "done" else None
        cur = self._exec("UPDATE kanban_cards SET status = ?, finished_at = ? WHERE id = ?",
                         (status, finished, card_id))
        return cur.rowcount > 0

    def kanban_list(self, status: str | None = None) -> list[dict]:
        if status:
            rows = self._all("SELECT * FROM kanban_cards WHERE status = ? ORDER BY id", (status,))
        else:
            rows = self._all("SELECT * FROM kanban_cards ORDER BY id")
        return [dict(r) for r in rows]

    def kanban_next_todo(self) -> dict | None:
        row = self._one("SELECT * FROM kanban_cards WHERE status = 'todo' ORDER BY id LIMIT 1")
        return dict(row) if row else None

    def kanban_in_progress_count(self) -> int:
        row = self._one("SELECT COUNT(*) AS c FROM kanban_cards WHERE status = 'in_progress'")
        return int(row["c"]) if row else 0

    def kanban_clear_done(self) -> int:
        cur = self._exec("DELETE FROM kanban_cards WHERE status = 'done'")
        return cur.rowcount

    def kanban_stats(self) -> dict:
        out = {}
        for col in COLUMNS:
            row = self._one("SELECT COUNT(*) AS c FROM kanban_cards WHERE status = ?", (col,))
            out[col] = int(row["c"]) if row else 0
        return out

    # ---- stats / export -----------------------------------------------------------
    def db_size_bytes(self) -> int:
        try:
            return self.path.stat().st_size
        except OSError:
            return 0

    def export_messages_markdown(self, session_id: str) -> str:
        sess = self.get_session(session_id) or {}
        lines = [f"# Session {session_id} — {sess.get('title', '')}", "",
                 f"*{sess.get('created_at', '')} · model: {sess.get('model', '')}* ", ""]
        for m in self.get_messages(session_id):
            role = m["role"].upper()
            if role == "TOOL":
                lines.append(f"### ┊ tool {m.get('name') or m.get('tool_call_id')}")
            else:
                lines.append(f"### {role}")
            lines.append("")
            lines.append(m["content"])
            lines.append("")
        return "\n".join(lines)



