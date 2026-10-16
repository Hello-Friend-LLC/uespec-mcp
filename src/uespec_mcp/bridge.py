from __future__ import annotations

import contextvars
import csv
import datetime as _datetime
import functools
import io
import json
import os
import platform
import re
import shlex
import socket
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
import uuid
from difflib import get_close_matches
from pathlib import Path
from typing import Any, Callable, Sequence

from pydantic import BaseModel, Field


def structured_error(
    code: str,
    message: str,
    *,
    path: str = "$",
    hint: str = "",
    nearest_declared: Sequence[str] | None = None,
) -> dict[str, Any]:
    return {
        "code": code,
        "path": path,
        "message": message,
        "hint": hint,
        "nearestDeclared": list(nearest_declared or []),
    }


class BridgeError(RuntimeError):
    """Base bridge exception. ``code`` is the structured error code tools surface."""

    code = "BRIDGE_ERROR"

    # Filled in by the instrumentation wrapper so tools can echo them on an error result.
    backend: str | None = None
    correlation_id: str | None = None
    duration_ms: float | None = None


class BridgeConfigurationError(BridgeError):
    """Raised when required bridge configuration is missing."""


class BridgeExecutionError(BridgeError):
    """Raised when Unreal execution fails without a structured result."""


class UnsupportedBridgeModeError(BridgeError):
    """Raised when a configured transport exists only as a placeholder."""


class EditorUnavailableError(BridgeError):
    """Live mode/auto mode: no UnrealMCPServer answered ``initialize`` in time. The bridge never launches an editor."""

    code = "EDITOR_UNAVAILABLE"


class LiveEditorRefusedError(BridgeError):
    """Commandlet mode refuses to start while an editor process is alive (FND-01098)."""

    code = "REFUSED_LIVE_EDITOR"


class CommandletInvocation(BaseModel):
    ok: bool
    command: list[str] = Field(default_factory=list)
    stdout: str = ""
    stderr: str = ""
    outputPath: str | None = None
    payload: Any = None


PROJECT_CONFIG_RELATIVE_PATH = Path("UESpec") / "uespec.hf.json"
DEFAULT_LIVE_PORT = 13579
DEFAULT_LIVE_TIMEOUT_S = 120.0
LIVE_CONNECT_TIMEOUT_S = 5.0
BRIDGE_MODES = ("live", "commandlet", "mock", "auto")


def _truthy(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    return str(value or "").strip().lower() in {"1", "true", "yes", "on"}


def load_project_config(project_root: Path | None) -> dict[str, Any]:
    """Read ``<project>/UESpec/uespec.hf.json`` (projectPath, enginePath, outputDir, maxAttempts, ...). Missing/invalid -> {}."""
    if project_root is None:
        return {}
    path = project_root / PROJECT_CONFIG_RELATIVE_PATH
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return data if isinstance(data, dict) else {}


class BridgeConfig(BaseModel):
    uproject_path: Path | None = None
    ue_engine_path: Path | None = None
    mode: str = "commandlet"
    project_root: Path | None = None
    output_dir: str | None = None
    max_attempts: int | None = None
    allow_live_editor: bool = False
    live_timeout_s: float = DEFAULT_LIVE_TIMEOUT_S
    live_connect_timeout_s: float = LIVE_CONNECT_TIMEOUT_S
    live_url: str | None = None
    live_token: str | None = None

    @classmethod
    def from_env(cls) -> "BridgeConfig":
        """Environment wins over ``<project>/UESpec/uespec.hf.json``; the default mode is ``auto`` (live editor or a structured error)."""
        uproject_env = os.getenv("UESPEC_UPROJECT_PATH")
        root_env = os.getenv("UESPEC_PROJECT_ROOT")
        project_root: Path | None = None
        if root_env:
            project_root = Path(root_env).expanduser()
        elif uproject_env:
            project_root = Path(uproject_env).expanduser().parent
        file_config = load_project_config(project_root)

        raw_mode = (
            os.getenv("UESPEC_BRIDGE_MODE") or os.getenv("UESPEC_MCP_MODE") or str(file_config.get("mode") or "") or "auto"
        ).strip().lower() or "auto"

        engine_path = os.getenv("UESPEC_UE_ENGINE_PATH") or file_config.get("enginePath")
        uproject_value = uproject_env or file_config.get("projectPath")
        uproject_path: Path | None = None
        if uproject_value:
            uproject_path = Path(str(uproject_value)).expanduser()
            if not uproject_path.is_absolute() and project_root is not None:
                uproject_path = project_root / uproject_path
        elif project_root is not None and project_root.is_dir():
            candidates = sorted(project_root.glob("*.uproject"))
            uproject_path = candidates[0] if candidates else None

        max_attempts = file_config.get("maxAttempts")
        timeout_env = os.getenv("UESPEC_LIVE_TIMEOUT_S")
        timeout_value = timeout_env or file_config.get("liveTimeoutSeconds")
        allow_env = os.getenv("UESPEC_ALLOW_LIVE_EDITOR")
        return cls(
            uproject_path=uproject_path,
            ue_engine_path=Path(str(engine_path)).expanduser() if engine_path else None,
            mode=raw_mode,
            project_root=project_root,
            output_dir=(os.getenv("UESPEC_OUTPUT_DIR") or file_config.get("outputDir") or None),
            max_attempts=int(max_attempts) if isinstance(max_attempts, (int, float)) and not isinstance(max_attempts, bool) else None,
            allow_live_editor=_truthy(allow_env if allow_env is not None else file_config.get("allowLiveEditor")),
            live_timeout_s=float(timeout_value) if timeout_value else DEFAULT_LIVE_TIMEOUT_S,
            live_url=os.getenv("UESPEC_LIVE_URL") or None,
            live_token=os.getenv("UESPEC_LIVE_TOKEN") or None,
        )

    def resolved_project_root(self) -> Path | None:
        if self.project_root is not None:
            return self.project_root
        if self.uproject_path is not None:
            return self.uproject_path.expanduser().parent
        return None


# ---------------------------------------------------------------------------------------------
# Per-operation instrumentation: every bridge result carries backend/correlationId/durationMs/artifacts[],
# and every operation appends one line to <project>/Saved/UESpec/logs/uespec_mcp.log.
# ---------------------------------------------------------------------------------------------

_CORRELATION_ID: contextvars.ContextVar[str | None] = contextvars.ContextVar("uespec_correlation_id", default=None)


def current_correlation_id() -> str:
    return _CORRELATION_ID.get() or str(uuid.uuid4())


def _collect_artifacts(result: dict[str, Any]) -> list[dict[str, str]]:
    artifacts: list[dict[str, str]] = []
    seen: set[str] = set()

    def add(kind: str, value: Any) -> None:
        if isinstance(value, dict):
            value = value.get("assetPath") or value.get("path")
        if isinstance(value, str) and value and value not in seen:
            seen.add(value)
            artifacts.append({"kind": kind, "path": value})

    for entry in result.get("generatedAssets") or []:
        add("asset", entry)
    add("package-file", result.get("savedPackageFile"))
    add("junit", result.get("outputReport") or result.get("junitPath") or result.get("reportPath"))
    add("failure-report", result.get("failureReportPath"))
    add("png", result.get("pngPath"))
    return artifacts


def _result_code(result: Any) -> str:
    if not isinstance(result, dict):
        return "ok"
    if result.get("code"):
        return str(result["code"])
    if result.get("ok"):
        return "ok"
    errors = result.get("errors") or []
    if errors and isinstance(errors[0], dict) and errors[0].get("code"):
        return str(errors[0]["code"])
    return "failed"


def _log_operation(
    config: Any,
    route: str,
    backend: str,
    code: str,
    duration_ms: float,
    artifacts: int,
    correlation_id: str,
) -> None:
    try:
        root = config.resolved_project_root() if config is not None else None
        if root is None:
            return
        log_path = Path(root) / "Saved" / "UESpec" / "logs" / "uespec_mcp.log"
        log_path.parent.mkdir(parents=True, exist_ok=True)
        stamp = _datetime.datetime.now(_datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        line = (
            f"{stamp} route={route} backend={backend} project={Path(root).name} code={code} "
            f"ms={duration_ms:.1f} artifacts={artifacts} correlationId={correlation_id}\n"
        )
        with log_path.open("a", encoding="utf-8", newline="") as handle:
            handle.write(line)
    except OSError:
        pass


def _instrumented(route: str) -> Callable[[Callable[..., Any]], Callable[..., Any]]:
    """Stamp a bridge operation's result (or exception) and write the per-op log line.

    Accepts an optional ``correlation_id=`` keyword on the wrapped method; absent -> a fresh uuid4.
    """

    def decorator(func: Callable[..., Any]) -> Callable[..., Any]:
        @functools.wraps(func)
        def wrapper(self: Any, *args: Any, correlation_id: str | None = None, **kwargs: Any) -> Any:
            cid = correlation_id or str(uuid.uuid4())
            backend = getattr(self, "backend_name", "unknown")
            token = _CORRELATION_ID.set(cid)
            started = time.perf_counter()
            try:
                result = func(self, *args, **kwargs)
            except BridgeError as exc:
                elapsed = round((time.perf_counter() - started) * 1000.0, 1)
                exc.backend = backend
                exc.correlation_id = cid
                exc.duration_ms = elapsed
                _log_operation(getattr(self, "config", None), route, backend, exc.code, elapsed, 0, cid)
                raise
            finally:
                _CORRELATION_ID.reset(token)
            elapsed = round((time.perf_counter() - started) * 1000.0, 1)
            if isinstance(result, dict):
                if "durationMs" in result and "editorDurationMs" not in result:
                    result["editorDurationMs"] = result["durationMs"]
                result["backend"] = backend
                result["correlationId"] = cid
                result["durationMs"] = elapsed
                result["artifacts"] = _collect_artifacts(result)
                _log_operation(getattr(self, "config", None), route, backend, _result_code(result), elapsed, len(result["artifacts"]), cid)
            return result

        return wrapper

    return decorator


def bridge_error_fields(exc: BaseException) -> dict[str, Any]:
    """Envelope fields a tool echoes when a bridge call raised instead of returning."""
    return {
        "backend": getattr(exc, "backend", None),
        "correlationId": getattr(exc, "correlation_id", None) or str(uuid.uuid4()),
        "durationMs": getattr(exc, "duration_ms", None) or 0.0,
        "artifacts": [],
    }


def _live_editor_processes() -> list[str]:
    """Names of live ``UnrealEditor*`` processes (UnrealEditor.exe, UnrealEditor-Cmd.exe, ...). Unknown -> []."""
    try:
        if platform.system() == "Windows":
            completed = subprocess.run(
                ["tasklist", "/FO", "CSV", "/NH"], capture_output=True, text=True, timeout=20, check=False
            )
            names = [row[0] for row in csv.reader(io.StringIO(completed.stdout)) if row]
        else:
            completed = subprocess.run(["ps", "-A", "-o", "comm="], capture_output=True, text=True, timeout=20, check=False)
            names = [line.strip() for line in completed.stdout.splitlines() if line.strip()]
    except (OSError, subprocess.SubprocessError):
        return []
    return sorted({name for name in names if name.lower().startswith("unrealeditor")})


class BridgeClient:
    """Bridge between Python MCP tools and Unreal commandlets."""

    backend_name = "commandlet"
    preview_unavailable_code: str | None = "PREVIEW_UNAVAILABLE_COMMANDLET"

    def __init__(self, config: BridgeConfig | None = None) -> None:
        self.config = config or BridgeConfig.from_env()

    @_instrumented("surface")
    def get_support_surface(self, format: str = "markdown") -> dict[str, Any]:
        self._ensure_commandlet_mode()
        normalized_format = format.strip().lower()
        if normalized_format not in {"markdown", "json"}:
            raise BridgeConfigurationError("Support surface format must be 'markdown' or 'json'.")

        suffix = ".md" if normalized_format == "markdown" else ".json"
        with tempfile.TemporaryDirectory(prefix="uespec-mcp-surface-") as temp_dir:
            output_path = Path(temp_dir) / f"support_surface{suffix}"
            invocation = self._run_commandlet(
                self._commandlet_name("support_surface"),
                [f"-Format={normalized_format}", f"-Output={output_path}"],
                output_path=output_path,
                output_mode="text" if normalized_format == "markdown" else "json",
            )
        if not invocation.ok or invocation.payload is None:
            raise BridgeExecutionError(
                f"UESpec support surface commandlet failed.\nSTDOUT:\n{invocation.stdout}\nSTDERR:\n{invocation.stderr}"
            )
        return {
            "ok": True,
            "format": normalized_format,
            "content": invocation.payload,
            "command": invocation.command,
            "outputPath": invocation.outputPath,
        }

    @_instrumented("validate")
    def validate_spec_content(self, spec_content: str) -> dict[str, Any]:
        self._ensure_commandlet_mode()
        with tempfile.TemporaryDirectory(prefix="uespec-mcp-validate-") as temp_dir:
            temp_root = Path(temp_dir)
            spec_path = temp_root / "spec.json"
            output_path = temp_root / "validate-result.json"
            spec_path.write_text(spec_content, encoding="utf-8")
            invocation = self._run_commandlet(
                self._commandlet_name("validate"),
                [f"-SpecFile={spec_path}", f"-Output={output_path}"],
                output_path=output_path,
                output_mode="json",
            )
        if invocation.payload is None and not invocation.ok:
            raise BridgeExecutionError(
                f"UESpec validate commandlet failed.\nSTDOUT:\n{invocation.stdout}\nSTDERR:\n{invocation.stderr}"
            )
        return normalize_compiler_result(invocation.payload, invocation.command, invocation.outputPath)

    @_instrumented("compile")
    def compile_spec(self, spec_path: str | Path, output_dir: str) -> dict[str, Any]:
        self._ensure_commandlet_mode()
        spec_file = Path(spec_path).expanduser().resolve()
        normalized_output_dir = output_dir.strip()
        if not normalized_output_dir:
            raise BridgeConfigurationError("compile_spec requires a non-empty Unreal package output directory, for example '/Game/UESpec/Generated'.")

        with tempfile.TemporaryDirectory(prefix="uespec-mcp-compile-") as temp_dir:
            result_path = Path(temp_dir) / "compile-result.json"
            invocation = self._run_commandlet(
                self._commandlet_name("compile"),
                [
                    f"-SpecFile={spec_file}",
                    f"-OutputDir={normalized_output_dir}",
                    f"-Output={result_path}",
                ],
                output_path=result_path,
                output_mode="json",
            )
        if invocation.payload is None and not invocation.ok:
            raise BridgeExecutionError(
                f"UESpec compile commandlet failed.\nSTDOUT:\n{invocation.stdout}\nSTDERR:\n{invocation.stderr}"
            )
        result = normalize_compiler_result(invocation.payload, invocation.command, invocation.outputPath)
        result["specPath"] = str(spec_file)
        result["outputDir"] = normalized_output_dir
        return result

    def read_failure_report(self, report_path: str | Path) -> dict[str, Any]:
        resolved_path = Path(report_path).expanduser().resolve()
        if not resolved_path.is_file():
            return {}
        return json.loads(resolved_path.read_text(encoding="utf-8"))

    @_instrumented("test")
    def run_test_spec(
        self,
        spec_path: str | Path,
        output_report: str | Path,
        failure_report_path: str | Path | None = None,
    ) -> dict[str, Any]:
        self._ensure_commandlet_mode()
        spec_file = Path(spec_path).expanduser().resolve()
        report_path = Path(output_report).expanduser().resolve()
        report_path.parent.mkdir(parents=True, exist_ok=True)
        resolved_failure_report_path = (
            Path(failure_report_path).expanduser().resolve()
            if failure_report_path is not None
            else report_path.with_name(f"{report_path.stem.removesuffix('.junit')}.failure-report.json")
        )
        resolved_failure_report_path.parent.mkdir(parents=True, exist_ok=True)

        with tempfile.TemporaryDirectory(prefix="uespec-mcp-test-") as temp_dir:
            result_path = Path(temp_dir) / "test-result.json"
            invocation = self._run_commandlet(
                self._commandlet_name("test"),
                [
                    f"-SpecFile={spec_file}",
                    f"-Report={report_path}",
                    f"-JsonReport={resolved_failure_report_path}",
                    f"-Output={result_path}",
                ],
                output_path=result_path,
                output_mode="json",
            )
        if invocation.payload is None and not invocation.ok:
            raise BridgeExecutionError(
                f"UESpec test commandlet failed.\nSTDOUT:\n{invocation.stdout}\nSTDERR:\n{invocation.stderr}"
            )
        payload = invocation.payload if isinstance(invocation.payload, dict) else {}
        return {
            "ok": bool(payload.get("ok", invocation.ok)),
            "specPath": str(spec_file),
            "outputReport": str(report_path),
            "failureReportPath": str(resolved_failure_report_path),
            "summary": payload.get("summary", {}),
            "errors": [normalize_error(entry) for entry in payload.get("errors", [])],
            "failureReport": self.read_failure_report(resolved_failure_report_path),
            "rawResult": payload,
            "command": invocation.command,
            "outputPath": invocation.outputPath,
        }

    def _ensure_commandlet_mode(self) -> None:
        mode = self.config.mode.strip().lower()
        if mode == "commandlet":
            if not self.config.allow_live_editor:
                running = _live_editor_processes()
                if running:
                    raise LiveEditorRefusedError(
                        f"Refusing to start a headless UnrealEditor-Cmd commandlet while an editor process is alive "
                        f"({', '.join(running)}); a headless run beside a live editor stalls the host (FND-01098). "
                        f"Use UESPEC_BRIDGE_MODE=live, close the editor, or set allow_live_editor=true."
                    )
            return
        if mode == "socket":
            raise UnsupportedBridgeModeError(
                "Socket mode is reserved for a future low-latency bridge and is not implemented in this skeleton yet."
            )
        raise BridgeConfigurationError(f"Unsupported UESpec MCP mode '{self.config.mode}'.")

    def _commandlet_name(self, kind: str) -> str:
        env_name = {
            "support_surface": "UESPEC_SUPPORT_SURFACE_COMMANDLET",
            "validate": "UESPEC_VALIDATE_COMMANDLET",
            "compile": "UESPEC_COMPILE_COMMANDLET",
            "test": "UESPEC_TEST_COMMANDLET",
        }[kind]
        defaults = {
            "support_surface": "UESpecDumpSupportSurface",
            "validate": "UESpecValidate",
            "compile": "UESpecCompile",
            "test": "UESpecRunTests",
        }
        return os.getenv(env_name, defaults[kind]).strip()

    def _run_commandlet(
        self,
        commandlet: str,
        extra_args: Sequence[str],
        *,
        output_path: Path | None = None,
        output_mode: str | None = None,
    ) -> CommandletInvocation:
        editor_cmd = self._resolve_editor_cmd()
        uproject_path = self._require_uproject_path()
        command = [
            str(editor_cmd),
            str(uproject_path),
            "-unattended",
            "-LiveCoding=false",
            "-NoSplash",
            "-NoP4",
            "-NoSound",
            "-NullRHI",
            f"-run={commandlet}",
            *[str(arg) for arg in extra_args],
        ]

        completed = subprocess.run(
            command,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            cwd=str(uproject_path.parent),
            timeout=600,
            check=False,
        )

        payload = self._read_output_payload(output_path, output_mode, completed.stdout)
        return CommandletInvocation(
            ok=completed.returncode == 0,
            command=command,
            stdout=completed.stdout,
            stderr=completed.stderr,
            outputPath=str(output_path) if output_path else None,
            payload=payload,
        )

    def _read_output_payload(self, output_path: Path | None, output_mode: str | None, stdout: str) -> Any:
        if output_mode is None:
            return None

        if output_path and output_path.is_file():
            if output_mode == "json":
                return json.loads(output_path.read_text(encoding="utf-8"))
            return output_path.read_text(encoding="utf-8")

        text = stdout.strip()
        if not text:
            return None
        if output_mode == "json":
            try:
                return json.loads(text)
            except json.JSONDecodeError:
                return None
        return text

    def _require_uproject_path(self) -> Path:
        if self.config.uproject_path is None:
            raise BridgeConfigurationError("UESPEC_UPROJECT_PATH is required for Unreal commandlet mode.")
        return self.config.uproject_path.expanduser().resolve()

    def _resolve_editor_cmd(self) -> Path:
        direct_override = os.getenv("UESPEC_UE_EDITOR_CMD")
        if direct_override:
            direct_path = Path(direct_override).expanduser().resolve()
            if direct_path.is_file():
                return direct_path
            raise BridgeConfigurationError(
                f"UESPEC_UE_EDITOR_CMD points to '{direct_path}', but that file does not exist."
            )

        candidates: list[Path] = []
        if self.config.ue_engine_path is not None:
            candidates.extend(self._expand_engine_path_candidates(self.config.ue_engine_path.expanduser()))

        project_dir = self._require_uproject_path().parent
        current = project_dir
        for _ in range(4):
            candidates.extend(self._expand_engine_path_candidates(current / "Engine"))
            if current.parent == current:
                break
            current = current.parent

        seen: set[str] = set()
        for candidate in candidates:
            key = str(candidate).lower()
            if key in seen:
                continue
            seen.add(key)
            if candidate.is_file():
                return candidate.resolve()

        raise BridgeConfigurationError(
            "Could not find UnrealEditor-Cmd. Set UESPEC_UE_ENGINE_PATH to the engine root or UESPEC_UE_EDITOR_CMD to the executable."
        )

    def _expand_engine_path_candidates(self, engine_path: Path) -> list[Path]:
        resolved = engine_path.resolve(strict=False)
        if resolved.is_file():
            return [resolved]

        binary_name = {
            "Windows": "UnrealEditor-Cmd.exe",
            "Darwin": "UnrealEditor-Cmd",
        }.get(platform.system(), "UnrealEditor-Cmd")
        platform_dir = {
            "Windows": "Win64",
            "Darwin": "Mac",
        }.get(platform.system(), "Linux")

        return [
            resolved / "Binaries" / platform_dir / binary_name,
            resolved / "Engine" / "Binaries" / platform_dir / binary_name,
        ]


class MockBridge:
    """Offline bridge for eval smoke runs when Unreal is unavailable.

    The registered surface mirrors the UESpec plugin (hf-5-8, WI-1586 t210): the component, converter,
    signal and action lists below are the plugin's, not invented. Error codes are a subset of the
    plugin's; the mock cannot emit codes that need a live Unreal type system (INVALID_CONVERTER).
    """

    _WIDGET_TYPES = {
        "Border",
        "Button",
        "CanvasPanel",
        "ChatLine",
        "DamageNumber",
        "EquipmentPanel",
        "HealthBar",
        "HorizontalBox",
        "Image",
        "ItemSlot",
        "ItemTooltip",
        "Overlay",
        "PlayerNamePlate",
        "ProgressBar",
        "QuestEntry",
        "RedDot",
        "ResourceBar",
        "SizeBox",
        "SkillSlot",
        "StatusEffectIcon",
        "TextBlock",
        "VerticalBox",
    }
    _CONVERTERS = {"BoolToVisibility", "LocText", "Negate", "Percent", "RarityColor", "Stringify", "TimeFormat"}
    _SIGNALS = {
        "onChatMessageReceived",
        "onDamageNumberSpawned",
        "onHealthChanged",
        "onItemEquipped",
        "onItemHovered",
        "onQuestTracked",
        "onResourceChanged",
        "onSkillUsed",
        "onStatusEffectAdded",
        "onTargetChanged",
        "onTooltipShown",
    }
    # (name, status) pairs as the plugin's GetActionEventSurface / GetActionPrimitiveSurface report them.
    _ACTION_EVENTS = (
        ("Clicked", "supported"),
        ("Pressed", "supported"),
        ("Released", "supported"),
        ("Hovered", "supported"),
        ("Unhovered", "supported"),
        ("FocusReceived", "unimplemented"),
        ("FocusLost", "unimplemented"),
        ("DoubleClicked", "unimplemented"),
        ("LongPressed", "unimplemented"),
        ("DragDetected", "unimplemented"),
        ("Drop", "unimplemented"),
    )
    _ACTION_PRIMITIVES = (
        ("openLayer", "supported"),
        ("closeLayer", "supported"),
        ("setState", "supported"),
        ("callVM", "supported"),
        ("emitSignal", "supported"),
        ("playAnim", "unimplemented"),
    )
    # Widgets with no text or percent destination: a `content` bind on them is UNSUPPORTED_BINDING_TARGET.
    _NON_CONTENT_BINDABLE = {
        "Border",
        "Button",
        "CanvasPanel",
        "HorizontalBox",
        "Image",
        "Overlay",
        "SizeBox",
        "VerticalBox",
    }
    _BINDING_TARGETS = ("content", "visibility")

    backend_name = "mock"
    preview_unavailable_code: str | None = "PREVIEW_UNAVAILABLE_MOCK"

    def __init__(self, config: BridgeConfig | None = None) -> None:
        self.config = config or BridgeConfig.from_env()

    @_instrumented("surface")
    def get_support_surface(self, format: str = "markdown") -> dict[str, Any]:
        normalized_format = format.strip().lower()
        content = self._support_surface_json()
        if normalized_format == "json":
            return {"ok": True, "format": "json", "content": content, "command": ["mock"]}
        if normalized_format != "markdown":
            raise BridgeConfigurationError("Support surface format must be 'markdown' or 'json'.")
        lines = [
            "# Mock UESpec Support Surface",
            "",
            "## Widgets",
            ", ".join(sorted(self._WIDGET_TYPES)),
            "",
            "## Converters",
            ", ".join(sorted(self._CONVERTERS)),
            "",
            "## Signals",
            ", ".join(sorted(self._SIGNALS)),
            "",
            "## Actions",
            "### Events",
            *[_format_action_entry(name, status) for name, status in self._ACTION_EVENTS],
            "",
            "### Primitives",
            *[_format_action_entry(name, status) for name, status in self._ACTION_PRIMITIVES],
        ]
        return {"ok": True, "format": "markdown", "content": "\n".join(lines), "command": ["mock"]}

    @_instrumented("validate")
    def validate_spec_content(self, spec_content: str) -> dict[str, Any]:
        try:
            parsed = json.loads(spec_content)
        except json.JSONDecodeError as exc:
            return {
                "ok": False,
                "stage": "semantic",
                "normalizedJson": None,
                "errors": [structured_error("INVALID_JSON", exc.msg, path=f"$[{exc.lineno}:{exc.colno}]")],
                "semanticValidation": {"status": "mock"},
            }
        errors = self._collect_action_errors(parsed)
        return {
            "ok": not errors,
            "stage": "semantic",
            "normalizedJson": json.dumps(parsed, ensure_ascii=False, indent=2, sort_keys=True),
            "errors": errors,
            "semanticValidation": {"status": "mock"},
        }

    @_instrumented("compile")
    def compile_spec(self, spec_path: str | Path, output_dir: str) -> dict[str, Any]:
        spec_file = Path(spec_path).expanduser().resolve()
        try:
            document = json.loads(spec_file.read_text(encoding="utf-8-sig"))
        except json.JSONDecodeError as exc:
            return self._compile_result(
                spec_file,
                output_dir,
                errors=[structured_error("INVALID_JSON", exc.msg, path=f"$[{exc.lineno}:{exc.colno}]")],
            )

        errors = self._collect_compile_errors(document)
        return self._compile_result(spec_file, output_dir, errors=errors, document=document)

    def read_failure_report(self, report_path: str | Path) -> dict[str, Any]:
        resolved_path = Path(report_path).expanduser().resolve()
        if not resolved_path.is_file():
            return {}
        return json.loads(resolved_path.read_text(encoding="utf-8"))

    @_instrumented("test")
    def run_test_spec(
        self,
        spec_path: str | Path,
        output_report: str | Path,
        failure_report_path: str | Path | None = None,
    ) -> dict[str, Any]:
        spec_file = Path(spec_path).expanduser().resolve()
        report_path = Path(output_report).expanduser().resolve()
        report_path.parent.mkdir(parents=True, exist_ok=True)
        resolved_failure_report_path = (
            Path(failure_report_path).expanduser().resolve()
            if failure_report_path is not None
            else report_path.with_name(f"{report_path.stem.removesuffix('.junit')}.failure-report.json")
        )
        resolved_failure_report_path.parent.mkdir(parents=True, exist_ok=True)

        mock_result_path = spec_file.parent / "mock-test-result.json"
        payload = (
            json.loads(mock_result_path.read_text(encoding="utf-8"))
            if mock_result_path.is_file()
            else {
                "ok": True,
                "summary": {"total": 1, "pass": 1, "fail": 0, "skip": 0},
                "errors": [],
                "failureReport": {"summary": {"total": 1, "pass": 1, "fail": 0, "skip": 0}, "failures": []},
            }
        )
        failure_report = payload.get("failureReport") or {"summary": payload.get("summary", {}), "failures": []}
        report_path.write_text("<testsuite tests=\"1\" />\n", encoding="utf-8")
        resolved_failure_report_path.write_text(json.dumps(failure_report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        return {
            "ok": bool(payload.get("ok")),
            "specPath": str(spec_file),
            "outputReport": str(report_path),
            "failureReportPath": str(resolved_failure_report_path),
            "summary": payload.get("summary", {}),
            "errors": [normalize_error(entry) for entry in payload.get("errors", [])],
            "failureReport": failure_report,
            "rawResult": payload,
            "command": ["mock"],
            "outputPath": None,
        }

    def _support_surface_json(self) -> dict[str, Any]:
        return {
            "SupportSurfaceVersion": "mock",
            "BuiltInUMGWidgets": [{"Type": name} for name in sorted(self._WIDGET_TYPES)],
            "CommonUIWidgets": [],
            "RegisteredComponents": [],
            "Converters": [{"Name": name} for name in sorted(self._CONVERTERS)],
            "StyleTokens": [],
            "Signals": [{"Name": name} for name in sorted(self._SIGNALS)],
            "ActionEvents": [{"Name": name, "Status": status} for name, status in self._ACTION_EVENTS],
            "ActionPrimitives": [{"Name": name, "Status": status} for name, status in self._ACTION_PRIMITIVES],
        }

    def _collect_action_errors(self, document: Any) -> list[dict[str, Any]]:
        """Validation-stage errors: unimplemented action events and primitives (plugin: ValidateActions)."""
        errors: list[dict[str, Any]] = []
        root = document.get("root") if isinstance(document, dict) else None
        if isinstance(root, dict):
            self._collect_widget_action_errors(root, "$.root", errors)
        return errors

    def _collect_widget_action_errors(self, widget: dict[str, Any], path: str, errors: list[dict[str, Any]]) -> None:
        unimplemented_events = {name for name, status in self._ACTION_EVENTS if status != "supported"}
        unimplemented_primitives = {name for name, status in self._ACTION_PRIMITIVES if status != "supported"}
        supported_events = [name for name, status in self._ACTION_EVENTS if status == "supported"]
        supported_primitives = [name for name, status in self._ACTION_PRIMITIVES if status == "supported"]

        actions = widget.get("actions", [])
        for action_index, action in enumerate(actions if isinstance(actions, list) else []):
            if not isinstance(action, dict):
                continue
            action_path = f"{path}.actions[{action_index}]"
            event = action.get("on")
            if isinstance(event, str) and event in unimplemented_events:
                errors.append(
                    structured_error(
                        "UNSUPPORTED_EVENT",
                        f"Action event '{event}' is declared in the schema but not implemented by the compiler.",
                        path=f"{action_path}.on",
                        hint=f"Use {', '.join(supported_events)}.",
                        nearest_declared=supported_events,
                    )
                )
            action_block = action.get("do")
            if isinstance(action_block, dict):
                for primitive in action_block:
                    if primitive in unimplemented_primitives:
                        errors.append(
                            structured_error(
                                "UNSUPPORTED_ACTION",
                                f"Action primitive '{primitive}' is declared in the schema but not implemented by the compiler.",
                                path=f"{action_path}.do.{primitive}",
                                hint=f"Use {', '.join(supported_primitives)}.",
                                nearest_declared=supported_primitives,
                            )
                        )

        children = widget.get("children", [])
        for index, child in enumerate(children if isinstance(children, list) else []):
            if isinstance(child, dict):
                self._collect_widget_action_errors(child, f"{path}.children[{index}]", errors)

    def _collect_compile_errors(self, document: dict[str, Any]) -> list[dict[str, Any]]:
        errors: list[dict[str, Any]] = self._collect_action_errors(document)
        view_model = document.get("viewModel") if isinstance(document, dict) else None
        declared_methods = {
            str(method.get("name"))
            for method in (view_model.get("methods", []) if isinstance(view_model, dict) else [])
            if isinstance(method, dict) and method.get("name")
        }
        declared_fields = {
            str(field.get("name"))
            for field in (view_model.get("fields", []) if isinstance(view_model, dict) else [])
            if isinstance(field, dict) and field.get("name")
        }
        root = document.get("root") if isinstance(document, dict) else None
        if isinstance(root, dict):
            self._collect_widget_errors(root, "$.root", declared_methods, errors, declared_fields)
        return errors

    def _collect_binding_errors(
        self,
        widget: dict[str, Any],
        path: str,
        declared_members: set[str],
        errors: list[dict[str, Any]],
    ) -> None:
        """Compile-stage binding errors (plugin: UESpecBindingBuilder). Only `content` and `visibility` are bound."""
        widget_type = str(widget.get("type") or "")
        for target in self._BINDING_TARGETS:
            expression = widget.get(target)
            if not isinstance(expression, dict) or not isinstance(expression.get("bind"), str):
                continue
            target_path = f"{path}.{target}"
            bind_path = expression["bind"]

            if target == "content" and widget_type in self._NON_CONTENT_BINDABLE:
                errors.append(
                    structured_error(
                        "UNSUPPORTED_BINDING_TARGET",
                        f"Widget '{widget.get('id')}' has no bindable destination for '{bind_path}'.",
                        path=target_path,
                        hint="Bind content only on a TextBlock (Text) or ProgressBar (Percent); visibility binds on any widget.",
                    )
                )

            if any(char in bind_path for char in "[]()"):
                errors.append(
                    structured_error(
                        "UNSUPPORTED_BINDING_PATH",
                        f"Bind path '{bind_path}' uses an index or call expression ([ ] ( )), which bindings do not support.",
                        path=f"{target_path}.bind",
                        hint="Bind a flat ViewModel field (for example expose the indexed value as its own field on the ViewModel).",
                    )
                )
                continue

            # An undeclared ViewModel cannot be checked; with fields declared, the first segment must resolve.
            head = bind_path.removeprefix("vm.").split(".", 1)[0]
            if declared_members and head not in declared_members:
                errors.append(
                    structured_error(
                        "UNRESOLVED_BINDING_PATH",
                        f"Bind path '{bind_path}' does not resolve to a property of the ViewModel.",
                        path=f"{target_path}.bind",
                        hint="Check the field name and casing against the viewModel fields.",
                        nearest_declared=get_close_matches(head, sorted(declared_members), n=3),
                    )
                )

            converter = expression.get("convert")
            if converter and str(converter) not in self._CONVERTERS:
                errors.append(
                    structured_error(
                        "UNKNOWN_CONVERTER",
                        f"Converter '{converter}' is not registered.",
                        path=f"{target_path}.convert",
                        nearest_declared=get_close_matches(str(converter), sorted(self._CONVERTERS), n=3),
                    )
                )

    def _collect_widget_errors(
        self,
        widget: dict[str, Any],
        path: str,
        declared_methods: set[str],
        errors: list[dict[str, Any]],
        declared_fields: set[str] | None = None,
    ) -> None:
        widget_type = str(widget.get("type") or "")
        if widget_type and widget_type not in self._WIDGET_TYPES:
            errors.append(
                structured_error(
                    "UNKNOWN_WIDGET_TYPE",
                    f"Widget type '{widget_type}' is not registered.",
                    path=f"{path}.type",
                    nearest_declared=get_close_matches(widget_type, sorted(self._WIDGET_TYPES), n=3),
                )
            )

        self._collect_binding_errors(widget, path, (declared_fields | declared_methods) if declared_fields else set(), errors)

        actions = widget.get("actions", [])
        if isinstance(actions, list):
            for action_index, action in enumerate(actions):
                if not isinstance(action, dict):
                    continue
                expression = str(action.get("do") or "")
                method = _extract_vm_method_name(expression)
                if method and declared_methods and method not in declared_methods:
                    errors.append(
                        structured_error(
                            "VM_METHOD_NOT_DECLARED",
                            f"ViewModel method '{method}' is not declared.",
                            path=f"{path}.actions[{action_index}].do",
                            nearest_declared=get_close_matches(method, sorted(declared_methods), n=3),
                        )
                    )

        children = widget.get("children", [])
        if isinstance(children, list):
            for index, child in enumerate(children):
                if isinstance(child, dict):
                    self._collect_widget_errors(child, f"{path}.children[{index}]", declared_methods, errors, declared_fields)

    def _compile_result(
        self,
        spec_file: Path,
        output_dir: str,
        *,
        errors: list[dict[str, Any]],
        document: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        normalized_output_dir = output_dir.strip() or "/Game/UESpec/Generated"
        generated_assets: list[str] = []
        if not errors:
            name = _safe_asset_name(str((document or {}).get("name") or spec_file.stem))
            generated_assets.append(f"{normalized_output_dir.rstrip('/')}/WBP_{name}")
        return {
            "ok": not errors,
            "normalizedJson": json.dumps(document, ensure_ascii=False, indent=2, sort_keys=True) if document else None,
            "errors": errors,
            "generatedAssets": generated_assets,
            "rawResult": {"ok": not errors, "errors": errors, "generatedAssets": generated_assets, "bridgeMode": "mock"},
            "command": ["mock"],
            "outputPath": None,
            "specPath": str(spec_file),
            "outputDir": normalized_output_dir,
        }


def _format_action_entry(name: str, status: str) -> str:
    # Same shape as the plugin's markdown dumper: unimplemented entries carry their status.
    return f"- {name}" if status == "supported" else f"- {name} ({status})"


def _extract_vm_method_name(expression: str) -> str | None:
    normalized = expression.strip()
    if not normalized.startswith("vm.") or "(" not in normalized:
        return None
    return normalized[3: normalized.find("(")].strip() or None


def _safe_asset_name(value: str) -> str:
    safe = "".join(char if char.isalnum() or char == "_" else "_" for char in value.strip())
    return safe or "Generated"


def normalize_error(raw_error: Any) -> dict[str, Any]:
    if isinstance(raw_error, dict):
        return structured_error(
            raw_error.get("code") or raw_error.get("Code") or "UNKNOWN_ERROR",
            raw_error.get("message") or raw_error.get("Message") or "Unknown error.",
            path=raw_error.get("path") or raw_error.get("Path") or "$",
            hint=raw_error.get("hint") or raw_error.get("Hint") or "",
            nearest_declared=raw_error.get("nearestDeclared")
            or raw_error.get("NearestDeclared")
            or [],
        )
    return structured_error("UNKNOWN_ERROR", str(raw_error))


def normalize_compiler_result(
    payload: Any,
    command: Sequence[str] | None = None,
    output_path: str | None = None,
) -> dict[str, Any]:
    data = payload if isinstance(payload, dict) else {}
    errors = data.get("errors") or data.get("Errors") or []
    generated_assets = data.get("generatedAssets") or data.get("GeneratedAssets") or []
    ok_value = data.get("ok", data.get("bOk"))
    if ok_value is None:
        ok_value = not bool(errors)
    return {
        "ok": bool(ok_value),
        "normalizedJson": data.get("normalizedJson") or data.get("NormalizedJson"),
        "errors": [normalize_error(entry) for entry in errors],
        "generatedAssets": list(generated_assets),
        "rawResult": data,
        "command": list(command or []),
        "outputPath": output_path,
    }


# ---------------------------------------------------------------------------------------------
# Live editor bridge: Streamable-HTTP MCP client for HF UnrealMCPServer (uispec_* tools, hf_uispec provider).
# Mirrors claude-plugin-hf-automation tools/wi-helpers/ue_driver.py `MCP` (initialize, Mcp-Session-Id, bearer
# token, 429 backoff) without importing it.
# ---------------------------------------------------------------------------------------------

# A 429 means MaxRequestsPerMinute rejected the request before dispatch: nothing ran, so retrying is safe.
RATE_LIMIT_BACKOFF_S: tuple[float, ...] = (2, 4, 8, 16, 32)
POLL_INTERVALS_S: tuple[float, ...] = (0.5, 1.0, 1.0, 2.0)
_SLEEP = time.sleep  # module-level so tests can stub them
_MONOTONIC = time.monotonic
NO_AUTH_PLACEHOLDER = "no-auth-token-required"
DEFAULT_EPIC_URL = "http://127.0.0.1:8000/mcp"
PREVIEW_TOOLSET = "VibeUE.WidgetService"
PREVIEW_TOOL = "capture_preview_state"
ROUTE_HF_BRIDGE = "hf-bridge"
ROUTE_EPIC_SERVER = "epic-8000"
DEFAULT_PREVIEW_WIDTH = 1280
DEFAULT_PREVIEW_HEIGHT = 720
_TOOL_DONE_STATES = {"completed", "failed"}


class _McpTimeout(Exception):
    """The request was sent (or connecting stalled) and no answer arrived in time."""


class _McpTransportError(Exception):
    def __init__(self, message: str, status: int | None = None) -> None:
        super().__init__(message)
        self.status = status


class _McpRpcError(Exception):
    def __init__(self, error: Any) -> None:
        super().__init__(json.dumps(error) if not isinstance(error, str) else error)
        self.error = error


class _McpHttpClient:
    def __init__(self, url: str, token: str) -> None:
        self.url = url
        self.token = token
        self.session_id: str | None = None
        self.next_id = 1

    def _post(self, payload: dict[str, Any], timeout: float) -> dict[str, Any] | None:
        body = json.dumps(payload).encode("utf-8")
        raw = ""
        headers: dict[str, str] = {}
        for delay in (*RATE_LIMIT_BACKOFF_S, None):
            request = urllib.request.Request(self.url, data=body, method="POST")
            request.add_header("Content-Type", "application/json")
            request.add_header("Accept", "application/json, text/event-stream")
            request.add_header("Authorization", f"Bearer {self.token}")
            if self.session_id:
                request.add_header("Mcp-Session-Id", self.session_id)
            try:
                with urllib.request.urlopen(request, timeout=timeout) as response:
                    headers = dict(response.headers)
                    raw = response.read().decode("utf-8", errors="replace")
                break
            except urllib.error.HTTPError as exc:
                if exc.code == 429 and delay is not None:
                    _SLEEP(delay)
                    continue
                raise _McpTransportError(f"HTTP {exc.code} from {self.url}", status=exc.code) from exc
            except (TimeoutError, socket.timeout) as exc:
                raise _McpTimeout(str(exc) or "timed out") from exc
            except urllib.error.URLError as exc:
                if isinstance(exc.reason, (TimeoutError, socket.timeout)):
                    raise _McpTimeout(str(exc.reason) or "timed out") from exc
                raise _McpTransportError(str(exc.reason)) from exc
            except OSError as exc:
                raise _McpTransportError(str(exc)) from exc
        session_id = headers.get("Mcp-Session-Id") or headers.get("mcp-session-id")
        if session_id:
            self.session_id = session_id
        text = raw.strip()
        if not text:
            return None
        if text.startswith("event:") or text.startswith("data:"):
            for line in text.splitlines():
                if line.startswith("data:"):
                    return json.loads(line[5:].strip())
            return None
        return json.loads(text)

    def request(self, method: str, params: dict[str, Any] | None, timeout: float) -> dict[str, Any]:
        payload: dict[str, Any] = {"jsonrpc": "2.0", "id": self.next_id, "method": method}
        self.next_id += 1
        if params is not None:
            payload["params"] = params
        try:
            data = self._post(payload, timeout)
        except json.JSONDecodeError as exc:
            raise _McpTransportError(f"non-JSON reply to {method}: {exc}") from exc
        if data is None:
            raise _McpTransportError(f"empty reply to {method}")
        if "error" in data:
            raise _McpRpcError(data["error"])
        result = data.get("result", {})
        return result if isinstance(result, dict) else {}

    def initialize(self, timeout: float) -> None:
        self.request(
            "initialize",
            {"protocolVersion": "2025-06-18", "capabilities": {}, "clientInfo": {"name": "uespec_mcp", "version": "1.0"}},
            timeout,
        )
        self._post({"jsonrpc": "2.0", "method": "notifications/initialized"}, timeout)

    def call_tool(self, name: str, arguments: dict[str, Any], timeout: float) -> dict[str, Any]:
        return self.request("tools/call", {"name": name, "arguments": arguments}, timeout)


def _extract_payload(result: Any) -> tuple[dict[str, Any], bool]:
    """(payload dict, isError) from an MCP tools/call result: structuredContent first, else JSON text content."""
    if not isinstance(result, dict):
        return {}, False
    is_error = bool(result.get("isError"))
    structured = result.get("structuredContent")
    if isinstance(structured, dict):
        return structured, is_error
    texts: list[str] = []
    for item in result.get("content") or []:
        if isinstance(item, dict) and item.get("type") == "text" and isinstance(item.get("text"), str):
            texts.append(item["text"])
    for text in texts:
        try:
            parsed = json.loads(text)
        except json.JSONDecodeError:
            continue
        if isinstance(parsed, dict):
            return parsed, is_error
    return ({"message": "\n".join(texts)} if texts else {}), is_error


def _is_within(path: Path, root: Path) -> bool:
    try:
        path.resolve().relative_to(root.resolve())
    except ValueError:
        return False
    return True


class LiveEditorBridge:
    """Talks to the running PrjEfz editor through HF UnrealMCPServer; never launches or kills an editor.

    ``mode=live`` waits up to the call budget for ``initialize``; ``mode=auto`` gives it ``live_connect_timeout_s``
    (5 s) and otherwise answers a structured ``EDITOR_UNAVAILABLE``. Calls are synchronous on the editor game
    thread today (FND-01496); a reply carrying ``task_id`` is polled through ``get_task_status`` inside the same
    budget, and on exhaustion the task is cancelled and the result is ``outcome: "uncertain"`` (a mutating call
    is never retried).
    """

    backend_name = "live"
    preview_unavailable_code: str | None = None

    def __init__(self, config: BridgeConfig | None = None) -> None:
        self.config = config or BridgeConfig.from_env()

    # -- endpoint -------------------------------------------------------------------------------

    def _endpoint(self) -> tuple[str, str]:
        if self.config.live_url:
            return self.config.live_url, self.config.live_token or NO_AUTH_PLACEHOLDER
        root = self.config.resolved_project_root()
        if root is None:
            raise BridgeConfigurationError(
                "Live mode needs the project: set UESPEC_PROJECT_ROOT (or UESPEC_UPROJECT_PATH) so Config/DefaultUnrealMCPServer.ini can be read."
            )
        port = DEFAULT_LIVE_PORT
        token: str | None = None
        require_auth = False
        ini_path = Path(root) / "Config" / "DefaultUnrealMCPServer.ini"
        try:
            ini_lines = ini_path.read_text(encoding="utf-8", errors="replace").splitlines()
        except OSError:
            ini_lines = []
        for line in ini_lines:
            stripped = line.strip()
            match = re.match(r"ServerPort\s*=\s*(\d+)", stripped, re.IGNORECASE)
            if match:
                port = int(match.group(1))
            match = re.match(r"\+?AuthTokens\s*=\s*(\S+)", stripped, re.IGNORECASE)
            if match and token is None:
                token = match.group(1)
            match = re.match(r"bRequireAuthToken\s*=\s*(\w+)", stripped, re.IGNORECASE)
            if match:
                require_auth = match.group(1).lower() == "true"
        if require_auth and token is None:
            raise BridgeConfigurationError(f"{ini_path} sets bRequireAuthToken=True but has no AuthTokens entry.")
        return f"http://127.0.0.1:{port}/mcp", token or NO_AUTH_PLACEHOLDER

    def _open(self, endpoint: tuple[str, str] | None = None) -> _McpHttpClient:
        url, token = endpoint or self._endpoint()
        mode = self.config.mode.strip().lower()
        timeout = self.config.live_connect_timeout_s if mode == "auto" else self.config.live_timeout_s
        client = _McpHttpClient(url, token)
        try:
            client.initialize(timeout)
        except (_McpTimeout, _McpTransportError, _McpRpcError) as exc:
            hint = " (HTTP 401: check AuthTokens in Config/DefaultUnrealMCPServer.ini)" if getattr(exc, "status", None) == 401 else ""
            raise EditorUnavailableError(
                f"No UnrealMCPServer answered initialize at {url} within {timeout:g}s: {exc}{hint}. "
                f"The bridge never launches an editor; start the project editor and retry."
            ) from exc
        return client

    # -- one tool call ----------------------------------------------------------------------------

    def _invoke(
        self,
        tool: str,
        arguments: dict[str, Any],
        *,
        target: str,
        mutating: bool,
        endpoint: tuple[str, str] | None = None,
        send_envelope: bool = True,
    ) -> dict[str, Any]:
        correlation_id = current_correlation_id()
        envelope: dict[str, Any] = {"correlationId": correlation_id}
        for key, env_name in (("workId", "UESPEC_WORK_ID"), ("todoId", "UESPEC_TODO_ID")):
            value = os.getenv(env_name)
            if value:
                envelope[key] = value
        call_arguments = {**arguments, "_hf": envelope} if send_envelope else dict(arguments)
        deadline = _MONOTONIC() + self.config.live_timeout_s
        client = self._open(endpoint)
        try:
            reply = client.call_tool(tool, call_arguments, timeout=max(1.0, deadline - _MONOTONIC()))
        except (_McpTimeout, _McpTransportError) as exc:
            if not mutating:
                raise EditorUnavailableError(f"Live call '{tool}' got no answer: {exc}.") from exc
            return self._uncertain(tool, target, None, f"Live call '{tool}' got no answer ({exc}); the editor may still be running it.", False)
        except _McpRpcError as exc:
            return self._rpc_error(tool, exc)

        payload, is_error = _extract_payload(reply)
        task_id = payload.get("task_id") or payload.get("taskId")
        if task_id and "ok" not in payload:
            return self._poll_task(client, str(task_id), tool, target, deadline, mutating)
        return self._finalize(payload, is_error)

    def _poll_task(
        self, client: _McpHttpClient, task_id: str, tool: str, target: str, deadline: float, mutating: bool
    ) -> dict[str, Any]:
        step = 0
        while True:
            remaining = deadline - _MONOTONIC()
            if remaining <= 0:
                break
            _SLEEP(min(POLL_INTERVALS_S[min(step, len(POLL_INTERVALS_S) - 1)], remaining))
            step += 1
            remaining = deadline - _MONOTONIC()
            if remaining <= 0:
                break
            try:
                status_reply = client.call_tool("get_task_status", {"task_id": task_id}, timeout=min(30.0, max(1.0, remaining)))
            except (_McpTimeout, _McpTransportError):
                continue
            except _McpRpcError as exc:
                return self._rpc_error("get_task_status", exc)
            snapshot, _ = _extract_payload(status_reply)
            state = str(snapshot.get("status") or "").lower()
            if state in _TOOL_DONE_STATES:
                payload, is_error = _extract_payload(snapshot.get("result"))
                return self._finalize(payload, is_error or state == "failed")
            if state == "cancelled":
                result = self._uncertain(tool, target, task_id, f"Task {task_id} for '{tool}' was cancelled in the editor.", False)
                result.update({"outcome": "cancelled", "code": "TASK_CANCELLED"})
                result["errors"] = [structured_error("TASK_CANCELLED", result["errors"][0]["message"])]
                return result
        cancelled = False
        try:
            client.call_tool("cancel_task", {"task_id": task_id}, timeout=10.0)
            cancelled = True
        except (_McpTimeout, _McpTransportError, _McpRpcError):
            pass
        budget = self.config.live_timeout_s
        return self._uncertain(
            tool,
            target,
            task_id,
            f"Task {task_id} for '{tool}' did not finish within the {budget:g}s call budget; cancel_task {'requested' if cancelled else 'failed'}.",
            cancelled,
        )

    @staticmethod
    def _uncertain(tool: str, target: str, task_id: str | None, message: str, cancel_requested: bool) -> dict[str, Any]:
        return {
            "ok": False,
            "outcome": "uncertain",
            "code": "OUTCOME_UNCERTAIN",
            "tool": tool,
            "target": target,
            "taskId": task_id,
            "cancelRequested": cancel_requested,
            "errors": [
                structured_error(
                    "OUTCOME_UNCERTAIN",
                    message,
                    path=target or "$",
                    hint="Re-query the target (uispec_get_artifact / asset registry) before any retry; this bridge never auto-retries a mutating call.",
                )
            ],
        }

    @staticmethod
    def _rpc_error(tool: str, exc: _McpRpcError) -> dict[str, Any]:
        error = exc.error if isinstance(exc.error, dict) else {"message": str(exc.error)}
        message = str(error.get("message") or exc)
        return {
            "ok": False,
            "code": "MCP_RPC_ERROR",
            "tool": tool,
            "rpcError": error,
            "errors": [structured_error("MCP_RPC_ERROR", message, hint="The editor rejected the request; check the tool name and argument names (uispec_* tools reject undeclared arguments).")],
        }

    @staticmethod
    def _finalize(payload: dict[str, Any], is_error: bool) -> dict[str, Any]:
        result = dict(payload)
        if is_error:
            code = str(result.get("code") or "TOOL_ERROR")
            message = str(result.get("message") or result.get("error") or "The editor tool reported an error.")
            result["ok"] = False
            result["code"] = code
            if not result.get("errors"):
                result["errors"] = [structured_error(code, message, hint=str(result.get("hint") or ""))]
        if "errors" in result:
            result["errors"] = [normalize_error(entry) for entry in result.get("errors") or []]
        return result

    # -- bridge API -------------------------------------------------------------------------------

    @_instrumented("surface")
    def get_support_surface(self, format: str = "markdown") -> dict[str, Any]:
        normalized_format = format.strip().lower()
        if normalized_format not in {"markdown", "json"}:
            raise BridgeConfigurationError("Support surface format must be 'markdown' or 'json'.")
        payload = self._invoke("uispec_capabilities", {"includeSurface": True}, target="supportSurface", mutating=False)
        surface = payload.get("supportSurface")
        if not isinstance(surface, dict):
            payload["ok"] = False
            payload.setdefault("errors", [structured_error("SUPPORT_SURFACE_UNAVAILABLE", "uispec_capabilities returned no supportSurface.")])
            payload.setdefault("format", normalized_format)
            payload.setdefault("content", None)
            return payload
        content: Any = surface
        if normalized_format == "markdown":
            content = "# UESpec support surface (live editor)\n\n```json\n" + json.dumps(surface, indent=2, sort_keys=True) + "\n```\n"
        identity = {key: payload[key] for key in ("engineVersion", "pluginSha", "hfPluginsSha", "surfaceHash", "projectPath") if key in payload}
        return {"ok": True, "format": normalized_format, "content": content, "identity": identity, "command": ["live", "uispec_capabilities"], "outputPath": None}

    @_instrumented("validate")
    def validate_spec_content(self, spec_content: str) -> dict[str, Any]:
        result = self._invoke("uispec_validate", {"specJson": spec_content}, target="specJson", mutating=False)
        result.setdefault("normalizedJson", None)
        result.setdefault("errors", [])
        if result.get("code") or result.get("outcome"):
            # The editor never produced a validation verdict (tool error, rpc error, uncertain): not a semantic answer.
            result["validationLevel"] = "schema-only"
            result["semanticValidation"] = {"status": "failed", "backend": "live", "code": result.get("code")}
        else:
            result["validationLevel"] = "semantic"
            result["semanticValidation"] = {"status": "completed", "backend": "live"}
        return result

    @_instrumented("compile")
    def compile_spec(self, spec_path: str | Path, output_dir: str) -> dict[str, Any]:
        spec_file = Path(spec_path).expanduser().resolve()
        normalized_output_dir = output_dir.strip()
        if not normalized_output_dir:
            raise BridgeConfigurationError("compile_spec requires a non-empty Unreal package output directory, for example '/Game/UESpec/Generated'.")
        result = self._invoke(
            "uispec_compile",
            {"specPath": spec_file.as_posix(), "outputDir": normalized_output_dir},
            target=normalized_output_dir,
            mutating=True,
        )
        result.setdefault("generatedAssets", [])
        result["specPath"] = str(spec_file)
        result["outputDir"] = normalized_output_dir
        return result

    def read_failure_report(self, report_path: str | Path) -> dict[str, Any]:
        resolved_path = Path(report_path).expanduser().resolve()
        if not resolved_path.is_file():
            return {}
        try:
            return json.loads(resolved_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return {}

    @_instrumented("test")
    def run_test_spec(
        self,
        spec_path: str | Path,
        output_report: str | Path,
        failure_report_path: str | Path | None = None,
    ) -> dict[str, Any]:
        """Run a test spec in the live editor. The editor writes the JUnit file only under ``<project>/Saved/UESpec``
        (an ``output_report`` elsewhere is ignored in favour of the editor default) and picks the failure-report path itself."""
        spec_file = Path(spec_path).expanduser().resolve()
        junit_path = Path(output_report).expanduser().resolve()
        arguments: dict[str, Any] = {"testSpecPath": spec_file.as_posix()}
        root = self.config.resolved_project_root()
        if root is not None and _is_within(junit_path, Path(root) / "Saved" / "UESpec"):
            arguments["junitPath"] = junit_path.as_posix()
        payload = self._invoke("uispec_run_tests", arguments, target=junit_path.as_posix(), mutating=True)
        resolved_report = str(payload.get("junitPath") or payload.get("reportPath") or junit_path)
        resolved_failure = str(payload.get("failureReportPath") or failure_report_path or "")
        result: dict[str, Any] = {
            **payload,
            "ok": bool(payload.get("ok", False)),
            "specPath": str(spec_file),
            "outputReport": resolved_report,
            "failureReportPath": resolved_failure,
            "summary": payload.get("summary", {}),
            "errors": [normalize_error(entry) for entry in payload.get("errors", [])],
            "failureReport": self.read_failure_report(resolved_failure) if resolved_failure else {},
        }
        return result

    # -- preview (VibeUE offscreen renderer) ------------------------------------------------------

    def _epic_url(self) -> str:
        return os.getenv("UESPEC_EPIC_URL") or DEFAULT_EPIC_URL

    def _capture_once(self, asset_path: str, width: int, height: int, state: str) -> tuple[dict[str, Any], str]:
        """One ``capture_preview_state`` call. Returns (normalized payload, route).

        Route 1 is the HF bridge ``epic_call_tool`` proxy; only when the bridge does not know that tool (0 epic_*
        tools registered) does it fall back to Epic's own server (``call_tool`` on UESPEC_EPIC_URL, default :8000).
        ``RefreshTools`` is never called (FND-00366).
        """
        inner = {
            "widget_path": asset_path,
            "width": width,
            "height": height,
            "options": {"state": state},
        }
        toolset_args = {"toolset_name": PREVIEW_TOOLSET, "tool_name": PREVIEW_TOOL, "arguments": inner}
        hf_reply = self._invoke("epic_call_tool", toolset_args, target=asset_path, mutating=False, send_envelope=False)
        if not _epic_bridge_missing(hf_reply):
            return hf_reply, ROUTE_HF_BRIDGE
        try:
            epic_reply = self._invoke(
                "call_tool",
                toolset_args,
                target=asset_path,
                mutating=False,
                endpoint=(self._epic_url(), NO_AUTH_PLACEHOLDER),
                send_envelope=False,
            )
        except BridgeError as exc:
            first = (hf_reply.get("errors") or [{}])[0].get("message", "epic_call_tool is not registered")
            unavailable = dict(hf_reply)
            unavailable["ok"] = False
            unavailable["code"] = "PREVIEW_ROUTE_UNAVAILABLE"
            unavailable["errors"] = [
                structured_error(
                    "PREVIEW_ROUTE_UNAVAILABLE",
                    f"HF bridge has no epic_call_tool ({first}) and the Epic server fallback failed: {exc}",
                    hint="Restart the editor so the bridge re-imports epic_* tools (never call ModelContextProtocol.RefreshTools, FND-00366), or start Epic's MCP server.",
                )
            ]
            return unavailable, ROUTE_EPIC_SERVER
        return epic_reply, ROUTE_EPIC_SERVER

    @_instrumented("preview")
    def capture_preview(
        self,
        asset_path: str,
        width: int = DEFAULT_PREVIEW_WIDTH,
        height: int = DEFAULT_PREVIEW_HEIGHT,
        state: str = "normal",
    ) -> dict[str, Any]:
        """Render a compiled Widget Blueprint offscreen (no PIE) through VibeUE ``capture_preview_state``.

        The first capture after a cold asset load can render blank (FND-01359): a blank image verdict triggers exactly
        one more capture before the result is a failure. ``UESPEC_IMG_CHECK`` (a command, optionally an ``img_check.py`` path)
        enables the image verdict; without it ``imageVerdict`` is ``"unchecked"``.
        """
        normalized_asset = asset_path.strip()
        if not normalized_asset.startswith("/"):
            raise BridgeConfigurationError("preview needs an Unreal asset path such as '/Game/HF/UISpecDemo/WBP_X.WBP_X'.")
        attempts: list[dict[str, Any]] = []
        result: dict[str, Any] = {}
        route = ROUTE_HF_BRIDGE
        for attempt in (1, 2):
            started_epoch = time.time() - 1.0
            reply, route = self._capture_once(normalized_asset, width, height, state)
            fields = _extract_preview_fields(reply)
            if reply.get("ok") is False and not fields.get("outputPath"):
                result = dict(reply)
                result.setdefault("code", "PREVIEW_FAILED")
                result.setdefault("errors", [structured_error("PREVIEW_FAILED", str(reply.get("message") or "capture_preview_state failed."))])
                break
            if fields.get("success") is False:
                message = fields.get("errorMessage") or "capture_preview_state reported bSuccess=false."
                result = {
                    "ok": False,
                    "code": "PREVIEW_FAILED",
                    "errors": [structured_error("PREVIEW_FAILED", str(message), hint="Check the asset path and that the WBP compiled; first-load blanks are retried automatically.")],
                    "stateLog": fields.get("stateLog"),
                }
                break
            png = self._resolve_preview_png(fields.get("outputPath"), normalized_asset)
            if png is None:
                result = {
                    "ok": False,
                    "code": "PREVIEW_OUTPUT_MISSING",
                    "errors": [structured_error("PREVIEW_OUTPUT_MISSING", f"capture_preview_state reported no readable PNG for {normalized_asset} (reported: {fields.get('outputPath')!r}).")],
                }
                break
            check = _run_image_check(png, started_epoch)
            attempts.append({"attempt": attempt, "pngPath": png.as_posix(), "imageVerdict": check["verdict"], "imageCheck": check["line"]})
            result = {
                "ok": check["verdict"] in {"ok", "unchecked"},
                "assetPath": normalized_asset,
                "pngPath": png.as_posix(),
                "width": fields.get("width") or width,
                "height": fields.get("height") or height,
                "state": state,
                "dpiScaleUsed": fields.get("dpiScaleUsed"),
                "stateLog": fields.get("stateLog"),
                "imageVerdict": check["verdict"],
                "imageCheck": check["line"],
            }
            if check["verdict"] == "blank" and attempt == 1:
                continue
            if not result["ok"]:
                code = {"blank": "PREVIEW_BLANK", "stale": "PREVIEW_STALE"}.get(check["verdict"], "PREVIEW_IMAGE_INVALID")
                result["code"] = code
                result["errors"] = [structured_error(code, f"Image check rejected the capture: {check['line']}")]
            break
        result["route"] = route
        result["blankRetried"] = len(attempts) > 1
        result["attempts"] = attempts
        result.setdefault("errors", [])
        return result

    def _resolve_preview_png(self, reported: Any, asset_path: str) -> Path | None:
        root = self.config.resolved_project_root()
        candidates: list[Path] = []
        if isinstance(reported, str) and reported.strip():
            raw = Path(reported.strip())
            candidates.append(raw if raw.is_absolute() or root is None else Path(root) / raw)
        if root is not None:
            stem = asset_path.rsplit("/", 1)[-1].split(".", 1)[0]
            candidates.append(Path(root) / "Saved" / "WidgetPreviews" / f"{stem}.png")
        for candidate in candidates:
            if candidate.is_file():
                return candidate
        return None


_IMG_CHECK_VERDICTS = {0: "ok", 41: "blank", 44: "blank", 42: "stale", 40: "missing", 43: "invalid"}
_PNG_PATH_PATTERN = re.compile(r"[A-Za-z]:[\\/][^\s\"'<>|]+?\.png", re.IGNORECASE)


def _epic_bridge_missing(reply: dict[str, Any]) -> bool:
    """True when the HF bridge does not know ``epic_call_tool`` (0 epic_* tools), as opposed to the call itself failing."""
    if reply.get("ok") is not False:
        return False
    if reply.get("code") not in {"MCP_RPC_ERROR", "TOOL_NOT_FOUND", "UNKNOWN_TOOL", "TOOL_ERROR"}:
        return False
    text = " ".join(str(entry.get("message", "")) for entry in reply.get("errors") or [] if isinstance(entry, dict))
    text += " " + str(reply.get("message") or "")
    return bool(re.search(r"unknown tool|no such tool|tool not found|not registered|epic_call_tool", text, re.IGNORECASE))


def _walk_json(value: Any, depth: int = 0) -> Any:
    """Yield dicts found in a reply, including JSON hidden in string values (Epic call_tool wraps results as text)."""
    if depth > 6:
        return
    if isinstance(value, dict):
        yield value
        for child in value.values():
            yield from _walk_json(child, depth + 1)
    elif isinstance(value, list):
        for child in value:
            yield from _walk_json(child, depth + 1)
    elif isinstance(value, str) and value.lstrip().startswith(("{", "[")):
        try:
            parsed = json.loads(value)
        except json.JSONDecodeError:
            return
        yield from _walk_json(parsed, depth + 1)


def _extract_preview_fields(reply: dict[str, Any]) -> dict[str, Any]:
    """Normalize the FWidgetPreviewResult fields wherever the proxy nested them (PascalCase, snake_case or camelCase)."""
    aliases = {
        "success": ("bsuccess", "b_success", "success"),
        "outputPath": ("outputpath", "output_path", "pngpath"),
        "width": ("width",),
        "height": ("height",),
        "errorMessage": ("errormessage", "error_message"),
        "dpiScaleUsed": ("dpiscaleused", "dpi_scale_used"),
        "stateLog": ("statelog", "state_log"),
    }
    found: dict[str, Any] = {}
    for node in _walk_json(reply):
        lowered = {str(key).lower(): value for key, value in node.items()}
        for name, keys in aliases.items():
            if name in found:
                continue
            for key in keys:
                if key in lowered and lowered[key] not in (None, ""):
                    found[name] = lowered[key]
                    break
    if "outputPath" not in found:
        match = _PNG_PATH_PATTERN.search(json.dumps(reply))
        if match:
            found["outputPath"] = match.group(0).replace("\\\\", "/")
    return found


def _img_check_argv(command: str) -> list[str]:
    stripped = command.strip().strip('"')
    if stripped.lower().endswith(".py") and Path(stripped).is_file():
        return [sys.executable, stripped]
    return shlex.split(command)


def _run_image_check(png: Path, after_epoch: float) -> dict[str, str]:
    """Run ``UESPEC_IMG_CHECK`` (``img_check.py``-compatible: ``--after <epoch> <png>``; exit 41/44 = blank). Optional."""
    command = os.getenv("UESPEC_IMG_CHECK", "").strip()
    if not command:
        return {"verdict": "unchecked", "line": "UESPEC_IMG_CHECK not set"}
    try:
        completed = subprocess.run(
            [*_img_check_argv(command), "--after", f"{after_epoch:.3f}", str(png)],
            capture_output=True,
            text=True,
            timeout=60,
            check=False,
        )
    except (OSError, subprocess.SubprocessError, ValueError) as exc:
        return {"verdict": "unchecked", "line": f"image check could not run: {exc}"}
    output = (completed.stdout or completed.stderr or "").strip().splitlines()
    line = output[0] if output else f"exit {completed.returncode}"
    verdict = _IMG_CHECK_VERDICTS.get(completed.returncode, "unchecked")
    return {"verdict": verdict, "line": line}


_BRIDGE: Any | None = None


def get_bridge() -> Any:
    global _BRIDGE
    if _BRIDGE is None:
        config = BridgeConfig.from_env()
        if config.mode == "mock":
            _BRIDGE = MockBridge(config)
        elif config.mode in {"live", "auto"}:
            _BRIDGE = LiveEditorBridge(config)
        else:
            _BRIDGE = BridgeClient(config)
    return _BRIDGE


def reset_bridge() -> None:
    global _BRIDGE
    _BRIDGE = None
