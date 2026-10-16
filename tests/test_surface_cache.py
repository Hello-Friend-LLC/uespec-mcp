from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
SRC_ROOT = REPO_ROOT / "src"
if str(SRC_ROOT) not in sys.path:
    sys.path.insert(0, str(SRC_ROOT))

from uespec_mcp import surface_cache
from uespec_mcp.bridge import BridgeConfig, BridgeExecutionError
from uespec_mcp.tools import surface

SURFACE = {"RegisteredComponents": [{"Type": "SkillSlot"}], "Converters": [{"Name": "Percent"}], "StyleTokens": [], "Signals": []}
SHA = "0123456789abcdef0123456789abcdef01234567"


class CountingBridge:
    backend_name = "live"

    def __init__(self, config: BridgeConfig, *, ok: bool = True) -> None:
        self.config = config
        self.calls = 0
        self.ok = ok

    def get_support_surface(self, format: str = "markdown") -> dict[str, Any]:
        self.calls += 1
        if not self.ok:
            raise BridgeExecutionError("editor unavailable")
        content: Any = SURFACE if format == "json" else "# surface"
        return {"ok": True, "format": format, "content": content, "identity": {"pluginSha": "abc"}, "backend": "live"}


@pytest.fixture()
def world(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    engine = tmp_path / "engine"
    (engine / "Engine" / "Build").mkdir(parents=True)
    (engine / "Engine" / "Build" / "Build.version").write_text(
        json.dumps({"MajorVersion": 5, "MinorVersion": 8, "PatchVersion": 3, "Changelist": 0}), encoding="utf-8"
    )
    plugins = engine / "Engine" / "Plugins" / "HF_Plugins"
    uespec = plugins / "UESpec"
    uespec.mkdir(parents=True)
    (uespec / "UESpec.uplugin").write_text('{"VersionName": "1"}', encoding="utf-8")
    git_dir = tmp_path / "gitdata" / "UESpec"
    (git_dir / "refs" / "heads").mkdir(parents=True)
    (git_dir / "HEAD").write_text("ref: refs/heads/hf-5-8\n", encoding="utf-8")
    (git_dir / "refs" / "heads" / "hf-5-8").write_text(SHA + "\n", encoding="utf-8")
    (uespec / ".git").write_text(f"gitdir: {git_dir.as_posix()}\n", encoding="utf-8")
    hf_uispec = plugins / "HF_UISpec"
    hf_uispec.mkdir()
    (hf_uispec / "HF_UISpec.uplugin").write_text('{"VersionName": "0.2.0"}', encoding="utf-8")

    project = tmp_path / "Project"
    project.mkdir()
    (project / "Project.uproject").write_text('{"EngineAssociation": ""}', encoding="utf-8")
    config = BridgeConfig(project_root=project, uproject_path=project / "Project.uproject", ue_engine_path=engine, mode="live")
    bridge = CountingBridge(config)
    monkeypatch.setattr(surface, "get_bridge", lambda: bridge)
    return {"bridge": bridge, "project": project, "engine": engine, "uespec": uespec, "hf_uispec": hf_uispec}


def test_second_surface_call_is_a_cache_hit_without_the_editor(world: dict[str, Any]) -> None:
    bridge: CountingBridge = world["bridge"]

    miss = surface.get_support_surface("json")
    hit = surface.get_support_surface("json")

    assert miss["cache"] == "miss" and miss["ok"] is True and bridge.calls == 1
    assert hit["cache"] == "hit" and hit["content"] == SURFACE and bridge.calls == 1
    assert hit["cacheKey"]["uespecPlugin"] == f"git:{SHA}"
    assert hit["cacheKey"]["engine"].startswith("5.8.3-CL0@")
    cache_file = Path(miss["cacheFile"])
    assert cache_file == (world["project"] / "Saved" / "UESpec" / "cache" / f"surface-{miss['cacheHash']}.json").resolve()
    assert {"kind": "surface-cache", "path": str(cache_file)} in hit["artifacts"]


def test_force_and_key_changes_invalidate_the_cache(world: dict[str, Any]) -> None:
    bridge: CountingBridge = world["bridge"]
    surface.get_support_surface("json")

    assert surface.get_support_surface("json", force=True)["cache"] == "miss"
    assert bridge.calls == 2
    assert surface.get_support_surface("json")["cache"] == "hit"

    for target, text in (
        (world["engine"] / "Engine" / "Build" / "Build.version", '{"MajorVersion": 5, "MinorVersion": 8, "PatchVersion": 4}'),
        (world["hf_uispec"] / "HF_UISpec.uplugin", '{"VersionName": "0.3.0"}'),
    ):
        before = bridge.calls
        target.write_text(text, encoding="utf-8")
        assert surface.get_support_surface("json")["cache"] == "miss", target.name
        assert surface.get_support_surface("json")["cache"] == "hit", target.name
        assert bridge.calls == before + 1

    (world["uespec"] / ".git").unlink()  # plugin HEAD no longer resolvable: falls back to the uplugin hash
    fallback = surface.get_support_surface("json")
    assert fallback["cache"] == "miss" and fallback["cacheKey"]["uespecPlugin"].startswith("uplugin:")


def test_formats_are_cached_independently_and_failures_are_not_cached(world: dict[str, Any]) -> None:
    bridge: CountingBridge = world["bridge"]
    assert surface.get_support_surface("markdown")["cache"] == "miss"
    assert surface.get_support_surface("json")["cache"] == "miss"
    assert surface.get_support_surface("markdown")["cache"] == "hit"

    (world["hf_uispec"] / "HF_UISpec.uplugin").write_text('{"VersionName": "9"}', encoding="utf-8")
    bridge.ok = False
    failed = surface.get_support_surface("json")
    assert failed["ok"] is False and failed["cache"] == "miss"
    bridge.ok = True
    assert surface.get_support_surface("json")["cache"] == "miss"


def test_unresolvable_key_components_are_recorded_as_unknown(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(surface_cache, "_registry_builds", lambda: {})
    project = tmp_path / "Bare"
    project.mkdir()

    key = surface_cache.compute_key(BridgeConfig(project_root=project))

    assert key["engine"] == "unknown" and key["uespecPlugin"] == "unknown" and key["hfUiSpecPlugin"] == "unknown"
    assert key["project"].endswith("/bare")


def test_component_catalog_is_served_from_the_cached_surface(world: dict[str, Any]) -> None:
    bridge: CountingBridge = world["bridge"]

    first = surface.get_component_catalog("widget")
    second = surface.get_component_catalog()

    assert first["catalog"] == {"widgets": ["SkillSlot"]} and first["cache"] == "miss"
    assert second["cache"] == "hit" and second["counts"]["converters"] == 1 and bridge.calls == 1


def test_get_schema_outline_and_section() -> None:
    from uespec_mcp.tools import validate

    outline = validate.get_schema()
    assert "widget" in outline["sections"] and outline["ok"] is True
    assert validate.get_schema("widget")["schema"]
    assert validate.get_schema("nope")["errors"][0]["code"] == "UNKNOWN_SCHEMA_SECTION"
