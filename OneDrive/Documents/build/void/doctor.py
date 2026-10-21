"""Doctor / status: SDK presence, API keys, model connectivity, DB size, config validity."""
from __future__ import annotations

import importlib
import shutil
import sys

from .config import Config, PROVIDER_KEY_NAMES

SDK_MODULES = {
    "openai": "openai",
    "anthropic": "anthropic",
    "google-generativeai": "google.generativeai",
}
OPTIONAL_MODULES = {"prompt_toolkit": "prompt_toolkit", "rich": "rich", "yaml": "yaml"}


def _sdk_installed(module: str) -> bool:
    """Import-probe a module, silencing vendor deprecation warnings (e.g. google-generativeai)
    so `void doctor` output stays readable."""
    import warnings
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("ignore")
            importlib.import_module(module)
        return True
    except Exception:
        return False


def run_doctor(config: Config, fix: bool = False, printer=print) -> int:
    """Print a health report. Returns 0 if everything critical passed, else 1."""
    ok = True
    printer(f"void doctor — profile: {config.profile}")
    printer("-" * 58)

    # python
    py = sys.version.split()[0]
    good = sys.version_info >= (3, 10)
    printer(f"{'✓' if good else '✗'} python {py}" + ("" if good else "  (need >= 3.10)"))
    ok &= good

    # SDKs
    for label, module in SDK_MODULES.items():
        present = _sdk_installed(module)
        line = f"{'✓' if present else '·'} sdk {label}: {'installed' if present else 'missing'}"
        printer(line)
        if not present and fix:
            printer(f"    installing {label}…")
            import subprocess
            subprocess.run([sys.executable, "-m", "pip", "install", label], check=False)
    for label, module in OPTIONAL_MODULES.items():
        present = _sdk_installed(module)
        printer(f"{'✓' if present else '·'} optional {label}: {'installed' if present else 'missing'}")

    # config validity
    problems = config.check()
    if problems:
        for p in problems:
            printer(f"✗ config: {p}")
        ok = False
    else:
        printer("✓ config: valid")

    # provider + keys
    provider = config.get("model.provider") or ""
    model = config.get("model.default") or ""
    if not provider:
        printer("✗ model: no provider configured (run `void setup model`)")
        ok = False
    else:
        keys = config.api_keys(provider)
        if provider == "custom":
            base = config.get("model.custom.base_url")
            printer(f"{'✓' if base else '✗'} custom base_url: {base or '(unset)'}")
        printer(f"{'✓' if keys else '✗'} api keys for {provider}: {len(keys)} in pool"
                f"{' (' + PROVIDER_KEY_NAMES.get(provider, '') + ')' if provider in PROVIDER_KEY_NAMES else ''}")
        if not keys and provider != "custom":
            ok = False

    # connectivity
    if provider and model:
        from .model_tools import test_provider_model
        good_conn, msg = test_provider_model(config, provider, model)
        printer(f"{'✓' if good_conn else '✗'} connectivity: {msg}")
        ok &= good_conn

    # disk / db
    try:
        usage = shutil.disk_usage(str(config.home))
        free_gb = usage.free / (1024 ** 3)
        printer(f"{'✓' if free_gb > 0.5 else '✗'} disk free: {free_gb:.1f} GB")
        ok &= free_gb > 0.5
    except OSError:
        printer("· disk: unknown")

    db_path = config.state_db_path
    size = db_path.stat().st_size if db_path.exists() else 0
    printer(f"✓ state db: {db_path} ({size} bytes)")

    printer("-" * 58)
    printer("all good ✓" if ok else "issues found (see ✗ above)"
            + ("" if fix else " — rerun with --fix to install missing SDKs"))
    return 0 if ok else 1


def status_line_full(config: Config, db, state, agent=None) -> str:
    """One-liner (multi-field) status used by `void status` and /status."""
    from .tools.registry import registry
    provider = (agent.provider.name if agent else config.get("model.provider")) or "unset"
    model = (agent.model if agent else config.get("model.default")) or "unset"
    toolsets = config.get("toolsets.enabled") or ["all"]
    try:
        cron_count = len(db.cron_list())
        kanban = db.kanban_stats()
        webhooks = len(db.webhook_list())
        sessions = len(db.list_sessions(limit=10000))
    except Exception:
        cron_count, kanban, webhooks, sessions = 0, {}, 0, 0
    mem_on = config.get("memory.enabled", True)
    parts = [
        f"model={provider}/{model}",
        f"profile={config.profile}",
        f"toolsets={','.join(map(str, toolsets))} ({len(registry.names())} tools)",
        f"yolo={'on' if state.yolo else 'off'}",
        f"sessions={sessions}",
        f"db={db.db_size_bytes()}B",
        f"memory={'on' if mem_on else 'off'}",
        f"cron={cron_count}",
        f"kanban={kanban.get('todo', 0)}/{kanban.get('in_progress', 0)}/{kanban.get('done', 0)}",
        f"webhooks={webhooks}",
    ]
    return " · ".join(parts)
