"""Skin system: YAML files mapping UI element names to rich style strings.

Built-ins: default, dark, light, mono. User skins live in ~/.void/skins/<name>.yaml.
`void skin set <key> <value>` edits the ACTIVE skin file (never forks the default:
the full merged skin is written so unconfigured keys are never lost).
"""
from __future__ import annotations

from pathlib import Path

import yaml

from .config import Config, atomic_write_yaml

BUILTIN_SKINS: dict[str, dict[str, str]] = {
    "default": {
        "prompt": "bold cyan",
        "status_bar": "dim",
        "spinner": "bold magenta",
        "code_fence": "yellow",
        "error": "bold red",
        "warning": "bold yellow",
        "success": "bold green",
        "tool_call": "cyan",
        "tool_result": "dim cyan",
        "thinking": "dim italic",
        "assistant_text": "",
        "user_text": "bold white",
        "banner": "bold magenta",
        "muted": "dim",
    },
    "dark": {
        "prompt": "bold bright_cyan",
        "status_bar": "grey50",
        "spinner": "bold bright_magenta",
        "code_fence": "bright_yellow",
        "error": "bold bright_red",
        "warning": "bold bright_yellow",
        "success": "bold bright_green",
        "tool_call": "bright_cyan",
        "tool_result": "grey62",
        "thinking": "grey58 italic",
        "assistant_text": "grey85",
        "user_text": "bold white",
        "banner": "bold bright_magenta",
        "muted": "grey50",
    },
    "light": {
        "prompt": "bold blue",
        "status_bar": "grey35",
        "spinner": "bold magenta",
        "code_fence": "dark_red",
        "error": "bold red",
        "warning": "bold dark_orange",
        "success": "bold green",
        "tool_call": "blue",
        "tool_result": "grey39",
        "thinking": "grey50 italic",
        "assistant_text": "black",
        "user_text": "bold black",
        "banner": "bold magenta",
        "muted": "grey50",
    },
    "mono": {
        "prompt": "",
        "status_bar": "dim",
        "spinner": "",
        "code_fence": "",
        "error": "bold",
        "warning": "bold",
        "success": "bold",
        "tool_call": "",
        "tool_result": "dim",
        "thinking": "dim italic",
        "assistant_text": "",
        "user_text": "bold",
        "banner": "bold",
        "muted": "dim",
    },
}

SKIN_KEYS = list(BUILTIN_SKINS["default"].keys())


class Skin:
    def __init__(self, config: Config):
        self.config = config
        self.name = "default"
        self.styles: dict[str, str] = dict(BUILTIN_SKINS["default"])
        self.reload()

    def reload(self) -> None:
        """Merge: builtin base -> builtin of active name -> user override file. Live-safe."""
        name = str(self.config.get("display.skin") or "default")
        self.name = name if name in BUILTIN_SKINS or (self.config.skins_dir / f"{name}.yaml").exists() else "default"
        merged = dict(BUILTIN_SKINS.get(self.name, BUILTIN_SKINS["default"]))
        user_file = self.config.skins_dir / f"{self.name}.yaml"
        if user_file.exists():
            try:
                data = yaml.safe_load(user_file.read_text(encoding="utf-8")) or {}
                if isinstance(data, dict):
                    for k, v in data.items():
                        if k in SKIN_KEYS and v is not None:
                            merged[k] = str(v)
            except yaml.YAMLError:
                pass
        self.styles = merged

    def get(self, key: str) -> str:
        return self.styles.get(key, "")

    def active_file(self) -> Path:
        return self.config.skins_dir / f"{self.name}.yaml"

    def set_key(self, key: str, value: str) -> None:
        """Edit the ACTIVE skin: write the full merged skin (never a partial fork)."""
        if key not in SKIN_KEYS:
            raise KeyError(f"unknown skin key '{key}'. Valid: {', '.join(SKIN_KEYS)}")
        self.styles[key] = value
        atomic_write_yaml(self.active_file(), self.styles)
        self.reload()

    def use(self, name: str) -> bool:
        if name not in BUILTIN_SKINS and not (self.config.skins_dir / f"{name}.yaml").exists():
            return False
        self.config.set("display.skin", name)
        self.reload()
        return True

    def list_skins(self) -> list[str]:
        names = set(BUILTIN_SKINS.keys())
        if self.config.skins_dir.exists():
            names |= {p.stem for p in self.config.skins_dir.glob("*.yaml")}
        return sorted(names)
