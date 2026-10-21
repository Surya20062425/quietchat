"""void entry point: global flag parsing, profile/env bootstrap, subcommand dispatch.

Bootstrapping order matters (architecture rule 7): resolve the profile and load .env into
os.environ BEFORE importing heavy modules (provider SDKs, prompt_toolkit), so config is
always read from the right place and no module-level cache goes stale.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

from . import __version__
from .config import Config, DEFAULT_CONFIG, resolve_profile

GLOBAL_FLAGS_WITH_VALUE = {
    "--profile": "profile", "-p": "profile",
    "--toolsets": "toolsets",
    "--skills": "skills",
    "--model": "model",
    "--resume": "resume",
    "--continue": "continue",
    "--worktree": "worktree_name",
    "--skin": "skin",
}
GLOBAL_FLAGS_BOOL = {
    "--yolo": "yolo", "--safe-mode": "safe_mode", "--debug": "debug",
    "--version": "version", "-V": "version", "--help": "help", "-h": "help",
}

COMMAND_NAMES = {"chat", "model", "config", "tools", "skills", "sessions", "cron", "webhook",
                 "kanban", "memory", "skin", "auth", "doctor", "status", "profile",
                 "completion", "update", "uninstall", "setup", "mcp", "logs", "version", "help"}


def parse_globals(argv: list[str]) -> tuple[dict, list[str]]:
    """Split leading global flags from the subcommand. Handles `--continue` with no value."""
    opts: dict = {"continue": None}
    rest: list[str] = []
    i = 0
    while i < len(argv):
        token = argv[i]
        if token == "--":
            rest.extend(argv[i + 1:])
            break
        if token in GLOBAL_FLAGS_BOOL:
            opts[GLOBAL_FLAGS_BOOL[token]] = True
            i += 1
            continue
        matched = False
        for flag, key in GLOBAL_FLAGS_WITH_VALUE.items():
            if token == flag or token.startswith(flag + "="):
                if "=" in token:
                    opts[key] = token.split("=", 1)[1]
                    i += 1
                elif key == "continue":
                    if i + 1 < len(argv) and not argv[i + 1].startswith("-") \
                            and argv[i + 1] not in COMMAND_NAMES:
                        opts[key] = argv[i + 1]
                        i += 2
                    else:
                        opts[key] = ""
                        i += 1
                elif i + 1 < len(argv):
                    opts[key] = argv[i + 1]
                    i += 2
                else:
                    opts[key] = ""
                    i += 1
                matched = True
                break
        if matched:
            continue
        rest.append(token)
        i += 1
    return opts, rest


def bootstrap(opts: dict) -> tuple[Config, dict]:
    """Resolve the profile and build Config (which loads .env into os.environ)."""
    profile = resolve_profile(opts.get("profile"))
    config = Config(profile=profile)
    if opts.get("safe_mode"):
        config.data = dict(DEFAULT_CONFIG)
        config.set("display.skin", "default")
        config.set("memory.enabled", False)
        config.set("compression.enabled", False)
        config.set("sessions.checkpoint.enabled", False)
        config.set("skills.sources", [])
    return config, opts


def _discover_tools() -> None:
    from .tools.registry import registry
    registry.discover(Path(__file__).resolve().parent / "tools")


def _force_utf8() -> None:
    """Windows consoles default to cp1252 and can't encode ┊ ✓ ✗ ❯ — force UTF-8."""
    for stream in (sys.stdout, sys.stderr):
        try:
            if hasattr(stream, "reconfigure"):
                stream.reconfigure(encoding="utf-8", errors="replace")
        except (ValueError, OSError, AttributeError):
            pass
    os.environ.setdefault("PYTHONIOENCODING", "utf-8")


def main(argv: list[str] | None = None) -> int:
    _force_utf8()
    argv = list(sys.argv[1:] if argv is None else argv)
    opts, rest = parse_globals(argv)

    if opts.get("version"):
        print_version()
        return 0

    command = rest[0] if rest else None
    args = rest[1:] if rest else []

    if opts.get("help") and not command:
        print_help()
        return 0

    config, opts = bootstrap(opts)
    from .logging_setup import setup_logging
    setup_logging(config.logs_dir, debug=bool(opts.get("debug")))
    _discover_tools()

    if opts.get("skin"):
        config.set("display.skin", opts["skin"])

    register_commands()
    if command and command not in COMMANDS:
        print(f"[void] unknown command: {command}\n")
        print_help()
        return 2
    if not command:
        return cmd_repl(config, [], opts)
    return COMMANDS[command](config, args, opts)


def print_version() -> None:
    print(f"void {__version__}")


def print_help() -> None:
    print(f"""void {__version__} — a self-contained AI agent terminal harness

USAGE
  void [global flags] [command] [args]

GLOBAL FLAGS
  --profile NAME        use a named profile
  --resume ID|TITLE     resume a session
  --continue [NAME]     resume the most recent session
  --worktree [NAME]     run inside an isolated git worktree
  --toolsets LIST       restrict tools for this invocation (e.g. terminal,file)
  --skills LIST         preload skills for this invocation
  --model NAME          override the model for this invocation
  --skin NAME           override the display skin
  --yolo                skip dangerous-command approval prompts
  --safe-mode           disable all customizations (troubleshooting)
  --debug               verbose logging
  -V, --version         print version

COMMANDS
  chat -q "PROMPT"      one-shot; prints only the final response (--image PATH for vision)
  model                 interactive provider -> model picker
  setup [section]       wizard (model|terminal|tools|security|display|memory|compression)
  config get|set|unset|show|check|migrate
  tools [list]          list toolsets and tools
  skills list|search|inspect|install|uninstall|update|tap
  sessions list|rename|delete|export|prune|stats|search
  cron create|list|edit|run|remove|status|pause|resume
  webhook subscribe|list|remove|test|serve
  kanban list|add|move|clear-done|stats|run
  memory setup|status|off|reset
  skin list|use|set
  auth add|remove|list
  profile list|create|use|show|delete|rename|export|import
  mcp serve             run as an MCP server (STDIO)
  logs [-f] [errors]    tail logs
  doctor [--fix]        health check
  status                one-line status
  completion bash|zsh|fish
  update | uninstall | version

Run `void` with no command to start the interactive REPL.
""")


COMMANDS: dict = {}


def register_commands() -> None:
    if COMMANDS:
        return
    COMMANDS.update({
        "chat": cmd_chat, "model": cmd_model, "config": cmd_config, "tools": cmd_tools,
        "skills": cmd_skills, "sessions": cmd_sessions, "cron": cmd_cron, "webhook": cmd_webhook,
        "kanban": cmd_kanban, "memory": cmd_memory, "skin": cmd_skin, "auth": cmd_auth,
        "doctor": cmd_doctor, "status": cmd_status, "profile": cmd_profile,
        "completion": cmd_completion, "update": cmd_update, "uninstall": cmd_uninstall,
        "setup": cmd_setup, "mcp": cmd_mcp, "logs": cmd_logs,
        "version": lambda c, a, o: (print_version(), 0)[1],
        "help": lambda c, a, o: (print_help(), 0)[1],
    })



# ---- shared helpers ---------------------------------------------------------------------
def _open_db(config: Config):
    from .session_db import SessionDB
    return SessionDB(config.state_db_path)


def _apply_worktree(config: Config, opts: dict) -> None:
    """--worktree: create an isolated git worktree and chdir into it."""
    name = opts.get("worktree_name") or ""
    import subprocess
    import time
    root = Path(config.project_dir)
    worktrees_dir = root / ".void-worktrees"
    worktrees_dir.mkdir(parents=True, exist_ok=True)
    branch = f"void-{name or time.strftime('%m%d-%H%M%S')}"
    target = worktrees_dir / branch
    try:
        subprocess.run(["git", "-C", str(root), "worktree", "add", "-b", branch, str(target)],
                       check=True, capture_output=True, text=True, timeout=60)
        config.set("terminal.project_dir", str(target))
        os.chdir(target)
        print(f"[void] isolated worktree: {target} (branch {branch})")
    except Exception as exc:
        print(f"[void] could not create a git worktree ({exc}); continuing in place")


def _build_state(config: Config, opts: dict):
    from .state import RuntimeState
    state = RuntimeState()
    state.yolo = bool(opts.get("yolo") or config.get("security.approvals.yolo"))
    state.safe_mode = bool(opts.get("safe_mode"))
    if opts.get("toolsets"):
        state.enabled_toolsets = [t.strip() for t in str(opts["toolsets"]).split(",") if t.strip()]
    if opts.get("model"):
        state.model_override = str(opts["model"])
    return state


def _preload_skills(config: Config, state, names: str | None) -> None:
    if not names:
        return
    from .skills import SkillLibrary
    lib = SkillLibrary(config)
    for name in [n.strip() for n in str(names).split(",") if n.strip()]:
        content = lib.content(name)
        if content:
            state.loaded_skills[name] = content


def _title_from(text: str, limit: int = 48) -> str:
    cleaned = " ".join((text or "").split())
    return (cleaned[:limit].rstrip() + "…") if len(cleaned) > limit else (cleaned or "untitled")



# ---- REPL / one-shot ---------------------------------------------------------------------
def cmd_repl(config: Config, args: list[str], opts: dict) -> int:
    if opts.get("worktree") or opts.get("worktree_name"):
        _apply_worktree(config, opts)
    db = _open_db(config)
    state = _build_state(config, opts)
    _preload_skills(config, state, opts.get("skills"))

    session_id = None
    if opts.get("resume"):
        sess = db.find_session(str(opts["resume"]))
        if not sess:
            print(f"[void] no session matching: {opts['resume']}")
            return 1
        session_id = sess["id"]
    elif opts.get("continue") is not None:
        sess = db.find_session(str(opts["continue"])) if opts["continue"] else db.recent_session()
        if not sess:
            print("[void] no previous session to continue")
        else:
            session_id = sess["id"]

    from .cli import HermesLiteCLI
    cli = HermesLiteCLI(config, db, state=state, session_id=session_id)
    _start_peripherals(config, db, cli)
    return cli.run()


def _start_peripherals(config: Config, db, cli) -> None:
    """Start cron scheduler / kanban dispatcher / webhook server (daemon threads)."""
    from .logging_setup import log_line
    try:
        if config.get("cron.enabled", True):
            from .cron import CronScheduler
            CronScheduler(config, db, on_output=lambda job, res: cli.info(
                f"┊ cron {job.get('name') or job['id']}: "
                f"{str(res.get('output') or res.get('error') or '')[:200]}")).start()
        if config.get("kanban.dispatcher.enabled"):
            from .kanban import KanbanDispatcher
            KanbanDispatcher(config, db, on_event=cli._on_event).start()
        if config.get("webhook.autostart"):
            from .webhooks import serve as wh_serve
            wh_serve(config, db, block=False)
    except Exception as exc:
        log_line(config.logs_dir, "void.log", "peripherals", f"failed to start: {exc}")


def cmd_chat(config: Config, args: list[str], opts: dict) -> int:
    """One-shot: print only the final response (for scripts/pipes)."""
    prompt = ""
    images: list[str] = []
    i = 0
    while i < len(args):
        token = args[i]
        if token in ("-q", "--query", "--prompt") and i + 1 < len(args):
            prompt, i = args[i + 1], i + 2
        elif token.startswith(("-q=", "--query=")):
            prompt, i = token.split("=", 1)[1], i + 1
        elif token == "--image" and i + 1 < len(args):
            images.append(args[i + 1])
            i += 2
        elif not prompt:
            prompt, i = token, i + 1
        else:
            prompt, i = prompt + " " + token, i + 1
    if not prompt and not images:
        print("[void] usage: void chat -q \"prompt\" [--image path]", file=sys.stderr)
        return 2

    aux = config.get("model.auxiliary.vision") or {}
    if images and (aux.get("provider") or aux.get("model")):
        if aux.get("provider"):
            config.set("model.provider", aux["provider"])
        if aux.get("model"):
            config.set("model.default", aux["model"])
        if aux.get("base_url"):
            config.set("model.custom.base_url", aux["base_url"])

    db = _open_db(config)
    state = _build_state(config, opts)
    state.stream_enabled = False
    _preload_skills(config, state, opts.get("skills"))
    try:
        from .agent.run_agent import AIAgent
        agent = AIAgent(config, state, db=db)
        session_id = db.create_session(_title_from(prompt), model=agent.model,
                                       provider=agent.provider.name)
        agent.state.session_id = session_id
        text = agent.run_conversation(prompt, images=images or None)
        db.touch_session(session_id, turns=state.turn_count, model=agent.model,
                         provider=agent.provider.name)
    except Exception as exc:
        print(f"[void] {type(exc).__name__}: {exc}", file=sys.stderr)
        return 1
    print(text)
    return 0


def cmd_model(config: Config, args: list[str], opts: dict) -> int:
    from .model_tools import pick_model_interactive
    return 0 if pick_model_interactive(config) else 1



# ---- config -------------------------------------------------------------------------------
def cmd_config(config: Config, args: list[str], opts: dict) -> int:
    import yaml
    sub = args[0] if args else "show"
    if sub == "get":
        if len(args) < 2:
            print("[void] usage: void config get <key.path>")
            return 2
        print(yaml.safe_dump({args[1]: config.get(args[1])}, sort_keys=False))
        return 0
    if sub == "set":
        if len(args) < 3:
            print("[void] usage: void config set <key.path> <value>")
            return 2
        from .cli import _coerce
        config.set(args[1], _coerce(" ".join(args[2:])))
        print(f"{args[1]} = {config.get(args[1])!r}")
        return 0
    if sub == "unset":
        if len(args) < 2:
            print("[void] usage: void config unset <key.path>")
            return 2
        print("removed" if config.unset(args[1]) else "key not set")
        return 0
    if sub == "check":
        problems = config.check()
        if not problems:
            print("config OK")
            return 0
        for p in problems:
            print(f"  ✗ {p}")
        return 1
    if sub == "migrate":
        notes = config.migrate()
        print("\n".join(notes) if notes else "already up to date")
        return 0
    if sub == "edit":
        import subprocess
        editor = os.environ.get("EDITOR", "notepad" if os.name == "nt" else "nano")
        subprocess.run([editor, str(config.config_path)], check=False)
        config.load()
        return 0
    print(yaml.safe_dump(config.data, sort_keys=False))
    return 0


def cmd_tools(config: Config, args: list[str], opts: dict) -> int:
    from .tools.registry import registry
    summary = registry.toolsets_summary()
    enabled = set(config.get("toolsets.enabled") or ["all"])
    print(f"tools registered: {len(registry.names())}")
    for name, tools in sorted(summary.items()):
        mark = "✓" if (name in enabled or "all" in enabled) else " "
        print(f"  [{mark}] {name:<10} {', '.join(tools) or '(none)'}")
    return 0


def cmd_skills(config: Config, args: list[str], opts: dict) -> int:
    from .skills import SkillLibrary
    lib = SkillLibrary(config)
    sub = args[0] if args else "list"
    rest = " ".join(args[1:])
    if sub in ("list", "browse"):
        skills = lib.list()
        if not skills:
            print("no skills installed")
        for s in skills:
            print(f"  {s.name:<24} v{s.version:<6} [{s.source}] {s.description[:70]}")
        for alias, ids in lib.bundles().items():
            print(f"  bundle /{alias} -> {', '.join(ids)}")
        return 0
    if sub == "search":
        for s in lib.search(rest):
            print(f"  {s.name:<24} {s.description[:70]}")
        return 0
    if sub == "inspect":
        content = lib.content(rest)
        print(content or f"no such skill: {rest}")
        return 0 if content else 1
    if sub in ("install", "uninstall", "update"):
        if not rest:
            print(f"[void] usage: void skills {sub} <name-or-path>")
            return 2
        ok, msg = getattr(lib, sub)(rest)
        print(msg)
        return 0 if ok else 1
    if sub == "tap":
        parts = rest.split(maxsplit=1)
        if not parts:
            for s in (config.get("skills.sources") or []):
                print(f"  {s}")
            return 0
        if parts[0] == "add" and len(parts) > 1:
            ok, msg = lib.add_source(parts[1])
            print(msg)
            return 0 if ok else 1
        if parts[0] == "remove" and len(parts) > 1:
            ok, msg = lib.remove_source(parts[1])
            print(msg)
            return 0
    print("[void] usage: void skills list|browse|search Q|inspect N|install SRC|uninstall N|"
          "update N|tap [add REPO|remove REPO]")
    return 2



# ---- sessions / memory ----------------------------------------------------------------------
def cmd_sessions(config: Config, args: list[str], opts: dict) -> int:
    db = _open_db(config)
    sub = args[0] if args else "list"
    rest = args[1:]
    if sub == "list":
        sessions = db.list_sessions(limit=50)
        if not sessions:
            print("no sessions yet")
        for s in sessions:
            print(f"  {s['id']}  turns={s['turns']:<4} msgs={s['message_count']:<4} "
                  f"{s['updated_at']}  {s['provider']}/{s['model']}  {s['title'][:44]}")
        return 0
    if sub == "search":
        if not rest:
            print("[void] usage: void sessions search <query>")
            return 2
        for m in db.search_messages(" ".join(rest), limit=25):
            print(f"  [{m['session_id']}] {m['role']}: {m['content'][:110]}")
        return 0
    if sub == "rename":
        if len(rest) < 2:
            print("[void] usage: void sessions rename <id-or-title> <new title>")
            return 2
        sess = db.find_session(rest[0])
        if not sess:
            print(f"[void] no session matching: {rest[0]}")
            return 1
        db.rename_session(sess["id"], " ".join(rest[1:]))
        print(f"renamed {sess['id']} → {' '.join(rest[1:])}")
        return 0
    if sub == "delete":
        if not rest:
            print("[void] usage: void sessions delete <id-or-title>")
            return 2
        sess = db.find_session(rest[0])
        if not sess:
            print(f"[void] no session matching: {rest[0]}")
            return 1
        db.delete_session(sess["id"])
        print(f"deleted {sess['id']}")
        return 0
    if sub == "export":
        if not rest:
            print("[void] usage: void sessions export <id-or-title> [jsonl|markdown] [path]")
            return 2
        sess = db.find_session(rest[0])
        if not sess:
            print(f"[void] no session matching: {rest[0]}")
            return 1
        fmt = rest[1] if len(rest) > 1 else "markdown"
        if fmt == "jsonl":
            import json as _json
            out = Path(rest[2]) if len(rest) > 2 else Path(f"session_{sess['id']}.jsonl")
            lines = [_json.dumps(m, ensure_ascii=False) for m in db.get_messages(sess["id"])]
            out.write_text("\n".join(lines) + "\n", encoding="utf-8")
        else:
            out = Path(rest[2]) if len(rest) > 2 else Path(f"session_{sess['id']}.md")
            out.write_text(db.export_messages_markdown(sess["id"]), encoding="utf-8")
        print(f"exported to {out}")
        return 0
    if sub == "prune":
        days = keep = None
        for i, a in enumerate(rest):
            if a == "--days" and i + 1 < len(rest):
                days = int(rest[i + 1])
            if a == "--keep" and i + 1 < len(rest):
                keep = int(rest[i + 1])
        removed = db.prune_sessions(days, keep)
        print(f"pruned {removed} session(s)")
        return 0
    if sub == "stats":
        from .doctor import status_line_full
        from .state import RuntimeState
        print(status_line_full(config, db, RuntimeState()))
        return 0
    print("[void] usage: void sessions list|search Q|rename ID TITLE|delete ID|export ID [fmt] [path]|"
          "prune [--days N] [--keep M]|stats")
    return 2


def cmd_memory(config: Config, args: list[str], opts: dict) -> int:
    from .memory import MemoryStores
    mem = MemoryStores(config)
    sub = args[0] if args else "status"
    if sub == "setup":
        config.set("memory.enabled", True)
        print("memory enabled")
        return 0
    if sub in ("off", "disable"):
        config.set("memory.enabled", False)
        print("memory disabled")
        return 0
    if sub == "reset":
        for store in ("agent", "user"):
            mem.clear(store)
        print("memory reset")
        return 0
    for store in ("user", "agent"):
        content = mem.read(store)
        limit = config.get(f"memory.{store}_limit_tokens")
        print(f"\n[{store}] ~{mem.token_count(store)}/{limit} tokens")
        print(content or "  (empty)")
    return 0



# ---- cron / webhook / kanban ------------------------------------------------------------------
def cmd_cron(config: Config, args: list[str], opts: dict) -> int:
    from . import cron as cron_mod
    db = _open_db(config)
    sub = args[0] if args else "list"
    rest = args[1:]
    if sub in ("list", "status"):
        jobs = cron_mod.list_jobs(config, db)
        if not jobs:
            print("no cron jobs")
        for j in jobs:
            print(f"  {j['id']} [{'on ' if j['enabled'] else 'off'}] {j['schedule']:<16} "
                  f"{'shell' if j['no_agent'] else 'agent'}  {j['prompt'][:50]}"
                  f"  last={j.get('last_status') or '-'}")
        return 0
    if sub == "create":
        if len(rest) < 2:
            print('[void] usage: void cron create "every 30m|*/2 * * * *" "prompt" '
                  '[--toolsets a,b] [--no-agent]')
            return 2
        schedule = rest[0]
        no_agent = "--no-agent" in rest
        toolsets: list[str] = []
        prompt_parts: list[str] = []
        i = 1
        while i < len(rest):
            if rest[i] == "--toolsets" and i + 1 < len(rest):
                toolsets = [t.strip() for t in rest[i + 1].split(",") if t.strip()]
                i += 2
            elif rest[i] == "--no-agent":
                i += 1
            else:
                prompt_parts.append(rest[i])
                i += 1
        prompt = " ".join(prompt_parts)
        if not prompt:
            print("[void] a prompt (or shell command) is required")
            return 2
        try:
            job = cron_mod.create_job(config, db, schedule, prompt,
                                      toolsets=toolsets, no_agent=no_agent)
        except ValueError as exc:
            print(f"[void] {exc}")
            return 2
        mode = "shell (no agent)" if no_agent else "agent"
        print(f"created job {job['id']}: {job['schedule']} -> {mode}")
        return 0
    if sub == "serve":
        from .cron import CronScheduler
        print("[void] cron scheduler running (Ctrl+C to stop)")
        sched = CronScheduler(config, db, on_output=lambda j, r: print(
            f"┊ cron {j.get('name') or j['id']}: {str(r.get('output') or r.get('error') or '')[:300]}"))
        sched.start()
        try:
            import time as _t
            while True:
                _t.sleep(1)
        except KeyboardInterrupt:
            sched.stop()
        return 0
    if not rest:
        print(f"[void] usage: void cron {sub} <job-id>")
        return 2
    jid = rest[0]
    if sub == "run":
        res = cron_mod.run_job_once(config, db, jid)
        print(res.get("output") or res.get("error") or res)
        return 0 if res.get("ok") else 1
    if sub in ("pause", "resume"):
        ok = cron_mod.set_enabled(config, db, jid, sub == "resume")
        print(f"{sub}d {jid}" if ok else f"no such job: {jid}")
        return 0 if ok else 1
    if sub == "remove":
        ok = cron_mod.remove_job(config, db, jid)
        print(f"removed {jid}" if ok else f"no such job: {jid}")
        return 0 if ok else 1
    if sub == "edit":
        if len(rest) < 2:
            print('[void] usage: void cron edit <id> "new prompt"')
            return 2
        ok = cron_mod.edit_job(config, db, jid, prompt=" ".join(rest[1:]))
        print("edited" if ok else "edit failed")
        return 0 if ok else 1
    print("[void] usage: void cron list|create|edit|run|remove|status|pause|resume|serve")
    return 2



def cmd_webhook(config: Config, args: list[str], opts: dict) -> int:
    from . import webhooks as wh
    db = _open_db(config)
    sub = args[0] if args else "list"
    rest = args[1:]
    if sub == "list":
        routes = wh.list_routes(config, db)
        if not routes:
            print("no webhook routes")
        for r in routes:
            print(f"  {r['name']:<16} {r['route']:<24} skills={r['skills']}  "
                  f"last_hit={r['last_hit'] or '-'}")
        return 0
    if sub == "subscribe":
        name = rest[0] if rest else ""
        route = f"/webhook/{name}"
        prompt = "Handle this event."
        skills: list[str] = []
        i = 1
        while i < len(rest):
            if rest[i] == "--route" and i + 1 < len(rest):
                route, i = rest[i + 1], i + 2
            elif rest[i] == "--prompt" and i + 1 < len(rest):
                prompt, i = rest[i + 1], i + 2
            elif rest[i] == "--skills" and i + 1 < len(rest):
                skills = [s.strip() for s in rest[i + 1].split(",") if s.strip()]
                i += 2
            else:
                i += 1
        if not name:
            print('[void] usage: void webhook subscribe NAME [--route /webhook/x] '
                  '[--prompt "..."] [--skills a,b]')
            return 2
        try:
            r = wh.subscribe(config, db, name, route, prompt, skills)
        except ValueError as exc:
            print(f"[void] {exc}")
            return 1
        print(f"subscribed {r['name']} → {r['route']}")
        return 0
    if sub == "serve":
        wh.serve(config, db)
        return 0
    if sub in ("remove", "test"):
        if not rest:
            print(f"[void] usage: void webhook {sub} <name>")
            return 2
        if sub == "remove":
            ok = wh.remove(config, db, rest[0])
            print(f"removed {rest[0]}" if ok else f"no such route: {rest[0]}")
            return 0 if ok else 1
        res = wh.test_route(config, db, rest[0])
        print(res.get("result", res))
        return 0 if res.get("ok") else 1
    print("[void] usage: void webhook list|subscribe|remove|test|serve")
    return 2


def cmd_kanban(config: Config, args: list[str], opts: dict) -> int:
    from . import kanban as kb
    db = _open_db(config)
    sub = args[0] if args else "list"
    rest = args[1:]
    if sub == "list":
        board = kb.board(db)
        for column in ("todo", "in_progress", "done"):
            print(f"\n[{column}] ({len(board.get(column, []))})")
            for c in board.get(column, []):
                print(f"  #{c['id']:<4} {c['title'][:64]}")
        return 0
    if sub == "add":
        if not rest:
            print("[void] usage: void kanban add <title>")
            return 2
        card = kb.add(db, " ".join(rest))
        print(f"card #{card['id']} added to todo")
        return 0
    if sub == "move":
        if len(rest) < 2 or not rest[0].isdigit():
            print("[void] usage: void kanban move <id> <todo|in_progress|done>")
            return 2
        ok = kb.move(db, int(rest[0]), rest[1])
        print("moved" if ok else "invalid id or column")
        return 0 if ok else 1
    if sub == "clear-done":
        print(f"cleared {kb.clear_done(db)} card(s)")
        return 0
    if sub == "stats":
        print(kb.stats(db))
        return 0
    if sub == "run":
        from .kanban import KanbanDispatcher
        disp = KanbanDispatcher(config, db, on_event=lambda e: print("┊ " + str(e.get("text"))))
        disp.start()
        print("[void] kanban dispatcher running (Ctrl+C to stop)")
        try:
            import time as _t
            while True:
                _t.sleep(1)
        except KeyboardInterrupt:
            disp.stop()
        return 0
    print("[void] usage: void kanban list|add TITLE|move ID COLUMN|clear-done|stats|run")
    return 2



# ---- skin / auth ------------------------------------------------------------------------------
def cmd_skin(config: Config, args: list[str], opts: dict) -> int:
    from .skin import SKIN_KEYS, Skin
    skin = Skin(config)
    sub = args[0] if args else "list"
    rest = args[1:]
    if sub == "list":
        for name in skin.list_skins():
            mark = "*" if name == skin.name else " "
            print(f"  [{mark}] {name}")
        return 0
    if sub == "use":
        if not rest or not skin.use(rest[0]):
            print(f"[void] usage: void skin use <{'|'.join(skin.list_skins())}>")
            return 2
        print(f"active skin: {rest[0]}")
        return 0
    if sub == "set":
        if len(rest) < 2:
            print(f"[void] usage: void skin set <key> <value>\nkeys: {', '.join(SKIN_KEYS)}")
            return 2
        try:
            skin.set_key(rest[0], " ".join(rest[1:]))
        except KeyError as exc:
            print(f"[void] {exc}")
            return 2
        print(f"{rest[0]} = {' '.join(rest[1:])!r} (written to {skin.active_file()})")
        return 0
    if sub == "show":
        for k, v in skin.styles.items():
            print(f"  {k:<16} {v}")
        return 0
    print("[void] usage: void skin list|use NAME|set KEY VALUE|show")
    return 2


def cmd_auth(config: Config, args: list[str], opts: dict) -> int:
    from .config import PROVIDER_KEY_NAMES
    sub = args[0] if args else "list"
    if sub == "list":
        for provider, env_name in PROVIDER_KEY_NAMES.items():
            keys = config.api_keys(provider)
            print(f"  {provider:<15} {env_name:<22} {len(keys)} key(s) in pool")
        print(f"\n  env file: {config.env_path}")
        return 0
    if sub == "add":
        if len(args) < 2:
            print(f"[void] usage: void auth add <{'|'.join(PROVIDER_KEY_NAMES)}> [key]")
            return 2
        provider = args[1]
        env_name = PROVIDER_KEY_NAMES.get(provider)
        if not env_name:
            print(f"[void] unknown provider: {provider}")
            return 2
        key = args[2] if len(args) > 2 else ""
        if not key:
            from getpass import getpass
            try:
                key = getpass(f"{env_name}: ").strip()
            except Exception:
                key = input(f"{env_name}: ").strip()
        if not key:
            print("[void] no key provided")
            return 1
        existing = config.api_keys(provider)
        name = env_name if not existing else f"{env_name}_{len(existing) + 1}"
        config.set_env(name, key)
        print(f"saved {name} to {config.env_path} (pool size {len(config.api_keys(provider))})")
        return 0
    if sub == "remove":
        if len(args) < 2:
            print("[void] usage: void auth remove <ENV_KEY_NAME>")
            return 2
        ok = config.remove_env(args[1])
        print(f"removed {args[1]}" if ok else f"not found: {args[1]}")
        return 0 if ok else 1
    print("[void] usage: void auth list|add <provider> [key]|remove <ENV_NAME>")
    return 2


# ---- doctor / status ---------------------------------------------------------------------------
def cmd_doctor(config: Config, args: list[str], opts: dict) -> int:
    from .doctor import run_doctor
    return run_doctor(config, fix="--fix" in args)


def cmd_status(config: Config, args: list[str], opts: dict) -> int:
    from .doctor import status_line_full
    from .state import RuntimeState
    db = _open_db(config)
    print(status_line_full(config, db, RuntimeState()))
    return 0


def cmd_logs(config: Config, args: list[str], opts: dict) -> int:
    from .logging_setup import tail_logs
    tail_logs(config.logs_dir, errors="errors" in args, follow=("-f" in args or "--follow" in args))
    return 0



# ---- profile ---------------------------------------------------------------------------------
def cmd_profile(config: Config, args: list[str], opts: dict) -> int:
    from . import profiles as prof
    sub = args[0] if args else "list"
    rest = args[1:]
    if sub == "list":
        for p in prof.list_profiles(config):
            mark = "*" if p["active"] else " "
            print(f"  [{mark}] {p['name']:<16} {p['path']}")
        return 0
    if sub == "create":
        if not rest:
            print("[void] usage: void profile create <name> [--clone] [--clone-all]")
            return 2
        ok, msg = prof.create_profile(config, rest[0], clone="--clone" in rest or "--clone-all" in rest,
                                     clone_all="--clone-all" in rest)
        print(msg)
        return 0 if ok else 1
    if sub == "use":
        if not rest:
            print("[void] usage: void profile use <name>")
            return 2
        ok, msg = prof.set_active(config, rest[0])
        print(msg)
        return 0 if ok else 1
    if sub == "show":
        import yaml
        info = prof.show_profile(config, rest[0] if rest else None)
        print(yaml.safe_dump(info, sort_keys=False))
        return 0
    if sub == "delete":
        if not rest:
            print("[void] usage: void profile delete <name>")
            return 2
        ok, msg = prof.delete_profile(config, rest[0])
        print(msg)
        return 0 if ok else 1
    if sub == "rename":
        if len(rest) < 2:
            print("[void] usage: void profile rename <old> <new>")
            return 2
        ok, msg = prof.rename_profile(config, rest[0], rest[1])
        print(msg)
        return 0 if ok else 1
    if sub == "export":
        if len(rest) < 2:
            print("[void] usage: void profile export <name> <dest.zip>")
            return 2
        ok, msg = prof.export_profile(config, rest[0], rest[1])
        print(msg)
        return 0 if ok else 1
    if sub == "import":
        if not rest:
            print("[void] usage: void profile import <archive.zip> [name]")
            return 2
        ok, msg = prof.import_profile(config, rest[0], rest[1] if len(rest) > 1 else None)
        print(msg)
        return 0 if ok else 1
    print("[void] usage: void profile list|create NAME [--clone|--clone-all]|use NAME|show [NAME]|"
          "delete NAME|rename OLD NEW|export NAME DEST|import ARCHIVE [NAME]")
    return 2


# ---- setup / mcp -------------------------------------------------------------------------------
def cmd_setup(config: Config, args: list[str], opts: dict) -> int:
    from .setup_wizard import run_wizard
    return run_wizard(config, args[0] if args else None)


def cmd_mcp(config: Config, args: list[str], opts: dict) -> int:
    sub = args[0] if args else "serve"
    if sub == "serve":
        from .mcp_server import serve
        db = _open_db(config)
        return serve(config, db)
    if sub == "add":
        import yaml
        if len(args) < 2:
            print('[void] usage: void mcp add NAME --command "..." | --url "..."')
            return 2
        name = args[1]
        entry: dict = {}
        i = 2
        while i < len(args):
            if args[i] == "--command" and i + 1 < len(args):
                entry["command"] = args[i + 1]
                i += 2
            elif args[i] == "--url" and i + 1 < len(args):
                entry["url"], i = args[i + 1], i + 2
            else:
                i += 1
        servers = dict(config.get("mcp.servers") or {})
        servers[name] = entry
        config.set("mcp.servers", servers)
        print(f"registered MCP server '{name}'")
        return 0
    if sub == "list":
        servers = config.get("mcp.servers") or {}
        if not servers:
            print("no external MCP servers configured")
        for name, cfg in servers.items():
            print(f"  {name:<16} {cfg}")
        return 0
    if sub == "remove":
        if len(args) < 2:
            print("[void] usage: void mcp remove NAME")
            return 2
        servers = dict(config.get("mcp.servers") or {})
        ok = servers.pop(args[1], None) is not None
        config.set("mcp.servers", servers)
        print(f"removed {args[1]}" if ok else f"no such server: {args[1]}")
        return 0 if ok else 1
    print("[void] usage: void mcp serve|add NAME --command CMD|--url URL|list|remove NAME")
    return 2



# ---- completion / update / uninstall -------------------------------------------------------------
def cmd_completion(config: Config, args: list[str], opts: dict) -> int:
    shell = args[0] if args else "bash"
    if shell == "bash":
        print("""# void bash completion — add to ~/.bashrc:
#   source <(void completion bash)
_void() {
  local cur prev words cword
  COMPREPLY=()
  cur="${COMP_WORDS[COMP_CWORD]}"
  local cmds="chat model setup config tools skills sessions cron webhook kanban memory skin auth profile mcp logs doctor status completion update uninstall version help"
  local gflags="--profile --resume --continue --worktree --toolsets --skills --model --skin --yolo --safe-mode --debug --version --help"
  if [[ ${COMP_CWORD} -eq 1 ]]; then
    COMPREPLY=( $(compgen -W "$cmds $gflags" -- "$cur") )
  else
    COMPREPLY=( $(compgen -W "$gflags" -- "$cur") )
  fi
}
complete -F _void void""")
        return 0
    if shell == "zsh":
        print("""# void zsh completion — add to ~/.zshrc:
#   source <(void completion zsh)
_void() {
  local -a cmds
  cmds=(chat model setup config tools skills sessions cron webhook kanban memory skin auth profile mcp logs doctor status completion update uninstall version help)
  _arguments '1: :->cmds' '*::arg:->args'
  case $state in
    cmds) _describe 'command' cmds ;;
  esac
}
compdef _void void""")
        return 0
    if shell == "fish":
        print("""# void fish completion — save to ~/.config/fish/completions/void.fish
complete -c void -f
for c in chat model setup config tools skills sessions cron webhook kanban memory skin auth profile mcp logs doctor status completion update uninstall version help
  complete -c void -n '__fish_use_subcommand' -a $c
end""")
        return 0
    print(f"[void] unknown shell: {shell} (use bash|zsh|fish)")
    return 2


def cmd_update(config: Config, args: list[str], opts: dict) -> int:
    package = config.get("update.pypi_package") or "void-cli"
    import shutil
    import subprocess
    pip = shutil.which("pip") or f"{sys.executable} -m pip"
    print(f"current version: {__version__}")
    try:
        subprocess.run([sys.executable, "-m", "pip", "install", "--upgrade", package], check=False)
    except Exception as exc:
        print(f"[void] update failed: {exc}")
        return 1
    print("update finished (restart void to use the new version)")
    return 0


def cmd_uninstall(config: Config, args: list[str], opts: dict) -> int:
    import shutil
    import subprocess
    from .config import atomic_write_text
    answer = "n"
    try:
        answer = input(f"Delete {config.home} (config, sessions, skills, memory)? [y/N] ").strip().lower()
    except (EOFError, KeyboardInterrupt):
        answer = "n"
    if answer in ("y", "yes"):
        shutil.rmtree(config.home, ignore_errors=True)
        print(f"removed {config.home}")
    else:
        print(f"kept {config.home}")
    print("to remove the package itself, run: pip uninstall void-cli")
    del atomic_write_text, subprocess
    return 0

