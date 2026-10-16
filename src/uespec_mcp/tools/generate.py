from __future__ import annotations

import asyncio
import base64
import json
import mimetypes
import os
import re
from pathlib import Path
from typing import Any

from ..bridge import BridgeError, get_bridge, structured_error
from ..examples import RetrievedExample, retrieve_examples
from ..llm.base import LLMConfigurationError
from ..llm.factory import create_llm_client
from ..llm.prompts.templates import render
from ..llm.types import LLMImageAttachment, LLMRequest
from ..orchestrator.core import Orchestrator
from . import surface, validate
from ._register import add_tool

try:  # the minimal FastMCP fallback used when fastmcp is not installed has no Context
    from fastmcp import Context
except ImportError:  # pragma: no cover
    Context = Any  # type: ignore[assignment,misc]


_SUPPORTED_TARGET_KINDS = {"HUD", "Menu", "Popup", "Modal", "Debug", "WorldSpace", "Component"}
_SUPPORTED_IMAGE_TYPES = {
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".webp": "image/webp",
    ".gif": "image/gif",
}
_DEFAULT_MAX_IMAGE_BYTES = 8 * 1024 * 1024
_SYSTEM_PROMPT = "You convert game UI screenshots into valid UESpec JSON."
_SAMPLING_SUFFIX = (
    '\n\nRespond with exactly one JSON object and nothing else: {"spec": <the UESpec document>, "notes": "<short>", '
    '"confidence": <0..1>}.'
)

__all__ = ["RetrievedExample", "generate_spec_from_image", "generate_spec_from_image_mcp", "resolve_provider"]


def resolve_provider(provider: str | None) -> str:
    """The provider name for a call: the explicit argument, else ``UESPEC_LLM_PROVIDER``, else ``none`` (never an implicit vendor)."""
    value = provider if provider is not None else os.getenv("UESPEC_LLM_PROVIDER", "")
    return str(value or "").strip().lower() or "none"


def _provider_not_configured() -> dict[str, Any]:
    return _error_result(
        "PROVIDER_NOT_CONFIGURED",
        "No LLM provider is configured for generate_spec_from_image.",
        path="$.provider",
        hint="Pass provider=claude|openai|mock|sampling or set UESPEC_LLM_PROVIDER. Host Claude sessions should call image_to_spec_context and write the spec themselves.",
    )


def _sampling_unsupported(reason: str) -> dict[str, Any]:
    return _error_result(
        "SAMPLING_UNSUPPORTED",
        reason,
        path="$.provider",
        hint="This MCP client did not advertise the sampling capability. Use image_to_spec_context (the host reads the image) or an adapter provider.",
    )


def generate_spec_from_image(
    image_path: str,
    target_kind: str = "HUD",
    output_path: str | None = None,
    visual_prompt: str = "",
    provider: str | None = None,
    max_examples: int = 4,
    validate_generated: bool = True,
) -> dict[str, Any]:
    normalized_kind = target_kind.strip() or "HUD"
    if normalized_kind not in _SUPPORTED_TARGET_KINDS:
        return _invalid_kind(target_kind)

    resolved_provider = resolve_provider(provider)
    if resolved_provider == "none":
        return _provider_not_configured()
    if resolved_provider == "sampling":
        return _sampling_unsupported("MCP sampling needs a live MCP session; a direct (non-MCP) call has none.")

    prepared = _prepare_request(image_path, normalized_kind, visual_prompt, max_examples)
    if not prepared.get("ok"):
        return prepared

    response = create_llm_client(resolved_provider).complete(
        LLMRequest(
            system_prompt=_SYSTEM_PROMPT,
            user_prompt=prepared["prompt"],
            images=[prepared["attachment"]],
            expected_output_schema=_generation_response_schema(),
            max_tokens=5000,
            temperature=0.1,
        )
    )
    return _finish(prepared, normalized_kind, response.parsed_output, _llm_metadata(response), output_path, visual_prompt, validate_generated)


async def generate_spec_from_image_mcp(
    image_path: str,
    target_kind: str = "HUD",
    output_path: str | None = None,
    visual_prompt: str = "",
    provider: str | None = None,
    max_examples: int = 4,
    validate_generated: bool = True,
    ctx: Context | None = None,
) -> dict[str, Any]:
    """MCP-facing wrapper: adapter providers run as before; ``provider="sampling"`` asks the calling host through MCP sampling."""
    resolved_provider = resolve_provider(provider)
    if resolved_provider != "sampling":
        return await asyncio.to_thread(
            generate_spec_from_image,
            image_path,
            target_kind,
            output_path,
            visual_prompt,
            resolved_provider,
            max_examples,
            validate_generated,
        )

    normalized_kind = target_kind.strip() or "HUD"
    if normalized_kind not in _SUPPORTED_TARGET_KINDS:
        return _invalid_kind(target_kind)
    supported, reason = probe_sampling(ctx)
    if not supported:
        return _sampling_unsupported(reason)

    prepared = await asyncio.to_thread(_prepare_request, image_path, normalized_kind, visual_prompt, max_examples)
    if not prepared.get("ok"):
        return prepared

    from mcp.types import ImageContent, SamplingMessage, TextContent

    attachment: LLMImageAttachment = prepared["attachment"]
    message = SamplingMessage(
        role="user",
        content=[
            TextContent(type="text", text=prepared["prompt"] + _SAMPLING_SUFFIX),
            ImageContent(type="image", data=attachment.data_base64, mimeType=attachment.media_type),
        ],
    )
    try:
        sampled = await ctx.sample([message], system_prompt=_SYSTEM_PROMPT, temperature=0.1, max_tokens=5000)  # type: ignore[union-attr]
    except Exception as exc:  # noqa: BLE001 - the host may decline or fail; report it, never fabricate a spec
        return _error_result(
            "SAMPLING_FAILED",
            f"The MCP host rejected or failed the sampling request: {type(exc).__name__}: {exc}",
            path="$.provider",
            hint="Retry, or use image_to_spec_context and write the spec in the host session.",
        )
    parsed = _parse_json_object(getattr(sampled, "text", None) or "")
    return _finish(
        prepared,
        normalized_kind,
        parsed,
        {"model": "mcp-sampling", "inputTokens": 0, "outputTokens": 0},
        output_path,
        visual_prompt,
        validate_generated,
    )


def probe_sampling(ctx: Any) -> tuple[bool, str]:
    """Whether the calling MCP client advertised the ``sampling`` capability (checked per call, never assumed)."""
    if ctx is None:
        return False, "No MCP request context is available, so the client's sampling capability cannot be probed."
    try:
        from mcp.types import ClientCapabilities, SamplingCapability

        session = ctx.session
        if session.check_client_capability(ClientCapabilities(sampling=SamplingCapability())):
            return True, ""
    except Exception as exc:  # noqa: BLE001 - an unprobeable session counts as unsupported
        return False, f"The client's sampling capability could not be probed ({type(exc).__name__}: {exc})."
    return False, "The MCP client did not advertise the sampling capability during initialize."


def _parse_json_object(text: str) -> dict[str, Any] | None:
    stripped = text.strip()
    fence = re.search(r"```(?:json)?\s*(.*?)```", stripped, flags=re.DOTALL)
    if fence:
        stripped = fence.group(1).strip()
    candidates = [stripped]
    start, end = stripped.find("{"), stripped.rfind("}")
    if 0 <= start < end:
        candidates.append(stripped[start : end + 1])
    for candidate in candidates:
        try:
            value = json.loads(candidate)
        except (ValueError, TypeError):
            continue
        if isinstance(value, dict):
            return value if "spec" in value else {"spec": value}
    return None


def _prepare_request(image_path: str, normalized_kind: str, visual_prompt: str, max_examples: int) -> dict[str, Any]:
    image_result = _load_image(image_path)
    if not image_result.get("ok"):
        return image_result

    surface_snapshot = _load_support_surface_snapshot()
    surface_summary = _summarize_support_surface(surface_snapshot)
    examples = _retrieve_examples(normalized_kind, surface_snapshot, max_examples=max_examples)
    prompt = render(
        "screenshot_to_uispec.j2",
        target_kind=normalized_kind,
        support_surface_summary=surface_summary,
        examples=[example.as_prompt_dict() for example in examples],
        visual_prompt=visual_prompt.strip(),
    )
    return {
        "ok": True,
        "image": image_result,
        "attachment": image_result["attachment"],
        "prompt": prompt,
        "examples": examples,
        "surface": surface_snapshot,
    }


def _finish(
    prepared: dict[str, Any],
    normalized_kind: str,
    parsed: Any,
    llm: dict[str, Any],
    output_path: str | None,
    visual_prompt: str,
    validate_generated: bool,
) -> dict[str, Any]:
    image_result = prepared["image"]
    examples: list[RetrievedExample] = prepared["examples"]
    if not isinstance(parsed, dict) or not isinstance(parsed.get("spec"), dict):
        return {
            "ok": False,
            "imagePath": str(image_result["path"]),
            "targetKind": normalized_kind,
            "generatedSpec": None,
            "specJson": None,
            "validation": None,
            "retrievedExamples": [example.as_result_dict() for example in examples],
            "errors": [
                structured_error(
                    "LLM_OUTPUT_INVALID",
                    "The LLM response did not contain a structured 'spec' object.",
                    path="$.spec",
                    hint="Retry with a model that supports structured output.",
                )
            ],
            "llm": llm,
        }

    spec = parsed["spec"]
    spec.setdefault("apiVersion", "0.2")
    spec.setdefault("kind", normalized_kind)
    spec.setdefault("meta", {})
    if isinstance(spec["meta"], dict):
        spec["meta"].setdefault("generatedBy", "uespec-round7")
        spec["meta"].setdefault("sourcePrompt", visual_prompt.strip() or "Generated from screenshot.")

    spec_json = json.dumps(spec, ensure_ascii=False, indent=2)
    validation = validate.validate_spec(spec_json) if validate_generated else {"ok": True, "stage": "skipped", "errors": []}

    resolved_output_path: Path | None = None
    if output_path:
        resolved_output_path = Path(output_path).expanduser().resolve()
        resolved_output_path.parent.mkdir(parents=True, exist_ok=True)
        resolved_output_path.write_text(spec_json, encoding="utf-8")

    return {
        "ok": bool(validation.get("ok")),
        "imagePath": str(image_result["path"]),
        "targetKind": normalized_kind,
        "generatedSpec": spec,
        "specJson": spec_json,
        "outputPath": str(resolved_output_path) if resolved_output_path else None,
        "validation": validation,
        "retrievedExamples": [example.as_result_dict() for example in examples],
        "supportSurfaceVersion": _surface_version(prepared["surface"]),
        "llm": llm,
        "errors": validation.get("errors", []),
    }


def _invalid_kind(target_kind: str) -> dict[str, Any]:
    return _error_result(
        "INVALID_TARGET_KIND",
        f"Unsupported target_kind '{target_kind}'.",
        path="$.target_kind",
        hint=f"Use one of: {', '.join(sorted(_SUPPORTED_TARGET_KINDS))}.",
    )


def generate_spec_from_image_with_autofix(
    image_path: str,
    test_spec_path: str,
    target_kind: str = "HUD",
    output_path: str | None = None,
    visual_prompt: str = "",
    provider: str | None = None,
    max_examples: int = 4,
    max_attempts: int = 3,
    history_dir: str | None = None,
    validate_generated: bool = True,
) -> dict[str, Any]:
    resolved_output_path = _resolve_generated_output_path(
        image_path=image_path,
        output_path=output_path,
        target_kind=target_kind,
    )
    generation_result = generate_spec_from_image(
        image_path=image_path,
        target_kind=target_kind,
        output_path=str(resolved_output_path),
        visual_prompt=visual_prompt,
        provider=provider,
        max_examples=max_examples,
        validate_generated=validate_generated,
    )

    generated_spec_path = generation_result.get("outputPath")
    if not generation_result.get("specJson") or not generated_spec_path:
        return {
            "ok": False,
            "status": "generation_failed",
            "specPath": None,
            "testSpecPath": None,
            "generation": generation_result,
            "autofix": None,
            "errors": generation_result.get("errors", []),
        }

    try:
        resolved_spec_path = Path(str(generated_spec_path)).expanduser().resolve()
        resolved_test_spec_path = _resolve_existing_file(test_spec_path)
    except FileNotFoundError as exc:
        return {
            "ok": False,
            "status": "test_spec_not_found",
            "specPath": str(generated_spec_path),
            "testSpecPath": None,
            "generation": generation_result,
            "autofix": None,
            "errors": [
                structured_error(
                    "TEST_SPEC_NOT_FOUND",
                    str(exc),
                    path="$.test_spec_path",
                    hint="Pass an existing UESpec test JSON file.",
                )
            ],
        }

    resolved_history_dir = Path(history_dir).expanduser().resolve() if history_dir else Path("./Saved/UESpec/PatchHistory")
    try:
        orchestrator = Orchestrator(
            get_bridge(),
            create_llm_client(resolve_provider(provider)),
            max_attempts=max_attempts,
            history_dir=resolved_history_dir,
        )
        autofix_result = orchestrator.run_with_autofix(resolved_spec_path, resolved_test_spec_path)
    except (BridgeError, LLMConfigurationError, ValueError) as exc:
        return {
            "ok": False,
            "status": "autofix_failed",
            "specPath": str(resolved_spec_path),
            "testSpecPath": str(resolved_test_spec_path),
            "generation": generation_result,
            "autofix": None,
            "errors": [
                structured_error(
                    "AUTOFIX_FAILED",
                    str(exc),
                    hint="Check bridge configuration, LLM provider configuration, and supplied test spec path.",
                )
            ],
        }

    return {
        "ok": bool(autofix_result.get("ok")),
        "status": autofix_result.get("status", "unknown"),
        "specPath": str(resolved_spec_path),
        "testSpecPath": str(resolved_test_spec_path),
        "generation": generation_result,
        "autofix": autofix_result,
        "errors": autofix_result.get("errors", []) if not autofix_result.get("ok") else [],
    }


def load_image(image_path: str) -> dict[str, Any]:
    """Validate and read an image file (type and size limits); ``ok`` replies carry ``path``, ``attachment`` and ``sizeBytes``."""
    return _load_image(image_path)


def _load_image(image_path: str) -> dict[str, Any]:
    resolved = Path(image_path).expanduser().resolve()
    if not resolved.is_file():
        return _error_result(
            "IMAGE_NOT_FOUND",
            f"Image file '{resolved}' does not exist.",
            path="$.image_path",
            hint="Pass an existing PNG, JPG, WEBP, or GIF file.",
        )

    max_bytes = int(os.getenv("UESPEC_MAX_IMAGE_BYTES", str(_DEFAULT_MAX_IMAGE_BYTES)) or _DEFAULT_MAX_IMAGE_BYTES)
    image_size = resolved.stat().st_size
    if image_size > max_bytes:
        return _error_result(
            "IMAGE_TOO_LARGE",
            f"Image file '{resolved}' is {image_size} bytes, above the {max_bytes} byte limit.",
            path="$.image_path",
            hint="Use a smaller screenshot or raise UESPEC_MAX_IMAGE_BYTES.",
        )

    media_type = _SUPPORTED_IMAGE_TYPES.get(resolved.suffix.lower()) or mimetypes.guess_type(resolved.name)[0]
    if media_type not in set(_SUPPORTED_IMAGE_TYPES.values()):
        return _error_result(
            "UNSUPPORTED_IMAGE_TYPE",
            f"Image file '{resolved}' has unsupported type '{media_type or resolved.suffix}'.",
            path="$.image_path",
            hint="Use PNG, JPG, WEBP, or GIF.",
        )

    data_base64 = base64.b64encode(resolved.read_bytes()).decode("ascii")
    return {
        "ok": True,
        "path": resolved,
        "attachment": LLMImageAttachment(media_type=media_type, data_base64=data_base64),
        "sizeBytes": image_size,
    }


def _load_support_surface_snapshot() -> dict[str, Any]:
    result = surface.get_support_surface(format="json")
    content = result.get("content")
    if result.get("ok") and isinstance(content, dict):
        return content
    return {}


def _summarize_support_surface(snapshot: dict[str, Any]) -> str:
    widgets = _extract_surface_names(snapshot, ["RegisteredComponents", "BuiltInUMGWidgets", "CommonUIWidgets"], "Type")
    converters = _extract_surface_names(snapshot, ["Converters"], "Name")
    tokens = _extract_surface_names(snapshot, ["StyleTokens"], "Name")
    signals = _extract_surface_names(snapshot, ["Signals"], "Name")
    return "\n".join(
        [
            f"Widgets/components: {', '.join(widgets[:60]) or 'unavailable'}",
            f"Converters: {', '.join(converters[:40]) or 'unavailable'}",
            f"Style tokens: {', '.join(tokens[:80]) or 'unavailable'}",
            f"Signals: {', '.join(signals[:40]) or 'unavailable'}",
        ]
    )


def _extract_surface_names(snapshot: dict[str, Any], keys: list[str], name_key: str) -> list[str]:
    names: set[str] = set()
    for key in keys:
        for entry in surface.surface_section(snapshot, key):
            if isinstance(entry, dict):
                name = entry.get(name_key) or entry.get(name_key[:1].lower() + name_key[1:])
                if isinstance(name, str) and name:
                    names.add(name)
    return sorted(names)


def _retrieve_examples(target_kind: str, snapshot: dict[str, Any], *, max_examples: int) -> list[RetrievedExample]:
    component_names = set(_extract_surface_names(snapshot, ["RegisteredComponents"], "Type"))
    examples, _where = retrieve_examples(target_kind, component_names, max_examples=max_examples)
    return examples


def _generation_response_schema() -> dict[str, Any]:
    return {
        "type": "object",
        "properties": {
            "spec": {"type": "object", "additionalProperties": True},
            "notes": {"type": "string"},
            "confidence": {"type": "number"},
        },
        "required": ["spec"],
        "additionalProperties": False,
    }


def _surface_version(snapshot: dict[str, Any]) -> Any:
    return snapshot.get("SupportSurfaceVersion") or snapshot.get("supportSurfaceVersion")


def _llm_metadata(response: Any) -> dict[str, Any]:
    return {
        "model": response.model,
        "inputTokens": response.input_tokens,
        "outputTokens": response.output_tokens,
    }


def _error_result(code: str, message: str, *, path: str = "$", hint: str = "") -> dict[str, Any]:
    return {
        "ok": False,
        "generatedSpec": None,
        "specJson": None,
        "validation": None,
        "retrievedExamples": [],
        "errors": [structured_error(code, message, path=path, hint=hint)],
    }


def _resolve_generated_output_path(*, image_path: str, output_path: str | None, target_kind: str) -> Path:
    if output_path:
        return Path(output_path).expanduser().resolve()
    stem = _safe_file_stem(Path(image_path).stem)
    kind = _safe_file_stem(target_kind.lower())
    return (Path.cwd() / "Saved" / "UESpec" / "GeneratedSpecs" / f"{stem}.{kind}.uispec.json").resolve()


def _safe_file_stem(value: str) -> str:
    safe = "".join(char if char.isalnum() or char in ("-", "_") else "-" for char in value.strip())
    return safe.strip("-_") or "screenshot"


def _resolve_existing_file(path: str) -> Path:
    resolved = Path(path).expanduser().resolve()
    if not resolved.is_file():
        raise FileNotFoundError(f"File '{resolved}' does not exist.")
    return resolved


def register(mcp: Any) -> None:
    add_tool(
        mcp,
        "generate_spec_from_image",
        "Explicit LLM path: UISpec from a screenshot via provider claude|openai|mock or MCP sampling (host must advertise it, else SAMPLING_UNSUPPORTED). Default none -> PROVIDER_NOT_CONFIGURED. Host Claude: use image_to_spec_context.",
        generate_spec_from_image_mcp,
    )
    add_tool(
        mcp,
        "generate_spec_from_image_with_autofix",
        "Generate a UISpec from a UI screenshot, then run the autofix orchestrator against a test spec.",
        generate_spec_from_image_with_autofix,
        compact=True,
    )
