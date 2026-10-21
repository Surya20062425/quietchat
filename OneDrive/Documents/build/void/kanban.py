"""Kanban: durable multi-agent work queue (board in the session DB) + dispatcher loop.

The dispatcher picks `todo` cards, spawns a child agent (the same core loop) with the card
as the prompt, marks it `in_progress`, then `done` when the child returns. Runs on a
background thread; `void kanban run` runs it in the foreground.
"""
from __future__ import annotations

import threading

from .logging_setup import log_line
from .session_db import COLUMNS


def board(db) -> dict[str, list[dict]]:
    cards = db.kanban_list()
    out: dict[str, list[dict]] = {c: [] for c in COLUMNS}
    for card in cards:
        out.setdefault(card["status"], []).append(card)
    return out


def stats(db) -> dict:
    return db.kanban_stats()


def add(db, title: str, description: str = "") -> dict:
    card_id = db.kanban_add(title, description)
    return {"id": card_id, "title": title, "status": "todo"}


def move(db, card_id: int, column: str) -> bool:
    return db.kanban_move(card_id, column)


def clear_done(db) -> int:
    return db.kanban_clear_done()


def _default_agent(config, db, card: dict, toolsets: list[str]):
    """Build + run a child agent for one card. Returns the final text."""
    from .agent.run_agent import AIAgent
    from .state import RuntimeState
    state = RuntimeState()
    state.stream_enabled = False
    state.enabled_toolsets = toolsets or ["all"]
    state.depth = 1  # dispatcher children may not delegate/cron/webhook
    sid = db.create_session(f"kanban:#{card['id']}", platform="kanban")
    state.session_id = sid
    agent = AIAgent(config, state, db=db)
    prompt = card["title"]
    if card.get("description"):
        prompt += "\n\n" + card["description"]
    text = agent.run_conversation(prompt)
    db.touch_session(sid, turns=state.turn_count)
    return text, sid


class KanbanDispatcher:
    """Background worker: todo -> in_progress -> done, up to max_concurrent cards."""

    def __init__(self, config, db, interval: float = 5.0, on_event=None, runner=None):
        self.config = config
        self.db = db
        self.interval = interval
        self.on_event = on_event
        self._runner = runner or _default_agent
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._active: set[int] = set()
        self._lock = threading.Lock()

    # ---- lifecycle -----------------------------------------------------------------
    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._loop, daemon=True, name="void-kanban")
        self._thread.start()
        log_line(self.config.logs_dir, "kanban.log", "kanban", "dispatcher started")

    def stop(self) -> None:
        self._stop.set()

    @property
    def max_concurrent(self) -> int:
        return int(self.config.get("kanban.dispatcher.max_concurrent", 2) or 2)

    @property
    def toolsets(self) -> list[str]:
        return list(self.config.get("kanban.dispatcher.toolsets") or ["terminal", "file", "web"])

    def _emit(self, event: dict) -> None:
        if self.on_event:
            try:
                self.on_event(event)
            except Exception:
                pass

    # ---- loop -----------------------------------------------------------------------
    def _loop(self) -> None:
        while not self._stop.is_set():
            try:
                self.run_pending()
            except Exception as exc:
                log_line(self.config.logs_dir, "kanban.log", "kanban", f"dispatcher error: {exc}")
            self._stop.wait(self.interval)

    def run_pending(self) -> int:
        """Start as many todo cards as the concurrency limit allows. Returns #started."""
        started = 0
        while len(self._active) < self.max_concurrent:
            card = self.db.kanban_next_todo()
            if card is None:
                break
            with self._lock:
                self._active.add(card["id"])
            self.db.kanban_move(card["id"], "in_progress")
            self._emit({"type": "kanban", "text": f"card #{card['id']} → in_progress: {card['title']}"})
            threading.Thread(target=self._work, args=(card,), daemon=True,
                             name=f"void-kanban-{card['id']}").start()
            started += 1
        return started

    def _work(self, card: dict) -> None:
        try:
            text, sid = self._runner(self.config, self.db, card, self.toolsets)
            self.db.kanban_move(card["id"], "done")
            log_line(self.config.logs_dir, "kanban.log", "kanban",
                     f"card #{card['id']} done: {str(text)[:300]}", sid)
            self._emit({"type": "kanban", "text": f"card #{card['id']} → done: {str(text)[:200]}"})
        except Exception as exc:
            log_line(self.config.logs_dir, "kanban.log", "kanban", f"card #{card['id']} failed: {exc}")
            self._emit({"type": "kanban", "text": f"card #{card['id']} FAILED: {exc}"})
            self.db.kanban_move(card["id"], "todo")  # put it back
        finally:
            with self._lock:
                self._active.discard(card["id"])
