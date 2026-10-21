"""Setup wizard: section-re-runnable interactive configuration (`void setup [section]`)."""
from __future__ import annotations

from .config import Config

SECTIONS = ["model", "terminal", "tools", "security", "display", "memory", "compression"]


def _ask(prompt: str, default: str = "") -> str:
    suffix = f" [{default}]" if default != "" else ""
    try:
        answer = input(f"{prompt}{suffix}: ").strip()
    except (EOFError, KeyboardInterrupt):
        return default
    return answer or default


def _yesno(prompt: str, default: bool = True) -> bool:
    d = "Y/n" if default else "y/N"
    try:
        answer = input(f"{prompt} [{d}]: ").strip().lower()
    except (EOFError, KeyboardInterrupt):
        return default
    if not answer:
        return default
    return answer in ("y", "yes")


def _choose(prompt: str, options: list[str], default_idx: int = 0) -> int:
    print(prompt)
    for i, opt in enumerate(options, 1):
        mark = "*" if (i - 1) == default_idx else " "
        print(f"  {mark} {i}. {opt}")
    try:
        raw = input("choice> ").strip()
    except (EOFError, KeyboardInterrupt):
        return default_idx
    if raw.isdigit() and 1 <= int(raw) <= len(options):
        return int(raw) - 1
    return default_idx


# ---- sections ------------------------------------------------------------------------
def section_model(config: Config) -> bool:
    print("\n== Model ==")
    from .model_tools import pick_model_interactive
    return pick_model_interactive(config)


def section_terminal(config: Config) -> None:
    print("\n== Terminal backend ==")
    backends = ["local", "docker", "ssh"]
    current = str(config.get("terminal.backend", "local") or "local")
    idx = _choose("Where should the terminal tool run commands?",
                  ["local (this machine)", "docker (exec into a container)", "ssh (remote host)"],
                  backends.index(current) if current in backends else 0)
    backend = backends[idx]
    config.set("terminal.backend", backend)
    if backend == "docker":
        config.set("terminal.docker_container",
                   _ask("Container name or id", str(config.get("terminal.docker_container") or "")))
    elif backend == "ssh":
        config.set("terminal.ssh_host", _ask("user@host", str(config.get("terminal.ssh_host") or "")))
    config.set("terminal.project_dir",
               _ask("Project directory (sandbox root)", str(config.get("terminal.project_dir") or "")))
    print(f"terminal backend = {backend}")


def section_tools(config: Config) -> None:
    print("\n== Tools ==")
    from .tools.registry import registry
    names = sorted(registry.toolsets_summary())
    print("Available toolsets: " + ", ".join(names))
    enabled = _ask("Enabled toolsets (comma-separated, 'all' for everything)",
                   ",".join(config.get("toolsets.enabled") or ["all"]))
    config.set("toolsets.enabled", [t.strip() for t in enabled.split(",") if t.strip()])
    disabled = _ask("Disabled toolsets (comma-separated)",
                    ",".join(config.get("toolsets.disabled") or []))
    config.set("toolsets.disabled", [t.strip() for t in disabled.split(",") if t.strip()])
    print(f"enabled = {config.get('toolsets.enabled')}")


def section_security(config: Config) -> None:
    print("\n== Security ==")
    enabled = _yesno("Prompt before running dangerous commands?",
                     bool(config.get("security.approvals.enabled", True)))
    config.set("security.approvals.enabled", enabled)
    print("  YOLO mode (security.approvals.yolo, also /yolo) skips ALL approval prompts.")
    print("  Use it only in throwaway sandboxes — it lets the agent run `rm -rf`, SQL drops, etc.")
    yolo = _yesno("Enable YOLO mode persistently? (not recommended)",
                  bool(config.get("security.approvals.yolo", False)))
    config.set("security.approvals.yolo", yolo)
    redact = _yesno("Redact secrets in UI output and logs?",
                    bool(config.get("security.redaction.enabled", True)))
    config.set("security.redaction.enabled", redact)

def section_display(config: Config) -> None:
    print("\n== Display ==")
    config.set("display.compact", _yesno("Compact output?", bool(config.get("display.compact", False))))
    config.set("display.streaming", _yesno("Stream tokens live?", bool(config.get("display.streaming", True))))
    config.set("display.show_reasoning", _yesno("Show model reasoning blocks?",
                                                bool(config.get("display.show_reasoning", False))))
    config.set("display.show_timestamps", _yesno("Show timestamps?",
                                                 bool(config.get("display.show_timestamps", False))))
    from .skin import Skin
    skins = Skin(config).list_skins()
    current = str(config.get("display.skin") or "default")
    idx = _choose("Skin:", skins, skins.index(current) if current in skins else 0)
    config.set("display.skin", skins[idx])


def section_memory(config: Config) -> None:
    print("\n== Memory ==")
    config.set("memory.enabled", _yesno("Enable agent + user memory?",
                                        bool(config.get("memory.enabled", True))))
    config.set("memory.agent_limit_tokens", int(_ask("Agent memory token cap",
                                                     str(config.get("memory.agent_limit_tokens", 800)))))
    config.set("memory.user_limit_tokens", int(_ask("User memory token cap",
                                                    str(config.get("memory.user_limit_tokens", 500)))))


def section_compression(config: Config) -> None:
    print("\n== Compression ==")
    config.set("compression.enabled", _yesno("Compress context when it gets long?",
                                             bool(config.get("compression.enabled", True))))
    config.set("compression.threshold", float(_ask("Threshold (fraction of context window)",
                                                   str(config.get("compression.threshold", 0.85)))))
    config.set("compression.context_window", int(_ask("Context window (tokens)",
                                                      str(config.get("compression.context_window", 128000)))))
    print("The summary model is configured under model.auxiliary.compression in config.yaml.")


RUNNERS = {
    "model": section_model,
    "terminal": section_terminal,
    "tools": section_tools,
    "security": section_security,
    "display": section_display,
    "memory": section_memory,
    "compression": section_compression,
}


def run_wizard(config: Config, section: str | None = None) -> int:
    if section:
        if section not in RUNNERS:
            print(f"unknown setup section '{section}'. Available: {', '.join(SECTIONS)}")
            return 1
        RUNNERS[section](config)
        print("\nsaved to", config.config_path)
        return 0
    print("void setup — press Enter to accept defaults; each section can be re-run "
          "individually (e.g. `void setup model`).")
    for name in SECTIONS:
        RUNNERS[name](config)
    print("\nsetup complete. config saved to", config.config_path)
    print("tip: secrets live in", config.env_path, "(never in config.yaml)")
    return 0

