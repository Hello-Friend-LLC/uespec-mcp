from __future__ import annotations

import json
import os
import platform
import subprocess
import tempfile
from difflib import get_close_matches
from pathlib import Path
from typing import Any, Sequence

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
    """Base bridge exception."""


class BridgeConfigurationError(BridgeError):
    """Raised when required bridge configuration is missing."""


class BridgeExecutionError(BridgeError):
    """Raised when Unreal execution fails without a structured result."""


class UnsupportedBridgeModeError(BridgeError):
    """Raised when a configured transport exists only as a placeholder."""


class CommandletInvocation(BaseModel):
    ok: bool
    command: list[str] = Field(default_factory=list)
    stdout: str = ""
    stderr: str = ""
    outputPath: str | None = None
    payload: Any = None


class BridgeConfig(BaseModel):
    uproject_path: Path | None = None
    ue_engine_path: Path | None = None
    mode: str = "commandlet"

    @classmethod
    def from_env(cls) -> "BridgeConfig":
        raw_mode = (os.getenv("UESPEC_BRIDGE_MODE") or os.getenv("UESPEC_MCP_MODE") or "commandlet").strip().lower() or "commandlet"
        engine_path = os.getenv("UESPEC_UE_ENGINE_PATH")
        uproject_path = os.getenv("UESPEC_UPROJECT_PATH")
        return cls(
            uproject_path=Path(uproject_path).expanduser() if uproject_path else None,
            ue_engine_path=Path(engine_path).expanduser() if engine_path else None,
            mode=raw_mode,
        )


class BridgeClient:
    """Bridge between Python MCP tools and Unreal commandlets."""

    def __init__(self, config: BridgeConfig | None = None) -> None:
        self.config = config or BridgeConfig.from_env()

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

    def __init__(self, config: BridgeConfig | None = None) -> None:
        self.config = config or BridgeConfig.from_env()

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


_BRIDGE: Any | None = None


def get_bridge() -> Any:
    global _BRIDGE
    if _BRIDGE is None:
        config = BridgeConfig.from_env()
        _BRIDGE = MockBridge(config) if config.mode == "mock" else BridgeClient(config)
    return _BRIDGE


def reset_bridge() -> None:
    global _BRIDGE
    _BRIDGE = None
