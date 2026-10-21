"""terminal tool — run shell commands (local by default; docker/ssh backends), background sessions."""
from __future__ import annotations

import os
import subprocess
import threading
import time
import uuid

from .registry import get_context, registry

_BACKPROC: dict[str, dict] = {}
_BACKLOCK = threading.Lock()
MAX_CAPTURE = 200_000


def _wrap_backend(cmd: str) -> tuple[list[str], bool]:
    """Return (argv, use_shell). Applies docker/ssh backend wrapping from config."""
    ctx = get_context()
    backend = str(ctx.config.get("terminal.backend", "local") or "local") if ctx.config else "local"
    if backend == "docker":
        container = str(ctx.config.get("terminal.docker_container", "") or "")
        return ["docker", "exec", "-i", container, "sh", "-c", cmd], False
    if backend == "ssh":
        host = str(ctx.config.get("terminal.ssh_host", "") or "")
        return ["ssh", host, cmd], False
    return [cmd], True


def _reader(proc, buffer: list, lock: threading.Lock) -> None:
    try:
        for raw in iter(proc.stdout.readline, ""):
            with lock:
                buffer.append(raw)
                del buffer[:-2000]
    except Exception:
        pass


def _run_background(cmd: str, cwd: str | None) -> dict:
    argv, use_shell = _wrap_backend(cmd)
    flags = subprocess.CREATE_NEW_PROCESS_GROUP if os.name == "nt" else 0
    proc = subprocess.Popen(argv, shell=use_shell, cwd=cwd, stdout=subprocess.PIPE,
                            stderr=subprocess.STDOUT, text=True, errors="replace", creationflags=flags)
    buffer: list[str] = []
    lock = threading.Lock()
    th = threading.Thread(target=_reader, args=(proc, buffer, lock), daemon=True)
    th.start()
    sid = uuid.uuid4().hex[:10]
    with _BACKLOCK:
        _BACKPROC[sid] = {"proc": proc, "buffer": buffer, "lock": lock, "cmd": cmd,
                          "started": time.time(), "thread": th}
    return {"ok": True, "session_id": sid, "message": f"background process started ({sid})"}


def _get_output(session_id: str) -> dict:
    with _BACKLOCK:
        entry = _BACKPROC.get(session_id)
    if entry is None:
        return {"ok": False, "error": f"unknown background session: {session_id}"}
    with entry["lock"]:
        out = "".join(entry["buffer"])[-MAX_CAPTURE:]
    code = entry["proc"].poll()
    return {"ok": True, "session_id": session_id, "running": code is None,
            "exit_code": code, "output": out}

def _kill(session_id: str) -> dict:
    with _BACKLOCK:
        entry = _BACKPROC.pop(session_id, None)
    if entry is None:
        return {"ok": False, "error": f"unknown background session: {session_id}"}
    try:
        entry["proc"].terminate()
        try:
            entry["proc"].wait(timeout=5)
        except subprocess.TimeoutExpired:
            entry["proc"].kill()
        return {"ok": True, "killed": session_id}
    except OSError as exc:
        return {"ok": False, "error": str(exc)}


def terminal_handler(args: dict) -> dict:
    action = str(args.get("action") or "run")
    if action == "list":
        with _BACKLOCK:
            running = [{"session_id": k, "cmd": v["cmd"], "exit_code": v["proc"].poll()}
                       for k, v in _BACKPROC.items()]
        return {"ok": True, "background_sessions": running}

    if action in ("wait", "kill"):
        sid = str(args.get("session_id") or "")
        if not sid:
            return {"ok": False, "error": "session_id required for wait/kill actions"}
        return _get_output(sid) if action == "wait" else _kill(sid)

    cmd = args.get("command")
    if isinstance(cmd, list):
        cmd = " ".join(str(c) for c in cmd)
    if not cmd or not str(cmd).strip():
        return {"ok": False, "error": "command is required"}
    cmd = str(cmd)
    ctx = get_context()
    timeout = int(args.get("timeout") or (ctx.config.get("terminal.timeout", 120) if ctx.config else 120) or 120)
    cwd = str(args.get("cwd") or (ctx.config.project_dir if ctx.config else os.getcwd()))

    if args.get("background"):
        return _run_background(cmd, cwd)

    started = time.time()
    try:
        proc = subprocess.run(
            cmd, shell=True, cwd=cwd, capture_output=True, text=True, errors="replace",
            timeout=timeout, env={**os.environ, "VOID_CLI": "1"})
        out = (proc.stdout or "") + (("\n[stderr]\n" + proc.stderr) if proc.stderr else "")
        return {"ok": proc.returncode == 0, "exit_code": proc.returncode,
                "output": out[-MAX_CAPTURE:], "duration_s": round(time.time() - started, 2)}
    except subprocess.TimeoutExpired:
        return {"ok": False, "error": f"command timed out after {timeout}s", "output": "(timeout)"}
    except OSError as exc:
        return {"ok": False, "error": f"failed to run command: {exc}"}


registry.register(
    "terminal",
    {
        "description": "Run a shell command on the local machine. Use for builds, git, running scripts, "
                       "inspecting files, anything a developer does in a terminal. "
                       "Set background=true for long-running processes (returns session_id; "
                       "then action=wait|kill with that session_id).",
        "parameters": {
            "type": "object",
            "properties": {
                "command": {"type": "string", "description": "The shell command to run."},
                "timeout": {"type": "integer", "description": "Timeout in seconds."},
                "cwd": {"type": "string", "description": "Working directory (defaults to project dir)."},
                "background": {"type": "boolean", "description": "Run in background, return session_id."},
                "action": {"type": "string", "enum": ["run", "wait", "kill", "list"],
                           "description": "run (default) executes; wait polls a background session; kill stops it."},
                "session_id": {"type": "string", "description": "Background session id for wait/kill."},
            },
            "required": ["command"],
        },
    },
    terminal_handler,
    toolset="terminal",
)

