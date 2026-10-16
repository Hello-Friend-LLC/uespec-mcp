"""Example-spec retrieval shared by ``generate_spec_from_image`` and ``image_to_spec_context``.

The examples directory is a config path (``BridgeConfig.examples_dir``: ``UESPEC_EXAMPLES_DIR`` /
``UESPEC_EXAMPLES_ROOT``, else ``examplesDir`` in ``<project>/UESpec/uespec.hf.json``). Without one it defaults to the
``Examples/`` folder of the UESpec plugin that the engine/project identity (``surface_cache``) resolves. The old sibling-
checkout lookup (``<repo>/../uespec/Examples``) survives only as a last resort and is reported as ``legacy-fallback``.
"""
from __future__ import annotations

import json
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from . import surface_cache
from .bridge import BridgeConfig


@dataclass(frozen=True)
class RetrievedExample:
    name: str
    kind: str
    path: str
    snippet: str
    score: int

    def as_prompt_dict(self) -> dict[str, Any]:
        return {"name": self.name, "kind": self.kind, "path": self.path, "snippet": self.snippet}

    def as_result_dict(self) -> dict[str, Any]:
        return {"name": self.name, "kind": self.kind, "path": self.path, "score": self.score}


def resolve_examples_dirs(config: BridgeConfig | None = None) -> tuple[list[Path], str]:
    """(directories to scan, source) where source is ``config`` | ``plugin`` | ``legacy-fallback``."""
    try:
        config = config or BridgeConfig.from_env()
    except Exception:  # noqa: BLE001 - config trouble degrades to the legacy lookup, never to a failed tool call
        config = None
    if config is not None and config.examples_dir is not None:
        return [config.examples_dir], "config"
    if config is not None:
        try:
            plugin_dir = surface_cache.find_plugin_dir("UESpec", config)
        except Exception:  # noqa: BLE001
            plugin_dir = None
        if plugin_dir is not None and (plugin_dir / "Examples").is_dir():
            return [plugin_dir / "Examples"], "plugin"
    return _legacy_roots(), "legacy-fallback"


def _legacy_roots() -> list[Path]:
    roots: list[Path] = []
    repo_hint = os.getenv("UESPEC_REPO_ROOT")
    if repo_hint:
        roots.append(Path(repo_hint).expanduser() / "Examples")
    roots.append(Path(__file__).resolve().parents[3].parent / "uespec" / "Examples")
    roots.append(Path.cwd() / "Examples")
    deduped: list[Path] = []
    seen: set[str] = set()
    for root in roots:
        try:
            key = str(root.resolve())
        except OSError:
            key = str(root)
        if key not in seen:
            seen.add(key)
            deduped.append(root)
    return deduped


def retrieve_examples(
    target_kind: str,
    component_names: set[str],
    *,
    max_examples: int,
    snippet_chars: int = 3500,
    config: BridgeConfig | None = None,
) -> tuple[list[RetrievedExample], dict[str, Any]]:
    """Top examples for ``target_kind`` plus ``{"dirs": [...], "source": ..., "found": n}`` describing where they came from."""
    roots, source = resolve_examples_dirs(config)
    examples: list[RetrievedExample] = []
    scanned: list[str] = []
    for root in roots:
        if not root.is_dir():
            continue
        scanned.append(str(root.resolve()).replace("\\", "/"))
        for path in root.rglob("*.json"):
            parsed = _read_json_file(path)
            if not isinstance(parsed, dict) or not isinstance(parsed.get("root"), dict):
                continue
            kind = str(parsed.get("kind", ""))
            name = str(parsed.get("name", path.stem))
            text = json.dumps(parsed, ensure_ascii=False, indent=2)
            score = 0
            if kind == target_kind:
                score += 50
            if path.parts and "HUDs" in path.parts:
                score += 20
            score += min(25, sum(1 for component in component_names if f'"type": "{component}"' in text))
            examples.append(
                RetrievedExample(
                    name=name,
                    kind=kind,
                    path=str(path.resolve()).replace("\\", "/"),
                    snippet=_truncate_text(text, snippet_chars) if snippet_chars > 0 else "",
                    score=score,
                )
            )
    examples.sort(key=lambda example: (-example.score, example.name.lower()))
    return examples[: max(0, max_examples)], {"dirs": scanned, "source": source, "found": len(examples)}


def _read_json_file(path: Path) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError, UnicodeDecodeError):
        return None


def _truncate_text(text: str, limit: int) -> str:
    if len(text) <= limit:
        return text
    return text[: limit - 20].rstrip() + "\n  ...\n}"
