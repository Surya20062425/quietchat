"""Logging: ~/.void/logs/{void.log, errors.log, cron.log, webhooks.log, kanban.log}."""
from __future__ import annotations

import logging
import sys
import time
from pathlib import Path

_LOGGERS: dict[str, logging.Logger] = {}


def setup_logging(logs_dir: Path, debug: bool = False) -> None:
    logs_dir.mkdir(parents=True, exist_ok=True)
    fmt = logging.Formatter("%(asctime)s %(levelname)s %(name)s %(message)s", "%Y-%m-%d %H:%M:%S")

    root = logging.getLogger("void")
    root.setLevel(logging.DEBUG if debug else logging.INFO)
    for h in list(root.handlers):
        root.removeHandler(h)

    main_h = logging.FileHandler(logs_dir / "void.log", encoding="utf-8")
    main_h.setFormatter(fmt)
    main_h.setLevel(logging.DEBUG if debug else logging.INFO)
    root.addHandler(main_h)

    err_h = logging.FileHandler(logs_dir / "errors.log", encoding="utf-8")
    err_h.setFormatter(fmt)
    err_h.setLevel(logging.ERROR)
    root.addHandler(err_h)

    # keep void's records out of the root logger (no stray stderr noise in the REPL)
    root.propagate = False

    if not logging.getLogger().handlers:
        quiet = logging.StreamHandler(sys.stderr)
        quiet.setLevel(logging.CRITICAL)
        logging.getLogger().addHandler(quiet)


def get_logger(component: str) -> logging.Logger:
    if component not in _LOGGERS:
        _LOGGERS[component] = logging.getLogger(f"void.{component}")
    return _LOGGERS[component]


def log_line(logs_dir: Path, filename: str, component: str, message: str, session_id: str | None = None) -> None:
    """Append one formatted line to a component log (cron/webhooks/kanban use this directly)."""
    line = f"{time.strftime('%Y-%m-%d %H:%M:%S')} INFO {component} {message}"
    if session_id:
        line += f" [session={session_id}]"
    try:
        (Path(logs_dir) / filename).parent.mkdir(parents=True, exist_ok=True)
        with open(Path(logs_dir) / filename, "a", encoding="utf-8") as f:
            f.write(line.rstrip() + "\n")
    except OSError:
        pass


def tail_logs(logs_dir: Path, errors: bool = False, follow: bool = False, lines: int = 50) -> None:
    path = (Path(logs_dir) / "errors.log") if errors else (Path(logs_dir) / "void.log")
    if not path.exists():
        print(f"[void] no log file yet: {path}")
        return
    with open(path, "r", encoding="utf-8", errors="replace") as f:
        data = f.readlines()
    sys.stdout.write("".join(data[-lines:]))
    if follow:
        with open(path, "r", encoding="utf-8", errors="replace") as f:
            f.seek(0, 2)
            try:
                while True:
                    chunk = f.readline()
                    if chunk:
                        sys.stdout.write(chunk)
                        sys.stdout.flush()
                    else:
                        time.sleep(0.4)
            except KeyboardInterrupt:
                return
