"""cron tool — create/list/edit/pause/resume/remove/run scheduled jobs from the agent loop."""
from __future__ import annotations

from .registry import get_context, registry


def _mod():
    ctx = get_context()
    if ctx.config is None:
        return None
    import importlib
    return importlib.import_module("void.cron")


def cron_handler(args: dict) -> dict:
    ctx = get_context()
    if ctx.config is None or ctx.db is None:
        return {"ok": False, "error": "cron requires config + session DB context"}
    action = str(args.get("action") or "list")
    try:
        mod = _mod()
        assert mod is not None
        if action == "list":
            jobs = mod.list_jobs(ctx.config, ctx.db)
            return {"ok": True, "jobs": jobs}
        if action == "create":
            schedule = str(args.get("schedule") or "")
            prompt = str(args.get("prompt") or "")
            if not schedule or not prompt:
                return {"ok": False, "error": "schedule and prompt are required"}
            toolsets = args.get("toolsets") or []
            no_agent = bool(args.get("no_agent"))
            job = mod.create_job(ctx.config, ctx.db, schedule, prompt,
                                 toolsets=toolsets, no_agent=no_agent,
                                 name=str(args.get("name") or ""))
            return {"ok": True, "job": job}
        job_id = str(args.get("id") or "")
        if action == "edit":
            fields = {}
            for k in ("schedule", "prompt", "name"):
                v = args.get(k)
                if v:
                    fields[k] = str(v)
            toolsets = args.get("toolsets")
            if toolsets:
                fields["toolsets"] = ",".join(str(t) for t in toolsets)
            ok = mod.edit_job(ctx.config, ctx.db, job_id, **fields)
            return {"ok": ok} if ok else {"ok": False, "error": f"job not found: {job_id}"}
        if action in ("pause", "resume"):
            ok = mod.set_enabled(ctx.config, ctx.db, job_id, action == "resume")
            return {"ok": ok, "enabled": action == "resume"} if ok else \
                {"ok": False, "error": f"job not found: {job_id}"}
        if action == "remove":
            ok = mod.remove_job(ctx.config, ctx.db, job_id)
            return {"ok": ok} if ok else {"ok": False, "error": f"job not found: {job_id}"}
        if action == "run":
            result = mod.run_job_once(ctx.config, ctx.db, job_id)
            return {"ok": True, "result": result}
        if action == "status":
            jobs = mod.list_jobs(ctx.config, ctx.db)
            return {"ok": True, "count": len(jobs),
                    "enabled": sum(1 for j in jobs if j.get("enabled")),
                    "jobs": jobs}
        return {"ok": False, "error": f"unknown action: {action}"}
    except Exception as exc:
        return {"ok": False, "error": f"cron {action} failed: {exc}"}


registry.register(
    "cron",
    {
        "description": "Manage scheduled jobs. Schedules: natural ('every 30m', 'every 2h'), cron syntax "
                       "('0 9 * * *'), or ISO timestamp for one-shot. prompt is run by the agent each fire "
                       "(set no_agent=true to run it as a plain shell/python command instead, saving tokens).",
        "parameters": {
            "type": "object",
            "properties": {
                "action": {"type": "string", "enum": ["list", "create", "edit", "pause", "resume",
                                                      "remove", "run", "status"]},
                "id": {"type": "string", "description": "Job id or name (for edit/pause/resume/remove/run)."},
                "schedule": {"type": "string", "description": "create/edit: schedule expression."},
                "prompt": {"type": "string", "description": "create/edit: prompt or shell command."},
                "name": {"type": "string", "description": "Optional job name."},
                "toolsets": {"type": "array", "items": {"type": "string"},
                             "description": "create: restrict the agent's toolsets for this job."},
                "no_agent": {"type": "boolean", "description": "create: run prompt as plain shell, no LLM."},
            },
        },
    },
    cron_handler,
    toolset="cron",
)
