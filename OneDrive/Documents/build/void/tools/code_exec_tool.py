"""code_exec tool — run a Python snippet in a child process with tools callable over stdio RPC.

The child gets web_search / web_fetch / terminal / read_file / write_file / list_files /
search_files injected as functions. Every call is an RPC round-trip to the parent, which
enforces a call budget (default 50) and a hard wall-clock timeout (default 300s).
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import time

from .registry import get_context, registry

EXPOSED = ["web_search", "web_fetch", "terminal", "read_file", "write_file",
           "edit_file", "list_files", "search_files"]

BOOTSTRAP = r'''
import json, sys
_RPC_ID = [0]
def _rpc(tool, args):
    _RPC_ID[0] += 1
    rid = _RPC_ID[0]
    sys.stdout.write(json.dumps({"__void_rpc__": "call", "id": rid, "tool": tool, "args": args}) + "\n")
    sys.stdout.flush()
    while True:
        line = sys.stdin.readline()
        if not line:
            raise RuntimeError("void: parent process closed the RPC channel")
        try:
            msg = json.loads(line)
        except ValueError:
            continue
        if msg.get("__void_rpc__") == "result" and msg.get("id") == rid:
            res = msg.get("result")
            if isinstance(res, dict) and res.get("ok") is False:
                raise RuntimeError(res.get("error") or "tool call failed")
            return res
def _mk(name):
    def _fn(**kwargs):
        return _rpc(name, kwargs)
    _fn.__name__ = name
    return _fn
for _t in %s:
    globals()[_t] = _mk(_t)
''' % (EXPOSED,)


def _dispatch_for_child(tool: str, args: dict) -> dict:
    if tool not in EXPOSED:
        return {"ok": False, "error": f"tool '{tool}' is not available inside code_exec"}
    result = registry.dispatch(tool, args or {})
    js = json.dumps(result, default=str)   # keep payloads small for the child
    if len(js) > 20000:
        return {"ok": bool(result.get("ok", True)), "truncated": True, "preview": js[:20000]}
    return result


def _stream_reader(proc, parent_to_child, on_event, max_rpc: int, timeout_s: float) -> dict:
    """Read child stdout; answer RPC calls; collect user output."""
    state = {"rpc_calls": 0, "output": [], "error": "", "exceeded": False}
    deadline = time.time() + timeout_s
    while True:
        if time.time() > deadline:
            state["error"] = f"code_exec timed out after {timeout_s:.0f}s"
            proc.kill()
            break
        line = proc.stdout.readline()
        if not line:
            break
        stripped = line.strip()
        if stripped.startswith('{"__void_rpc__"'):
            try:
                msg = json.loads(stripped)
            except ValueError:
                continue
            state["rpc_calls"] += 1
            if state["rpc_calls"] > max_rpc:
                state["exceeded"] = True
                proc.kill()
                break
            on_event({"type": "tool_start", "name": f"code_exec→{msg.get('tool')}", "args": msg.get("args")})
            result = _dispatch_for_child(msg.get("tool"), msg.get("args"))
            parent_to_child.write(json.dumps({"__void_rpc__": "result", "id": msg.get("id"),
                                              "result": result}, default=str) + "\n")
            parent_to_child.flush()
        else:
            state["output"].append(line.rstrip("\n"))
    return state

def code_exec_handler(args: dict) -> dict:
    code = str(args.get("code") or "")
    if not code.strip():
        return {"ok": False, "error": "code is required"}
    ctx = get_context()
    timeout_s = float(args.get("timeout")
                      or (ctx.config.get("tools.code_exec_timeout", 300) if ctx.config else 300) or 300)
    max_rpc = int(args.get("max_rpc")
                  or (ctx.config.get("tools.code_exec_max_rpc", 50) if ctx.config else 50) or 50)
    env = {k: v for k, v in os.environ.items()
           if k not in ("OPENAI_API_KEY", "ANTHROPIC_API_KEY", "GEMINI_API_KEY",
                        "OPENROUTER_API_KEY", "CUSTOM_API_KEY", "BRAVE_API_KEY")}
    env["VOID_CLI"] = "1"
    proc = subprocess.Popen([sys.executable, "-u", "-c", BOOTSTRAP + "\n" + code],
                            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                            text=True, errors="replace", env=env,
                            cwd=str(ctx.config.project_dir) if ctx.config else None)
    state = _stream_reader(proc, proc.stdin, ctx.emit, max_rpc, timeout_s)
    try:
        proc.wait(timeout=10)
    except subprocess.TimeoutExpired:
        proc.kill()
    stderr = proc.stderr.read() if proc.stderr else ""
    out = "\n".join(state["output"])[-100_000:]
    result = {"ok": not state["error"] and proc.returncode == 0, "exit_code": proc.returncode,
              "rpc_calls": state["rpc_calls"], "stdout": out, "stderr": (stderr or "")[-8000:]}
    if state["exceeded"]:
        result["error"] = f"RPC budget exceeded ({max_rpc} calls)"
    elif state["error"]:
        result["error"] = state["error"]
    return result


registry.register(
    "code_exec",
    {"description": "Run a Python snippet in an isolated child process. Inside the snippet you can call "
                    "web_search(...), web_fetch(...), terminal(command=...), read_file(path=...), "
                    "write_file(path=..., content=...), list_files(...), search_files(query=...) — each "
                    "is an RPC back to the harness. print() output is returned. Good for data crunching, "
                    "loops over many files, or multi-step logic with tool calls in the middle.",
     "parameters": {"type": "object",
                    "properties": {"code": {"type": "string", "description": "Python source to execute."},
                                   "timeout": {"type": "integer", "description": "Seconds (default 300)."},
                                   "max_rpc": {"type": "integer", "description": "Max tool calls (default 50)."}},
                    "required": ["code"]}},
    code_exec_handler, toolset="code")

