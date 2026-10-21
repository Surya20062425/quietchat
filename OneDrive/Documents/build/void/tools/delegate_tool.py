"""delegate tool — spawn a child agent (max depth 2) with an isolated conversation."""
from __future__ import annotations

from .registry import get_context, registry


def delegate_handler(args: dict) -> dict:
    ctx = get_context()
    prompt = str(args.get("prompt") or "").strip()
    if not prompt:
        return {"ok": False, "error": "prompt is required"}
    if ctx.agent_factory is None:
        return {"ok": False, "error": "delegation is not available in this context"}
    depth = int(ctx.depth or 0)
    if depth >= 2:
        return {"ok": False, "error": "delegation depth limit reached (max 2)"}
    toolsets = args.get("toolsets") or ["terminal", "file", "web"]
    toolsets = [str(t) for t in toolsets]
    blocked = {"delegate", "cron", "webhook"}
    toolsets = [t for t in toolsets if t not in blocked]
    label = str(args.get("label") or "child")
    ctx.emit({"type": "tool_start", "name": f"delegate({label})", "args": {"prompt": prompt[:200]}})
    try:
        child = ctx.agent_factory(prompt, toolsets=toolsets, depth=depth + 1, label=label)
    except ValueError as exc:
        return {"ok": False, "error": str(exc)}
    try:
        text = child.run_conversation(prompt)
    except Exception as exc:
        return {"ok": False, "error": f"child agent failed: {exc}"}
    return {"ok": True, "label": label, "toolsets": toolsets, "result": text}


registry.register(
    "delegate",
    {"description": "Delegate a self-contained task to a child agent that runs its own tool loop and "
                    "returns only its final answer. Use for parallelizable or context-heavy work so the "
                    "main conversation stays clean. Child agents cannot delegate further.",
     "parameters": {"type": "object",
                    "properties": {
                        "prompt": {"type": "string", "description": "The full task for the child agent."},
                        "toolsets": {"type": "array", "items": {"type": "string"},
                                     "description": "Toolsets the child may use (default terminal,file,web)."},
                        "label": {"type": "string", "description": "Short label for the child (logging)."}},
                    "required": ["prompt"]}},
    delegate_handler, toolset="delegate")
