"""Cron: schedule parsing, DB-backed job storage, and a stdlib threaded scheduler.

Schedules accepted:
  natural  — "every 30m", "30m", "2h", "every 2h", "daily", "hourly"
  cron     — 5-field crontab syntax: "*/2 * * * *", "0 9 * * 1-5"
  ISO      — a one-shot timestamp: "2026-01-01T09:00:00"
Runs on a background thread (no APScheduler dependency); each fire is logged to
~/.void/logs/cron.log and its last output stored on the job row.
"""
from __future__ import annotations

import re
import subprocess
import sys
import threading
import time
from datetime import datetime, timedelta

from .logging_setup import log_line

_ISO_RE = re.compile(r"^\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}")
_EVERY_RE = re.compile(r"^(?:every\s+)?(\d+)\s*(s|sec|secs|seconds?|m|min|mins|minutes?|h|hr|hrs|hours?|d|days?)$",
                       re.I)
_NAMED = {"hourly": "0 * * * *", "daily": "0 0 * * *", "weekly": "0 0 * * 0",
          "monthly": "0 0 1 * *", "every minute": "* * * * *", "minutely": "* * * * *"}


def parse_schedule(expr: str) -> dict:
    """Return {'kind': 'interval'|'cron'|'once', ...} or raise ValueError."""
    raw = (expr or "").strip()
    if not raw:
        raise ValueError("empty schedule")
    low = raw.lower()
    if low in _NAMED:
        return {"kind": "cron", "expr": _NAMED[low], "raw": raw}
    m = _EVERY_RE.match(low)
    if m:
        n = int(m.group(1))
        unit = m.group(2).lower()[0]
        seconds = {"s": 1, "m": 60, "h": 3600, "d": 86400}[unit] * max(1, n)
        return {"kind": "interval", "seconds": seconds, "raw": raw}
    if _ISO_RE.match(raw):
        return {"kind": "once", "at": raw.replace(" ", "T"), "raw": raw}
    fields = raw.split()
    if len(fields) == 5:
        return {"kind": "cron", "expr": raw, "raw": raw}
    raise ValueError(f"unrecognized schedule: {expr!r} (use 'every 30m', '*/5 * * * *', or an ISO time)")


def _field_matches(field: str, value: int, low: int, high: int) -> bool:
    for part in field.split(","):
        part = part.strip()
        if not part:
            continue
        step = 1
        if "/" in part:
            part, _, step_s = part.partition("/")
            try:
                step = max(1, int(step_s))
            except ValueError:
                step = 1
        if part in ("*", ""):
            rng = range(low, high + 1)
        elif "-" in part:
            a, _, b = part.partition("-")
            try:
                rng = range(int(a), int(b) + 1)
            except ValueError:
                continue
        else:
            try:
                if value == int(part):
                    return True
            except ValueError:
                continue
            continue
        if value in rng and (value - low) % step == 0:
            return True
    return False


def cron_matches(expr: str, when: datetime) -> bool:
    minute, hour, dom, month, dow = (expr.split() + ["*"] * 5)[:5]
    weekday = when.weekday()  # Mon=0..Sun=6
    cron_dow = (weekday + 1) % 7  # cron uses Sun=0
    return (_field_matches(minute, when.minute, 0, 59)
            and _field_matches(hour, when.hour, 0, 23)
            and _field_matches(dom, when.day, 1, 31)
            and _field_matches(month, when.month, 1, 12)
            and _field_matches(dow, cron_dow, 0, 7))


def next_run(parsed: dict, last: float | None = None) -> float:
    """Best-effort next fire timestamp (epoch seconds) for display."""
    now = time.time()
    if parsed["kind"] == "interval":
        return (last or now) + parsed["seconds"]
    if parsed["kind"] == "once":
        try:
            return datetime.fromisoformat(parsed["at"]).timestamp()
        except ValueError:
            return now
    dt = datetime.now().replace(second=0, microsecond=0) + timedelta(minutes=1)
    for _ in range(60 * 24 * 8):
        if cron_matches(parsed["expr"], dt):
            return dt.timestamp()
        dt += timedelta(minutes=1)
    return now

# ---- job storage helpers (used by the cron tool + CLI) -------------------------------
def _job_dict(row: dict) -> dict:
    row = dict(row)
    row["toolsets"] = [t for t in str(row.get("toolsets") or "").split(",") if t]
    row["no_agent"] = bool(row.get("no_agent"))
    row["enabled"] = bool(row.get("enabled"))
    try:
        row["parsed"] = parse_schedule(row.get("schedule") or "")
    except ValueError:
        row["parsed"] = {"kind": "invalid"}
    if row.get("parsed", {}).get("kind") == "invalid":
        row.pop("parsed")
    return row


def list_jobs(config, db) -> list[dict]:
    return [_job_dict(r) for r in db.cron_list()]


def create_job(config, db, schedule: str, prompt: str, toolsets: list[str] | None = None,
               no_agent: bool = False, name: str = "") -> dict:
    parsed = parse_schedule(schedule)
    jid = db.cron_create(name or "", schedule, prompt, toolsets=toolsets or [], no_agent=no_agent)
    job = db.cron_get(jid) or {"id": jid}
    job = _job_dict(job)
    log_line(config.logs_dir, "cron.log", "cron", f"created job {jid} schedule={schedule!r} "
                                                 f"no_agent={no_agent} name={name!r}")
    return job


def edit_job(config, db, job_id: str, **fields) -> bool:
    if "schedule" in fields and fields["schedule"]:
        parse_schedule(str(fields["schedule"]))
    return db.cron_update(job_id, **fields)


def set_enabled(config, db, job_id: str, enabled: bool) -> bool:
    return db.cron_update(job_id, enabled=enabled)


def remove_job(config, db, job_id: str) -> bool:
    return db.cron_remove(job_id)


def run_job_once(config, db, job_id: str, on_output=None) -> dict:
    """Execute a job immediately. no_agent => plain shell; else a full agent turn."""
    job = db.cron_get(job_id)
    if job is None:
        return {"ok": False, "error": f"job not found: {job_id}"}
    job = _job_dict(job)
    started = time.strftime("%Y-%m-%d %H:%M:%S")
    if job["no_agent"]:
        result = _run_shell_job(config, job)
    else:
        result = _run_agent_job(config, db, job)
    status = "ok" if result.get("ok") else "error"
    db.cron_update(job_id, last_run=started, last_status=status,
                   last_output=str(result.get("output") or result.get("error") or "")[:4000])
    log_line(config.logs_dir, "cron.log", "cron",
             f"ran job {job['id']} ({status}): {str(result.get('output') or result.get('error') or '')[:400]}")
    if on_output:
        on_output(job, result)
    return result


def _clip(text: str, limit: int) -> str:
    text = text or ""
    return text if len(text) <= limit else text[:limit] + f"\n...[truncated {len(text) - limit} chars]"


def _run_shell_job(config, job: dict) -> dict:
    limit = int(config.get("cron.max_output_chars", 4000) or 4000)
    if job["prompt"].strip().startswith(("python", "py ")) or job["prompt"].strip().endswith(".py"):
        argv = ["python", "-c", job["prompt"]] if not job["prompt"].strip().endswith(".py") \
            else ["python", job["prompt"].strip()]
        shell = False
    else:
        argv, shell = job["prompt"], True
    try:
        proc = subprocess.run(argv, shell=shell, capture_output=True, text=True, errors="replace",
                              timeout=int(config.get("terminal.timeout", 120) or 120),
                              cwd=str(config.project_dir))
        out = (proc.stdout or "") + (("\n[stderr]\n" + proc.stderr) if proc.stderr else "")
        return {"ok": proc.returncode == 0, "exit_code": proc.returncode, "output": _clip(out, limit)}
    except subprocess.TimeoutExpired:
        return {"ok": False, "error": "job timed out"}
    except Exception as exc:
        return {"ok": False, "error": f"{type(exc).__name__}: {exc}"}

def _run_agent_job(config, db, job: dict) -> dict:
    """Run the job's prompt as a normal agent turn (same core loop as the REPL)."""
    limit = int(config.get("cron.max_output_chars", 4000) or 4000)
    try:
        from .agent.run_agent import AIAgent
        from .state import RuntimeState
        state = RuntimeState()
        state.enabled_toolsets = job.get("toolsets") or ["all"]
        state.stream_enabled = False
        sid = db.create_session(f"cron:{job.get('name') or job['id']}", platform="cron")
        state.session_id = sid
        agent = AIAgent(config, state, db=db)
        text = agent.run_conversation(job["prompt"])
        db.touch_session(sid, turns=state.turn_count)
        return {"ok": True, "output": _clip(text, limit), "session_id": sid}
    except Exception as exc:
        return {"ok": False, "error": f"{type(exc).__name__}: {exc}"}


# ---- background scheduler -----------------------------------------------------------------
class CronScheduler:
    """Minimal threaded scheduler: wakes every `tick` seconds, fires due jobs."""

    def __init__(self, config, db, tick: int = 20, on_output=None):
        self.config = config
        self.db = db
        self.tick = max(5, int(tick))
        self.on_output = on_output
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._last_fire: dict[str, float] = {}

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._loop, daemon=True, name="void-cron")
        self._thread.start()
        log_line(self.config.logs_dir, "cron.log", "cron", f"scheduler started (tick={self.tick}s)")

    def stop(self) -> None:
        self._stop.set()

    def _loop(self) -> None:
        while not self._stop.is_set():
            try:
                self._tick_once()
            except Exception as exc:
                log_line(self.config.logs_dir, "cron.log", "cron", f"scheduler error: {exc}")
            self._stop.wait(self.tick)

    def _tick_once(self) -> None:
        now = time.time()
        now_dt = datetime.now()
        for row in self.db.cron_list(include_disabled=False):
            job = _job_dict(row)
            parsed = job.get("parsed")
            if not parsed or parsed.get("kind") == "invalid":
                continue
            last = self._last_fire.get(job["id"])
            if parsed["kind"] == "interval":
                due = last is None or (now - last) >= parsed["seconds"]
            elif parsed["kind"] == "cron":
                if not cron_matches(parsed["expr"], now_dt):
                    due = False
                else:
                    due = last is None or (now - last) >= 55
            else:  # once
                try:
                    target = datetime.fromisoformat(parsed["at"]).timestamp()
                except ValueError:
                    continue
                due = last is None and now >= target
            if not due:
                continue
            self._last_fire[job["id"]] = now
            try:
                run_job_once(self.config, self.db, job["id"], on_output=self.on_output)
            except Exception as exc:
                log_line(self.config.logs_dir, "cron.log", "cron",
                         f"job {job['id']} crashed: {exc}")
            if parsed["kind"] == "once":
                self.db.cron_update(job["id"], enabled=0)


