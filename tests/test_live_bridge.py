"""LiveEditorBridge against a local fake HTTP MCP server (WI-1586 t310): live/auto/refusal/task_id polling/uncertain+cancel."""

from __future__ import annotations

import json
import socket
import subprocess
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Callable

import pytest

from uespec_mcp import bridge
from uespec_mcp.bridge import (
    BridgeClient,
    BridgeConfig,
    EditorUnavailableError,
    LiveEditorBridge,
    LiveEditorRefusedError,
    MockBridge,
)
from uespec_mcp.tools import compile as compile_tools
from uespec_mcp.tools import validate


def _structured(payload: dict[str, Any], is_error: bool = False) -> dict[str, Any]:
    return {"structuredContent": payload, "content": [{"type": "text", "text": json.dumps(payload)}], "isError": is_error}


class FakeEditor:
    """Minimal Streamable-HTTP MCP endpoint recording every request."""

    def __init__(self) -> None:
        self.tools: dict[str, Callable[[dict[str, Any]], dict[str, Any]]] = {}
        self.requests: list[dict[str, Any]] = []
        self.headers_seen: list[dict[str, str]] = []
        self.rate_limit_remaining = 0
        self.tool_delay_s = 0.0
        self._lock = threading.Lock()
        editor = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args: Any) -> None:  # silence
                return

            def do_POST(self) -> None:  # noqa: N802
                length = int(self.headers.get("Content-Length") or 0)
                payload = json.loads(self.rfile.read(length) or b"{}")
                with editor._lock:
                    editor.requests.append(payload)
                    editor.headers_seen.append({key.lower(): value for key, value in self.headers.items()})
                    limited = editor.rate_limit_remaining > 0
                    if limited:
                        editor.rate_limit_remaining -= 1
                try:
                    if limited:
                        self.send_response(429)
                        self.send_header("Content-Length", "0")
                        self.end_headers()
                        return
                    method = payload.get("method")
                    if method == "notifications/initialized":
                        self.send_response(202)
                        self.send_header("Content-Length", "0")
                        self.end_headers()
                        return
                    if method == "initialize":
                        self._reply(payload, {"protocolVersion": "2025-06-18", "capabilities": {}}, session=True)
                        return
                    if method == "tools/call":
                        if editor.tool_delay_s:
                            time.sleep(editor.tool_delay_s)
                        name = payload["params"]["name"]
                        arguments = payload["params"].get("arguments", {})
                        handler = editor.tools.get(name)
                        if handler is None:
                            self._reply(payload, None, error={"code": -32602, "message": f"Unknown tool: {name}"})
                            return
                        self._reply(payload, handler(arguments))
                        return
                    self._reply(payload, None, error={"code": -32601, "message": "Method not found"})
                except (BrokenPipeError, ConnectionResetError):
                    pass

            def _reply(self, request: dict[str, Any], result: Any, *, error: Any = None, session: bool = False) -> None:
                body = {"jsonrpc": "2.0", "id": request.get("id")}
                if error is not None:
                    body["error"] = error
                else:
                    body["result"] = result
                data = json.dumps(body).encode("utf-8")
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                if session:
                    self.send_header("Mcp-Session-Id", "fake-session")
                self.end_headers()
                self.wfile.write(data)

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.server.daemon_threads = True
        self.port = self.server.server_address[1]
        self.url = f"http://127.0.0.1:{self.port}/mcp"
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def calls(self, tool: str) -> list[dict[str, Any]]:
        return [r["params"]["arguments"] for r in self.requests if r.get("method") == "tools/call" and r["params"]["name"] == tool]

    def close(self) -> None:
        self.server.shutdown()
        self.server.server_close()


@pytest.fixture
def editor() -> Any:
    fake = FakeEditor()
    yield fake
    fake.close()


@pytest.fixture(autouse=True)
def _fast_polling(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(bridge, "RATE_LIMIT_BACKOFF_S", (0, 0, 0))
    monkeypatch.setattr(bridge, "POLL_INTERVALS_S", (0.01,))


def _live(editor: FakeEditor, tmp_path: Path, **overrides: Any) -> LiveEditorBridge:
    values: dict[str, Any] = {"mode": "live", "live_url": editor.url, "project_root": tmp_path}
    values.update(overrides)
    return LiveEditorBridge(BridgeConfig(**values))


def _closed_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def test_live_validate_round_trip_envelope_and_log(editor: FakeEditor, tmp_path: Path) -> None:
    editor.tools["uispec_validate"] = lambda args: _structured(
        {"ok": True, "normalizedJson": "{}", "errors": [], "backend": "live", "durationMs": 3.0, "_hf": args.get("_hf")}
    )

    result = _live(editor, tmp_path).validate_spec_content("{}")

    assert result["ok"] is True
    assert result["backend"] == "live"
    assert result["validationLevel"] == "semantic"
    assert result["artifacts"] == []
    assert result["durationMs"] >= 0
    assert result["editorDurationMs"] == 3.0
    sent = editor.calls("uispec_validate")[0]
    assert sent["specJson"] == "{}"
    assert sent["_hf"]["correlationId"] == result["correlationId"]
    assert set(sent) == {"specJson", "_hf"}
    assert editor.headers_seen[0]["authorization"].startswith("Bearer ")
    assert editor.headers_seen[-1]["mcp-session-id"] == "fake-session"

    log_text = (tmp_path / "Saved" / "UESpec" / "logs" / "uespec_mcp.log").read_text(encoding="utf-8")
    assert "route=validate backend=live" in log_text
    assert f"project={tmp_path.name}" in log_text
    assert "code=ok" in log_text
    assert f"correlationId={result['correlationId']}" in log_text


def test_correlation_id_is_forwarded_when_supplied(editor: FakeEditor, tmp_path: Path) -> None:
    editor.tools["uispec_compile"] = lambda args: _structured(
        {"ok": True, "generatedAssets": ["/Game/X/WBP_A.WBP_A"], "savedPackageFile": "C:/p/Content/X/WBP_A.uasset", "errors": []}
    )
    spec = tmp_path / "spec.json"
    spec.write_text("{}", encoding="utf-8")

    result = _live(editor, tmp_path).compile_spec(spec, "/Game/X", correlation_id="corr-123")

    assert result["correlationId"] == "corr-123"
    assert editor.calls("uispec_compile")[0]["_hf"]["correlationId"] == "corr-123"
    assert editor.calls("uispec_compile")[0]["outputDir"] == "/Game/X"
    assert [a["kind"] for a in result["artifacts"]] == ["asset", "package-file"]


def test_endpoint_port_and_bearer_token_come_from_the_project_ini(editor: FakeEditor, tmp_path: Path) -> None:
    ini = tmp_path / "Config" / "DefaultUnrealMCPServer.ini"
    ini.parent.mkdir(parents=True)
    ini.write_text(
        f"[/Script/UnrealMCPServer.MCPSettings]\nServerPort={editor.port}\nbRequireAuthToken=True\n+AuthTokens=secret-token\n",
        encoding="utf-8",
    )
    editor.tools["uispec_validate"] = lambda args: _structured({"ok": True, "errors": []})

    LiveEditorBridge(BridgeConfig(mode="live", project_root=tmp_path)).validate_spec_content("{}")

    assert editor.headers_seen[0]["authorization"] == "Bearer secret-token"


def test_auth_required_without_token_is_a_configuration_error(tmp_path: Path) -> None:
    ini = tmp_path / "Config" / "DefaultUnrealMCPServer.ini"
    ini.parent.mkdir(parents=True)
    ini.write_text("ServerPort=1\nbRequireAuthToken=True\n", encoding="utf-8")

    with pytest.raises(bridge.BridgeConfigurationError):
        LiveEditorBridge(BridgeConfig(mode="live", project_root=tmp_path)).validate_spec_content("{}")


def test_auto_mode_without_an_editor_is_a_structured_error_not_a_launch(tmp_path: Path) -> None:
    config = BridgeConfig(mode="auto", live_url=f"http://127.0.0.1:{_closed_port()}/mcp", project_root=tmp_path)

    with pytest.raises(EditorUnavailableError) as raised:
        LiveEditorBridge(config).validate_spec_content("{}")

    assert raised.value.code == "EDITOR_UNAVAILABLE"
    assert raised.value.backend == "live"
    log_text = (tmp_path / "Saved" / "UESpec" / "logs" / "uespec_mcp.log").read_text(encoding="utf-8")
    assert "code=EDITOR_UNAVAILABLE" in log_text


def test_auto_mode_initialize_timeout_is_editor_unavailable(tmp_path: Path) -> None:
    listener = socket.socket()
    listener.bind(("127.0.0.1", 0))
    listener.listen(1)  # accepts the TCP connection but never answers
    try:
        config = BridgeConfig(
            mode="auto",
            live_url=f"http://127.0.0.1:{listener.getsockname()[1]}/mcp",
            live_connect_timeout_s=0.3,
            project_root=tmp_path,
        )
        started = time.perf_counter()
        with pytest.raises(EditorUnavailableError):
            LiveEditorBridge(config).validate_spec_content("{}")
        assert time.perf_counter() - started < 3.0
    finally:
        listener.close()


def test_validate_spec_is_never_ok_when_the_bridge_is_unavailable(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv("UESPEC_BRIDGE_MODE", "auto")
    monkeypatch.setenv("UESPEC_LIVE_URL", f"http://127.0.0.1:{_closed_port()}/mcp")
    monkeypatch.setenv("UESPEC_PROJECT_ROOT", str(tmp_path))
    spec = {"apiVersion": "0.2", "kind": "HUD", "name": "Plain", "root": {"id": "rootPanel", "type": "CanvasPanel", "children": []}}

    result = validate.validate_spec(json.dumps(spec))

    assert result["ok"] is False
    assert result["code"] == "BRIDGE_UNAVAILABLE"
    assert result["validationLevel"] == "schema-only"
    assert result["semanticValidation"]["bridgeCode"] == "EDITOR_UNAVAILABLE"
    assert result["errors"][0]["code"] == "BRIDGE_UNAVAILABLE"
    assert result["backend"] == "live" and result["correlationId"]


def test_429_is_retried_because_nothing_ran(editor: FakeEditor, tmp_path: Path) -> None:
    editor.rate_limit_remaining = 2
    editor.tools["uispec_validate"] = lambda args: _structured({"ok": True, "errors": []})

    result = _live(editor, tmp_path).validate_spec_content("{}")

    assert result["ok"] is True
    assert len(editor.calls("uispec_validate")) == 1


def test_task_id_reply_is_polled_through_get_task_status(editor: FakeEditor, tmp_path: Path) -> None:
    polls = {"count": 0}
    editor.tools["uispec_compile"] = lambda args: _structured({"task_id": "task-7", "status": "working", "tool": "uispec_compile"})

    def status(args: dict[str, Any]) -> dict[str, Any]:
        polls["count"] += 1
        if polls["count"] < 3:
            return _structured({"task_id": args["task_id"], "status": "working"})
        inner = _structured({"ok": True, "assetAction": "created", "generatedAssets": ["/Game/T/WBP_A.WBP_A"], "errors": []})
        return _structured({"task_id": args["task_id"], "status": "completed", "result": inner})

    editor.tools["get_task_status"] = status
    spec = tmp_path / "spec.json"
    spec.write_text("{}", encoding="utf-8")

    result = _live(editor, tmp_path).compile_spec(spec, "/Game/T")

    assert result["ok"] is True
    assert result["assetAction"] == "created"
    assert polls["count"] == 3
    assert editor.calls("get_task_status")[0] == {"task_id": "task-7"}
    assert editor.calls("cancel_task") == []
    assert result["artifacts"][0]["path"] == "/Game/T/WBP_A.WBP_A"


def test_failed_task_surfaces_the_tool_error(editor: FakeEditor, tmp_path: Path) -> None:
    editor.tools["uispec_compile"] = lambda args: _structured({"task_id": "task-8", "status": "working"})
    inner = _structured({"code": "INTERNAL", "message": "compile blew up"}, is_error=True)
    editor.tools["get_task_status"] = lambda args: _structured({"task_id": "task-8", "status": "failed", "result": inner})
    spec = tmp_path / "spec.json"
    spec.write_text("{}", encoding="utf-8")

    result = _live(editor, tmp_path).compile_spec(spec, "/Game/T")

    assert result["ok"] is False
    assert result["code"] == "INTERNAL"
    assert result["errors"][0]["message"] == "compile blew up"


def test_budget_exhaustion_cancels_the_task_and_returns_uncertain(editor: FakeEditor, tmp_path: Path) -> None:
    editor.tools["uispec_compile"] = lambda args: _structured({"task_id": "task-9", "status": "working"})
    editor.tools["get_task_status"] = lambda args: _structured({"task_id": "task-9", "status": "working"})
    editor.tools["cancel_task"] = lambda args: _structured({"ok": True})
    spec = tmp_path / "spec.json"
    spec.write_text("{}", encoding="utf-8")

    result = _live(editor, tmp_path, live_timeout_s=0.4).compile_spec(spec, "/Game/Target")

    assert result["ok"] is False
    assert result["outcome"] == "uncertain"
    assert result["code"] == "OUTCOME_UNCERTAIN"
    assert result["target"] == "/Game/Target"
    assert result["taskId"] == "task-9"
    assert result["cancelRequested"] is True
    assert editor.calls("cancel_task") == [{"task_id": "task-9"}]
    assert len(editor.calls("uispec_compile")) == 1  # never auto-retried


def test_synchronous_timeout_of_a_mutating_call_is_uncertain_and_not_retried(editor: FakeEditor, tmp_path: Path) -> None:
    editor.tools["uispec_compile"] = lambda args: _structured({"ok": True})
    editor.tool_delay_s = 2.5
    spec = tmp_path / "spec.json"
    spec.write_text("{}", encoding="utf-8")

    result = _live(editor, tmp_path, live_timeout_s=1.0).compile_spec(spec, "/Game/Slow")

    assert result["outcome"] == "uncertain"
    assert result["target"] == "/Game/Slow"
    assert result["taskId"] is None
    time.sleep(2.6)  # let the slow handler finish before asserting it ran exactly once
    assert len(editor.calls("uispec_compile")) == 1


def test_target_busy_error_keeps_its_code_and_holder(editor: FakeEditor, tmp_path: Path) -> None:
    editor.tools["uispec_compile"] = lambda args: _structured(
        {"code": "TARGET_BUSY", "mcpCode": -32003, "message": "busy", "heldBy": {"tool": "uispec_compile", "correlationId": "other"}},
        is_error=True,
    )
    spec = tmp_path / "spec.json"
    spec.write_text("{}", encoding="utf-8")

    result = _live(editor, tmp_path).compile_spec(spec, "/Game/T")

    assert result["ok"] is False
    assert result["code"] == "TARGET_BUSY"
    assert result["heldBy"]["correlationId"] == "other"
    assert result["errors"][0]["code"] == "TARGET_BUSY"


def test_unknown_tool_is_a_structured_rpc_error(editor: FakeEditor, tmp_path: Path) -> None:
    result = _live(editor, tmp_path).compile_spec(_write(tmp_path / "spec.json"), "/Game/T")

    assert result["ok"] is False
    assert result["code"] == "MCP_RPC_ERROR"


def _write(path: Path) -> Path:
    path.write_text("{}", encoding="utf-8")
    return path


def test_run_tests_maps_the_editor_reply_and_confines_junit_to_saved_uespec(editor: FakeEditor, tmp_path: Path) -> None:
    failure = tmp_path / "Saved" / "UESpec" / "TestReports" / "a.failure-report.json"
    failure.parent.mkdir(parents=True)
    failure.write_text(json.dumps({"failures": []}), encoding="utf-8")
    editor.tools["uispec_run_tests"] = lambda args: _structured(
        {
            "ok": True,
            "summary": {"total": 1, "passed": 1, "failed": 0},
            "junitPath": str(tmp_path / "Saved" / "UESpec" / "TestReports" / "a.junit.xml"),
            "failureReportPath": str(failure),
            "errors": [],
        }
    )
    bridge_under_test = _live(editor, tmp_path)
    test_spec = _write(tmp_path / "a.test.json")

    inside = tmp_path / "Saved" / "UESpec" / "TestReports" / "a.junit.xml"
    result = bridge_under_test.run_test_spec(test_spec, inside)
    outside = bridge_under_test.run_test_spec(test_spec, tmp_path / "elsewhere" / "a.junit.xml")

    assert result["ok"] is True and result["summary"]["passed"] == 1
    assert result["failureReport"] == {"failures": []}
    assert editor.calls("uispec_run_tests")[0]["junitPath"] == inside.resolve().as_posix()
    assert "junitPath" not in editor.calls("uispec_run_tests")[1]
    assert outside["ok"] is True
    assert {a["kind"] for a in result["artifacts"]} == {"junit", "failure-report"}


def test_support_surface_json_and_markdown(editor: FakeEditor, tmp_path: Path) -> None:
    editor.tools["uispec_capabilities"] = lambda args: _structured(
        {"ok": True, "supportSurface": {"converters": [{"name": "BoolToVisibility"}]}, "engineVersion": "5.8.0", "surfaceHash": "abc"}
    )
    live = _live(editor, tmp_path)

    as_json = live.get_support_surface(format="json")
    as_markdown = live.get_support_surface(format="markdown")

    assert as_json["content"]["converters"][0]["name"] == "BoolToVisibility"
    assert as_json["identity"]["surfaceHash"] == "abc"
    assert "BoolToVisibility" in as_markdown["content"]
    assert editor.calls("uispec_capabilities")[0]["includeSurface"] is True


# ---- commandlet refusal (FND-01098) and mode selection -------------------------------------------------------


def test_commandlet_mode_refuses_while_an_editor_is_alive(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setattr(bridge, "_live_editor_processes", lambda: ["UnrealEditor.exe"])
    client = BridgeClient(BridgeConfig(mode="commandlet", uproject_path=tmp_path / "P.uproject", project_root=tmp_path))

    with pytest.raises(LiveEditorRefusedError) as raised:
        client.compile_spec(_write(tmp_path / "spec.json"), "/Game/T")

    assert raised.value.code == "REFUSED_LIVE_EDITOR"
    assert "UnrealEditor.exe" in str(raised.value)
    assert "code=REFUSED_LIVE_EDITOR" in (tmp_path / "Saved" / "UESpec" / "logs" / "uespec_mcp.log").read_text(encoding="utf-8")


def test_commandlet_refusal_surfaces_through_the_compile_tool(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setattr(bridge, "_live_editor_processes", lambda: ["UnrealEditor-Cmd.exe"])
    monkeypatch.setenv("UESPEC_BRIDGE_MODE", "commandlet")
    monkeypatch.setenv("UESPEC_UPROJECT_PATH", str(tmp_path / "P.uproject"))

    result = compile_tools.compile_spec_to_wbp(str(_write(tmp_path / "spec.json")), "/Game/T")

    assert result["ok"] is False
    assert result["errors"][0]["code"] == "REFUSED_LIVE_EDITOR"
    assert result["backend"] == "commandlet"


def test_allow_live_editor_lets_the_commandlet_start(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setattr(bridge, "_live_editor_processes", lambda: ["UnrealEditor.exe"])
    client = BridgeClient(
        BridgeConfig(mode="commandlet", uproject_path=tmp_path / "P.uproject", project_root=tmp_path, allow_live_editor=True)
    )
    captured: dict[str, Any] = {}

    def fake_run(commandlet: str, extra_args: Any, **kwargs: Any) -> bridge.CommandletInvocation:
        captured["commandlet"] = commandlet
        return bridge.CommandletInvocation(ok=True, payload={"ok": True, "errors": []})

    monkeypatch.setattr(client, "_run_commandlet", fake_run)

    result = client.compile_spec(_write(tmp_path / "spec.json"), "/Game/T")

    assert result["ok"] is True and result["backend"] == "commandlet"
    assert captured["commandlet"] == "UESpecCompile"


def test_commandlet_launch_never_inherits_the_mcp_stdin(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """FND-01502: UnrealEditor-Cmd inherited the MCP stdio pipe and hung 600 s at LogPython."""
    client = BridgeClient(BridgeConfig(mode="commandlet", uproject_path=tmp_path / "P.uproject", project_root=tmp_path))
    monkeypatch.setattr(client, "_resolve_editor_cmd", lambda: tmp_path / "UnrealEditor-Cmd.exe")
    seen: list[dict[str, Any]] = []

    def fake_run(command: Any, **kwargs: Any) -> Any:
        seen.append(kwargs)
        return subprocess.CompletedProcess(command, 0, stdout="{}", stderr="")

    monkeypatch.setattr(bridge.subprocess, "run", fake_run)

    client._run_commandlet("UESpecCompile", [])

    assert seen and seen[0].get("stdin") is subprocess.DEVNULL


def test_editor_unavailable_hint_names_the_real_remedy(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    class _Down:
        def compile_spec(self, spec: Path, output_dir: str) -> Any:
            raise EditorUnavailableError("no editor answering on the MCP port")

    monkeypatch.setattr(compile_tools, "get_bridge", lambda: _Down())

    result = compile_tools.compile_spec_to_wbp(str(_write(tmp_path / "spec.json")), "/Game/T")

    hint = result["errors"][0]["hint"]
    assert "start the project editor" in hint and "UESPEC_BRIDGE_MODE=commandlet" in hint
    assert "/Game/UESpec/Generated" not in hint


@pytest.mark.parametrize(
    ("mode", "expected"),
    [(None, LiveEditorBridge), ("auto", LiveEditorBridge), ("live", LiveEditorBridge), ("commandlet", BridgeClient), ("mock", MockBridge)],
)
def test_mode_selection(monkeypatch: pytest.MonkeyPatch, mode: str | None, expected: type) -> None:
    if mode is not None:
        monkeypatch.setenv("UESPEC_BRIDGE_MODE", mode)

    assert type(bridge.get_bridge()) is expected


def test_project_config_file_supplies_paths_and_env_wins(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    (tmp_path / "UESpec").mkdir()
    (tmp_path / "UESpec" / "uespec.hf.json").write_text(
        json.dumps(
            {
                "projectPath": "PrjEfz.uproject",
                "enginePath": "E:/UE/Engine",
                "outputDir": "/Game/HF/UISpecDemo",
                "maxAttempts": 2,
                "mode": "commandlet",
                "allowLiveEditor": True,
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setenv("UESPEC_PROJECT_ROOT", str(tmp_path))

    config = BridgeConfig.from_env()
    assert config.mode == "commandlet"
    assert config.uproject_path == tmp_path / "PrjEfz.uproject"
    assert config.ue_engine_path == Path("E:/UE/Engine")
    assert config.output_dir == "/Game/HF/UISpecDemo"
    assert config.max_attempts == 2
    assert config.allow_live_editor is True

    monkeypatch.setenv("UESPEC_BRIDGE_MODE", "live")
    monkeypatch.setenv("UESPEC_ALLOW_LIVE_EDITOR", "false")
    config = BridgeConfig.from_env()
    assert config.mode == "live"
    assert config.allow_live_editor is False
