"""preview_spec / LiveEditorBridge.capture_preview against fake HTTP MCP servers (WI-1586 t320)."""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

import pytest

from test_live_bridge import FakeEditor, _live, _structured
from uespec_mcp import bridge, server
from uespec_mcp.bridge import BridgeConfig, LiveEditorBridge, MockBridge
from uespec_mcp.tools import preview

ASSET = "/Game/HF/UISpecDemo/T320/WBP_Demo.WBP_Demo"


@pytest.fixture(autouse=True)
def _fast_polling(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(bridge, "RATE_LIMIT_BACKOFF_S", (0, 0, 0))
    monkeypatch.setattr(bridge, "POLL_INTERVALS_S", (0.01,))


@pytest.fixture
def editor() -> Any:
    fake = FakeEditor()
    yield fake
    fake.close()


@pytest.fixture
def epic() -> Any:
    fake = FakeEditor()
    yield fake
    fake.close()


def _png(tmp_path: Path) -> Path:
    path = tmp_path / "Saved" / "WidgetPreviews" / "WBP_Demo.png"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"\x89PNG fake")
    return path


def _capture_handler(png: Path, **overrides: Any):
    def handler(args: dict[str, Any]) -> dict[str, Any]:
        body = {"bSuccess": True, "OutputPath": png.as_posix(), "Width": 1280, "Height": 720, "ErrorMessage": "", "DpiScaleUsed": 1.0, "StateLog": "ok"}
        body.update(overrides)
        return _structured({"result": body})

    return handler


def _verdicts(monkeypatch: pytest.MonkeyPatch, *verdicts: str) -> list[float]:
    calls: list[float] = []
    queue = list(verdicts)

    def fake(png: Path, after: float) -> dict[str, str]:
        calls.append(after)
        verdict = queue.pop(0) if len(queue) > 1 else queue[0]
        return {"verdict": verdict, "line": f"{verdict.upper()}: {png.name}"}

    monkeypatch.setattr(bridge, "_run_image_check", fake)
    return calls


def test_capture_preview_through_the_hf_bridge(editor: FakeEditor, tmp_path: Path) -> None:
    png = _png(tmp_path)
    editor.tools["epic_call_tool"] = _capture_handler(png)

    result = _live(editor, tmp_path).capture_preview(ASSET, 640, 360, "hovered")

    assert result["ok"] is True
    assert result["route"] == "hf-bridge"
    assert result["pngPath"] == png.as_posix()
    assert result["imageVerdict"] == "unchecked"
    assert result["blankRetried"] is False
    assert result["backend"] == "live" and result["correlationId"]
    assert {"kind": "png", "path": png.as_posix()} in result["artifacts"]
    sent = editor.calls("epic_call_tool")
    assert len(sent) == 1
    assert sent[0]["toolset_name"] == "VibeUE.WidgetService"
    assert sent[0]["tool_name"] == "capture_preview_state"
    assert sent[0]["arguments"]["widget_path"] == ASSET
    assert (sent[0]["arguments"]["width"], sent[0]["arguments"]["height"]) == (640, 360)
    assert sent[0]["arguments"]["options"] == {"state": "hovered"}
    assert "_hf" not in sent[0]


def test_blank_first_capture_is_retried_once_then_passes(editor: FakeEditor, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    editor.tools["epic_call_tool"] = _capture_handler(_png(tmp_path))
    afters = _verdicts(monkeypatch, "blank", "ok")

    result = _live(editor, tmp_path).capture_preview(ASSET)

    assert result["ok"] is True and result["imageVerdict"] == "ok"
    assert result["blankRetried"] is True
    assert [a["imageVerdict"] for a in result["attempts"]] == ["blank", "ok"]
    assert len(editor.calls("epic_call_tool")) == 2
    assert afters[1] >= afters[0]


def test_two_blank_captures_fail_without_a_third_try(editor: FakeEditor, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    editor.tools["epic_call_tool"] = _capture_handler(_png(tmp_path))
    _verdicts(monkeypatch, "blank")

    result = _live(editor, tmp_path).capture_preview(ASSET)

    assert result["ok"] is False
    assert result["code"] == "PREVIEW_BLANK"
    assert result["errors"][0]["code"] == "PREVIEW_BLANK"
    assert len(editor.calls("epic_call_tool")) == 2


def test_capture_reporting_failure_is_a_structured_error(editor: FakeEditor, tmp_path: Path) -> None:
    editor.tools["epic_call_tool"] = _capture_handler(_png(tmp_path), bSuccess=False, OutputPath="", ErrorMessage="Failed to load widget")

    result = _live(editor, tmp_path).capture_preview(ASSET)

    assert result["ok"] is False
    assert result["code"] == "PREVIEW_FAILED"
    assert "Failed to load widget" in result["errors"][0]["message"]
    assert len(editor.calls("epic_call_tool")) == 1


def test_missing_png_is_reported_not_assumed(editor: FakeEditor, tmp_path: Path) -> None:
    editor.tools["epic_call_tool"] = _capture_handler(tmp_path / "nope.png")

    result = _live(editor, tmp_path).capture_preview(ASSET)

    assert result["ok"] is False
    assert result["code"] == "PREVIEW_OUTPUT_MISSING"


def test_text_wrapped_result_is_parsed(editor: FakeEditor, tmp_path: Path) -> None:
    png = _png(tmp_path)
    inner = '{"bSuccess": true, "OutputPath": "%s", "Width": 10, "Height": 5}' % png.as_posix()
    editor.tools["epic_call_tool"] = lambda args: {"content": [{"type": "text", "text": inner}], "isError": False}

    result = _live(editor, tmp_path).capture_preview(ASSET)

    assert result["ok"] is True and result["pngPath"] == png.as_posix()
    assert (result["width"], result["height"]) == (10, 5)


def test_falls_back_to_the_epic_server_only_when_the_bridge_has_no_epic_tools(
    editor: FakeEditor, epic: FakeEditor, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("UESPEC_EPIC_URL", epic.url)
    epic.tools["call_tool"] = _capture_handler(_png(tmp_path))

    result = _live(editor, tmp_path).capture_preview(ASSET)

    assert result["ok"] is True
    assert result["route"] == "epic-8000"
    assert epic.calls("call_tool")[0]["tool_name"] == "capture_preview_state"
    assert editor.calls("epic_call_tool")  # the HF bridge was tried first


def test_both_routes_down_is_preview_route_unavailable(editor: FakeEditor, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    dead = FakeEditor()
    dead_url = dead.url
    dead.close()
    monkeypatch.setenv("UESPEC_EPIC_URL", dead_url)

    result = _live(editor, tmp_path, live_connect_timeout_s=0.5, live_timeout_s=1.0).capture_preview(ASSET)

    assert result["ok"] is False
    assert result["code"] == "PREVIEW_ROUTE_UNAVAILABLE"
    assert "RefreshTools" in result["errors"][0]["hint"]


def test_preview_spec_compiles_then_previews_with_one_correlation_id(
    editor: FakeEditor, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    png = _png(tmp_path)
    editor.tools["uispec_compile"] = lambda args: _structured(
        {"ok": True, "assetAction": "created", "assetPath": ASSET, "generatedAssets": [ASSET], "errors": []}
    )
    editor.tools["epic_call_tool"] = _capture_handler(png)
    spec = tmp_path / "spec.json"
    spec.write_text("{}", encoding="utf-8")
    monkeypatch.setenv("UESPEC_BRIDGE_MODE", "live")
    monkeypatch.setenv("UESPEC_LIVE_URL", editor.url)
    monkeypatch.setenv("UESPEC_PROJECT_ROOT", str(tmp_path))

    result = preview.preview_spec(str(spec), "/Game/HF/UISpecDemo/T320")

    assert result["ok"] is True
    assert result["assetPath"] == ASSET
    assert result["compile"]["assetAction"] == "created"
    assert result["compile"]["correlationId"] == result["correlationId"]
    assert [a["kind"] for a in result["artifacts"]] == ["asset", "png"]
    order = [r["params"]["name"] for r in editor.requests if r.get("method") == "tools/call"]
    assert order == ["uispec_compile", "epic_call_tool"]
    assert editor.calls("uispec_compile")[0]["outputDir"] == "/Game/HF/UISpecDemo/T320"


def test_preview_spec_stops_at_a_failed_compile(editor: FakeEditor, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    editor.tools["uispec_compile"] = lambda args: _structured(
        {"ok": False, "errors": [{"code": "UNRESOLVED_BINDING_PATH", "message": "bad bind", "path": "$.root"}], "generatedAssets": []}
    )
    editor.tools["epic_call_tool"] = _capture_handler(_png(tmp_path))
    spec = tmp_path / "spec.json"
    spec.write_text("{}", encoding="utf-8")
    monkeypatch.setenv("UESPEC_BRIDGE_MODE", "live")
    monkeypatch.setenv("UESPEC_LIVE_URL", editor.url)
    monkeypatch.setenv("UESPEC_PROJECT_ROOT", str(tmp_path))

    result = preview.preview_spec(str(spec), "/Game/HF/UISpecDemo/T320")

    assert result["ok"] is False
    assert result["stage"] == "compile"
    assert result["errors"][0]["code"] == "UNRESOLVED_BINDING_PATH"
    assert editor.calls("epic_call_tool") == []


def test_preview_spec_with_asset_path_skips_compile(editor: FakeEditor, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    editor.tools["epic_call_tool"] = _capture_handler(_png(tmp_path))
    monkeypatch.setenv("UESPEC_BRIDGE_MODE", "live")
    monkeypatch.setenv("UESPEC_LIVE_URL", editor.url)
    monkeypatch.setenv("UESPEC_PROJECT_ROOT", str(tmp_path))

    result = preview.preview_spec(asset_path=ASSET)

    assert result["ok"] is True and result["compile"] is None
    assert editor.calls("uispec_compile") == []


def test_mock_mode_returns_preview_unavailable_mock(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("UESPEC_BRIDGE_MODE", "mock")

    result = preview.preview_spec(asset_path=ASSET)

    assert isinstance(bridge.get_bridge(), MockBridge)
    assert result["ok"] is False
    assert result["code"] == "PREVIEW_UNAVAILABLE_MOCK"
    assert result["errors"][0]["code"] == "PREVIEW_UNAVAILABLE_MOCK"


def test_preview_spec_is_registered_and_documents_pie() -> None:
    tools = server.list_registered_tools(server.build_server())

    assert "preview_spec" in tools
    assert "PIE is NOT driven" in preview.PREVIEW_DESCRIPTION


def test_image_check_command_maps_exit_codes(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    script = tmp_path / "fake_img_check.py"
    script.write_text(
        "import sys\nprint('OK: ' + sys.argv[-1])\nsys.exit(int(open(sys.argv[0] + '.code').read()))\n",
        encoding="utf-8",
    )
    png = tmp_path / "a.png"
    png.write_bytes(b"x")

    assert bridge._run_image_check(png, 0.0)["verdict"] == "unchecked"
    monkeypatch.setenv("UESPEC_IMG_CHECK", script.as_posix())
    for code, verdict in ((0, "ok"), (44, "blank"), (41, "blank"), (42, "stale"), (43, "invalid"), (45, "unchecked")):
        Path(str(script) + ".code").write_text(str(code), encoding="utf-8")
        checked = bridge._run_image_check(png, 123.0)
        assert checked["verdict"] == verdict, code
        assert checked["line"].startswith("OK: ")
    monkeypatch.setenv("UESPEC_IMG_CHECK", f"{Path(sys.executable).as_posix()} {script.as_posix()}")
    Path(str(script) + ".code").write_text("0", encoding="utf-8")
    assert bridge._run_image_check(png, 1.0)["verdict"] == "ok"
