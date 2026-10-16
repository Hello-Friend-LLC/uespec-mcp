"""Per-spec repair history for the host-Claude repair flow (WI-1586 t500).

Layout (one directory per spec, under ``<project>/Saved/UESpec/PatchHistory/<spec-stem>/``)::

    <n>.json        one entry per apply_spec_patch / rollback_spec / repair reset:
                    {n, kind, specPath, ts, note, patch, before, after, beforeHash, afterHash, validation, ...}
    _spec.json      the spec path the directory belongs to (two specs with the same stem get ``<stem>-<hash8>``)
    _compiled.json  compile ledger: the assets compile_spec_to_wbp produced from this spec, with source and asset hashes

``before`` / ``after`` are the exact spec file text, so a rollback restores bytes, not a re-serialisation. State lives on
disk keyed by the spec path; nothing is process-global (one routed backend serves every Claude session).
"""
from __future__ import annotations

import hashlib
import json
import os
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

DEFAULT_MAX_ATTEMPTS = 3
_ENTRY_NAME = re.compile(r"^(\d+)\.json$")


def text_hash(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:16]


def file_hash(path: str | Path) -> str | None:
    try:
        return hashlib.sha256(Path(path).read_bytes()).hexdigest()[:16]
    except OSError:
        return None


def now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def max_attempts(configured: int | None = None) -> int:
    """``UESPEC_MAX_ATTEMPTS`` wins over the project config ``maxAttempts``; the default is 3."""
    for candidate in (os.getenv("UESPEC_MAX_ATTEMPTS"), configured):
        try:
            value = int(candidate) if candidate is not None and str(candidate).strip() else 0
        except (TypeError, ValueError):
            value = 0
        if value > 0:
            return value
    return DEFAULT_MAX_ATTEMPTS


def read_spec_text(path: Path) -> str:
    return path.read_bytes().decode("utf-8")


def write_spec_text(path: Path, text: str) -> None:
    path.write_bytes(text.encode("utf-8"))


def _write_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_name(path.name + ".tmp")
    temp.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8", newline="\n")
    os.replace(temp, path)


def _read_json(path: Path) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None


class RepairHistory:
    def __init__(self, spec_path: str | Path, history_root: str | Path) -> None:
        self.spec_path = Path(spec_path).expanduser().resolve()
        root = Path(history_root).expanduser().resolve()
        directory = root / self.spec_path.stem
        meta = _read_json(directory / "_spec.json")
        key = os.path.normcase(str(self.spec_path))
        if isinstance(meta, dict) and meta.get("specKey") not in (None, key):
            directory = root / f"{self.spec_path.stem}-{hashlib.sha256(key.encode('utf-8')).hexdigest()[:8]}"
        self.directory = directory
        self._spec_key = key

    # -- entries -----------------------------------------------------------------------------------

    def _ensure_dir(self) -> None:
        self.directory.mkdir(parents=True, exist_ok=True)
        meta_path = self.directory / "_spec.json"
        if not meta_path.is_file():
            _write_json(meta_path, {"specPath": self.spec_path.as_posix(), "specKey": self._spec_key})

    def entries(self) -> list[dict[str, Any]]:
        found: list[dict[str, Any]] = []
        if not self.directory.is_dir():
            return found
        for child in self.directory.iterdir():
            match = _ENTRY_NAME.match(child.name)
            if not match:
                continue
            payload = _read_json(child)
            if isinstance(payload, dict):
                payload.setdefault("n", int(match.group(1)))
                found.append(payload)
        return sorted(found, key=lambda entry: int(entry["n"]))

    def entry(self, n: int) -> dict[str, Any] | None:
        payload = _read_json(self.directory / f"{int(n)}.json")
        return payload if isinstance(payload, dict) else None

    def next_n(self) -> int:
        existing = self.entries()
        return (int(existing[-1]["n"]) + 1) if existing else 1

    def write_entry(self, entry: dict[str, Any]) -> Path:
        self._ensure_dir()
        path = self.directory / f"{int(entry['n'])}.json"
        _write_json(path, entry)
        return path

    def update_entry(self, n: int, **fields: Any) -> dict[str, Any]:
        entry = self.entry(n) or {"n": n}
        entry.update(fields)
        self.write_entry(entry)
        return entry

    # -- attempt accounting --------------------------------------------------------------------------

    def attempts_used(self, entries: list[dict[str, Any]] | None = None) -> int:
        """Patch attempts since the last green patch (validation ok) or ``reset`` entry; rollbacks do not refund attempts."""
        used = 0
        for entry in reversed(entries if entries is not None else self.entries()):
            kind = entry.get("kind")
            if kind == "reset":
                break
            if kind != "patch":
                continue
            validation = entry.get("validation")
            if isinstance(validation, dict) and validation.get("ok") is True:
                break
            used += 1
        return used

    def text_at(self, to: int, entries: list[dict[str, Any]] | None = None) -> str | None:
        """Spec text after entry ``to`` (``0`` = the text before the first entry), or None when there is no such entry."""
        items = entries if entries is not None else self.entries()
        if not items:
            return None
        if to == 0:
            return str(items[0].get("before", ""))
        for entry in items:
            if int(entry["n"]) == to:
                return str(entry.get("after", ""))
        return None

    # -- compile ledger --------------------------------------------------------------------------------

    @property
    def ledger_path(self) -> Path:
        return self.directory / "_compiled.json"

    def compiles(self) -> list[dict[str, Any]]:
        payload = _read_json(self.ledger_path)
        return [item for item in payload if isinstance(item, dict)] if isinstance(payload, list) else []

    def record_compile(self, record: dict[str, Any]) -> None:
        self._ensure_dir()
        items = self.compiles()
        items.append(record)
        _write_json(self.ledger_path, items[-20:])
