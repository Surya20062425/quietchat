"""Security: dangerous command detection, approval prompts, secret redaction."""
from __future__ import annotations

import re
import sys
from typing import Callable

DEFAULT_DANGEROUS_PATTERNS: list[str] = [
    r"\brm\b[^|;&]*\s-[a-zA-Z]*r[a-zA-Z]*f[a-zA-Z]*\b",   # rm -rf (any target)
    r"\brm\b[^|;&]*\s-[a-zA-Z]*f[a-zA-Z]*r[a-zA-Z]*\b",   # rm -fr
    r"\brm\s+(-[a-zA-Z]*[rf][a-zA-Z]*\s+)+(/|~|\$HOME|\.\./)*\s*$",  # rm -rf /
    r"\bmkfs\b", r"\b:\(\)\{.*\};:", r"\bdd\s+if=/dev/(zero|random)\s+of=/dev/[sh]d",
    r"\bchmod\s+(-R\s+)?777\s+/(etc|usr|bin|sbin|lib|var)\b",
    r"\b(DROP|TRUNCATE)\s+TABLE\b", r"\bDELETE\s+FROM\s+\w+\s*;?\s*$",
    r"\bshutdown\b", r"\breboot\b", r"\bforkdump\b", r">\s*/dev/sd[a-z]",
    r"\bgit\s+push\s+.*\s+--force\b",
    r"\bcurl[^|]*\|\s*(ba)?sh\b", r"\bwget[^|]*\|\s*(ba)?sh\b",
    r"\bInvoke-WebRequest.*Invoke-Expression\b", r"\biex\s*\(.*(iwr|Invoke-WebRequest)\b",
    r"\bRemove-Item\s+.*-Force\b",
]

# Heuristics: commands that mutate system state or install software.
HEURISTIC_PATTERNS: list[tuple[str, str]] = [
    (r"\b(sudo|doas)\b", "runs with elevated privileges"),
    (r"\b(apt|apt-get|yum|dnf|brew|choco|winget|pip|pip3|npm)\s+install\b", "installs packages"),
    (r"\b(apt|apt-get|yum|dnf)\s+(remove|purge|autoremove)\b", "removes system packages"),
    (r"\breg\s+(add|delete)\b", "edits the Windows registry"),
    (r"\bSet-ItemProperty\b.*HK", "edits the Windows registry"),
    (r"\bformat\s+[a-z]:", "formats a drive"),
    (r"\bdel\s+/[sq]\b", "deletes recursively (Windows)"),
    (r"\brd\s+/s\b", "deletes recursively (Windows)"),
    (r"\bRemove-Item\s+.*-Recurse\b", "deletes recursively (PowerShell)"),
    (r"\bkill\s+-9\s+1\b", "kills init"),
]

REDACTION_PATTERNS: list[str] = [
    r"sk-[A-Za-z0-9_\-]{16,}",          # openai-style keys
    r"ghp_[A-Za-z0-9]{20,}",            # github PAT
    r"gho_[A-Za-z0-9]{20,}",
    r"xox[baprs]-[A-Za-z0-9\-]{10,}",   # slack tokens
    r"AIza[0-9A-Za-z_\-]{30,}",         # google api keys
    r"AKIA[0-9A-Z]{16}",                # aws access key
    r"-----BEGIN [A-Z ]*PRIVATE KEY-----",
    r"Bearer\s+[A-Za-z0-9_\-\.]{20,}",
    r"(?i)(api[_-]?key|secret|token|password|passwd|pwd)\s*[=:]\s*['\"]?[^\s'\"]{8,}",
]


def compile_patterns(extra: list[str] | None = None) -> list[re.Pattern]:
    pats = [re.compile(p, re.I) for p in DEFAULT_DANGEROUS_PATTERNS]
    for p in (extra or []):
        try:
            pats.append(re.compile(p, re.I))
        except re.error:
            continue
    return pats


def check_command(cmd: str, extra_patterns: list[str] | None = None) -> tuple[bool, str]:
    """Return (dangerous, reason)."""
    cmd = cmd or ""
    for rx in compile_patterns(extra_patterns):
        if rx.search(cmd):
            return True, f"matches dangerous pattern {rx.pattern!r}"
    for pat, why in HEURISTIC_PATTERNS:
        try:
            rx = re.compile(pat, re.I)
        except re.error:
            continue
        if rx.search(cmd):
            return True, why
    return False, ""


def redact(text: str, extra_patterns: list[str] | None = None, enabled: bool = True) -> str:
    """Redact likely secrets from text shown to the user / stored in logs."""
    if not enabled or not text:
        return text
    for pattern in REDACTION_PATTERNS + (extra_patterns or []):
        try:
            rx = re.compile(pattern)
        except re.error:
            continue
        def _sub(m: re.Match) -> str:
            s = m.group(0)
            if len(s) <= 8:
                return "*" * len(s)
            return s[:4] + "*" * (len(s) - 8) + s[-4:]
        text = rx.sub(_sub, text)
    # long high-entropy tokens that look assigned (key: value)
    text = re.sub(r"(?i)\b([a-z0-9_\-]*(?:key|token|secret|passwd|password)[a-z0-9_\-]*)\s*[:=]\s*"
                  r"([A-Za-z0-9_\-]{24,})", r"\1=********", text)
    return text


def approve_command(cmd: str, reason: str, yolo: bool, input_fn: Callable[[str], str] | None = None) -> bool:
    """Ask the user to approve a dangerous command. yolo bypasses with a warning."""
    if yolo:
        print(f"  [yolo] skipping approval for: {cmd}", file=sys.stderr)
        return True
    prompt = f"\n⚠ potentially dangerous command ({reason}):\n  {cmd}\nApprove? [Y/n] "
    fn = input_fn or input
    try:
        answer = fn(prompt)
    except (EOFError, OSError):
        return False
    return answer.strip().lower() in ("", "y", "yes")


def make_approval_hook(yolo: bool, enabled: bool, input_fn: Callable[[str], str] | None = None) -> Callable:
    """Build the approval hook used by the terminal tool via ToolContext."""
    def hook(cmd: str) -> bool:
        if not enabled:
            return True
        dangerous, reason = check_command(cmd)
        if not dangerous:
            return True
        return approve_command(cmd, reason, yolo, input_fn)
    return hook
