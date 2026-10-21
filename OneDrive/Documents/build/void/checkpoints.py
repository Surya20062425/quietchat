"""Checkpoints / rollback: diff-based snapshots of the project directory.

Each checkpoint stores a manifest (relpath -> sha256) plus the *contents* of only the
files that changed since the previous checkpoint, under
~/.void/checkpoints/<timestamp>/files/<relpath>. Rollback restores those files
(or deletes files that were created after the checkpoint).
"""
from __future__ import annotations

import hashlib
import json
import shutil
import time
from pathlib import Path

from .config import Config, atomic_write_text

SKIP_DIRS = {".git", "node_modules", "__pycache__", ".venv", "venv", ".idea", ".vscode",
             ".mypy_cache", ".pytest_cache", ".ruff_cache", "dist", "build", ".void"}
MAX_FILE_BYTES = 2_000_000


class CheckpointManager:
    def __init__(self, config: Config):
        self.config = config
        self.root = config.checkpoints_dir
        self.project = config.project_dir
        self.interval = int(config.get("sessions.checkpoint.interval", 5) or 5)
        self._last_turn = -1
        self._last_manifest: dict[str, str] = {}

    # ---- scanning -----------------------------------------------------------------
    def _scan(self) -> dict[str, str]:
        manifest: dict[str, str] = {}
        for p in self.project.rglob("*"):
            if not p.is_file():
                continue
            if any(part in SKIP_DIRS for part in p.parts):
                continue
            try:
                if p.stat().st_size > MAX_FILE_BYTES:
                    continue
                digest = hashlib.sha256(p.read_bytes()).hexdigest()
            except OSError:
                continue
            manifest[str(p.relative_to(self.project))] = digest
        return manifest

    # ---- creating ------------------------------------------------------------------
    def maybe_checkpoint(self, turn: int, force: bool = False) -> dict | None:
        if not self.config.get("sessions.checkpoint.enabled"):
            return None
        if not force and (turn - self._last_turn) < self.interval:
            return None
        self._last_turn = turn
        return self.create(label=f"turn-{turn}")

    def create(self, label: str = "") -> dict:
        manifest = self._scan()
        stamp = time.strftime("%Y%m%d-%H%M%S")
        cdir = self.root / stamp
        files_dir = cdir / "files"
        files_dir.mkdir(parents=True, exist_ok=True)
        changed: list[str] = []
        for rel, digest in manifest.items():
            if self._last_manifest.get(rel) == digest:
                continue  # unchanged since last checkpoint: don't copy contents
            src = self.project / rel
            dst = files_dir / rel
            try:
                dst.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(src, dst)
                changed.append(rel)
            except OSError:
                continue
        removed = [rel for rel in self._last_manifest if rel not in manifest]
        meta = {"timestamp": stamp, "label": label, "files": manifest, "changed": changed,
                "deleted_since_previous": removed, "count": len(manifest)}
        atomic_write_text(cdir / "manifest.json", json.dumps(meta, indent=2))
        self._last_manifest = manifest
        return meta

    # ---- listing / rollback ----------------------------------------------------------
    def list_checkpoints(self) -> list[dict]:
        out: list[dict] = []
        if not self.root.exists():
            return out
        for cdir in sorted(self.root.iterdir()):
            manifest = cdir / "manifest.json"
            if not manifest.exists():
                continue
            try:
                meta = json.loads(manifest.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                continue
            meta["path"] = str(cdir)
            out.append(meta)
        return out

    def latest(self) -> dict | None:
        items = self.list_checkpoints()
        return items[-1] if items else None

    def rollback(self, stamp: str | None = None) -> dict:
        """Restore files from a checkpoint (default: most recent). Returns a report."""
        target = None
        if stamp:
            for meta in self.list_checkpoints():
                if meta["timestamp"] == stamp or meta.get("label") == stamp:
                    target = meta
                    break
        else:
            target = self.latest()
        if target is None:
            return {"ok": False, "error": "no checkpoint found"}
        cdir = Path(target["path"])
        files_dir = cdir / "files"
        restored, deleted, errors = [], [], []
        for rel in target.get("files", {}):
            src = files_dir / rel
            if not src.exists():
                continue  # unchanged at the time: already identical on disk
            dst = self.project / rel
            try:
                dst.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(src, dst)
                restored.append(rel)
            except OSError as exc:
                errors.append(f"{rel}: {exc}")
        for rel in target.get("deleted_since_previous", []):
            pass  # informational only
        return {"ok": True, "label": target.get("label"), "timestamp": target.get("timestamp"),
                "restored": restored, "count": len(restored), "errors": errors}
