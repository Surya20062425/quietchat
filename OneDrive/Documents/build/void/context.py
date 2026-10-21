"""System prompt assembly. Kept small by design (architecture rule 4):
identity + tool list + memory + session context + actively loaded skills. Nothing else.
"""
from __future__ import annotations

import os
import time


def _tool_lines(tool_names: list[str], registry) -> list[str]:
    lines = []
    for name in tool_names:
        tool = registry.get(name)
        if tool is None:
            continue
        params = tool.parameters.get("properties", {}) or {}
        req = tool.parameters.get("required", []) or []
        bits = []
        for pname, pschema in params.items():
            ptype = str(pschema.get("type", "any"))
            mark = pname if pname in req else pname + "?"
            bits.append(f"{mark}:{ptype}")
        desc = " ".join((tool.description or "").split())[:180]
        lines.append(f"- {name}({', '.join(bits)}) — {desc}")
    return lines


def build_system_prompt(config, state, memory, tool_names, registry) -> str:
    cwd = str(config.project_dir)
    provider = state.provider_override or config.get("model.provider") or "unset"
    model = state.model_override or config.get("model.default") or "unset"
    yolo = bool(state.yolo)

    parts: list[str] = []
    parts.append(
        "You are void, a pragmatic terminal-first AI agent. You operate directly on this machine: "
        "you can run shell commands, read/write/edit files, search the web, run python, delegate "
        "sub-tasks to child agents, and manage scheduled work. Be direct and terse. Prefer taking "
        "action over asking questions when the request is clear; when a command fails, read the "
        "error and fix it. Never fabricate tool results. "
        f"Date: {time.strftime('%Y-%m-%d')}. Working directory: {cwd}.")

    parts.append(f"Current model: {provider}/{model}.")

    lines = _tool_lines(tool_names, registry)
    if lines:
        parts.append("## Tools available to you\n" + "\n".join(lines) +
                     "\n\nCall tools with structured tool_calls. After each batch you will receive "
                     "results as tool messages. Chain multiple tools to accomplish complex tasks.")
    else:
        parts.append("## Tools\nNo tools are enabled in this session; answer from your own knowledge.")

    block = memory.prompt_block() if memory else ""
    if block:
        parts.append("## Memory\n" + block)

    if state.resumed_from:
        parts.append(f"## Session\nResumed session '{state.resumed_from}' — earlier messages are in "
                     "context. Continue naturally; do not greet or re-introduce yourself.")

    if state.worktree_dir:
        parts.append(f"## Worktree\nYou are operating in isolated git worktree: {state.worktree_dir}. "
                     "All file work happens here; the main checkout is untouched.")

    if yolo:
        parts.append("## YOLO mode\nDangerous-command approvals are DISABLED for this session. "
                     "The user accepted the risk; do not ask for confirmation.")

    loaded = getattr(state, "loaded_skills", {}) or {}
    if loaded:
        for name, content in loaded.items():
            parts.append(f"## Loaded skill: {name}\n{content}")

    return "\n\n".join(parts)
