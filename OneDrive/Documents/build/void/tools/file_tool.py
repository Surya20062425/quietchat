"""file tools — read_file / write_file / edit_file / list_files / search_files.

Paths are sandboxed to the project directory (config terminal.project_dir or cwd)
unless the path is inside the sandbox root. Sandboxing can be disabled with
config terminal.sandbox: false.
"""
from __future__ import annotations

import os
import re
from pathlib import Path

from .registry import get_context, registry

SKIP_DIRS = {".git", "node_modules", "__pycache__", ".venv", "venv", ".void", ".void-worktrees",
             ".idea", ".vscode", "dist", "build", ".mypy_cache", ".pytest_cache", ".ruff_cache"}
MAX_FILE_CHARS = 120_000


def _sandbox_root() -> Path:
    ctx = get_context()
    if ctx.config is not None:
        if ctx.config.get("terminal.sandbox") is False:
            return Path(os.getcwd())
        return ctx.config.project_dir
    return Path(os.getcwd())


def _resolve(path_str: str) -> Path:
    root = _sandbox_root().resolve()
    p = Path(path_str)
    if not p.is_absolute():
        p = root / p
    p = p.resolve()
    if ctx_config_allows() is False and not (p == root or root in p.parents):
        raise PermissionError(
            f"path '{path_str}' is outside the sandbox root '{root}'. "
            f"Set config terminal.sandbox: false to allow arbitrary paths.")
    return p


def ctx_config_allows() -> bool | None:
    ctx = get_context()
    if ctx.config is not None and ctx.config.get("terminal.sandbox") is False:
        return False
    return True  # sandboxing on


def read_file_handler(args: dict) -> dict:
    path = _resolve(str(args.get("path") or ""))
    if not path.exists() or not path.is_file():
        return {"ok": False, "error": f"file not found: {path}"}
    max_chars = int(args.get("max_chars") or MAX_FILE_CHARS)
    offset = int(args.get("offset") or 0)
    try:
        data = path.read_bytes()
        if b"\x00" in data[:1024]:
            return {"ok": False, "error": "binary file (refusing to display); use terminal tool instead"}
        text = data.decode("utf-8", errors="replace")
        lines = text.splitlines()
        total = len(lines)
        lines = lines[offset:offset + int(args.get("limit") or 100000)]
        shown = "\n".join(lines)
        if len(shown) > max_chars:
            shown = shown[:max_chars] + f"\n...[truncated, total {total} lines]"
        return {"ok": True, "path": str(path), "total_lines": total, "content": shown}
    except OSError as exc:
        return {"ok": False, "error": str(exc)}


def write_file_handler(args: dict) -> dict:
    path = _resolve(str(args.get("path") or ""))
    content = args.get("content")
    if content is None:
        return {"ok": False, "error": "content is required"}
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        existed = path.exists()
        path.write_text(str(content), encoding="utf-8")
        return {"ok": True, "path": str(path), "bytes": path.stat().st_size,
                "action": "overwritten" if existed else "created"}
    except OSError as exc:
        return {"ok": False, "error": str(exc)}


def edit_file_handler(args: dict) -> dict:
    path = _resolve(str(args.get("path") or ""))
    old = str(args.get("old_text") or "")
    new = str(args.get("new_text") or "")
    if not path.exists():
        return {"ok": False, "error": f"file not found: {path}"}
    text = path.read_text(encoding="utf-8", errors="replace")
    if old not in text:
        return {"ok": False, "error": "old_text not found in file"}
    count = text.count(old)
    if count > 1 and not args.get("replace_all"):
        return {"ok": False, "error": f"old_text matches {count} times; pass replace_all=true or add context"}
    if args.get("replace_all"):
        text = text.replace(old, new)
    else:
        text = text.replace(old, new, 1)
    try:
        path.write_text(text, encoding="utf-8")
        return {"ok": True, "path": str(path), "replacements": count if args.get("replace_all") else 1}
    except OSError as exc:
        return {"ok": False, "error": str(exc)}

def list_files_handler(args: dict) -> dict:
    root = _resolve(str(args.get("path") or "."))
    if not root.exists():
        return {"ok": False, "error": f"path not found: {root}"}
    limit = int(args.get("limit") or 500)
    pattern = str(args.get("glob") or "**/*")
    results: list[str] = []
    for p in sorted(root.glob(pattern)):
        if p.is_dir():
            continue
        rel = p.relative_to(root) if root in p.parents or p.parent == root else p
        if any(part in SKIP_DIRS for part in p.parts):
            continue
        results.append(str(rel))
        if len(results) >= limit:
            break
    return {"ok": True, "path": str(root), "count": len(results), "files": results}


def search_files_handler(args: dict) -> dict:
    query = str(args.get("query") or "")
    if not query:
        return {"ok": False, "error": "query is required"}
    mode = str(args.get("mode") or "content")  # content | filename
    root = _resolve(str(args.get("path") or "."))
    glob_filter = str(args.get("glob") or "**/*")
    case_sensitive = bool(args.get("case_sensitive"))
    max_results = int(args.get("max_results") or 40)
    max_file_bytes = 2_000_000
    results: list[dict] = []
    flags = 0 if case_sensitive else re.IGNORECASE
    try:
        rx = re.compile(query, flags)
    except re.error as exc:
        return {"ok": False, "error": f"invalid regex: {exc}"}
    files = [p for p in root.glob(glob_filter) if p.is_file()
             and not any(part in SKIP_DIRS for part in p.parts)]
    for p in files:
        if mode == "filename":
            if rx.search(p.name):
                results.append({"path": str(p.relative_to(root)), "match": p.name})
        else:
            try:
                if p.stat().st_size > max_file_bytes:
                    continue
                blob = p.read_bytes()
                if b"\x00" in blob[:1024]:
                    continue
                text = blob.decode("utf-8", errors="replace")
            except OSError:
                continue
            for i, line in enumerate(text.splitlines(), 1):
                if rx.search(line):
                    results.append({"path": str(p.relative_to(root)), "line": i, "match": line.strip()[:300]})
        if len(results) >= max_results:
            results = results[:max_results]
            break
    return {"ok": True, "mode": mode, "count": len(results), "results": results}


registry.register(
    "read_file",
    {"description": "Read a text file. Returns numbered context with total line count.",
     "parameters": {"type": "object",
                    "properties": {"path": {"type": "string", "description": "File path (sandboxed to project dir)."},
                                   "offset": {"type": "integer", "description": "Start line (0-based)."},
                                   "limit": {"type": "integer", "description": "Max lines to return."},
                                   "max_chars": {"type": "integer", "description": "Max chars returned."}},
                    "required": ["path"]}},
    read_file_handler, toolset="file")

registry.register(
    "write_file",
    {"description": "Create or overwrite a file with the given content (dirs auto-created).",
     "parameters": {"type": "object",
                    "properties": {"path": {"type": "string"}, "content": {"type": "string"}},
                    "required": ["path", "content"]}},
    write_file_handler, toolset="file")

registry.register(
    "edit_file",
    {"description": "Replace an exact old_text snippet with new_text inside a file. "
                    "Fails unless the snippet matches exactly (once, or with replace_all).",
     "parameters": {"type": "object",
                    "properties": {"path": {"type": "string"}, "old_text": {"type": "string"},
                                   "new_text": {"type": "string"},
                                   "replace_all": {"type": "boolean"}},
                    "required": ["path", "old_text", "new_text"]}},
    edit_file_handler, toolset="file")

registry.register(
    "list_files",
    {"description": "List files under a directory (respects common skip dirs like .git, node_modules).",
     "parameters": {"type": "object",
                    "properties": {"path": {"type": "string", "description": "Directory (default .)."},
                                   "glob": {"type": "string", "description": "Glob pattern (default **/*)."},
                                   "limit": {"type": "integer"}}}},
    list_files_handler, toolset="file")

registry.register(
    "search_files",
    {"description": "Search file contents (mode=content, regex) or filenames (mode=filename) under a path.",
     "parameters": {"type": "object",
                    "properties": {"query": {"type": "string", "description": "Regex to search for."},
                                   "mode": {"type": "string", "enum": ["content", "filename"]},
                                   "path": {"type": "string"}, "glob": {"type": "string"},
                                   "case_sensitive": {"type": "boolean"},
                                   "max_results": {"type": "integer"}},
                    "required": ["query"]}},
    search_files_handler, toolset="file")


