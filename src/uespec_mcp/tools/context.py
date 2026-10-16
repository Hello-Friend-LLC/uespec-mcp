"""Path resolution shared by every tool.

There is deliberately NO process-global "active spec": one routed backend child serves every Claude session, so a
session-scoped default would leak between clients (WI-1586 t420). Every tool takes an explicit ``spec_path`` (a string or
an array of strings), resolved here on the machine that executes the tool.
"""
from __future__ import annotations

from pathlib import Path
from typing import Any, Iterable

from ..bridge import BridgeConfig


def project_root() -> Path | None:
    """The configured project root (UESPEC_PROJECT_ROOT / UESPEC_UPROJECT_PATH / uespec.hf.json), or None."""
    try:
        return BridgeConfig.from_env().resolved_project_root()
    except Exception:  # noqa: BLE001 - config trouble must never break path resolution
        return None


def path_list(value: str | Iterable[str] | None) -> list[str]:
    """Normalize a ``str | list[str] | None`` tool argument into a list of non-empty strings."""
    if value is None:
        return []
    if isinstance(value, (str, Path)):
        text = str(value).strip()
        return [text] if text else []
    return [text for text in (str(item).strip() for item in value) if text]


def expand_path(candidate: str) -> Path:
    path = Path(candidate).expanduser()
    if path.is_absolute():
        return path.resolve()
    root = project_root()
    options = [root / path] if root is not None else []
    options.append(Path.cwd() / path)
    for option in options:
        if option.exists():
            return option.resolve()
    return options[0].resolve()


def resolve_spec_path(spec_path: str | None, *, must_exist: bool = True) -> Path:
    if not spec_path or not str(spec_path).strip():
        raise ValueError("spec_path is required: pass an explicit spec file path (there is no active spec).")
    resolved = expand_path(str(spec_path).strip())
    if must_exist and not resolved.is_file():
        raise FileNotFoundError(f"Spec file '{resolved}' does not exist.")
    return resolved


def resolve_spec_paths(spec_path: str | Iterable[str] | None, *, must_exist: bool = True) -> list[Path]:
    """Resolve one path or an array of paths; the first bad entry raises (ValueError / FileNotFoundError)."""
    items = path_list(spec_path)
    if not items:
        raise ValueError("spec_path is required: pass an explicit spec file path or an array of paths.")
    return [resolve_spec_path(item, must_exist=must_exist) for item in items]


def resolve_base_dir(base_dir: str | None) -> Path:
    if base_dir:
        resolved = expand_path(base_dir)
        if not resolved.exists():
            raise FileNotFoundError(f"Base directory '{resolved}' does not exist.")
        if not resolved.is_dir():
            raise NotADirectoryError(f"Base directory '{resolved}' is not a directory.")
        return resolved
    root = project_root()
    if root is not None:
        specs_dir = root / "UESpec"
        if specs_dir.is_dir():
            return specs_dir.resolve()
        if root.is_dir():
            return root.resolve()
    return Path.cwd()


def path_error(exc: Exception, *, path: str = "$.spec_path") -> dict[str, Any]:
    """Structured error for a spec path that was missing (SPEC_PATH_REQUIRED) or not on disk (SPEC_NOT_FOUND)."""
    from ..bridge import structured_error

    code = "SPEC_PATH_REQUIRED" if isinstance(exc, ValueError) else "SPEC_NOT_FOUND"
    return structured_error(code, str(exc), path=path, hint="Pass spec_path explicitly (a string, or an array of strings).")
