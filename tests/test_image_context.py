from __future__ import annotations

import asyncio
import json
import struct
import sys
import zlib
from pathlib import Path
from typing import Any

import pytest

SRC_ROOT = Path(__file__).resolve().parents[1] / "src"
if str(SRC_ROOT) not in sys.path:
    sys.path.insert(0, str(SRC_ROOT))

from uespec_mcp import examples, server, surface_cache
from uespec_mcp.bridge import BridgeConfig
from uespec_mcp.tools import generate, image_context

SURFACE = {
    "supportSurfaceVersion": "test-1",
    "builtInUMGWidgets": [
        {"type": "CanvasPanel", "keys": ["children", "slot", "style"], "accepts": []},
        {"type": "TextBlock", "keys": ["content", "slot", "style"], "accepts": []},
    ],
    "commonUIWidgets": [],
    "registeredComponents": [{"type": "HealthBar", "keys": ["children", "style"], "accepts": ["children", "style"]}],
    "converters": [{"name": "BoolToVisibility", "signature": "BoolToVisibility(bValue: bool) -> ESlateVisibility"}],
    "kinds": [
        {"kind": "HUD", "baseClass": "/Script/CommonUI.CommonActivatableWidget"},
        {"kind": "Component", "baseClass": "/Script/UMG.UserWidget"},
        {"kind": "Test", "baseClass": "N/A"},
    ],
    "styleTokens": [{"name": "@color.white", "value": "#FFFFFFFF"}],
    "signals": [{"name": "onHit"}],
    "actionEvents": [{"name": "Clicked", "status": "supported"}, {"name": "Drop", "status": "unimplemented"}],
    "actionPrimitives": [{"name": "setState", "status": "supported"}, {"name": "playAnim", "status": "unimplemented"}],
}


def _png(path: Path, width: int = 64, height: int = 32) -> Path:
    def chunk(tag: bytes, data: bytes) -> bytes:
        body = tag + data
        return struct.pack(">I", len(data)) + body + struct.pack(">I", zlib.crc32(body) & 0xFFFFFFFF)

    raw = b"".join(b"\x00" + b"\x00\x00\x00" * width for _ in range(height))
    ihdr = struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0)
    path.write_bytes(b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", ihdr) + chunk(b"IDAT", zlib.compress(raw)) + chunk(b"IEND", b""))
    return path


@pytest.fixture
def examples_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    root = tmp_path / "Examples"
    (root / "HUDs").mkdir(parents=True)
    (root / "HUDs" / "combat.json").write_text(
        json.dumps({"apiVersion": "0.2", "kind": "HUD", "name": "CombatHud", "root": {"type": "HealthBar", "id": "playerHealthShell"}}),
        encoding="utf-8",
    )
    monkeypatch.setenv("UESPEC_EXAMPLES_DIR", str(root))
    return root


@pytest.fixture
def fake_surface(monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    calls: dict[str, Any] = {"n": 0}

    def get_support_surface(format: str = "json", force: bool = False) -> dict[str, Any]:
        calls["n"] += 1
        return {"ok": True, "format": format, "content": SURFACE, "cache": "hit", "cacheHash": "abc"}

    monkeypatch.setattr(image_context.surface, "get_support_surface", get_support_surface)
    return calls


def test_context_bundle_has_catalog_schema_skeleton_constraints_and_examples(tmp_path: Path, examples_dir: Path, fake_surface: dict[str, Any]) -> None:
    image = _png(tmp_path / "WBP_CombatStatusHUD_normal_1280x720.png")

    result = image_context.image_to_spec_context(str(image), hints="top-left strip")

    assert result["ok"] is True
    assert result["imagePath"] == str(image).replace("\\", "/")
    assert (result["image"]["width"], result["image"]["height"], result["image"]["mediaType"]) == (64, 32, "image/png")
    assert result["hints"] == "top-left strip"
    assert result["parentClass"] == "/Script/CommonUI.CommonActivatableWidget"
    assert result["skeleton"]["kind"] == "HUD" and result["skeleton"]["name"] == "CombatStatusHUDNormal"
    assert result["skeleton"]["root"]["type"] == "CanvasPanel" and result["skeletonSchemaValid"] is True
    widgets = {name for group in result["catalogDigest"]["widgets"] for name in group["types"]}
    assert widgets == {"CanvasPanel", "TextBlock", "HealthBar"}
    assert result["catalogDigest"]["registeredComponents"] == ["HealthBar"]
    assert result["catalogDigest"]["styleTokens"] == {"@color.white": "#FFFFFFFF"}
    # t210 statuses decide what the host may use
    assert result["constraints"]["events"] == {"supported": ["Clicked"], "unsupported": ["Drop"]}
    assert result["constraints"]["actionPrimitives"]["unsupported"] == ["playAnim"]
    assert result["constraints"]["converters"] == ["BoolToVisibility"]
    assert "widget" in result["schemaDigest"]["defs"] and "root" in result["schemaDigest"]["topLevelKeys"]
    assert [item["name"] for item in result["examples"]["items"]] == ["CombatHud"] and result["examples"]["source"] == "config"
    assert set(result["sizes"]) >= {"catalogDigest", "schemaDigest", "skeleton", "constraints", "examples", "total"}
    assert result["surface"]["available"] is True and result["surface"]["cache"] == "hit"


def test_parent_class_selects_the_kind(tmp_path: Path, examples_dir: Path, fake_surface: dict[str, Any]) -> None:
    image = _png(tmp_path / "icon.png")

    result = image_context.image_to_spec_context(str(image), target_kind="HUD", parent_class="UserWidget")

    assert result["targetKind"] == "Component" and result["parentClass"] == "/Script/UMG.UserWidget"
    assert result["skeleton"]["kind"] == "Component"

    unknown = image_context.image_to_spec_context(str(image), parent_class="NoSuchWidget")
    assert unknown["ok"] is False and unknown["errors"][0]["code"] == "PARENT_CLASS_UNKNOWN"


def test_unavailable_surface_degrades_honestly_without_fabricating(tmp_path: Path, examples_dir: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        image_context.surface,
        "get_support_surface",
        lambda format="json", force=False: {"ok": False, "content": None, "code": "EDITOR_UNAVAILABLE", "errors": [{"code": "EDITOR_UNAVAILABLE", "message": "no editor"}]},
    )
    image = _png(tmp_path / "shot.png")

    result = image_context.image_to_spec_context(str(image))

    assert result["ok"] is True
    assert result["surface"] == {"available": False, "code": "EDITOR_UNAVAILABLE", "message": "no editor"}
    assert result["catalogDigest"]["available"] is False and result["catalogDigest"]["code"] == "EDITOR_UNAVAILABLE"
    assert "widgets" not in result["catalogDigest"] and "events" not in result["constraints"]
    assert result["parentClass"] is None and result["skeleton"]["kind"] == "HUD"


@pytest.mark.parametrize(
    ("kwargs", "code"),
    [
        ({"image_path": "missing.png"}, "IMAGE_NOT_FOUND"),
        ({"image_path": ""}, "IMAGE_PATH_REQUIRED"),
        ({"image_path": "{png}", "target_kind": "Nope"}, "INVALID_TARGET_KIND"),
    ],
)
def test_input_errors_are_structured(tmp_path: Path, fake_surface: dict[str, Any], kwargs: dict[str, str], code: str) -> None:
    kwargs = {key: (str(_png(tmp_path / "ok.png")) if value == "{png}" else value) for key, value in kwargs.items()}
    result = image_context.image_to_spec_context(**kwargs)
    assert result["ok"] is False and result["errors"][0]["code"] == code
    assert fake_surface["n"] == 0


def test_unsupported_image_type_is_rejected(tmp_path: Path, fake_surface: dict[str, Any]) -> None:
    bmp = tmp_path / "shot.bmp"
    bmp.write_bytes(b"BM")
    assert image_context.image_to_spec_context(str(bmp))["errors"][0]["code"] == "UNSUPPORTED_IMAGE_TYPE"


def test_examples_dir_defaults_to_the_plugin_then_the_legacy_lookup(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    plugin = tmp_path / "UESpec"
    (plugin / "Examples").mkdir(parents=True)
    monkeypatch.setattr(surface_cache, "find_plugin_dir", lambda name, config: plugin if name == "UESpec" else None)
    monkeypatch.chdir(tmp_path)

    assert examples.resolve_examples_dirs(BridgeConfig()) == ([plugin / "Examples"], "plugin")

    monkeypatch.setattr(surface_cache, "find_plugin_dir", lambda name, config: None)
    dirs, source = examples.resolve_examples_dirs(BridgeConfig())
    assert source == "legacy-fallback" and tmp_path / "Examples" in dirs

    configured = BridgeConfig(examples_dir=tmp_path / "mine")
    assert examples.resolve_examples_dirs(configured) == ([tmp_path / "mine"], "config")


def test_examples_dir_comes_from_env_or_the_project_config_file(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    (tmp_path / "UESpec").mkdir()
    (tmp_path / "UESpec" / "uespec.hf.json").write_text(json.dumps({"examplesDir": "Docs/Examples"}), encoding="utf-8")
    monkeypatch.setenv("UESPEC_PROJECT_ROOT", str(tmp_path))
    assert BridgeConfig.from_env().examples_dir == tmp_path / "Docs" / "Examples"

    monkeypatch.setenv("UESPEC_EXAMPLES_DIR", str(tmp_path / "env"))
    assert BridgeConfig.from_env().examples_dir == tmp_path / "env"


def test_tools_are_registered_and_the_list_stays_lean(monkeypatch: pytest.MonkeyPatch) -> None:
    from fastmcp import Client

    for name in ("UESPEC_LLM_TOOLS", "UESPEC_LLM_PROVIDER", "ANTHROPIC_API_KEY", "OPENAI_API_KEY"):
        monkeypatch.delenv(name, raising=False)

    async def names(mcp: object) -> list[str]:
        async with Client(mcp) as client:
            return [tool.name for tool in await client.list_tools()]

    assert "image_to_spec_context" in asyncio.run(names(server.build_server()))
    assert "generate_spec_from_image" not in asyncio.run(names(server.build_server()))
    monkeypatch.setenv("UESPEC_LLM_PROVIDER", "sampling")
    assert "generate_spec_from_image" in asyncio.run(names(server.build_server()))


# --- generate_spec_from_image: provider none / sampling probe --------------------------------------------------------


def test_default_provider_is_none_and_reports_provider_not_configured(tmp_path: Path) -> None:
    result = generate.generate_spec_from_image(str(_png(tmp_path / "a.png")))
    assert result["ok"] is False and result["errors"][0]["code"] == "PROVIDER_NOT_CONFIGURED"
    explicit = generate.generate_spec_from_image(str(_png(tmp_path / "a.png")), provider="none")
    assert explicit["errors"][0]["code"] == "PROVIDER_NOT_CONFIGURED"


def test_direct_call_with_sampling_has_no_session_to_probe(tmp_path: Path) -> None:
    result = generate.generate_spec_from_image(str(_png(tmp_path / "a.png")), provider="sampling")
    assert result["errors"][0]["code"] == "SAMPLING_UNSUPPORTED"


def _call_generate(mcp: object, image: Path, sampling_handler: Any = None, **extra: Any) -> dict[str, Any]:
    from fastmcp import Client

    async def run() -> dict[str, Any]:
        async with Client(mcp, sampling_handler=sampling_handler) as client:
            reply = await client.call_tool("generate_spec_from_image", {"image_path": str(image), "provider": "sampling", **extra})
            return reply.structured_content or json.loads(reply.content[0].text)

    return asyncio.run(run())


def test_sampling_is_probed_per_call_and_unsupported_hosts_get_a_code(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("UESPEC_LLM_TOOLS", "on")
    result = _call_generate(server.build_server(), _png(tmp_path / "a.png"))
    assert result["ok"] is False
    assert result["errors"][0]["code"] == "SAMPLING_UNSUPPORTED"
    assert "did not advertise" in result["errors"][0]["message"]


def test_sampling_host_receives_the_image_and_prompt_and_its_spec_is_validated(tmp_path: Path, examples_dir: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("UESPEC_LLM_TOOLS", "on")
    monkeypatch.setattr(generate.surface, "get_support_surface", lambda format="json": {"ok": True, "content": SURFACE})
    monkeypatch.setattr(generate.validate, "validate_spec", lambda spec: {"ok": True, "stage": "schema", "errors": [], "normalizedJson": spec})
    seen: dict[str, Any] = {}

    async def host(messages: list[Any], params: Any, context: Any) -> str:
        blocks = messages[0].content_as_list
        seen["types"] = [block.type for block in blocks]
        seen["mime"] = next(block.mimeType for block in blocks if block.type == "image")
        seen["prompt"] = next(block.text for block in blocks if block.type == "text")
        seen["system"] = params.systemPrompt
        return 'Here you go:\n```json\n{"spec": {"name": "SampledHud", "root": {"type": "CanvasPanel", "id": "rootPanel"}}, "confidence": 0.7}\n```'

    out = tmp_path / "sampled.json"
    result = _call_generate(server.build_server(), _png(tmp_path / "a.png"), sampling_handler=host, output_path=str(out))

    assert result["ok"] is True and result["generatedSpec"]["name"] == "SampledHud"
    assert result["generatedSpec"]["kind"] == "HUD" and result["llm"]["model"] == "mcp-sampling"
    assert json.loads(out.read_text(encoding="utf-8"))["name"] == "SampledHud"
    assert seen["types"] == ["text", "image"] and seen["mime"] == "image/png"
    assert "CombatHud" in seen["prompt"] and "HealthBar" in seen["prompt"] and seen["prompt"].rstrip().endswith("<0..1>}.")


def test_sampling_reply_that_is_not_json_is_an_llm_output_error(tmp_path: Path, examples_dir: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("UESPEC_LLM_TOOLS", "on")
    monkeypatch.setattr(generate.surface, "get_support_surface", lambda format="json": {"ok": True, "content": SURFACE})

    async def host(messages: list[Any], params: Any, context: Any) -> str:
        return "I cannot see an image."

    result = _call_generate(server.build_server(), _png(tmp_path / "a.png"), sampling_handler=host)
    assert result["ok"] is False and result["errors"][0]["code"] == "LLM_OUTPUT_INVALID"
