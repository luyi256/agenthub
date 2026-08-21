from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any


REASONING_EFFORTS = ("low", "medium", "high", "xhigh", "max")


def runtime_options() -> dict[str, Any]:
    return {
        "tcodex": _tcodex_options(),
        "tclaude": _tclaude_options(),
        "codex": {
            "default_model": None,
            "models": [],
            "custom_model": True,
            "reasoning_efforts": list(REASONING_EFFORTS),
        },
        "claude": {
            "default_model": None,
            "models": [
                {"id": "opus", "label": "Opus"},
                {"id": "sonnet", "label": "Sonnet"},
                {"id": "haiku", "label": "Haiku"},
            ],
            "custom_model": True,
            "reasoning_efforts": list(REASONING_EFFORTS),
        },
    }


def validate_runtime_selection(
    runtime: str,
    model: str | None,
    reasoning_effort: str | None,
) -> tuple[str | None, str | None]:
    selected_model = (model or "").strip() or None
    selected_effort = (reasoning_effort or "").strip() or None
    if selected_model and (
        len(selected_model) > 120
        or not re.fullmatch(r"[A-Za-z0-9._:/\[\]-]+", selected_model)
    ):
        raise ValueError("模型名称格式无效")
    if selected_effort and selected_effort not in REASONING_EFFORTS:
        raise ValueError("不支持的推理强度")
    options = runtime_options().get(runtime) or {}
    models = {
        item["id"]: item
        for item in options.get("models") or []
        if item.get("id")
    }
    if (
        selected_model
        and models
        and not options.get("custom_model")
        and selected_model not in models
    ):
        raise ValueError(f"{runtime} 当前不支持模型 {selected_model}")
    if selected_model in models and selected_effort:
        supported = models[selected_model].get("reasoning_efforts") or []
        if supported and selected_effort not in supported:
            raise ValueError(
                f"{selected_model} 不支持推理强度 {selected_effort}"
            )
    return selected_model, selected_effort


def _tcodex_options() -> dict[str, Any]:
    home = Path.home() / ".tcodex"
    default_model = _toml_string(home / "config.toml", "model")
    candidates = [
        path
        for path in (home / "instances").glob("*/models.json")
        if path.is_file()
    ]
    models: list[dict[str, Any]] = []
    if candidates:
        latest = max(candidates, key=lambda path: path.stat().st_mtime_ns)
        try:
            payload = json.loads(latest.read_text())
            for item in payload.get("models") or []:
                model_id = str(item.get("slug") or "").strip()
                if not model_id or item.get("visibility") == "hide":
                    continue
                efforts = [
                    str(level.get("effort"))
                    for level in item.get("supported_reasoning_levels") or []
                    if level.get("effort")
                ]
                models.append(
                    {
                        "id": model_id,
                        "label": item.get("display_name") or model_id,
                        "description": item.get("description") or "",
                        "default": model_id == default_model,
                        "reasoning_efforts": efforts,
                        "default_reasoning_effort": (
                            item.get("default_reasoning_level")
                        ),
                    }
                )
        except (OSError, ValueError, TypeError):
            models = []
    return {
        "default_model": default_model,
        "models": models,
        "custom_model": not bool(models),
        "reasoning_efforts": list(REASONING_EFFORTS),
    }


def _tclaude_options() -> dict[str, Any]:
    home = Path.home() / ".tclaude"
    default_model = _json_string(home / "settings.json", "model")
    product_candidates = (
        Path("/usr/local/lib/node_modules/@tencent/tclaude/product.json"),
        Path(
            "/usr/local/lib/nodejs/node-v22.23.1-linux-x64/lib/node_modules/"
            "@tencent/tclaude/product.json"
        ),
    )
    product = next((path for path in product_candidates if path.is_file()), None)
    models: list[dict[str, Any]] = []
    seen: set[str] = set()
    if default_model:
        models.append(
            {
                "id": default_model,
                "label": default_model,
                "description": "当前默认",
                "default": True,
                "reasoning_efforts": list(REASONING_EFFORTS),
            }
        )
        seen.add(default_model)
    if product:
        try:
            payload = json.loads(product.read_text())
            for item in payload.get("models") or []:
                if item.get("disabled"):
                    continue
                model_id = str(item.get("name") or item.get("id") or "").strip()
                if not model_id or model_id in seen:
                    continue
                models.append(
                    {
                        "id": model_id,
                        "label": model_id,
                        "description": item.get("credits") or "",
                        "default": bool(item.get("isDefault")),
                        "reasoning_efforts": (
                            list(REASONING_EFFORTS)
                            if item.get("supportsReasoning")
                            else []
                        ),
                    }
                )
                seen.add(model_id)
        except (OSError, ValueError, TypeError):
            pass
    return {
        "default_model": default_model,
        "models": models,
        "custom_model": True,
        "reasoning_efforts": list(REASONING_EFFORTS),
    }


def _json_string(path: Path, key: str) -> str | None:
    try:
        value = json.loads(path.read_text()).get(key)
        if value is None:
            return None
        return str(value).strip() or None
    except (OSError, ValueError, TypeError):
        return None


def _toml_string(path: Path, key: str) -> str | None:
    try:
        pattern = re.compile(
            rf"^\s*{re.escape(key)}\s*=\s*[\"']([^\"']+)[\"']\s*$"
        )
        for line in path.read_text().splitlines():
            match = pattern.match(line)
            if match:
                return match.group(1).strip() or None
    except OSError:
        pass
    return None
