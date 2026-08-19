from __future__ import annotations

import hashlib
import json
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


_ACTIVITY_CACHE_LOCK = threading.RLock()
_ACTIVITY_CACHE: dict[str, dict[str, Any]] = {}
_MAX_ACTIVITY_ITEMS = 800
_MAX_ACTIVITY_TEXT = 20_000
_MAX_ACTIVITY_CACHE_FILES = 3


def load_runtime_history(
    *,
    runtime: str,
    runtime_id: str,
    rollout_path: str | None = None,
    limit: int = 500,
) -> list[dict[str, Any]]:
    if runtime in {"codex", "tcodex"}:
        path = Path(rollout_path or "")
        if not path.is_file():
            path = _find_codex_rollout(runtime, runtime_id)
        items = _load_codex_history(path) if path else []
    elif runtime in {"claude", "tclaude"}:
        path = _find_claude_history(runtime, runtime_id)
        items = _load_claude_history(path) if path else []
    else:
        items = []
    return items[-max(1, min(limit, 2000)) :]


def load_runtime_activities(
    *,
    runtime: str,
    runtime_id: str,
    rollout_path: str | None = None,
    limit: int = 200,
) -> list[dict[str, Any]]:
    """Load CLI-visible plan and tool activity without exposing raw reasoning."""
    if runtime in {"codex", "tcodex"}:
        path = Path(rollout_path or "")
        if not path.is_file():
            path = _find_codex_rollout(runtime, runtime_id)
        items = _load_codex_activities(path) if path else []
    elif runtime in {"claude", "tclaude"}:
        path = _find_claude_history(runtime, runtime_id)
        items = _load_claude_activities(path) if path else []
    else:
        items = []
    return items[-max(1, min(limit, 500)) :]


def load_runtime_activity_detail(
    *,
    runtime: str,
    runtime_id: str,
    activity_id: str,
    rollout_path: str | None = None,
) -> dict[str, Any] | None:
    """Read one tool activity directly from its native transcript without truncation."""
    if runtime in {"codex", "tcodex"}:
        path = Path(rollout_path or "")
        if not path.is_file():
            path = _find_codex_rollout(runtime, runtime_id)
        return _load_codex_activity_detail(path, activity_id) if path else None
    if runtime in {"claude", "tclaude"}:
        path = _find_claude_history(runtime, runtime_id)
        return _load_claude_activity_detail(path, activity_id) if path else None
    return None


def _find_codex_rollout(runtime: str, runtime_id: str) -> Path | None:
    home = Path.home() / (".tcodex" if runtime == "tcodex" else ".codex")
    matches = list(home.glob(f"sessions/*/*/*/rollout-*-{runtime_id}.jsonl"))
    return max(matches, key=lambda path: path.stat().st_mtime) if matches else None


def _find_claude_history(runtime: str, runtime_id: str) -> Path | None:
    home = Path.home() / (".tclaude" if runtime == "tclaude" else ".claude")
    matches = [
        path
        for path in home.glob(f"projects/*/{runtime_id}.jsonl")
        if path.is_file()
    ]
    return max(matches, key=lambda path: path.stat().st_mtime) if matches else None


def _load_codex_history(path: Path) -> list[dict[str, Any]]:
    items: list[dict[str, Any]] = []
    runtime_id = runtime_id_from_rollout(path)
    with path.open(errors="replace") as source:
        for raw in source:
            try:
                record = json.loads(raw)
            except json.JSONDecodeError:
                continue
            if record.get("type") != "event_msg":
                continue
            payload = record.get("payload") or {}
            event_type = payload.get("type")
            if event_type == "user_message":
                role = "human"
                content = payload.get("message")
            elif event_type == "agent_message":
                if payload.get("phase") not in {None, "final_answer"}:
                    continue
                role = "assistant"
                content = payload.get("message")
            else:
                continue
            if not isinstance(content, str) or not content.strip():
                continue
            timestamp = _timestamp(record.get("timestamp"))
            items.append(
                _history_item(
                    runtime_id=runtime_id,
                    role=role,
                    content=content,
                    timestamp=timestamp,
                    source_id=payload.get("turn_id"),
                    source_path=path,
                )
            )
    return items


def _load_claude_history(path: Path) -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    with path.open(errors="replace") as source:
        for raw in source:
            try:
                record = json.loads(raw)
            except json.JSONDecodeError:
                continue
            records.append(record)

    by_uuid = {
        str(record["uuid"]): record
        for record in records
        if record.get("uuid")
    }
    candidates = [
        record
        for record in records
        if record.get("uuid")
        and record.get("type") in {"user", "assistant"}
        and not record.get("isSidechain")
        and not record.get("isMeta")
    ]
    chain: list[dict[str, Any]] = []
    if candidates:
        current = candidates[-1]
        seen: set[str] = set()
        while current:
            uuid_value = str(current.get("uuid") or "")
            if uuid_value and uuid_value in seen:
                break
            if uuid_value:
                seen.add(uuid_value)
            chain.append(current)
            parent = current.get("parentUuid")
            current = by_uuid.get(str(parent)) if parent else None
        chain.reverse()
    else:
        chain = records

    by_key: dict[str, dict[str, Any]] = {}
    order: list[str] = []
    for record in chain:
        record_type = record.get("type")
        if (
            record_type not in {"user", "assistant"}
            or record.get("isSidechain")
            or record.get("isMeta")
        ):
            continue
        message = record.get("message") or {}
        content = _claude_text(message.get("content"))
        if not content:
            continue
        role = "human" if record_type == "user" else "assistant"
        source_id = str(
            message.get("id")
            or record.get("uuid")
            or record.get("promptId")
            or ""
        )
        key = source_id or _digest(
            role, content, str(record.get("timestamp") or "")
        )
        item = _history_item(
            runtime_id=str(record.get("sessionId") or path.stem),
            role=role,
            content=content,
            timestamp=_timestamp(record.get("timestamp")),
            source_id=source_id,
            source_path=path,
        )
        if key not in by_key:
            order.append(key)
        by_key[key] = item
    return [by_key[key] for key in order]


def _load_codex_activities(path: Path) -> list[dict[str, Any]]:
    records = _cached_jsonl_records(path)
    calls: dict[str, dict[str, Any]] = {}
    order: list[str] = []
    for index, record in enumerate(records):
        if record.get("type") == "event_msg":
            payload = record.get("payload") or {}
            if (
                payload.get("type") == "agent_message"
                and payload.get("phase") == "commentary"
                and isinstance(payload.get("message"), str)
                and payload["message"].strip()
            ):
                activity_id = f"commentary-{index}"
                calls[activity_id] = _activity_item(
                    activity_id=f"act_{_digest(str(path), activity_id)}",
                    kind="commentary",
                    name="进度",
                    status="completed",
                    input_value=payload["message"],
                    result=None,
                    timestamp=_timestamp(record.get("timestamp")),
                    source_path=path,
                    call_id=None,
                )
                order.append(activity_id)
            continue
        if record.get("type") != "response_item":
            continue
        payload = record.get("payload") or {}
        item_type = payload.get("type")
        timestamp = _timestamp(record.get("timestamp"))
        call_id = str(payload.get("call_id") or payload.get("id") or "")
        if item_type in {
            "function_call",
            "custom_tool_call",
            "web_search_call",
            "tool_search_call",
        }:
            if not call_id:
                call_id = _digest(str(path), str(index), item_type)
            name, input_value = _codex_tool_details(payload)
            activity = _activity_item(
                activity_id=f"act_{_digest(str(path), call_id, 'tool')}",
                kind="plan" if name == "update_plan" else "tool",
                name="Plan" if name == "update_plan" else name,
                status=_normalize_tool_status(payload.get("status"), "running"),
                input_value=input_value,
                result=None,
                timestamp=timestamp,
                source_path=path,
                call_id=call_id,
            )
            calls[call_id] = activity
            order.append(call_id)
        elif item_type in {
            "function_call_output",
            "custom_tool_call_output",
            "tool_search_output",
        }:
            result = _bounded_text(payload.get("output") or payload.get("result") or payload)
            matching = calls.get(call_id)
            if matching:
                matching["result"] = result
                matching["status"] = _result_status(result)
                matching["updated_at"] = timestamp
            else:
                unmatched_id = call_id or _digest(str(path), str(index), item_type)
                calls[unmatched_id] = _activity_item(
                    activity_id=f"act_{_digest(str(path), unmatched_id, 'result')}",
                    kind="tool",
                    name="Tool result",
                    status=_result_status(result),
                    input_value="",
                    result=result,
                    timestamp=timestamp,
                    source_path=path,
                    call_id=call_id or None,
                )
                order.append(unmatched_id)
    return _ordered_activities(calls, order)


def _load_claude_activities(path: Path) -> list[dict[str, Any]]:
    records = _cached_jsonl_records(path)
    calls: dict[str, dict[str, Any]] = {}
    order: list[str] = []
    for record in records:
        if record.get("isSidechain") or record.get("isMeta"):
            continue
        content = (record.get("message") or {}).get("content")
        if not isinstance(content, list):
            continue
        timestamp = _timestamp(record.get("timestamp"))
        for block in content:
            if not isinstance(block, dict):
                continue
            block_type = block.get("type")
            if block_type == "tool_use":
                call_id = str(block.get("id") or _digest(str(path), timestamp, str(len(order))))
                name = str(block.get("name") or "Tool")
                input_value = block.get("input") or {}
                if name == "ExitPlanMode" and isinstance(input_value, dict):
                    plan_text = input_value.get("plan")
                    if plan_text:
                        input_value = plan_text
                elif name == "EnterPlanMode" and not input_value:
                    input_value = "进入 Plan 模式"
                calls[call_id] = _activity_item(
                    activity_id=f"act_{_digest(str(path), call_id, 'tool')}",
                    kind=(
                        "plan"
                        if name in {"EnterPlanMode", "ExitPlanMode"}
                        else "tool"
                    ),
                    name=(
                        "Plan"
                        if name in {"EnterPlanMode", "ExitPlanMode"}
                        else name
                    ),
                    status="running",
                    input_value=input_value,
                    result=None,
                    timestamp=timestamp,
                    source_path=path,
                    call_id=call_id,
                )
                order.append(call_id)
            elif block_type == "tool_result":
                call_id = str(block.get("tool_use_id") or "")
                result = _bounded_text(
                    _claude_result_text(block.get("content"))
                )
                matching = calls.get(call_id)
                if matching:
                    matching["result"] = result
                    matching["status"] = (
                        "failed" if block.get("is_error") else "completed"
                    )
                    matching["updated_at"] = timestamp
                else:
                    unmatched_id = call_id or _digest(
                        str(path), timestamp, str(len(order)), "result"
                    )
                    calls[unmatched_id] = _activity_item(
                        activity_id=f"act_{_digest(str(path), unmatched_id, 'result')}",
                        kind="tool",
                        name="Tool result",
                        status="failed" if block.get("is_error") else "completed",
                        input_value="",
                        result=result,
                        timestamp=timestamp,
                        source_path=path,
                        call_id=call_id or None,
                    )
                    order.append(unmatched_id)
    return _ordered_activities(calls, order)


def _load_codex_activity_detail(
    path: Path,
    activity_id: str,
) -> dict[str, Any] | None:
    calls: dict[str, dict[str, Any]] = {}
    order: list[str] = []
    try:
        source = path.open(errors="replace")
    except OSError:
        return None
    with source:
        for index, raw in enumerate(source):
            try:
                record = json.loads(raw)
            except json.JSONDecodeError:
                continue
            if record.get("type") != "response_item":
                continue
            payload = record.get("payload") or {}
            item_type = payload.get("type")
            timestamp = _timestamp(record.get("timestamp"))
            call_id = str(payload.get("call_id") or payload.get("id") or "")
            if item_type in {
                "function_call",
                "custom_tool_call",
                "web_search_call",
                "tool_search_call",
            }:
                if not call_id:
                    call_id = _digest(str(path), str(index), str(item_type))
                name, input_value = _codex_tool_details(payload)
                calls[call_id] = _activity_item(
                    activity_id=f"act_{_digest(str(path), call_id, 'tool')}",
                    kind="plan" if name == "update_plan" else "tool",
                    name="Plan" if name == "update_plan" else name,
                    status=_normalize_tool_status(
                        payload.get("status"), "running"
                    ),
                    input_value=input_value,
                    result=None,
                    timestamp=timestamp,
                    source_path=path,
                    call_id=call_id,
                    truncate=False,
                )
                order.append(call_id)
            elif item_type in {
                "function_call_output",
                "custom_tool_call_output",
                "tool_search_output",
            }:
                result = _full_text(
                    payload.get("output")
                    if "output" in payload
                    else payload.get("result")
                    if "result" in payload
                    else payload.get("tools") or payload
                )
                matching = calls.get(call_id)
                if matching:
                    matching["result"] = result
                    matching["status"] = _result_status(result)
                    matching["updated_at"] = timestamp
                else:
                    unmatched_id = call_id or _digest(
                        str(path), str(index), str(item_type)
                    )
                    calls[unmatched_id] = _activity_item(
                        activity_id=(
                            f"act_{_digest(str(path), unmatched_id, 'result')}"
                        ),
                        kind="tool",
                        name="Tool result",
                        status=_result_status(result),
                        input_value="",
                        result=result,
                        timestamp=timestamp,
                        source_path=path,
                        call_id=call_id or None,
                        truncate=False,
                    )
                    order.append(unmatched_id)
    return next(
        (
            item
            for item in _ordered_activities(calls, order)
            if item["activity_id"] == activity_id
        ),
        None,
    )


def _load_claude_activity_detail(
    path: Path,
    activity_id: str,
) -> dict[str, Any] | None:
    calls: dict[str, dict[str, Any]] = {}
    order: list[str] = []
    try:
        source = path.open(errors="replace")
    except OSError:
        return None
    with source:
        for record_index, raw in enumerate(source):
            try:
                record = json.loads(raw)
            except json.JSONDecodeError:
                continue
            if (
                record.get("type") not in {"user", "assistant"}
                or record.get("isSidechain")
                or record.get("isMeta")
            ):
                continue
            content = (record.get("message") or {}).get("content")
            if not isinstance(content, list):
                continue
            timestamp = _timestamp(record.get("timestamp"))
            for block_index, block in enumerate(content):
                if not isinstance(block, dict):
                    continue
                block_type = block.get("type")
                if block_type == "tool_use":
                    call_id = str(
                        block.get("id")
                        or _digest(
                            str(path),
                            str(record_index),
                            str(block_index),
                        )
                    )
                    name = str(block.get("name") or "Tool")
                    input_value = block.get("input") or {}
                    if name == "ExitPlanMode" and isinstance(
                        input_value, dict
                    ):
                        input_value = input_value.get("plan") or input_value
                    elif name == "EnterPlanMode" and not input_value:
                        input_value = "进入 Plan 模式"
                    calls[call_id] = _activity_item(
                        activity_id=(
                            f"act_{_digest(str(path), call_id, 'tool')}"
                        ),
                        kind=(
                            "plan"
                            if name in {"EnterPlanMode", "ExitPlanMode"}
                            else "tool"
                        ),
                        name=(
                            "Plan"
                            if name in {"EnterPlanMode", "ExitPlanMode"}
                            else name
                        ),
                        status="running",
                        input_value=input_value,
                        result=None,
                        timestamp=timestamp,
                        source_path=path,
                        call_id=call_id,
                        truncate=False,
                    )
                    order.append(call_id)
                elif block_type == "tool_result":
                    call_id = str(block.get("tool_use_id") or "")
                    result = _claude_result_text(block.get("content"))
                    matching = calls.get(call_id)
                    if matching:
                        matching["result"] = result
                        matching["status"] = (
                            "failed"
                            if block.get("is_error")
                            else "completed"
                        )
                        matching["updated_at"] = timestamp
                    else:
                        unmatched_id = call_id or _digest(
                            str(path),
                            str(record_index),
                            str(block_index),
                            "result",
                        )
                        calls[unmatched_id] = _activity_item(
                            activity_id=(
                                "act_"
                                + _digest(
                                    str(path), unmatched_id, "result"
                                )
                            ),
                            kind="tool",
                            name="Tool result",
                            status=(
                                "failed"
                                if block.get("is_error")
                                else "completed"
                            ),
                            input_value="",
                            result=result,
                            timestamp=timestamp,
                            source_path=path,
                            call_id=call_id or None,
                            truncate=False,
                        )
                        order.append(unmatched_id)
    return next(
        (
            item
            for item in _ordered_activities(calls, order)
            if item["activity_id"] == activity_id
        ),
        None,
    )


def _cached_jsonl_records(path: Path) -> list[dict[str, Any]]:
    try:
        stat = path.stat()
    except OSError:
        return []
    key = str(path)
    with _ACTIVITY_CACHE_LOCK:
        cached = _ACTIVITY_CACHE.get(key)
        if (
            cached
            and cached.get("size") == stat.st_size
            and cached.get("mtime_ns") == stat.st_mtime_ns
        ):
            cached["last_access"] = time.monotonic()
            return cached["records"]
        if (
            cached
            and cached.get("size", 0) <= stat.st_size
            and cached.get("mtime_ns", 0) <= stat.st_mtime_ns
        ):
            offset = int(cached.get("size") or 0)
            records = list(cached.get("records") or [])
        else:
            offset = 0
            records = []
    final_offset = offset
    try:
        with path.open("rb") as source:
            if offset:
                source.seek(offset)
            while True:
                line_offset = source.tell()
                raw = source.readline()
                if not raw:
                    final_offset = source.tell()
                    break
                try:
                    value = json.loads(raw.decode("utf-8", errors="replace"))
                except (UnicodeDecodeError, json.JSONDecodeError):
                    if not raw.endswith(b"\n"):
                        # The runtime may still be appending this JSONL record.
                        # Re-read it on the next poll instead of caching past it.
                        final_offset = line_offset
                        break
                    continue
                if isinstance(value, dict):
                    compact = _compact_activity_record(value)
                    if compact is not None:
                        records.append(compact)
                final_offset = source.tell()
    except OSError:
        return []
    try:
        final_stat = path.stat()
    except OSError:
        final_stat = stat
    with _ACTIVITY_CACHE_LOCK:
        _ACTIVITY_CACHE[key] = {
            "size": final_offset,
            "mtime_ns": final_stat.st_mtime_ns,
            "records": records,
            "last_access": time.monotonic(),
        }
        while len(_ACTIVITY_CACHE) > _MAX_ACTIVITY_CACHE_FILES:
            oldest = min(
                _ACTIVITY_CACHE,
                key=lambda item: float(
                    _ACTIVITY_CACHE[item].get("last_access") or 0
                ),
            )
            if oldest == key and len(_ACTIVITY_CACHE) > 1:
                candidates = [item for item in _ACTIVITY_CACHE if item != key]
                oldest = min(
                    candidates,
                    key=lambda item: float(
                        _ACTIVITY_CACHE[item].get("last_access") or 0
                    ),
                )
            _ACTIVITY_CACHE.pop(oldest, None)
    return records


def _compact_activity_record(
    record: dict[str, Any],
) -> dict[str, Any] | None:
    if record.get("type") == "response_item":
        payload = dict(record.get("payload") or {})
        payload_type = payload.get("type")
        if payload_type in {
            "function_call",
            "custom_tool_call",
            "web_search_call",
            "tool_search_call",
            "function_call_output",
            "custom_tool_call_output",
            "tool_search_output",
        }:
            if payload_type == "function_call":
                payload["arguments"] = _bounded_text(
                    payload.get("arguments")
                )
            elif payload_type == "custom_tool_call":
                payload["input"] = _bounded_input(payload.get("input"))
            if payload_type in {
                "function_call_output",
                "custom_tool_call_output",
            }:
                payload["output"] = _bounded_text(payload.get("output"))
            elif payload_type == "tool_search_output":
                payload = {
                    "type": payload_type,
                    "id": payload.get("id"),
                    "call_id": payload.get("call_id"),
                    "status": payload.get("status"),
                    "result": _bounded_text(
                        payload.get("result")
                        if "result" in payload
                        else payload.get("tools") or payload
                    ),
                }
            return {
                "type": "response_item",
                "timestamp": record.get("timestamp"),
                "payload": payload,
            }
        return None
    if record.get("type") == "event_msg":
        payload = record.get("payload") or {}
        if (
            payload.get("type") == "agent_message"
            and payload.get("phase") == "commentary"
        ):
            return {
                "type": "event_msg",
                "timestamp": record.get("timestamp"),
                "payload": {
                    "type": "agent_message",
                    "phase": "commentary",
                    "message": payload.get("message"),
                },
            }
        return None
    if record.get("type") not in {"user", "assistant"} or not record.get("uuid"):
        return None
    content = (record.get("message") or {}).get("content")
    tool_blocks = (
        [
            (
                {
                    **block,
                    "content": _bounded_text(
                        _claude_result_text(block.get("content"))
                    ),
                }
                if block.get("type") == "tool_result"
                else block
            )
            for block in content
            if isinstance(block, dict)
            and block.get("type") in {"tool_use", "tool_result"}
        ]
        if isinstance(content, list)
        else []
    )
    return {
        "type": record.get("type"),
        "uuid": record.get("uuid"),
        "parentUuid": record.get("parentUuid"),
        "sessionId": record.get("sessionId"),
        "timestamp": record.get("timestamp"),
        "isSidechain": record.get("isSidechain"),
        "isMeta": record.get("isMeta"),
        "message": {"content": tool_blocks},
    }


def _codex_tool_details(payload: dict[str, Any]) -> tuple[str, Any]:
    item_type = payload.get("type")
    if item_type in {"function_call", "custom_tool_call"}:
        name = str(payload.get("name") or "Tool")
        value = payload.get("arguments")
        if value is None:
            value = payload.get("input")
        return name, _json_value(value)
    if item_type == "web_search_call":
        return "Web search", payload.get("action") or {}
    if item_type == "tool_search_call":
        return "Tool search", payload.get("arguments") or {}
    return "Tool", payload


def _json_value(value: Any) -> Any:
    if not isinstance(value, str):
        return value
    try:
        return json.loads(value)
    except json.JSONDecodeError:
        return value


def _bounded_text(value: Any) -> str:
    text = _full_text(value)
    if len(text) <= _MAX_ACTIVITY_TEXT:
        return text
    return text[:_MAX_ACTIVITY_TEXT] + "\n…（结果过长，已截断）"


def _full_text(value: Any) -> str:
    if isinstance(value, str):
        return value
    try:
        return json.dumps(value, ensure_ascii=False, indent=2)
    except (TypeError, ValueError):
        return str(value)


def _claude_result_text(value: Any) -> str:
    if not isinstance(value, list):
        return _full_text(value)
    text_parts = [
        str(block.get("text"))
        for block in value
        if isinstance(block, dict)
        and block.get("type") in {"text", "output_text"}
        and block.get("text") is not None
    ]
    if text_parts:
        return "\n".join(text_parts)
    return _full_text(value)


def _activity_item(
    *,
    activity_id: str,
    kind: str,
    name: str,
    status: str,
    input_value: Any,
    result: str | None,
    timestamp: str,
    source_path: Path,
    call_id: str | None,
    truncate: bool = True,
) -> dict[str, Any]:
    return {
        "activity_id": activity_id,
        "kind": kind,
        "name": name,
        "status": status,
        "input": (
            _bounded_input(input_value)
            if truncate
            else input_value
        ),
        "result": result,
        "created_at": timestamp,
        "updated_at": timestamp,
        "metadata": {
            "call_id": call_id,
            "source_path": str(source_path),
        },
    }


def _bounded_input(value: Any) -> Any:
    if isinstance(value, str):
        return _bounded_text(value)
    try:
        serialized = json.dumps(value, ensure_ascii=False, indent=2)
    except (TypeError, ValueError):
        return _bounded_text(value)
    if len(serialized) <= _MAX_ACTIVITY_TEXT:
        return value
    return serialized[:_MAX_ACTIVITY_TEXT] + "\n…（参数过长，已截断）"


def _ordered_activities(
    calls: dict[str, dict[str, Any]],
    order: list[str],
) -> list[dict[str, Any]]:
    seen: set[str] = set()
    items: list[dict[str, Any]] = []
    for key in order:
        if key in seen or key not in calls:
            continue
        seen.add(key)
        items.append(calls[key])
    return items[-_MAX_ACTIVITY_ITEMS:]


def _normalize_tool_status(value: Any, fallback: str) -> str:
    normalized = str(value or "").lower()
    if normalized in {"completed", "success", "succeeded"}:
        return "completed"
    if normalized in {"failed", "error", "cancelled", "canceled"}:
        return "failed"
    if normalized in {"pending", "running", "in_progress"}:
        return "running"
    return fallback


def _result_status(result: str) -> str:
    lowered = result.lower()
    if (
        "exit code: 0" in lowered
        or "process exited with code 0" in lowered
        or lowered.strip() in {"plan updated", "success"}
    ):
        return "completed"
    if (
        "exit code:" in lowered
        or "process exited with code" in lowered
        or "error" in lowered[:200]
    ):
        return "failed"
    return "completed"


def _claude_text(content: Any) -> str:
    if isinstance(content, str):
        return content.strip()
    if not isinstance(content, list):
        return ""
    parts: list[str] = []
    for block in content:
        if not isinstance(block, dict):
            continue
        if block.get("type") in {"text", "output_text"}:
            text = block.get("text")
            if isinstance(text, str) and text.strip():
                parts.append(text.strip())
    return "\n\n".join(parts)


def _history_item(
    *,
    runtime_id: str,
    role: str,
    content: str,
    timestamp: str,
    source_id: Any,
    source_path: Path,
) -> dict[str, Any]:
    source_key = str(source_id or "")
    message_id = "hist_" + _digest(
        runtime_id,
        role,
        timestamp,
        source_key,
        content,
    )
    return {
        "message_id": message_id,
        "role": role,
        "content": content,
        "status": "completed",
        "created_at": timestamp,
        "updated_at": timestamp,
        "metadata": {
            "imported": True,
            "source_id": source_key or None,
            "source_path": str(source_path),
        },
    }


def runtime_id_from_rollout(path: Path) -> str:
    try:
        with path.open(errors="replace") as source:
            first = json.loads(source.readline())
        payload = first.get("payload") or {}
        value = payload.get("id") or payload.get("session_id")
        if value:
            return str(value)
    except Exception:
        pass
    return path.stem.rsplit("-", 1)[-1]


def _timestamp(value: Any) -> str:
    if isinstance(value, str) and value:
        try:
            return datetime.fromisoformat(value.replace("Z", "+00:00")).isoformat()
        except ValueError:
            pass
    return datetime.now(timezone.utc).isoformat()


def _digest(*parts: str) -> str:
    return hashlib.sha256("\0".join(parts).encode()).hexdigest()[:32]
