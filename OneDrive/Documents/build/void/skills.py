"""Skills: Markdown files with YAML frontmatter, invoked explicitly (never auto-loaded).

Locations searched in order: bundled `skills/` (next to the package), optional-skills/
(install-on-demand), then ~/.void/skills/ plus any tapped source repos.
"""
from __future__ import annotations

import re
import shutil
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path

import yaml

from .config import Config, atomic_write_text

FRONTMATTER_RE = re.compile(r"^---\s*\n(.*?)\n---\s*\n?(.*)$", re.S)


@dataclass
class Skill:
    name: str
    description: str = ""
    version: str = "0"
    tags: list[str] = field(default_factory=list)
    platforms: list[str] = field(default_factory=list)
    path: Path | None = None
    body: str = ""
    source: str = "user"

    def to_dict(self) -> dict:
        return {"name": self.name, "description": self.description, "version": self.version,
                "tags": self.tags, "platforms": self.platforms, "source": self.source,
                "path": str(self.path) if self.path else ""}


def parse_skill_text(text: str, path: Path | None = None, source: str = "user") -> Skill:
    front: dict = {}
    body = text
    m = FRONTMATTER_RE.match(text.lstrip("\ufeff"))
    if m:
        try:
            loaded = yaml.safe_load(m.group(1)) or {}
            if isinstance(loaded, dict):
                front = loaded
        except yaml.YAMLError:
            front = {}
        body = m.group(2)
    name = str(front.get("name") or (path.stem if path else "untitled"))
    tags = front.get("tags") or []
    if isinstance(tags, str):
        tags = [t.strip() for t in tags.split(",") if t.strip()]
    platforms = front.get("platforms") or []
    if isinstance(platforms, str):
        platforms = [p.strip() for p in platforms.split(",") if p.strip()]
    return Skill(name=name, description=str(front.get("description") or ""),
                 version=str(front.get("version") or "0"), tags=[str(t) for t in tags],
                 platforms=[str(p) for p in platforms], path=path, body=body.strip(), source=source)


def parse_skill_file(path: Path, source: str = "user") -> Skill | None:
    try:
        if path.suffix.lower() not in (".md", ".markdown"):
            return None
        return parse_skill_text(path.read_text(encoding="utf-8", errors="replace"), path, source)
    except OSError:
        return None


class SkillLibrary:
    def __init__(self, config: Config):
        self.config = config
        self.dirs: list[tuple[Path, str]] = []
        pkg_root = Path(__file__).resolve().parent.parent
        for d, src in ((pkg_root / "skills", "bundled"),
                       (pkg_root / "optional-skills", "optional"),
                       (config.user_skills_dir, "user")):
            self.dirs.append((d, src))
        for tap in (config.get("skills.sources") or []):
            self.dirs.append((Path(str(tap)), "tap"))

    # ---- scanning -------------------------------------------------------------------
    def _scan(self) -> dict[str, Skill]:
        found: dict[str, Skill] = {}
        for directory, source in self.dirs:
            if not directory.exists():
                continue
            for path in sorted(directory.rglob("*.md")):
                skill = parse_skill_file(path, source)
                if skill is None or not skill.name:
                    continue
                found.setdefault(skill.name, skill)  # earlier dirs (bundled) win
        return found

    def list(self) -> list[Skill]:
        return sorted(self._scan().values(), key=lambda s: s.name.lower())

    def get(self, name_or_path: str) -> Skill | None:
        p = Path(name_or_path)
        if p.exists() and p.is_file():
            return parse_skill_file(p, "path")
        return self._scan().get(name_or_path)

    def search(self, query: str) -> list[Skill]:
        q = (query or "").lower().strip()
        if not q:
            return self.list()
        hits = []
        for s in self.list():
            haystack = " ".join([s.name, s.description, " ".join(s.tags)]).lower()
            if q in haystack:
                hits.append(s)
        return hits

    def content(self, name_or_path: str) -> str | None:
        """Full skill text injected as a user message when invoked."""
        skill = self.get(name_or_path)
        if skill is None:
            return None
        if skill.body:
            return f"# Skill: {skill.name}\n\n{skill.body}"
        return skill.path.read_text(encoding="utf-8", errors="replace")

    # ---- bundles ----------------------------------------------------------------------
    def bundles(self) -> dict[str, list[str]]:
        out: dict[str, list[str]] = {}
        for directory, _src in self.dirs:
            bundle_file = directory / "bundles.yaml"
            if bundle_file.exists():
                try:
                    data = yaml.safe_load(bundle_file.read_text(encoding="utf-8")) or {}
                    if isinstance(data, dict):
                        for alias, ids in data.items():
                            out[str(alias)] = [str(i) for i in (ids if isinstance(ids, list) else [ids])]
                except yaml.YAMLError:
                    continue
        return out

    # ---- install / update / remove ------------------------------------------------------
    def install(self, source: str) -> tuple[bool, str]:
        """Install from an https URL to a SKILL.md, or copy a local path into ~/.void/skills."""
        dest_dir = self.config.user_skills_dir
        dest_dir.mkdir(parents=True, exist_ok=True)
        if source.startswith(("http://", "https://")):
            try:
                req = urllib.request.Request(source, headers={"User-Agent": "void-cli/1.0"})
                with urllib.request.urlopen(req, timeout=30) as resp:
                    text = resp.read(2_000_000).decode("utf-8", errors="replace")
            except Exception as exc:
                return False, f"download failed: {exc}"
            skill = parse_skill_text(text, None)
            target = dest_dir / f"{_slug(skill.name)}.md"
            atomic_write_text(target, text)
            return True, f"installed '{skill.name}' → {target}"
        src = Path(source)
        if not src.exists():
            return False, f"not found: {source}"
        if src.is_file():
            skill = parse_skill_file(src, "user") or parse_skill_text(src.read_text(encoding="utf-8"), src)
            target = dest_dir / src.name
            shutil.copy2(src, target)
            return True, f"installed '{skill.name}' → {target}"
        count = 0
        for md in src.rglob("*.md"):
            rel = md.relative_to(src)
            target = dest_dir / rel
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(md, target)
            count += 1
        return True, f"installed {count} skill file(s) from {src}"

    def uninstall(self, name_or_path: str) -> tuple[bool, str]:
        skill = self.get(name_or_path)
        if skill is None or skill.path is None:
            return False, f"not found: {name_or_path}"
        if skill.source in ("bundled", "optional"):
            return False, f"'{skill.name}' is {skill.source}; refusing to delete shipped skills"
        try:
            skill.path.unlink()
            return True, f"removed {skill.path}"
        except OSError as exc:
            return False, str(exc)

    def update(self, name_or_path: str) -> tuple[bool, str]:
        """Re-read the file and report whether the frontmatter version changed."""
        skill = self.get(name_or_path)
        if skill is None or skill.path is None:
            return False, f"not found: {name_or_path}"
        old = skill.version
        fresh = parse_skill_file(skill.path, skill.source)
        if fresh is None:
            return False, "could not re-parse skill file"
        if fresh.version != old:
            return True, f"'{fresh.name}' updated: {old} → {fresh.version}"
        return True, f"'{fresh.name}' unchanged (version {old})"

    # ---- taps ---------------------------------------------------------------------------
    def add_source(self, path_or_repo: str) -> tuple[bool, str]:
        sources = list(self.config.get("skills.sources") or [])
        if path_or_repo in sources:
            return False, "already a source"
        p = Path(path_or_repo)
        if p.exists():
            target = str(p.resolve())
        else:
            dest = self.config.home / "skill-taps" / _slug(path_or_repo)
            dest.parent.mkdir(parents=True, exist_ok=True)
            if dest.exists():
                shutil.rmtree(dest, ignore_errors=True)
            import subprocess
            try:
                subprocess.run(["git", "clone", "--depth", "1", path_or_repo, str(dest)],
                               check=True, capture_output=True, text=True, timeout=120)
            except Exception as exc:
                return False, f"clone failed: {exc}"
            target = str(dest)
        sources.append(target)
        self.config.set("skills.sources", sources)
        self.dirs.append((Path(target), "tap"))
        return True, f"added skill source: {target}"

    def remove_source(self, target: str) -> tuple[bool, str]:
        sources = [s for s in (self.config.get("skills.sources") or []) if s != target]
        self.config.set("skills.sources", sources)
        return True, f"removed source: {target}"


def _slug(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", (text or "").lower()).strip("-") or "skill"

