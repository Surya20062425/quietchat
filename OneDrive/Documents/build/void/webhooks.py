"""Webhooks: route storage + a lightweight HTTP receiver that turns payloads into agent turns.

Routes live in the session DB. `void webhook serve` starts this server; each POST to a
registered path runs one agent turn (same core loop) with the route's prompt + skills.
"""
from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from .logging_setup import log_line


def list_routes(config, db) -> list[dict]:
    out = []
    for row in db.webhook_list():
        row = dict(row)
        row["skills"] = [s for s in str(row.get("skills") or "").split(",") if s]
        out.append(row)
    return out


def subscribe(config, db, name: str, route: str, prompt: str, skills: list[str] | None = None) -> dict:
    if not route.startswith("/"):
        route = "/" + route
    ok = db.webhook_add(name, route, prompt or "", skills or [])
    if not ok:
        raise ValueError(f"a route named '{name}' or path '{route}' already exists")
    log_line(config.logs_dir, "webhooks.log", "webhook", f"subscribed {name} -> {route}")
    return {"name": name, "route": route, "prompt": prompt, "skills": skills or []}


def remove(config, db, name: str) -> bool:
    ok = db.webhook_remove(name)
    if ok:
        log_line(config.logs_dir, "webhooks.log", "webhook", f"removed route {name}")
    return ok


def test_route(config, db, name: str) -> dict:
    """Send a sample payload through the real handler without a socket."""
    route = db.webhook_get(name)
    if route is None:
        return {"ok": False, "error": f"no such route: {name}"}
    payload = {"test": True, "source": "void webhook test", "name": name}
    result = handle_payload(config, db, route, payload)
    return {"ok": True, "route": name, "result": result}


def handle_payload(config, db, route_row: dict, payload: dict) -> dict:
    """Run one agent turn for a webhook payload. Returns {ok, response, session_id}."""
    from .agent.run_agent import AIAgent
    from .state import RuntimeState
    from .skills import SkillLibrary

    prompt_text = json.dumps(payload, ensure_ascii=False, indent=2)[:6000]
    full_prompt = (route_row.get("prompt") or "Handle this event.") + "\n\nPayload:\n" + prompt_text

    state = RuntimeState()
    state.stream_enabled = False
    state.enabled_toolsets = ["all"]
    skills = [s for s in str(route_row.get("skills") or "").split(",") if s]
    if skills:
        lib = SkillLibrary(config)
        for s in skills:
            content = lib.content(s)
            if content:
                state.loaded_skills[s] = content
    try:
        sid = db.create_session(f"webhook:{route_row.get('name')}", platform="webhook")
        state.session_id = sid
        agent = AIAgent(config, state, db=db)
        text = agent.run_conversation(full_prompt)
        db.webhook_touch(route_row.get("name"))
        db.touch_session(sid, turns=state.turn_count)
        log_line(config.logs_dir, "webhooks.log", "webhook",
                 f"handled {route_row.get('name')} -> session {sid}", sid)
        return {"ok": True, "response": text, "session_id": sid}
    except Exception as exc:
        log_line(config.logs_dir, "webhooks.log", "webhook",
                 f"handler failed for {route_row.get('name')}: {exc}")
        return {"ok": False, "error": f"{type(exc).__name__}: {exc}"}

class _WebhookHandler(BaseHTTPRequestHandler):
    server_version = "void-webhook/1.0"
    config = None
    db = None

    def log_message(self, fmt, *args):  # keep stdout clean; log to file instead
        log_line(self.config.logs_dir, "webhooks.log", "webhook", fmt % args)

    def _reply(self, code: int, body: dict) -> None:
        data = json.dumps(body, ensure_ascii=False, default=str).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):  # health / route listing
        if self.path in ("/", "/health"):
            self._reply(200, {"ok": True, "service": "void-webhook",
                              "routes": [r["route"] for r in list_routes(self.config, self.db)]})
            return
        self._reply(404, {"ok": False, "error": "not found"})

    def do_POST(self):
        route = self.db.webhook_get_by_route(self.path)
        if route is None:
            self._reply(404, {"ok": False, "error": f"no route for {self.path}"})
            return
        try:
            length = int(self.headers.get("Content-Length") or 0)
            raw = self.rfile.read(length) if length else b"{}"
            payload = json.loads(raw.decode("utf-8", errors="replace") or "{}")
            if not isinstance(payload, dict):
                payload = {"payload": payload}
        except (ValueError, OSError):
            payload = {"raw": "unparseable payload"}
        result = handle_payload(self.config, self.db, route, payload)
        self._reply(200 if result.get("ok") else 500, result)


def serve(config, db, host: str | None = None, port: int | None = None,
          block: bool = True) -> ThreadingHTTPServer:
    """Start the webhook receiver. Returns the server (already started)."""
    host = host or str(config.get("webhook.host", "127.0.0.1"))
    port = int(port or config.get("webhook.port", 8791) or 8791)
    handler = type("VoidWebhookHandler", (_WebhookHandler,), {"config": config, "db": db})
    server = ThreadingHTTPServer((host, port), handler)
    routes = [r["route"] for r in list_routes(config, db)]
    log_line(config.logs_dir, "webhooks.log", "webhook",
             f"serving on http://{host}:{port} ({len(routes)} routes)")
    print(f"[void] webhook server listening on http://{host}:{port}")
    for r in routes:
        print(f"       POST {r}")
    if block:
        try:
            server.serve_forever()
        except KeyboardInterrupt:
            server.shutdown()
    else:
        threading.Thread(target=server.serve_forever, daemon=True, name="void-webhook").start()
    return server

