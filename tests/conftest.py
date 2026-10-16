from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

SRC_ROOT = Path(__file__).resolve().parents[1] / "src"
if str(SRC_ROOT) not in sys.path:
    sys.path.insert(0, str(SRC_ROOT))

from uespec_mcp import bridge


@pytest.fixture(autouse=True)
def _isolated_bridge_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    """Every test starts with no UESPEC_* configuration, no cached bridge and no 'live' editor process."""
    for name in list(os.environ):
        if name.startswith("UESPEC_"):
            monkeypatch.delenv(name, raising=False)
    monkeypatch.setattr(bridge, "_live_editor_processes", lambda: [])
    bridge.reset_bridge()
    yield
    bridge.reset_bridge()
