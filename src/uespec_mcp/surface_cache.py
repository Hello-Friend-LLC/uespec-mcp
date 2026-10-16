"""On-disk cache for the support surface (``get_support_surface`` / capabilities).

The surface only changes when the engine build, the UESpec plugin or the HF_UISpec plugin change, so it is cached under
``<project>/Saved/UESpec/cache/surface-<hash>.json`` and keyed by:

* ``project``       - the project path (``.uproject`` or project root)
* ``engine``        - the engine ``Build.version`` (version label + content hash)
* ``uespecPlugin``  - ``HF_Plugins/UESpec`` git HEAD SHA, else the ``UESpec.uplugin`` hash
* ``hfUiSpecPlugin``- the ``HF_UISpec.uplugin`` hash

A component that cannot be resolved is recorded as ``"unknown"`` (never an exception). ``force=True`` bypasses the read.
"""
from __future__ import annotations

import datetime as _datetime
import hashlib
import json
import os
import sys
from pathlib import Path
from typing import Any

CACHE_VERSION = 1
UNKNOWN = "unknown"
_HF_PLUGINS_MARKER = ("Engine", "Plugins", "HF_Plugins")


def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _file_sha(path: Path) -> str | None:
    try:
        return _sha(path.read_bytes().replace(b"\r\n", b"\n"))[:16]
    except OSError:
        return None


def _is_engine_root(path: Path) -> bool:
    return (path / "Engine" / "Build" / "Build.version").is_file()


def _engine_root_from_value(value: str | Path | None) -> Path | None:
    if not value:
        return None
    path = Path(str(value)).expanduser()
    candidates = [path, path.parent, path.parent.parent]
    if path.name.lower() == "engine":
        candidates.insert(0, path.parent)
    for candidate in candidates:
        try:
            if _is_engine_root(candidate):
                return candidate
        except OSError:
            continue
    return None


def _registry_builds() -> dict[str, str]:
    if sys.platform != "win32":
        return {}
    try:
        import winreg

        out: dict[str, str] = {}
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, r"Software\Epic Games\Unreal Engine\Builds") as key:
            index = 0
            while True:
                try:
                    name, value, kind = winreg.EnumValue(key, index)
                except OSError:
                    break
                index += 1
                if kind == winreg.REG_SZ and isinstance(value, str):
                    out[name] = value
        return out
    except OSError:
        return {}
    except Exception:  # noqa: BLE001 - registry access must never break discovery
        return {}


def resolve_engine_root(config: Any) -> Path | None:
    """Engine root from the configured engine path, else the ``.uproject`` EngineAssociation (path, registry GUID, marker scan)."""
    root = _engine_root_from_value(getattr(config, "ue_engine_path", None))
    if root is not None:
        return root
    uproject = getattr(config, "uproject_path", None)
    association = None
    if uproject is not None:
        try:
            association = json.loads(Path(uproject).read_text(encoding="utf-8-sig")).get("EngineAssociation")
        except (OSError, ValueError, AttributeError):
            association = None
    if isinstance(association, str) and association:
        root = _engine_root_from_value(association)
        if root is not None:
            return root
        builds = _registry_builds()
        if association in builds:
            root = _engine_root_from_value(builds[association])
            if root is not None:
                return root
    # The GUID is per machine and may be unregistered: scan every registered build for the HF_Plugins marker.
    for value in _registry_builds().values():
        root = _engine_root_from_value(value)
        if root is not None and root.joinpath(*_HF_PLUGINS_MARKER).is_dir():
            return root
    return None


def _engine_identity(engine_root: Path | None) -> str:
    if engine_root is None:
        return UNKNOWN
    path = engine_root / "Engine" / "Build" / "Build.version"
    try:
        raw = path.read_bytes()
        data = json.loads(raw.decode("utf-8-sig"))
        label = f"{data.get('MajorVersion')}.{data.get('MinorVersion')}.{data.get('PatchVersion')}-CL{data.get('Changelist')}"
        digest = _sha(raw.replace(b"\r\n", b"\n"))[:12]
        return f"{label}@{digest}"
    except (OSError, ValueError, AttributeError):
        return UNKNOWN


def _find_plugin_dir(name: str, engine_root: Path | None, project_root: Path | None) -> Path | None:
    candidates: list[Path] = []
    if engine_root is not None:
        candidates += [engine_root / "Engine" / "Plugins" / "HF_Plugins" / name, engine_root / "Engine" / "Plugins" / name]
    if project_root is not None:
        candidates += [project_root / "Plugins" / name, project_root / "Plugins" / "HF_Plugins" / name]
    for candidate in candidates:
        if (candidate / f"{name}.uplugin").is_file():
            return candidate
    return None


def _git_dir(plugin_dir: Path) -> Path | None:
    dot_git = plugin_dir / ".git"
    try:
        if dot_git.is_dir():
            return dot_git
        if dot_git.is_file():
            text = dot_git.read_text(encoding="utf-8").strip()
            if text.startswith("gitdir:"):
                target = Path(text.split(":", 1)[1].strip())
                return (target if target.is_absolute() else plugin_dir / target).resolve()
    except OSError:
        return None
    return None


def git_head_sha(plugin_dir: Path) -> str | None:
    """HEAD commit of a plugin checkout (plain repo or submodule), read without spawning git. None if unresolvable."""
    git_dir = _git_dir(plugin_dir)
    if git_dir is None:
        return None
    try:
        head = (git_dir / "HEAD").read_text(encoding="utf-8").strip()
        if not head.startswith("ref:"):
            return head if len(head) >= 40 else None
        ref = head.split(":", 1)[1].strip()
        loose = git_dir / ref
        if loose.is_file():
            return loose.read_text(encoding="utf-8").strip() or None
        common = git_dir / "commondir"
        search = [git_dir]
        if common.is_file():
            search.append((git_dir / common.read_text(encoding="utf-8").strip()).resolve())
        for base in search:
            if (base / ref).is_file():
                return (base / ref).read_text(encoding="utf-8").strip() or None
            packed = base / "packed-refs"
            if packed.is_file():
                for line in packed.read_text(encoding="utf-8").splitlines():
                    if line.endswith(" " + ref) and not line.startswith("#"):
                        return line.split(" ", 1)[0]
    except OSError:
        return None
    return None


def _uespec_identity(plugin_dir: Path | None) -> str:
    if plugin_dir is None:
        return UNKNOWN
    sha = git_head_sha(plugin_dir)
    if sha:
        return f"git:{sha}"
    digest = _file_sha(plugin_dir / "UESpec.uplugin")
    return f"uplugin:{digest}" if digest else UNKNOWN


def _hf_uispec_identity(plugin_dir: Path | None) -> str:
    if plugin_dir is None:
        return UNKNOWN
    digest = _file_sha(plugin_dir / "HF_UISpec.uplugin")
    return f"uplugin:{digest}" if digest else UNKNOWN


def compute_key(config: Any) -> dict[str, str]:
    project_root = config.resolved_project_root() if hasattr(config, "resolved_project_root") else None
    uproject = getattr(config, "uproject_path", None)
    project_ref = uproject or project_root
    engine_root = resolve_engine_root(config)
    return {
        "project": str(Path(project_ref).resolve()).replace("\\", "/").lower() if project_ref else UNKNOWN,
        "engine": _engine_identity(engine_root),
        "uespecPlugin": _uespec_identity(_find_plugin_dir("UESpec", engine_root, project_root)),
        "hfUiSpecPlugin": _hf_uispec_identity(_find_plugin_dir("HF_UISpec", engine_root, project_root)),
    }


def key_hash(key: dict[str, str]) -> str:
    canonical = json.dumps({"v": CACHE_VERSION, **key}, sort_keys=True, separators=(",", ":"))
    return _sha(canonical.encode("utf-8"))[:16]


def cache_path(project_root: Path, digest: str) -> Path:
    return Path(project_root) / "Saved" / "UESpec" / "cache" / f"surface-{digest}.json"


def load(project_root: Path, digest: str, fmt: str) -> dict[str, Any] | None:
    """The cached entry when it holds ``fmt`` content for ``digest``; None on any miss or unreadable file."""
    path = cache_path(project_root, digest)
    try:
        entry = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(entry, dict) or entry.get("cacheVersion") != CACHE_VERSION or entry.get("hash") != digest:
        return None
    contents = entry.get("contents")
    if not isinstance(contents, dict) or fmt not in contents:
        return None
    return entry


def store(project_root: Path, digest: str, key: dict[str, str], fmt: str, content: Any, identity: Any = None) -> str | None:
    path = cache_path(project_root, digest)
    entry: dict[str, Any] = {}
    try:
        existing = json.loads(path.read_text(encoding="utf-8"))
        if isinstance(existing, dict) and existing.get("hash") == digest and existing.get("cacheVersion") == CACHE_VERSION:
            entry = existing
    except (OSError, ValueError):
        entry = {}
    contents = entry.get("contents") if isinstance(entry.get("contents"), dict) else {}
    contents[fmt] = content
    entry.update(
        {
            "cacheVersion": CACHE_VERSION,
            "hash": digest,
            "key": key,
            "identity": identity if identity is not None else entry.get("identity"),
            "cachedAt": _datetime.datetime.now(_datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
            "contents": contents,
        }
    )
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(f".tmp{os.getpid()}")
        tmp.write_text(json.dumps(entry, ensure_ascii=False, indent=1), encoding="utf-8")
        os.replace(tmp, path)
    except OSError:
        return None
    return str(path.resolve())
