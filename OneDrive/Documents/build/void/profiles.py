"""Profiles: ~/.void/profiles/<name>/ each with its own config.yaml, .env, state.db, skills.

The active profile resolves from --profile > VOID_PROFILE > ~/.void/active_profile > default.
"""
from __future__ import annotations

import shutil
from pathlib import Path

from .config import Config, DEFAULT_CONFIG, atomic_write_yaml

ACTIVE_PROFILE_FILE = "active_profile"


def profiles_dir(config: Config) -> Path:
    d = config.profiles_dir
    d.mkdir(parents=True, exist_ok=True)
    return d


def list_profiles(config: Config) -> list[dict]:
    out = [{"name": "default", "path": str(config.home), "active": config.profile == "default"}]
    d = profiles_dir(config)
    for p in sorted(d.iterdir()):
        if not p.is_dir():
            continue
        out.append({"name": p.name, "path": str(p), "active": p.name == config.profile,
                    "has_env": (p / ".env").exists(),
                    "has_db": (p / "state.db").exists(),
                    "has_skills": (p / "skills").is_dir()})
    return out


def profile_path(config: Config, name: str) -> Path:
    return config.home if name == "default" else profiles_dir(config) / name


def create_profile(config: Config, name: str, clone: bool = False, clone_all: bool = False) -> tuple[bool, str]:
    if name in ("", "default"):
        return False, "cannot create a profile named 'default'"
    target = profiles_dir(config) / name
    if target.exists():
        return False, f"profile already exists: {name}"
    target.mkdir(parents=True)

    if clone or clone_all:
        src_dir = profile_path(config, config.profile)
        src_cfg = src_dir / "config.yaml"
        if src_cfg.exists():
            shutil.copy2(src_cfg, target / "config.yaml")
            # point sessions at the new profile's own DB
            from .config import atomic_write_yaml as _w
        else:
            atomic_write_yaml(target / "config.yaml", dict(DEFAULT_CONFIG))
        if clone_all:
            for extra in (".env", "skills", "skins"):
                src = src_dir / extra
                if src.is_dir():
                    shutil.copytree(src, target / extra, dirs_exist_ok=True)
                elif src.is_file():
                    shutil.copy2(src, target / extra)
            # optionally carry over the sessions DB
            if (src_dir / "state.db").exists():
                shutil.copy2(src_dir / "state.db", target / "state.db")
    else:
        atomic_write_yaml(target / "config.yaml", dict(DEFAULT_CONFIG))

    (target / "skills").mkdir(exist_ok=True)
    scope = "config+env+skills" if clone_all else ("config" if clone else "empty defaults")
    return True, f"created profile '{name}' ({scope}) at {target}"


def delete_profile(config: Config, name: str) -> tuple[bool, str]:
    if name == "default":
        return False, "cannot delete the default profile"
    target = profiles_dir(config) / name
    if not target.exists():
        return False, f"no such profile: {name}"
    shutil.rmtree(target)
    if config.profile == name:
        set_active(config, "default")
    return True, f"deleted profile '{name}'"


def rename_profile(config: Config, old: str, new: str) -> tuple[bool, str]:
    if old == "default" or new == "default":
        return False, "cannot rename to/from 'default'"
    src = profiles_dir(config) / old
    dst = profiles_dir(config) / new
    if not src.exists():
        return False, f"no such profile: {old}"
    if dst.exists():
        return False, f"profile already exists: {new}"
    src.rename(dst)
    return True, f"renamed '{old}' → '{new}'"


def set_active(config: Config, name: str) -> tuple[bool, str]:
    if name != "default" and not (profiles_dir(config) / name).exists():
        return False, f"no such profile: {name}"
    path = config.home / ACTIVE_PROFILE_FILE
    from .config import atomic_write_text
    atomic_write_text(path, name + "\n")
    return True, f"active profile is now '{name}'"


def show_profile(config: Config, name: str | None = None) -> dict:
    target = profile_path(config, name or config.profile)
    cfg_file = target / "config.yaml"
    info = {"name": name or config.profile, "path": str(target),
            "config": str(cfg_file), "config_exists": cfg_file.exists(),
            "env_exists": (target / ".env").exists(),
            "db_exists": (target / "state.db").exists(),
            "skills_dir": str(target / "skills")}
    if cfg_file.exists():
        import yaml
        try:
            info["settings"] = yaml.safe_load(cfg_file.read_text(encoding="utf-8")) or {}
        except yaml.YAMLError:
            info["settings"] = {}
    return info


def export_profile(config: Config, name: str, dest: str) -> tuple[bool, str]:
    src = profile_path(config, name)
    if not src.exists():
        return False, f"no such profile: {name}"
    dest_path = Path(dest)
    archive = shutil.make_archive(str(dest_path), "zip", root_dir=str(src))
    return True, f"exported '{name}' → {archive}"


def import_profile(config: Config, archive: str, name: str | None = None) -> tuple[bool, str]:
    src = Path(archive)
    if not src.exists():
        return False, f"archive not found: {archive}"
    target_name = name or src.stem
    target = profiles_dir(config) / target_name
    if target.exists():
        return False, f"profile already exists: {target_name}"
    target.mkdir(parents=True)
    shutil.unpack_archive(str(src), str(target))
    return True, f"imported '{target_name}' → {target}"
