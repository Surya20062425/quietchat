"""Configuration: config.yaml (settings, never secrets), .env (secrets), profiles.

All writes are atomic (temp file + os.replace). The active profile is resolved from
--profile flag > VOID_PROFILE env > ~/.void/active_profile file > "default".
"""
from __future__ import annotations

import copy
import os
import tempfile
from pathlib import Path

import yaml

CONFIG_VERSION = 1

DEFAULT_CONFIG: dict = {
    "version": CONFIG_VERSION,
    "model": {
        "provider": "",            # openrouter | anthropic | openai | google-gemini | custom
        "default": "",             # model id
        "context_window": 0,       # 0 => use compression.context_window
        "custom": {"base_url": "", "api_key_env": "CUSTOM_API_KEY"},
        "auxiliary": {},           # task -> {provider, base_url?, model}
    },
    "display": {
        "compact": False,
        "streaming": True,
        "show_reasoning": False,
        "show_timestamps": False,
        "skin": "default",
        "max_tool_lines": 40,
    },
    "toolsets": {"enabled": ["all"], "disabled": []},
    "security": {
        "approvals": {"enabled": True, "yolo": False},
        "redaction": {"enabled": True, "patterns": []},
        "dangerous_patterns": [],
    },
    "sessions": {
        "auto_save": True,
        "max_turns": 2000,
        "shared_db": False,
        "checkpoint": {"enabled": False, "interval": 5},
    },
    "compression": {
        "enabled": True,
        "threshold": 0.85,
        "context_window": 128000,
        "protect_first": 3,
        "protect_last": 4,
        "summary_model": {},
    },
    "cron": {"enabled": True, "timezone": "local", "max_output_chars": 4000},
    "webhook": {"host": "127.0.0.1", "port": 8791, "autostart": False},
    "kanban": {"dispatcher": {"enabled": False, "max_concurrent": 2, "toolsets": ["terminal", "file", "web"]}},
    "memory": {"enabled": True, "agent_limit_tokens": 800, "user_limit_tokens": 500, "backend": "file"},
    "terminal": {"backend": "local", "project_dir": "", "timeout": 120, "docker_container": "", "ssh_host": ""},
    "skills": {"sources": []},
    "mcp": {"servers": {}},
    "budget": {"max_tokens": 0},  # 0 = unlimited
    "tools": {"max_result_chars": 20000, "max_iterations": 90, "code_exec_timeout": 300, "code_exec_max_rpc": 50},
    "agent": {"temperature": None, "max_tokens": None, "request_timeout": 180},
    "update": {"pypi_package": ""},
}

PROVIDER_KEY_NAMES = {
    "openai": "OPENAI_API_KEY",
    "anthropic": "ANTHROPIC_API_KEY",
    "google-gemini": "GEMINI_API_KEY",
    "openrouter": "OPENROUTER_API_KEY",
    "custom": "CUSTOM_API_KEY",
}
PROVIDERS = ["openrouter", "anthropic", "openai", "google-gemini", "custom"]


# --------------------------------------------------------------------------- io helpers
def atomic_write_text(path: Path, text: str) -> None:
    """Write text atomically: temp file in the same dir, then os.replace."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix=path.name + ".", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="") as f:
            f.write(text)
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def atomic_write_yaml(path: Path, data: dict) -> None:
    atomic_write_text(path, yaml.safe_dump(data, sort_keys=False, allow_unicode=True, default_flow_style=False))


def deep_merge(base: dict, override: dict) -> dict:
    """Return a new dict: base values kept unless override provides them (recursive)."""
    out = copy.deepcopy(base)
    for key, value in (override or {}).items():
        if isinstance(value, dict) and isinstance(out.get(key), dict):
            out[key] = deep_merge(out[key], value)
        else:
            out[key] = copy.deepcopy(value)
    return out


def resolve_profile(explicit: str | None = None) -> str:
    if explicit:
        return explicit
    env = os.environ.get("VOID_PROFILE", "").strip()
    if env:
        return env
    active = Path(os.environ.get("VOID_HOME", str(Path.home() / ".void"))) / "active_profile"
    if active.exists():
        name = active.read_text(encoding="utf-8").strip()
        if name:
            return name
    return "default"


# --------------------------------------------------------------------------- .env parsing
def parse_env_text(text: str) -> dict[str, str]:
    out: dict[str, str] = {}
    for raw in (text or "").splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if line.lower().startswith("export "):
            line = line[7:].strip()
        if "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        if key:
            out[key] = value
    return out


def load_env_file(path: Path, override: bool = True) -> dict[str, str]:
    """Parse a .env file into os.environ (and return it). Secrets only; never read by config commands."""
    if not path.exists():
        return {}
    parsed = parse_env_text(path.read_text(encoding="utf-8", errors="replace"))
    for key, value in parsed.items():
        if override or key not in os.environ or os.environ.get(key, "") == "":
            os.environ[key] = value
    return parsed


# --------------------------------------------------------------------------- Config
class Config:
    """Loads/validates/writes config.yaml for the active profile and manages .env secrets."""

    def __init__(self, profile: str | None = None, home: str | None = None):
        self.home = Path(home or os.environ.get("VOID_HOME", str(Path.home() / ".void")))
        self.profile = resolve_profile(profile)
        if self.profile == "default":
            self.profile_dir: Path | None = None
            self.config_path = self.home / "config.yaml"
            self.env_path = self.home / ".env"
        else:
            self.profile_dir = self.home / "profiles" / self.profile
            self.config_path = self.profile_dir / "config.yaml"
            self.env_path = self.profile_dir / ".env"
        self.data: dict = copy.deepcopy(DEFAULT_CONFIG)
        self.ensure_dirs()
        self.load()
        self.load_env()

    # ---- paths ----------------------------------------------------------------
    def ensure_dirs(self) -> None:
        for d in [self.home, self.logs_dir, self.cache_dir, self.skins_dir, self.user_skills_dir,
                  self.checkpoints_dir, self.profiles_dir]:
            d.mkdir(parents=True, exist_ok=True)

    @property
    def profiles_dir(self) -> Path:
        return self.home / "profiles"

    @property
    def logs_dir(self) -> Path:
        return self.home / "logs"

    @property
    def cache_dir(self) -> Path:
        return self.home / "cache"

    @property
    def skins_dir(self) -> Path:
        return self.home / "skins"

    @property
    def user_skills_dir(self) -> Path:
        return self.home / "skills"

    @property
    def checkpoints_dir(self) -> Path:
        return self.home / "checkpoints"

    @property
    def state_db_path(self) -> Path:
        if self.profile_dir is not None and not self.get("sessions.shared_db", False):
            return self.profile_dir / "state.db"
        return self.home / "state.db"

    @property
    def history_path(self) -> Path:
        base = self.profile_dir or self.home
        return base / "history"

    @property
    def memory_agent_path(self) -> Path:
        base = self.profile_dir or self.home
        return base / "memory" / "agent_memory.md"

    @property
    def memory_user_path(self) -> Path:
        base = self.profile_dir or self.home
        return base / "memory" / "user_memory.md"

    @property
    def goal_path(self) -> Path:
        base = self.profile_dir or self.home
        return base / "goal.json"

    @property
    def project_dir(self) -> Path:
        return Path(self.get("terminal.project_dir") or os.getcwd())

    # ---- config load/save -------------------------------------------------------
    def load(self) -> dict:
        user: dict = {}
        if self.config_path.exists():
            try:
                user = yaml.safe_load(self.config_path.read_text(encoding="utf-8")) or {}
                if not isinstance(user, dict):
                    user = {}
            except yaml.YAMLError as exc:
                raise SystemExit(f"[void] config.yaml is invalid YAML: {exc}\n{self.config_path}")
        self.data = deep_merge(DEFAULT_CONFIG, user)
        return self.data

    def save(self) -> None:
        atomic_write_yaml(self.config_path, self.data)

    def get(self, key_path: str, default=None):
        node = self.data
        for part in key_path.split("."):
            if isinstance(node, dict) and part in node:
                node = node[part]
            else:
                return default
        return node

    def set(self, key_path: str, value) -> None:
        parts = key_path.split(".")
        node = self.data
        for part in parts[:-1]:
            nxt = node.get(part)
            if not isinstance(nxt, dict):
                nxt = {}
                node[part] = nxt
            node = nxt
        node[parts[-1]] = value
        self.save()

    def unset(self, key_path: str) -> bool:
        parts = key_path.split(".")
        node = self.data
        for part in parts[:-1]:
            if not isinstance(node, dict) or part not in node:
                return False
            node = node[part]
        if isinstance(node, dict) and parts[-1] in node:
            del node[parts[-1]]
            self.save()
            return True
        return False

    def check(self) -> list[str]:
        """Return a list of problems (empty list = healthy)."""
        problems: list[str] = []
        provider = self.get("model.provider") or ""
        if provider and provider not in PROVIDERS:
            problems.append(f"model.provider '{provider}' is not one of {', '.join(PROVIDERS)}")
        if provider and not self.get("model.default"):
            problems.append("model.default is empty (run `void model`)")
        if provider == "custom" and not self.get("model.custom.base_url"):
            problems.append("model.custom.base_url is empty (required for the custom provider)")
        if self.get("security.approvals.yolo") and self.get("security.approvals.enabled"):
            problems.append("security.approvals.yolo=true: approval prompts are OFF (dangerous)")
        try:
            threshold = float(self.get("compression.threshold", 0.85))
            if not 0 < threshold <= 1:
                problems.append("compression.threshold must be within (0, 1]")
        except (TypeError, ValueError):
            problems.append("compression.threshold is not a number")
        return problems

    def migrate(self) -> list[str]:
        """Fill in any keys missing from older configs (idempotent)."""
        before = yaml.safe_dump(self.data, sort_keys=True)
        self.data = deep_merge(DEFAULT_CONFIG, self.data)
        self.data["version"] = CONFIG_VERSION
        self.save()
        after = yaml.safe_dump(self.data, sort_keys=True)
        return [] if before == after else [f"config updated to v{CONFIG_VERSION} (missing keys filled with defaults)"]

    # ---- env / credentials ------------------------------------------------------
    def load_env(self) -> dict[str, str]:
        """Global ~/.void/.env first, then profile .env overrides."""
        loaded = load_env_file(self.home / ".env", override=False)
        loaded.update(load_env_file(self.env_path, override=True))
        return loaded

    def api_keys(self, provider: str) -> list[str]:
        """Credential pool: <BASE>, <BASE>_2..9, <BASE>S (comma-separated). 'none'/empty skipped."""
        base = PROVIDER_KEY_NAMES.get(provider)
        if not base:
            return []
        candidates: list[str] = []
        csv_val = os.environ.get(base + "S", "")
        if csv_val:
            candidates.extend(k.strip() for k in csv_val.split(",") if k.strip())
        primary = os.environ.get(base, "")
        if primary and primary.lower() != "none":
            candidates.insert(0, primary)
        for i in range(2, 10):
            v = os.environ.get(f"{base}_{i}", "")
            if v and v.lower() != "none":
                candidates.append(v)
        seen: set[str] = set()
        return [k for k in candidates if not (k in seen or seen.add(k))]

    def set_env(self, key: str, value: str) -> None:
        """Insert/replace a single KEY=value line in the profile .env, atomically."""
        key = key.strip()
        lines: list[str] = []
        if self.env_path.exists():
            lines = self.env_path.read_text(encoding="utf-8", errors="replace").splitlines()
        escaped = value.replace('"', '\\"')
        replaced = False
        for i, line in enumerate(lines):
            if line.strip().startswith("#"):
                continue
            if line.split("=", 1)[0].strip() == key:
                lines[i] = f'{key}="{escaped}"'
                replaced = True
                break
        if not replaced:
            lines.append(f'{key}="{escaped}"')
        atomic_write_text(self.env_path, "\n".join(lines) + "\n")
        os.environ[key] = value

    def remove_env(self, key: str) -> bool:
        if not self.env_path.exists():
            return False
        lines = self.env_path.read_text(encoding="utf-8", errors="replace").splitlines()
        kept = [ln for ln in lines if ln.split("=", 1)[0].strip() != key]
        if len(kept) == len(lines):
            return False
        atomic_write_text(self.env_path, "\n".join(kept) + ("\n" if kept else ""))
        os.environ.pop(key, None)
        return True

    def env_keys_present(self) -> list[str]:
        if not self.env_path.exists():
            return []
        return list(parse_env_text(self.env_path.read_text(encoding="utf-8", errors="replace")).keys())


