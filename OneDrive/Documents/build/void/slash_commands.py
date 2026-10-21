"""Slash command registry — the single source of truth for REPL commands.

Every surface derives from here: REPL autocomplete, /help text, and (if a gateway is ever
added) a platform manifest. Each CommandDef names a handler method on the CLI object
(`cli.slash_<name>`); dispatch() resolves aliases and calls it.
"""
from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class CommandDef:
    name: str
    description: str
    category: str = "general"
    aliases: list[str] = field(default_factory=list)
    args_hint: str = ""
    platforms: list[str] = field(default_factory=lambda: ["cli"])
    handler: str = ""   # CLI method name; defaults to slash_<name>

    def resolved_handler(self) -> str:
        return self.handler or ("slash_" + self.name.replace("-", "_").replace("/", "_"))


COMMANDS: list[CommandDef] = [
    CommandDef("help", "Show this command list", "session", aliases=["h", "?"]),
    CommandDef("exit", "Exit the REPL", "session", aliases=["quit", "q"]),
    CommandDef("clear", "Clear the conversation in this session", "session"),
    CommandDef("model", "Re-run the model picker mid-session", "model"),
    CommandDef("tools", "List/enable/disable toolsets", "tools", args_hint="[list|enable|disable NAME]"),
    CommandDef("toolsets", "Alias for /tools", "tools", handler="slash_tools"),
    CommandDef("skills", "Manage skills", "skills",
               args_hint="list|browse|search Q|inspect NAME|load NAME|unload NAME|install SRC|uninstall NAME"),
    CommandDef("skill", "Load a skill by name into this session", "skills", handler="slash_skill",
               args_hint="NAME"),
    CommandDef("memory", "View/add/clear agent + user memory", "memory", args_hint="[show|add STORE TEXT|clear STORE]"),
    CommandDef("resume", "List sessions and resume one", "sessions", args_hint="[SESSION_ID_OR_TITLE]"),
    CommandDef("sessions", "Alias for /resume", "sessions", handler="slash_resume"),
    CommandDef("rollback", "Revert files to a prior checkpoint", "checkpoints", args_hint="[CHECKPOINT]"),
    CommandDef("checkpoint", "Enable/disable/list filesystem checkpoints", "checkpoints",
               args_hint="enable|disable|list"),
    CommandDef("yolo", "Toggle dangerous-command approval bypass", "security"),
    CommandDef("spam", "Nickname for /yolo", "security", handler="slash_yolo"),
    CommandDef("config", "Read/write config from inside the session", "config",
               args_hint="get KEY|set KEY VALUE|show"),
    CommandDef("background", "Run a task in a background agent", "agent", args_hint="PROMPT"),
    CommandDef("bg", "Alias for /background", "agent", handler="slash_background"),
    CommandDef("goal", "Long-term objective tracking", "agent", args_hint="set TEXT|clear|status"),
    CommandDef("kanban", "Multi-agent work-queue board", "kanban",
               args_hint="list|add TITLE|move ID COLUMN|clear-done|stats|run"),
    CommandDef("cron", "Scheduled tasks", "cron",
               args_hint="list|create SCHED PROMPT|edit|pause|resume|run|remove"),
    CommandDef("webhook", "Event-driven triggers", "webhook",
               args_hint="list|subscribe NAME|remove NAME|test NAME"),
    CommandDef("xm", "Extended model info: context window + token usage", "model"),
    CommandDef("status", "One-line session status", "session"),
    CommandDef("doctor", "Check SDKs, keys, and model connectivity", "session"),
    CommandDef("save", "Force-save the current session", "session"),
    CommandDef("export", "Export this session to a file", "sessions", args_hint="[markdown|jsonl]"),
]


def all_commands() -> list[CommandDef]:
    return list(COMMANDS)


def find(name: str) -> CommandDef | None:
    key = name.lstrip("/").strip().lower()
    for cmd in COMMANDS:
        if cmd.name == key or key in [a.lower() for a in cmd.aliases]:
            return cmd
    return None


def autocomplete_candidates() -> list[tuple[str, str]]:
    """(insert_text, description) pairs for prompt_toolkit completion."""
    out: list[tuple[str, str]] = []
    for cmd in COMMANDS:
        out.append(("/" + cmd.name, cmd.description))
    return out


def help_text() -> str:
    """Grouped help derived from the registry."""
    groups: dict[str, list[CommandDef]] = {}
    for cmd in COMMANDS:
        groups.setdefault(cmd.category, []).append(cmd)
    lines = ["Available commands:"]
    for category in sorted(groups):
        lines.append(f"\n  [{category}]")
        for cmd in sorted(groups[category], key=lambda c: c.name):
            alias = f" (aliases: {', '.join('/' + a for a in cmd.aliases)})" if cmd.aliases else ""
            hint = f" {cmd.args_hint}" if cmd.args_hint else ""
            lines.append(f"    /{cmd.name}{hint}{alias}\n        {cmd.description}")
    return "\n".join(lines)


def dispatch(cli, line: str) -> bool:
    """Execute a slash line against the CLI object. Returns True if handled."""
    text = line.strip()
    if not text.startswith("/"):
        return False
    body = text[1:]
    name, _, args = body.partition(" ")
    name = name.strip()
    cmd = find(name)
    if cmd is None:
        if _dispatch_bundle(cli, name, args.strip()):
            return True
        cli.error(f"unknown command: /{name}  (try /help)")
        return True
    handler = getattr(cli, cmd.resolved_handler(), None)
    if handler is None:
        cli.error(f"command /{cmd.name} is registered but not implemented on this surface")
        return True
    handler(args.strip())
    return True


def _dispatch_bundle(cli, name: str, args: str) -> bool:
    """Skill bundles are exposed as slash commands: /<alias> loads each listed skill."""
    try:
        from .skills import SkillLibrary
        bundles = SkillLibrary(cli.config).bundles()
    except Exception:
        return False
    ids = bundles.get(name)
    if not ids:
        return False
    missing: list[str] = []
    for skill_id in ids:
        content = None
        try:
            from .skills import SkillLibrary
            content = SkillLibrary(cli.config).content(skill_id)
        except Exception:
            content = None
        if content:
            cli.state.loaded_skills[skill_id] = content
        else:
            missing.append(skill_id)
    cli._refresh_system_prompt()
    loaded = [s for s in ids if s not in missing]
    cli.ok(f"bundle /{name}: loaded {', '.join(loaded) or 'nothing'}"
           + (f" (missing: {', '.join(missing)})" if missing else ""))
    del args
    return True
