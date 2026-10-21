"""HermesLiteCLI — the prompt_toolkit REPL: input, slash dispatch, streaming render, status bar.

The agent call is synchronous; it runs on a worker thread so Ctrl+C can cancel it while the
REPL stays responsive. Rich is used when available, otherwise plain stdout.
"""
from __future__ import annotations

import os
import sys
import threading
import time
from pathlib import Path

from . import __version__
from .agent.run_agent import AIAgent
from .config import Config
from .session_db import SessionDB
from .skin import Skin
from .slash_commands import autocomplete_candidates, dispatch as slash_dispatch, help_text
from .state import RuntimeState

try:  # optional pretty output
    from rich.console import Console
    from rich.markdown import Markdown
    _RICH = True
except Exception:  # pragma: no cover
    _RICH = False


class HermesLiteCLI:
    """Interactive REPL + slash command surface. One instance per process."""

    def __init__(self, config: Config, db: SessionDB, state: RuntimeState | None = None,
                 session_id: str | None = None):
        self.config = config
        self.db = db
        self.state = state or RuntimeState()
        self.state.on_event = self._on_event
        self.state.approval_hook = self._approve
        self.skin = Skin(config)
        self.console = Console(highlight=False) if _RICH else None
        self._lock = threading.Lock()
        self._streaming_text = False
        self._tool_lines: list[str] = []
        self._status = ""
        self.session = None
        self.agent: AIAgent | None = None
        self._agent_error: str = ""

        if session_id:
            self.state.session_id = session_id

        try:
            self.agent = AIAgent(config, self.state, db=db, cli=self,
                                 session_id=session_id)
            if session_id and not self.state.session_title:
                sess = db.get_session(session_id) or {}
                self.state.session_title = sess.get("title")
        except Exception as exc:  # no model configured yet, etc.
            self._agent_error = str(exc)

    # ---- output helpers ---------------------------------------------------------------
    def _style(self, key: str) -> str:
        return self.skin.get(key)

    def info(self, text: str) -> None:
        self._write(text, "muted")

    def error(self, text: str) -> None:
        self._write(f"error: {text}", "error")

    def warn(self, text: str) -> None:
        self._write(text, "warning")

    def ok(self, text: str) -> None:
        self._write(text, "success")

    def _write(self, text: str, style_key: str = "") -> None:
        if self.console is not None:
            self.console.print(text, style=self._style(style_key) or None)
        else:
            print(text)

    def print_markdown(self, text: str) -> None:
        if self.console is not None:
            try:
                self.console.print(Markdown(text or ""))
                return
            except Exception:
                pass
        print(text)

    def _stream_token(self, token: str) -> None:
        with self._lock:
            if not self._streaming_text:
                self._streaming_text = True
            sys.stdout.write(token)
            sys.stdout.flush()

    def _end_stream(self) -> bool:
        """Returns True if anything was streamed (caller then needn't reprint)."""
        with self._lock:
            was = self._streaming_text
            self._streaming_text = False
        if was:
            sys.stdout.write("\n")
            sys.stdout.flush()
        return was

    # ---- agent events -----------------------------------------------------------------
    def _on_event(self, event: dict) -> None:
        etype = event.get("type")
        if etype == "token":
            self._stream_token(str(event.get("text") or ""))
        elif etype == "reasoning":
            self._write("\n[thinking]\n" + str(event.get("text")) + "\n[/thinking]\n", "thinking")
        elif etype == "tool_start":
            name = event.get("name")
            args = event.get("args") or {}
            summary = _summarize_args(args)
            line = f"┊ {name} {summary}".rstrip()
            self._tool_lines.append(line)
            self.warn(line)
        elif etype == "tool_result":
            preview = str(event.get("preview") or "")
            icon = "ok" if event.get("ok", True) else "FAILED"
            self.info(f"┊ {event.get('name')} → {icon}: {preview[:160]}")
        elif etype == "warning":
            self.warn("┊ " + str(event.get("text")))
        elif etype == "info":
            self.info("┊ " + str(event.get("text")))
        elif etype == "kanban":
            self.info("┊ " + str(event.get("text")))
        elif etype == "background_done":
            self._write("\n[background task finished]\n" + str(event.get("text")), "muted")

    def _approve(self, prompt_text: str) -> bool:
        """Security approval hook. Runs on the agent thread; asks on the terminal."""
        if self.state.yolo:
            self.warn("  [yolo] auto-approving dangerous command")
            return True
        try:
            with self._terminal_lock():
                print("\n" + prompt_text)
                answer = input("Approve? [Y/n] ").strip().lower()
            return answer in ("", "y", "yes")
        except (EOFError, KeyboardInterrupt, OSError):
            return False

    def _terminal_lock(self):
        from contextlib import nullcontext
        try:
            from prompt_toolkit.patch_stdout import patch_stdout
            return patch_stdout()
        except Exception:
            return nullcontext()

    # ---- session / turn handling ---------------------------------------------------------
    def banner(self) -> None:
        prov = self.state.provider_override or self.config.get("model.provider") or "unset"
        model = self.state.model_override or self.config.get("model.default") or "unset"
        self._write(f"void {__version__}  ·  {prov}/{model}  ·  profile: {self.config.profile}", "banner")
        if self._agent_error:
            self.warn(f"no model ready: {self._agent_error}")
            self.info("run `void model` (or /model) to pick one.")
        sid = self.state.session_id or "(new)"
        if self.config.get("toolsets.enabled", ["all"]) == ["all"]:
            ts = "all"
        else:
            ts = ", ".join(self.config.get("toolsets.enabled") or [])
        self.info(f"session: {sid}  ·  toolsets: {ts}  ·  /help for commands, /exit to quit")


    def handle_message(self, text: str) -> None:
        """Run one agent turn on a worker thread so Ctrl+C can cancel it."""
        if self.agent is None:
            self.error("no model configured — run /model to pick one")
            return
        if self.state.session_id is None:
            title = _auto_title(text)
            self.state.session_id = self.db.create_session(
                title, model=self.agent.model, provider=self.agent.provider.name)
            self.state.session_title = title
            self.agent.state.session_id = self.state.session_id
        self.state.reset_for_turn()
        print()

        result: dict = {"text": "", "error": None}

        def _run() -> None:
            try:
                result["text"] = self.agent.run_conversation(text)
            except KeyboardInterrupt:
                result["error"] = "cancelled"
            except Exception as exc:
                result["error"] = f"{type(exc).__name__}: {exc}"

        worker = threading.Thread(target=_run, daemon=True, name="void-turn")
        worker.start()
        try:
            while worker.is_alive():
                worker.join(timeout=0.25)
        except KeyboardInterrupt:
            self.state.cancel_token.cancel()
            self.warn("cancelling…")
            worker.join(timeout=5)
            self.state.cancel_token.reset()

        streamed = self._end_stream()
        if result["error"]:
            self.error(result["error"])
            return
        final = result["text"] or ""
        if not streamed and final:
            self.print_markdown(final)
        # persist + refresh session metadata
        self.db.touch_session(self.state.session_id, turns=self.state.turn_count,
                              model=self.agent.model, provider=self.agent.provider.name)
        self.info(f"  turn {self.state.turn_count} · {self.state.calls_made} call(s) · "
                  f"{self.state.budget_used_tokens} tokens")

    def status_line(self) -> str:
        prov = self.agent.provider.name if self.agent else "unset"
        model = self.agent.model if self.agent else "unset"
        sid = (self.state.session_id or "-")[:12]
        yolo = " · YOLO" if self.state.yolo else ""
        return f"{prov}/{model} · {sid} · turn {self.state.turn_count}{yolo}"

    # ---- slash command handlers -----------------------------------------------------------
    def slash_help(self, args: str = "") -> None:
        print(help_text())

    def slash_exit(self, args: str = "") -> None:
        raise SystemExit(0)

    def slash_clear(self, args: str = "") -> None:
        if self.agent is not None:
            self.agent.messages = [{"role": "system", "content": self.agent.build_system_prompt()}]
        self.state.turn_count = 0
        self.ok("conversation cleared (session history kept in the DB)")

    def slash_model(self, args: str = "") -> None:
        from .model_tools import pick_model_interactive
        if pick_model_interactive(self.config, self.agent.router if self.agent else None):
            self.ok("reloading agent with the new model…")
            self.reload_agent()

    def reload_agent(self) -> None:
        try:
            self.agent = AIAgent(self.config, self.state, db=self.db, cli=self,
                                 session_id=self.state.session_id)
            self._agent_error = ""
        except Exception as exc:
            self._agent_error = str(exc)
            self.error(f"could not reload model: {exc}")

    def slash_tools(self, args: str = "") -> None:
        from .tools.registry import registry
        parts = args.split()
        if not parts or parts[0] == "list":
            summary = registry.toolsets_summary()
            active = set(self.state.active_tools)
            print("toolsets (✓ = a tool from it is active this session):")
            for name, tools in sorted(summary.items()):
                mark = "✓" if any(t in active for t in tools) else " "
                print(f"  [{mark}] {name:<10} {', '.join(tools) or '(none)'}")
            print("\nuse: /tools enable NAME  ·  /tools disable NAME")
            return
        action = parts[0]
        names = parts[1:] or self._prompt_toolsets()
        if not names:
            return
        current = list(self.state.enabled_toolsets or self.config.get("toolsets.enabled") or ["all"])
        if action == "enable":
            for n in names:
                if n not in current:
                    current.append(n)
            self.state.enabled_toolsets = current
            self.ok(f"enabled: {', '.join(names)}")
        elif action == "disable":
            current = [c for c in current if c not in names]
            self.state.disabled_toolsets = list(set(self.state.disabled_toolsets) | set(names))
            self.state.enabled_toolsets = current or ["all"]
            self.ok(f"disabled: {', '.join(names)}")
        else:
            self.error("usage: /tools [list|enable NAME|disable NAME]")
            return
        self.reload_agent()

    def _prompt_toolsets(self) -> list[str]:
        from .tools.registry import registry
        names = sorted(registry.toolsets_summary())
        self.info("select toolsets (comma-separated):")
        for i, n in enumerate(names, 1):
            print(f"  {i}. {n}")
        try:
            raw = input("toolsets> ").strip()
        except (EOFError, KeyboardInterrupt):
            return []
        picked = []
        for token in raw.replace(",", " ").split():
            if token.isdigit() and 1 <= int(token) <= len(names):
                picked.append(names[int(token) - 1])
            elif token in names:
                picked.append(token)
        return picked

    def slash_skills(self, args: str = "") -> None:
        from .skills import SkillLibrary
        lib = SkillLibrary(self.config)
        parts = args.split(maxsplit=1)
        sub = parts[0] if parts else "list"
        rest = parts[1] if len(parts) > 1 else ""
        if sub in ("list", "browse"):
            skills = lib.list()
            if not skills:
                self.info("no skills installed (use `/skills install <url-or-path>`)")
            for s in skills:
                print(f"  {s.name:<24} v{s.version:<6} [{s.source}] {s.description[:70]}")
            bundles = lib.bundles()
            if bundles:
                print("\n  bundles: " + ", ".join(f"/{k}" for k in bundles))
            return
        if sub == "search":
            for s in lib.search(rest):
                print(f"  {s.name:<24} {s.description[:70]}")
            return
        if sub == "inspect":
            content = lib.content(rest)
            self.print_markdown(content) if content else self.error(f"no such skill: {rest}")
            return
        if sub == "load":
            self._load_skill(rest)
            return
        if sub == "unload":
            self.state.loaded_skills.pop(rest, None)
            self._refresh_system_prompt()
            self.ok(f"unloaded skill: {rest}")
            return
        if sub in ("install", "uninstall", "update"):
            ok, msg = getattr(lib, sub)(rest)
            (self.ok if ok else self.error)(msg)
            return
        if sub == "tap":
            target = rest[len("add"):].strip() if rest.startswith("add") else rest
            ok, msg = lib.add_source(target)
            (self.ok if ok else self.error)(msg)
            return
        self.error("usage: /skills list|browse|search Q|inspect N|load N|unload N|install SRC|"
                   "uninstall N|update N|tap add REPO")

    def slash_skill(self, args: str = "") -> None:
        self._load_skill(args.strip())

    def _load_skill(self, name: str) -> None:
        from .skills import SkillLibrary
        content = SkillLibrary(self.config).content(name)
        if not content:
            self.error(f"no such skill: {name}")
            return
        self.state.loaded_skills[name] = content
        self._refresh_system_prompt()
        self.ok(f"loaded skill '{name}' into the system prompt")

    def _refresh_system_prompt(self) -> None:
        if self.agent is not None:
            self.agent.messages[0]["content"] = self.agent.build_system_prompt()

    def slash_memory(self, args: str = "") -> None:
        parts = args.split(maxsplit=2)
        sub = parts[0] if parts else "show"
        if self.agent is None or self.agent.memory is None:
            self.error("memory is disabled (config memory.enabled)")
            return
        mem = self.agent.memory
        if sub == "show":
            for store in ("user", "agent"):
                content = mem.read(store)
                limit = self.config.get(f"memory.{store}_limit_tokens")
                print(f"\n[{store} memory · ~{mem.token_count(store)}/{limit} tokens]")
                print(content or "  (empty)")
            return
        if sub == "add" and len(parts) >= 3:
            store = parts[1] if parts[1] in ("agent", "user") else "agent"
            text = parts[2] if parts[1] in ("agent", "user") else f"{parts[1]} {parts[2]}"
            res = mem.add(store, text)
            (self.ok if res.get("ok") else self.error)(str(res))
            return
        if sub == "clear":
            store = parts[1] if len(parts) > 1 else "agent"
            mem.clear(store)
            self.ok(f"cleared {store} memory")
            return
        self.error("usage: /memory [show|add agent|user TEXT|clear agent|user]")

    def slash_yolo(self, args: str = "") -> None:
        self.state.yolo = not self.state.yolo
        if self.state.yolo:
            self.warn("YOLO ON — dangerous commands run without approval for this session")
        else:
            self.ok("YOLO OFF — dangerous commands will prompt again")

    def slash_config(self, args: str = "") -> None:
        parts = args.split(maxsplit=2)
        sub = parts[0] if parts else "show"
        if sub == "get" and len(parts) > 1:
            import yaml as _yaml
            print(_yaml.safe_dump({parts[1]: self.config.get(parts[1])}, sort_keys=False))
            return
        if sub == "set" and len(parts) > 2:
            self.config.set(parts[1], _coerce(parts[2]))
            self.ok(f"{parts[1]} = {self.config.get(parts[1])!r}")
            self.reload_agent()
            return
        if sub == "show":
            import yaml as _yaml
            print(_yaml.safe_dump(self.config.data, sort_keys=False))
            return
        self.error("usage: /config get KEY | /config set KEY VALUE | /config show")

    def slash_resume(self, args: str = "") -> None:
        if not args:
            sessions = self.db.list_sessions(limit=25)
            if not sessions:
                self.info("no saved sessions yet")
                return
            print("recent sessions:")
            for s in sessions:
                print(f"  {s['id']}  turns={s['turns']:<4} {s['updated_at']}  {s['title'][:50]}")
            try:
                args = input("resume which (id or title, blank to skip)> ").strip()
            except (EOFError, KeyboardInterrupt):
                return
            if not args:
                return
        sess = self.db.find_session(args)
        if not sess:
            self.error(f"no session matching: {args}")
            return
        self.state.session_id = sess["id"]
        self.state.session_title = sess.get("title")
        if self.agent is not None:
            self.agent.load_history(sess["id"])
        self.ok(f"resumed '{sess.get('title')}' ({sess['id']}) — {sess['turns']} turns")

    def slash_rollback(self, args: str = "") -> None:
        from .checkpoints import CheckpointManager
        mgr = CheckpointManager(self.config)
        items = mgr.list_checkpoints()
        if not items:
            self.error("no checkpoints (enable with /checkpoint enable)")
            return
        target = args.strip()
        if not target:
            print("checkpoints:")
            for i, c in enumerate(items, 1):
                print(f"  {i}. {c.get('label')}  {c.get('timestamp')}  ({c.get('count')} files)")
            try:
                pick = input("roll back to which (blank = most recent)> ").strip()
            except (EOFError, KeyboardInterrupt):
                return
            if pick.isdigit() and 1 <= int(pick) <= len(items):
                target = items[int(pick) - 1]["timestamp"]
        result = mgr.rollback(target or None)
        if result.get("ok"):
            self.ok(f"rolled back to {result.get('label')} — restored {result.get('count')} file(s)")
        else:
            self.error(str(result.get("error")))

    def slash_checkpoint(self, args: str = "") -> None:
        from .checkpoints import CheckpointManager
        sub = (args.split() or ["status"])[0]
        if sub in ("enable", "on"):
            self.config.set("sessions.checkpoint.enabled", True)
            self.reload_agent()
            self.ok("checkpoints enabled")
        elif sub in ("disable", "off"):
            self.config.set("sessions.checkpoint.enabled", False)
            self.reload_agent()
            self.ok("checkpoints disabled")
        elif sub == "interval":
            parts = args.split()
            if len(parts) > 1 and parts[1].isdigit():
                self.config.set("sessions.checkpoint.interval", int(parts[1]))
                self.ok(f"checkpoint interval = {parts[1]} turns")
        else:
            items = CheckpointManager(self.config).list_checkpoints()
            enabled = self.config.get("sessions.checkpoint.enabled")
            print(f"checkpoints: {'enabled' if enabled else 'disabled'} "
                  f"(every {self.config.get('sessions.checkpoint.interval')} turns)")
            for c in items[-10:]:
                print(f"  {c.get('label')}  {c.get('timestamp')}  {c.get('count')} files")

    def slash_background(self, args: str = "") -> None:
        if not args.strip():
            self.error("usage: /background <prompt>")
            return
        if self.agent is None:
            self.error("no agent available")
            return
        self.agent.run_background(args.strip())
        self.ok(f"background task started: {args.strip()[:60]}")

    def slash_goal(self, args: str = "") -> None:
        parts = args.split(maxsplit=1)
        sub = parts[0] if parts else "status"
        path = self.config.goal_path
        import json
        if sub == "set" and len(parts) > 1:
            path.parent.mkdir(parents=True, exist_ok=True)
            from .config import atomic_write_text
            atomic_write_text(path, json.dumps({"goal": parts[1], "set_at": time.time()}, indent=2))
            self.state.goal = parts[1]
            self.ok(f"goal set: {parts[1]}")
        elif sub == "clear":
            path.unlink(missing_ok=True)
            self.state.goal = ""
            self.ok("goal cleared")
        else:
            goal = ""
            if path.exists():
                try:
                    goal = json.loads(path.read_text(encoding="utf-8")).get("goal", "")
                except Exception:
                    goal = ""
            print(f"goal: {goal or '(none set)'}")
        self._refresh_system_prompt()

    def slash_kanban(self, args: str = "") -> None:
        from . import kanban as kb
        parts = args.split(maxsplit=2)
        sub = parts[0] if parts else "list"
        if sub == "add":
            if len(parts) < 2:
                self.error("usage: /kanban add <title>")
                return
            card = kb.add(self.db, parts[1], parts[2] if len(parts) > 2 else "")
            self.ok(f"card #{card['id']} added to todo: {card['title']}")
            return
        if sub == "move":
            if len(parts) < 3 or not parts[1].isdigit():
                self.error("usage: /kanban move <id> <todo|in_progress|done>")
                return
            ok = kb.move(self.db, int(parts[1]), parts[2])
            (self.ok if ok else self.error)(f"card #{parts[1]} → {parts[2]}" if ok else "move failed")
            return
        if sub == "clear-done":
            n = kb.clear_done(self.db)
            self.ok(f"cleared {n} done card(s)")
            return
        if sub == "stats":
            print(kb.stats(self.db))
            return
        if sub == "run":
            from .kanban import KanbanDispatcher
            disp = KanbanDispatcher(self.config, self.db, on_event=self._on_event)
            started = disp.run_pending()
            self.ok(f"dispatcher started {started} card(s); max_concurrent="
                    f"{self.config.get('kanban.dispatcher.max_concurrent')}")
            return
        board = kb.board(self.db)
        for column in ("todo", "in_progress", "done"):
            cards = board.get(column, [])
            print(f"\n[{column}] ({len(cards)})")
            for c in cards:
                print(f"  #{c['id']:<4} {c['title'][:60]}")
        if not any(board.values()):
            self.info("board is empty — /kanban add <title>")

    def slash_cron(self, args: str = "") -> None:
        from . import cron as cron_mod
        parts = args.split(maxsplit=2)
        sub = parts[0] if parts else "list"
        if sub == "list":
            jobs = cron_mod.list_jobs(self.config, self.db)
            if not jobs:
                self.info("no cron jobs — /cron create \"every 30m\" \"prompt\"")
            for j in jobs:
                flag = "on " if j["enabled"] else "off"
                print(f"  {j['id']} [{flag}] {j['schedule']:<16} "
                      f"{'shell' if j['no_agent'] else 'agent'}  {j['prompt'][:50]}")
            return
        if sub == "create":
            if len(parts) < 3:
                self.error('usage: /cron create "every 30m|*/2 * * * *" "prompt"  (!prefix = shell, no agent)')
                return
            no_agent = parts[2].startswith("!")
            prompt = parts[2][1:].strip() if no_agent else parts[2]
            job = cron_mod.create_job(self.config, self.db, parts[1], prompt, no_agent=no_agent)
            self.ok(f"job {job['id']} created ({job['schedule']})")
            return
        if len(parts) < 2:
            self.error("usage: /cron list|create SCHED PROMPT|run ID|pause ID|resume ID|remove ID")
            return
        jid = parts[1]
        if sub == "run":
            res = cron_mod.run_job_once(self.config, self.db, jid)
            print(res.get("output") or res.get("error"))
        elif sub in ("pause", "resume"):
            ok = cron_mod.set_enabled(self.config, self.db, jid, sub == "resume")
            (self.ok if ok else self.error)(f"{sub}d {jid}" if ok else f"no such job: {jid}")
        elif sub == "remove":
            ok = cron_mod.remove_job(self.config, self.db, jid)
            (self.ok if ok else self.error)(f"removed {jid}" if ok else f"no such job: {jid}")
        elif sub == "edit" and len(parts) > 2:
            ok = cron_mod.edit_job(self.config, self.db, jid, prompt=parts[2])
            (self.ok if ok else self.error)("edited" if ok else "edit failed")
        else:
            self.error(f"unknown /cron action: {sub}")

    def slash_webhook(self, args: str = "") -> None:
        from . import webhooks as wh
        parts = args.split(maxsplit=2)
        sub = parts[0] if parts else "list"
        if sub == "list":
            routes = wh.list_routes(self.config, self.db)
            if not routes:
                self.info("no webhook routes — /webhook subscribe NAME PATH \"prompt\"")
            for r in routes:
                print(f"  {r['name']:<14} {r['route']:<22} skills={r['skills']}  {r['prompt'][:40]}")
            return
        if sub == "subscribe":
            if len(parts) < 3:
                self.error('usage: /webhook subscribe NAME PATH "prompt"')
                return
            try:
                route = wh.subscribe(self.config, self.db, parts[1], parts[1], parts[2], [])
            except ValueError as exc:
                self.error(str(exc))
                return
            self.ok(f"subscribed {route['name']} → {route['route']} (start with `void webhook serve`)")
            return
        if len(parts) < 2:
            self.error("usage: /webhook list|subscribe|remove|test NAME")
            return
        name = parts[1]
        if sub == "remove":
            ok = wh.remove(self.config, self.db, name)
            (self.ok if ok else self.error)(f"removed {name}" if ok else f"no such route: {name}")
        elif sub == "test":
            res = wh.test_route(self.config, self.db, name)
            print(res.get("result", res))
        else:
            self.error(f"unknown /webhook action: {sub}")

    def slash_xm(self, args: str = "") -> None:
        window = int(self.config.get("compression.context_window", 128000) or 128000)
        if self.agent is None:
            self.info(f"  provider : {self.config.get('model.provider') or 'unset'} "
                      f"(agent not loaded: {self._agent_error or 'no model configured'})")
            self.info(f"  context  : {window} tokens "
                      f"(compression {'on' if self.config.get('compression.enabled') else 'off'}, "
                      f"threshold={self.config.get('compression.threshold')})")
            return
        used = self.state.budget_used_tokens
        pct = (used / window * 100) if window else 0
        print(f"  provider : {self.agent.provider.name} ({self.agent.provider.display})")
        print(f"  model    : {self.agent.model}")
        print(f"  context  : {window} tokens")
        print(f"  used     : {used} tokens ({pct:.1f}%) over {self.state.calls_made} call(s)")
        print(f"  messages : {len(self.agent.messages)} in context · "
              f"{self.db.message_count(self.state.session_id or '')} in DB")
        print(f"  tools    : {len(self.state.active_tools)} active")
        print(f"  compression: {'on' if self.config.get('compression.enabled') else 'off'} "
              f"threshold={self.config.get('compression.threshold')}")

    def slash_status(self, args: str = "") -> None:
        from .doctor import status_line_full
        print(status_line_full(self.config, self.db, self.state, self.agent))

    def slash_doctor(self, args: str = "") -> None:
        from .doctor import run_doctor
        run_doctor(self.config, fix=False, printer=print)

    def slash_save(self, args: str = "") -> None:
        if self.state.session_id:
            self.db.touch_session(self.state.session_id, turns=self.state.turn_count)
            self.ok(f"saved session {self.state.session_id}")
        else:
            self.info("nothing to save yet")

    def slash_export(self, args: str = "") -> None:
        if not self.state.session_id:
            self.error("no session to export")
            return
        fmt = args.strip() or "markdown"
        ext = "jsonl" if fmt == "jsonl" else "md"
        out = Path(self.config.project_dir) / f"session_{self.state.session_id}.{ext}"
        if fmt == "jsonl":
            import json as _json
            lines = [_json.dumps(m, ensure_ascii=False) for m in self.db.get_messages(self.state.session_id)]
            out.write_text("\n".join(lines) + "\n", encoding="utf-8")
        else:
            out.write_text(self.db.export_messages_markdown(self.state.session_id), encoding="utf-8")
        self.ok(f"exported to {out}")

    # ---- REPL ----------------------------------------------------------------------------
    def _build_session(self):
        """prompt_toolkit session with history, completion, autosuggest (or a plain fallback).

        Returns (session, reason_it_is_none). prompt_toolkit needs a real console screen
        buffer; when stdout is piped there isn't one, so we degrade to plain input().
        """
        try:
            from prompt_toolkit import PromptSession
            from prompt_toolkit.auto_suggest import AutoSuggestFromHistory
            from prompt_toolkit.completion import Completer, Completion
            from prompt_toolkit.history import FileHistory
            from prompt_toolkit.styles import Style
        except ImportError as exc:
            return None, f"prompt_toolkit not installed ({exc})"

        commands = autocomplete_candidates()
        bundles = []
        try:
            from .skills import SkillLibrary
            bundles = [(f"/{alias}", "skill bundle") for alias in SkillLibrary(self.config).bundles()]
        except Exception:
            bundles = []

        class SlashCompleter(Completer):
            def get_completions(self, document, complete_event):
                text = document.text_before_cursor
                if not text.startswith("/"):
                    return
                word = text.split()[-1] if text.split() else text
                for name, desc in commands + bundles:
                    if name.startswith(word):
                        yield Completion(name, start_position=-len(word), display_meta=desc)
                        continue
                    # second-level completion: /cmd sub
                    if " " not in word and name.startswith(word):
                        yield Completion(name, start_position=-len(word), display_meta=desc)

        style = Style.from_dict({
            "prompt": self.skin.get("prompt") or "",
            "bottom-toolbar": self.skin.get("status_bar") or "",
        })
        try:
            self.config.history_path.parent.mkdir(parents=True, exist_ok=True)
            session = PromptSession(history=FileHistory(str(self.config.history_path)),
                                    auto_suggest=AutoSuggestFromHistory(),
                                    completer=SlashCompleter(),
                                    complete_while_typing=True,
                                    multiline=False,
                                    style=style,
                                    bottom_toolbar=lambda: self.status_line())
            return session, None
        except Exception as exc:
            return None, str(exc)

    def run(self) -> int:
        self.banner()
        session, reason = self._build_session()
        if session is None:
            self.info(f"(plain input mode — no history/completion: {reason})")
        while True:
            try:
                if session is not None:
                    text = session.prompt("void ❯ ")
                else:
                    text = input("void ❯ ")
            except KeyboardInterrupt:
                print()
                continue
            except EOFError:
                print()
                break
            stripped = text.strip()
            if not stripped:
                continue
            if stripped.startswith("/"):
                try:
                    slash_dispatch(self, stripped)
                except SystemExit:
                    break
                except Exception as exc:
                    self.error(f"command failed: {type(exc).__name__}: {exc}")
                continue
            try:
                self.handle_message(stripped)
            except Exception as exc:
                self.error(f"turn failed: {type(exc).__name__}: {exc}")
        self._shutdown()
        return 0

    def _shutdown(self) -> None:
        try:
            if self.agent is not None and self.state.turn_count >= 6:
                self.agent.flush_memory()
        except Exception:
            pass
        try:
            self.db.close()
        except Exception:
            pass
        self.info("bye.")


# ---- module helpers ---------------------------------------------------------------------
def _auto_title(text: str, limit: int = 48) -> str:
    """Cheap heuristic title from the first user message (no extra model call)."""
    cleaned = " ".join((text or "").split())
    if len(cleaned) > limit:
        cleaned = cleaned[:limit].rstrip() + "…"
    return cleaned or "untitled"


def _summarize_args(args: dict, limit: int = 120) -> str:
    """One-line rendering of tool args for the ┊ activity line."""
    if not isinstance(args, dict) or not args:
        return ""
    primary = ""
    for key in ("command", "path", "url", "query", "title", "prompt", "code", "schedule"):
        if key in args:
            primary = f"{key}={str(args[key])[:limit]}"
            break
    if not primary:
        primary = ", ".join(f"{k}={str(v)[:40]}" for k, v in list(args.items())[:3])
    return primary[:limit]


def _coerce(value: str):
    """Turn a CLI string into bool/int/float/None/list where obvious."""
    low = value.strip().lower()
    if low in ("true", "yes", "on"):
        return True
    if low in ("false", "no", "off"):
        return False
    if low in ("null", "none", ""):
        return None
    if "," in value and not value.startswith("["):
        return [v.strip() for v in value.split(",") if v.strip()]
    try:
        return int(value)
    except ValueError:
        pass
    try:
        return float(value)
    except ValueError:
        pass
    return value







