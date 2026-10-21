"""Agent + user memory: bounded plain-text stores injected into the system prompt.

Backend interface (pluggable via config memory.backend): read() -> str, write(str) -> None,
clear() -> None. Default 'file' backend writes ~/.void/memory/{agent,user}_memory.md.
"""
from __future__ import annotations

from pathlib import Path

from .logging_setup import get_logger

log = get_logger("memory")

CHARS_PER_TOKEN = 4  # rough estimate; good enough for caps


class FileBackend:
    def __init__(self, path: Path):
        self.path = Path(path)

    def read(self) -> str:
        if not self.path.exists():
            return ""
        try:
            return self.path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            return ""

    def write(self, text: str) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(self.path.suffix + ".tmp")
        try:
            tmp.write_text(text, encoding="utf-8")
            tmp.replace(self.path)  # atomic on same filesystem
        except OSError as exc:
            log.error("memory write failed: %s", exc)
        finally:
            if tmp.exists():
                tmp.unlink(missing_ok=True)

    def clear(self) -> None:
        try:
            self.path.unlink(missing_ok=True)
        except OSError:
            pass


class MemoryStores:
    """Two bounded stores: 'agent' (~800 tok) and 'user' (~500 tok)."""

    def __init__(self, config, enabled: bool | None = None):
        self.config = config
        self.enabled = config.get("memory.enabled", True) if enabled is None else enabled
        self.backends = {
            "agent": FileBackend(config.memory_agent_path),
            "user": FileBackend(config.memory_user_path),
        }
        self.limits_chars = {
            "agent": int(config.get("memory.agent_limit_tokens", 800)) * CHARS_PER_TOKEN,
            "user": int(config.get("memory.user_limit_tokens", 500)) * CHARS_PER_TOKEN,
        }
        self._dirty = False

    # ---- backend passthrough -----------------------------------------------------
    def read(self, store: str) -> str:
        if store not in self.backends or not self.enabled:
            return ""
        return self.backends[store].read().strip()

    def token_count(self, store: str) -> int:
        return len(self.read(store)) // CHARS_PER_TOKEN

    def clear(self, store: str) -> None:
        if store in self.backends:
            self.backends[store].clear()
            self._dirty = True

    # ---- editing -------------------------------------------------------------------
    def add(self, store: str, text: str) -> dict:
        if store not in self.backends:
            return {"ok": False, "error": f"unknown store {store}"}
        current = self.backends[store].read().rstrip()
        entry = text.strip()
        if entry.lower() in current.lower():
            return {"ok": True, "message": "already present, skipped", "tokens": self.token_count(store)}
        bullet = f"- {entry}"
        new_content = (current + "\n" + bullet).lstrip("\n")
        limit = self.limits_chars[store]
        if len(new_content) > limit:
            # drop oldest lines until it fits
            lines = new_content.splitlines()
            while len(lines) > 1 and len("\n".join(lines)) > limit:
                lines.pop(0)
            if len("\n".join(lines)) > limit:
                return {"ok": False, "error": f"memory full (limit ~{limit // CHARS_PER_TOKEN} tokens); "
                                              f"remove something first"}
            new_content = "\n".join(lines)
        self.backends[store].write(new_content)
        self._dirty = True
        return {"ok": True, "store": store, "tokens": self.token_count(store)}

    def replace(self, store: str, old: str, new: str) -> dict:
        if store not in self.backends:
            return {"ok": False, "error": f"unknown store {store}"}
        content = self.backends[store].read()
        if old not in content:
            return {"ok": False, "error": "text to replace not found"}
        content = content.replace(old, new, 1)
        if len(content) > self.limits_chars[store]:
            return {"ok": False, "error": "replacement exceeds token cap"}
        self.backends[store].write(content)
        self._dirty = True
        return {"ok": True, "store": store}

    def remove(self, store: str, needle: str) -> dict:
        if store not in self.backends:
            return {"ok": False, "error": f"unknown store {store}"}
        content = self.backends[store].read()
        if not needle:
            return {"ok": False, "error": "text (substring) required"}
        lines = [ln for ln in content.splitlines() if needle.lower() not in ln.lower()]
        removed = len(content.splitlines()) - len(lines)
        if removed == 0:
            return {"ok": False, "error": "no line matched"}
        self.backends[store].write("\n".join(lines))
        self._dirty = True
        return {"ok": True, "removed_lines": removed, "store": store}

    # ---- prompt injection ----------------------------------------------------------
    def prompt_block(self) -> str:
        if not self.enabled:
            return ""
        agent = self.read("agent")
        user = self.read("user")
        parts: list[str] = []
        if user:
            parts.append("## About the user\n" + user)
        if agent:
            parts.append("## Your memory (notes from previous sessions)\n" + agent)
        return "\n\n".join(parts)

    def flush_if_needed(self, turns: int) -> None:
        """Stores are written through on every edit; this hook exists for flush-on-exit semantics."""
        del turns, self._dirty
