from __future__ import annotations

import asyncio
import contextlib
import json
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import FileResponse, JSONResponse
from starlette.routing import Route, WebSocketRoute
from starlette.websockets import WebSocket, WebSocketDisconnect

from .config import HubConfig, PACKAGE_DIR
from .db import AliasConflictError, HubDatabase
from .discovery import DiscoveryResult, SessionDiscoverer
from .gen_tmux import GenTmuxService
from .naming import ascii_slug, session_uid, short_uid
from .project_tmux import ProjectTmuxManager
from .runtime_options import runtime_options
from .runtime_manager import (
    RuntimeBusyError,
    RuntimeManager,
    RuntimeUnavailableError,
)
from .session_history import (
    load_runtime_activities,
    load_runtime_activity_detail,
    load_runtime_history,
)


class HubService:
    def __init__(self, config: HubConfig):
        self.config = config
        self.db = HubDatabase(config.db_path)
        self.discoverer = SessionDiscoverer(config)
        self.scan_lock = asyncio.Lock()
        self.websockets: set[WebSocket] = set()
        self.last_diagnostics: list[dict[str, Any]] = []
        self.last_scan_error: str | None = None
        self.gen_tmux = GenTmuxService()
        self.runtime_manager = RuntimeManager(
            config,
            self.db,
            self.runtime_broadcast,
            self.gen_tmux,
        )

    async def scan(self) -> dict[str, Any]:
        async with self.scan_lock:
            try:
                result: DiscoveryResult = await asyncio.to_thread(
                    self.discoverer.discover
                )
                await asyncio.to_thread(
                    self.db.replace_discovery,
                    result.records,
                    result.scanned_runtimes,
                )
                self.last_diagnostics = result.diagnostics
                self.last_scan_error = None
            except Exception as error:
                self.last_scan_error = str(error)
                raise
            snapshot = self.snapshot()
            await self.broadcast({"type": "snapshot", "data": snapshot})
            return snapshot

    def snapshot(self) -> dict[str, Any]:
        sessions = self.db.list_sessions()
        links = self.db.list_links()
        counts = {
            "total": len(sessions),
            "online": sum(item["presence"] == "online" for item in sessions),
            "messageable": sum(
                item["attach_state"] == "messageable"
                or bool(
                    item.get("managed")
                    and (item.get("capabilities") or {}).get("chat")
                )
                for item in sessions
            ),
            "links": len(links),
        }
        return {
            "sessions": sessions,
            "links": links,
            "counts": counts,
            "diagnostics": self.last_diagnostics,
            "last_scan_error": self.last_scan_error,
            "mode": "hybrid",
            "runtime_options": runtime_options(),
        }

    async def broadcast(self, message: dict[str, Any]) -> None:
        if not self.websockets:
            return
        text = json.dumps(message, ensure_ascii=False)
        stale: list[WebSocket] = []
        for websocket in list(self.websockets):
            try:
                await websocket.send_text(text)
            except Exception:
                stale.append(websocket)
        for websocket in stale:
            self.websockets.discard(websocket)

    async def runtime_broadcast(self, message: dict[str, Any]) -> None:
        if message.get("type") == "snapshot.refresh":
            await self.broadcast({"type": "snapshot", "data": self.snapshot()})
            return
        await self.broadcast(message)

    async def periodic_scan(self) -> None:
        while True:
            try:
                await self.scan()
            except Exception:
                pass
            await asyncio.sleep(self.config.scan_interval)

    async def gen_snapshot(self, *, force: bool = False) -> dict[str, Any]:
        snapshot = await asyncio.to_thread(
            self.gen_tmux.snapshot, force=force
        )
        for window in snapshot["windows"]:
            expected_uid = session_uid(
                window["runtime"], window["runtime_id"]
            )
            uid = window.get("adopted_session_uid")
            session = self.db.get_session(uid) if uid else None
            session_config = (
                (session.get("managed_config") or {}) if session else {}
            )
            bound_to_live_process = True
            if (
                session
                and session["runtime"] in {"codex", "tcodex"}
                and uid != expected_uid
            ):
                bound_rollout = self.gen_tmux._open_rollout_for_runtime_id(
                    int(window.get("agent_pid") or 0),
                    session["runtime_id"],
                )
                bound_to_live_process = bool(bound_rollout)
                if bound_rollout:
                    window["runtime"] = session["runtime"]
                    window["runtime_id"] = session["runtime_id"]
                    window["session_uid"] = uid
                    window["rollout_path"] = bound_rollout
            valid_binding = bool(
                session
                and session.get("transport") == "gen-tmux-relay"
                and session.get("status") != "closed"
                and session_config.get("source_tmux_window_id")
                == window["window_id"]
                and session_config.get("source_tmux_pane_id")
                == window["pane_id"]
                and bound_to_live_process
            )
            if not valid_binding and uid:
                await asyncio.to_thread(
                    self.gen_tmux.unbind_chat, window["window_id"]
                )
                try:
                    imported = await self.import_gen_chat(
                        window["window_id"], broadcast=False
                    )
                    session = imported["session"]
                    uid = session["session_uid"]
                    window["adopted_session_uid"] = uid
                except RuntimeError:
                    session = None
                    uid = None
                    window["adopted_session_uid"] = None
            if not uid or not session:
                window["chat_session_uid"] = None
                window["chat_status"] = None
                window["chat_transport"] = None
                window["model"] = None
                window["reasoning_effort"] = None
                continue
            if session.get("transport") == "gen-tmux-relay":
                mapped_status = {
                    "blocked": "waiting_approval",
                    "busy": "running",
                    "done": "idle",
                    "idle": "idle",
                }.get(window["state"], "idle")
                self.db.update_session_runtime_state(
                    uid,
                    status=mapped_status,
                    presence="online",
                    pid=window.get("agent_pid"),
                )
                session = self.db.get_session(uid) or session
            window["chat_session_uid"] = uid
            window["chat_status"] = session.get("status")
            window["chat_transport"] = session.get("transport")
            window["model"] = self._session_model(session)
            window["reasoning_effort"] = self._session_reasoning_effort(
                session
            )
        return snapshot

    async def import_gen_chat(
        self, window_id: str, *, broadcast: bool = True
    ) -> dict[str, Any]:
        window = await asyncio.to_thread(
            self.gen_tmux.get_window, window_id
        )
        uid = session_uid(window["runtime"], window["runtime_id"])
        existing = self.db.get_session(uid)
        if existing and existing.get("managed"):
            if existing.get("transport") != "gen-tmux-relay":
                raise RuntimeError(
                    "该原生 session 已由其他 Agent Hub worker 管理，不能同时桥接"
                )
            mapped_status = {
                "blocked": "waiting_approval",
                "busy": "running",
                "done": "idle",
                "idle": "idle",
            }.get(window["state"], "idle")
            config = existing.get("managed_config") or {}
            workspace = ProjectTmuxManager.identify_workspace(
                window["cwd"],
                workspace_name=Path(window["cwd"]).name,
            )
            config.update(
                {
                    "permission_profile": (
                        window.get("permission_profile") or "safe"
                    ),
                    "workspace_id": workspace.workspace_id,
                    "workspace_name": workspace.name,
                    "source_tmux_session": "gen",
                    "source_tmux_window_id": window["window_id"],
                    "source_tmux_window_index": window["window_index"],
                    "source_tmux_pane_id": window["pane_id"],
                    "source_tmux_agent_pgid": window.get("agent_pgid"),
                    "source_rollout_path": window.get("rollout_path"),
                    "relay_mode": "native-tmux",
                }
            )
            self.db.register_managed_session(
                runtime=existing["runtime"],
                runtime_id=existing["runtime_id"],
                runtime_version=existing.get("runtime_version"),
                native_name=existing.get("native_name")
                or existing.get("auto_native_name"),
                alias=existing.get("alias"),
                user_title=existing.get("user_title"),
                role=existing.get("role"),
                cwd=window["cwd"],
                transport="gen-tmux-relay",
                managed_config=config,
                capabilities=existing.get("capabilities") or {},
                pid=window.get("agent_pid"),
            )
            self.db.update_session_runtime_state(
                uid,
                status=mapped_status,
                presence="online",
                pid=window.get("agent_pid"),
                metadata_patch={
                    "reopened_from_tmux": {
                        "session": "gen",
                        "window_id": window["window_id"],
                        "window_index": window["window_index"],
                    }
                },
            )
            existing = self.db.get_session(uid) or existing
            await self.runtime_manager.sync_gen_relay_history(
                existing,
                rollout_path=(
                    window.get("rollout_path")
                    or config.get("source_rollout_path")
                ),
            )
            await asyncio.to_thread(
                self.gen_tmux.bind_chat,
                window_id,
                session_uid_value=uid,
                runtime=window["runtime"],
                runtime_id=window["runtime_id"],
            )
            return {
                "session": existing,
                "messages": self.db.list_messages(uid),
                "window": window,
            }

        workspace = ProjectTmuxManager.identify_workspace(
            window["cwd"],
            workspace_name=Path(window["cwd"]).name,
        )
        alias = f"gen/{window['window_index']}"
        try:
            self.db.assert_alias_available(alias, exclude_uid=uid)
        except AliasConflictError:
            alias = f"gen/{window['window_index']}-{short_uid(uid, 4)}"
        native_name = ascii_slug(
            window["display_name"],
            fallback=f"gen-{window['window_index']}",
            max_length=54,
        )
        session = self.db.register_managed_session(
            runtime=window["runtime"],
            runtime_id=window["runtime_id"],
            runtime_version=None,
            native_name=native_name,
            alias=alias,
            user_title=(
                f"gen:{window['window_index']} · {window['display_name']}"
            ),
            role="imported-tmux-gen",
            cwd=window["cwd"],
            transport="gen-tmux-relay",
            managed_config={
                "permission_profile": window.get("permission_profile")
                or "safe",
                "workspace_id": workspace.workspace_id,
                "workspace_name": workspace.name,
                "source_tmux_session": "gen",
                "source_tmux_window_id": window["window_id"],
                "source_tmux_window_index": window["window_index"],
                "source_tmux_pane_id": window["pane_id"],
                "source_tmux_agent_pgid": window.get("agent_pgid"),
                "source_rollout_path": window.get("rollout_path"),
                "relay_mode": "native-tmux",
            },
            capabilities={
                "observable": True,
                "full_stream": True,
                "chat": True,
                "approvals": window["runtime"] in {"codex", "tcodex"},
                "tmux": True,
                "imported_history": True,
            },
            pid=window.get("agent_pid"),
        )
        messages = await asyncio.to_thread(
            load_runtime_history,
            runtime=window["runtime"],
            runtime_id=window["runtime_id"],
            rollout_path=window.get("rollout_path"),
        )
        await asyncio.to_thread(self.db.import_messages, uid, messages)
        mapped_status = {
            "blocked": "waiting_approval",
            "busy": "running",
            "done": "idle",
            "idle": "idle",
        }.get(window["state"], "idle")
        self.db.update_session_runtime_state(
            uid,
            status=mapped_status,
            presence="online",
            pid=window.get("agent_pid"),
            metadata_patch={
                "imported_from_tmux": {
                    "session": "gen",
                    "window_id": window["window_id"],
                    "window_index": window["window_index"],
                }
            },
        )
        await asyncio.to_thread(
            self.gen_tmux.bind_chat,
            window_id,
            session_uid_value=uid,
            runtime=window["runtime"],
            runtime_id=window["runtime_id"],
        )
        session = self.db.get_session(uid) or session
        if broadcast:
            await self.broadcast({"type": "snapshot", "data": self.snapshot()})
        return {
            "session": session,
            "messages": self.db.list_messages(uid),
            "window": window,
        }

    @staticmethod
    def _session_model(session: dict[str, Any]) -> str | None:
        metadata = session.get("metadata") or {}
        worker = metadata.get("worker") or {}
        config = session.get("managed_config") or {}
        return (
            worker.get("model")
            or metadata.get("model")
            or config.get("model")
        )

    @staticmethod
    def _session_reasoning_effort(
        session: dict[str, Any],
    ) -> str | None:
        metadata = session.get("metadata") or {}
        worker = metadata.get("worker") or {}
        config = session.get("managed_config") or {}
        return (
            worker.get("reasoning_effort")
            or metadata.get("reasoning_effort")
            or config.get("reasoning_effort")
        )

    async def close_gen_window(self, window_id: str) -> dict[str, Any]:
        window = await asyncio.to_thread(
            self.gen_tmux.get_window,
            window_id,
        )
        uid = window.get("adopted_session_uid")
        result = await asyncio.to_thread(
            self.gen_tmux.close_window,
            window_id,
        )
        if uid:
            await self.runtime_manager.close_gen_relay_session(uid)
        await self.broadcast({"type": "snapshot", "data": self.snapshot()})
        return result


def get_hub(request: Request) -> HubService:
    return request.app.state.hub


async def homepage(_: Request) -> FileResponse:
    return FileResponse(PACKAGE_DIR / "static" / "index.html")


async def static_file(request: Request) -> FileResponse | JSONResponse:
    name = request.path_params["name"]
    if name not in {"app.js", "styles.css"}:
        return JSONResponse({"error": "not found"}, status_code=404)
    return FileResponse(PACKAGE_DIR / "static" / name)


async def api_health(request: Request) -> JSONResponse:
    hub = get_hub(request)
    return JSONResponse(
        {
            "ok": True,
            "mode": "hybrid",
            "db": str(hub.config.db_path),
            "scan_interval": hub.config.scan_interval,
            "tmux_socket_name": hub.config.tmux_socket_name,
        }
    )


async def api_snapshot(request: Request) -> JSONResponse:
    return JSONResponse(get_hub(request).snapshot())


async def api_gen_windows(request: Request) -> JSONResponse:
    try:
        snapshot = await get_hub(request).gen_snapshot(
            force=request.query_params.get("refresh") == "1",
        )
        return JSONResponse(snapshot)
    except Exception as error:
        return JSONResponse({"error": str(error)}, status_code=500)


async def api_patch_gen_window(request: Request) -> JSONResponse:
    try:
        body = await request.json()
        window = await asyncio.to_thread(
            get_hub(request).gen_tmux.rename,
            request.path_params["window_id"],
            body.get("name"),
        )
        return JSONResponse(window)
    except (ValueError, RuntimeError, json.JSONDecodeError) as error:
        return JSONResponse({"error": str(error)}, status_code=400)


async def api_close_gen_window(request: Request) -> JSONResponse:
    try:
        result = await get_hub(request).close_gen_window(
            request.path_params["window_id"]
        )
        return JSONResponse(result)
    except (ValueError, RuntimeError) as error:
        return JSONResponse({"error": str(error)}, status_code=409)


async def api_import_gen_chat(request: Request) -> JSONResponse:
    try:
        result = await get_hub(request).import_gen_chat(
            request.path_params["window_id"]
        )
        return JSONResponse(result)
    except (ValueError, RuntimeError) as error:
        return JSONResponse({"error": str(error)}, status_code=409)


async def api_gen_window_attn(request: Request) -> JSONResponse:
    try:
        body = await request.json()
        window = await asyncio.to_thread(
            get_hub(request).gen_tmux.set_attn,
            request.path_params["window_id"],
            body["action"],
        )
        return JSONResponse(window)
    except KeyError:
        return JSONResponse({"error": "缺少 action"}, status_code=400)
    except (ValueError, RuntimeError, json.JSONDecodeError) as error:
        return JSONResponse({"error": str(error)}, status_code=400)


async def api_create_managed_session(request: Request) -> JSONResponse:
    hub = get_hub(request)
    try:
        body = await request.json()
        if body.get("use_tmux", True) is not True:
            raise ValueError(
                "use_tmux 必须为 true；"
                "所有新 managed session 只能使用 tmux-worker"
            )
        session = await hub.runtime_manager.create_session(
            runtime=body["runtime"],
            cwd=body["cwd"],
            alias=body.get("alias") or None,
            title=body.get("title") or None,
            role=body.get("role") or None,
            permission_profile=body.get("permission_profile", "safe"),
            ephemeral=bool(body.get("ephemeral", False)),
            workspace_id=body.get("workspace_id") or None,
            workspace_name=body.get("workspace_name") or None,
            use_tmux=True,
            model=body.get("model") or None,
            reasoning_effort=body.get("reasoning_effort") or None,
        )
        return JSONResponse(session, status_code=201)
    except KeyError as error:
        return JSONResponse({"error": f"缺少字段：{error.args[0]}"}, status_code=400)
    except AliasConflictError as error:
        return JSONResponse({"error": str(error)}, status_code=409)
    except (ValueError, RuntimeError, json.JSONDecodeError) as error:
        return JSONResponse({"error": str(error)}, status_code=400)
    except Exception as error:
        return JSONResponse({"error": str(error)}, status_code=500)


async def api_messages(request: Request) -> JSONResponse:
    hub = get_hub(request)
    uid = request.path_params["uid"]
    if request.method == "GET":
        session = hub.db.get_session(uid)
        if session is None:
            return JSONResponse({"error": "session 不存在"}, status_code=404)
        if session.get("transport") == "gen-tmux-relay":
            await hub.runtime_manager.sync_gen_relay_history(
                session,
            )
        activities = await _session_activities(session)
        return JSONResponse(
            {
                "messages": hub.db.list_messages(uid),
                "activities": [
                    _activity_summary(activity) for activity in activities
                ],
                "approvals": hub.db.list_approvals(uid),
            }
        )
    try:
        body = await request.json()
        assistant = await hub.runtime_manager.send_message(uid, body["text"])
        return JSONResponse(assistant, status_code=202)
    except KeyError:
        return JSONResponse({"error": "缺少 text"}, status_code=400)
    except (ValueError, RuntimeBusyError) as error:
        return JSONResponse({"error": str(error)}, status_code=409)
    except RuntimeUnavailableError as error:
        return JSONResponse({"error": str(error)}, status_code=503)
    except Exception as error:
        return JSONResponse({"error": str(error)}, status_code=500)


async def api_activity_detail(request: Request) -> JSONResponse:
    hub = get_hub(request)
    uid = request.path_params["uid"]
    activity_id = request.path_params["activity_id"]
    session = hub.db.get_session(uid)
    if session is None:
        return JSONResponse({"error": "session 不存在"}, status_code=404)
    config = session.get("managed_config") or {}
    activity = await asyncio.to_thread(
        load_runtime_activity_detail,
        runtime=session["runtime"],
        runtime_id=session["runtime_id"],
        activity_id=activity_id,
        rollout_path=config.get("source_rollout_path"),
    )
    if activity is None:
        return JSONResponse({"error": "activity 不存在"}, status_code=404)
    return JSONResponse(activity)


async def _session_activities(
    session: dict[str, Any],
) -> list[dict[str, Any]]:
    config = session.get("managed_config") or {}
    return await asyncio.to_thread(
        load_runtime_activities,
        runtime=session["runtime"],
        runtime_id=session["runtime_id"],
        rollout_path=config.get("source_rollout_path"),
    )


def _activity_summary(activity: dict[str, Any]) -> dict[str, Any]:
    if activity.get("kind") != "tool":
        return dict(activity)
    result = activity.get("result")
    input_value = activity.get("input")
    return {
        **activity,
        "input": None,
        "result": None,
        "input_preview": _first_activity_line(input_value),
        "result_preview": _first_activity_line(result),
        "has_details": bool(input_value or result),
    }


def _first_activity_line(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, str):
        text = value
    else:
        try:
            text = json.dumps(value, ensure_ascii=False)
        except (TypeError, ValueError):
            text = str(value)
    return next(
        (line.strip() for line in text.splitlines() if line.strip()),
        "",
    )[:500]


async def api_session_handoff(request: Request) -> JSONResponse:
    hub = get_hub(request)
    source_uid = request.path_params["source_uid"]
    try:
        body = await request.json()
        if not isinstance(body, dict):
            return JSONResponse({"error": "JSON 正文必须是对象"}, status_code=400)
        target_uid_value = body["target_session_uid"]
        text_value = body["text"]
        if not isinstance(target_uid_value, str) or not target_uid_value.strip():
            return JSONResponse(
                {"error": "target_session_uid 不能为空"},
                status_code=400,
            )
        if not isinstance(text_value, str) or not text_value.strip():
            return JSONResponse({"error": "text 不能为空"}, status_code=400)

        target_uid = target_uid_value.strip()
        text = text_value.strip()
        source = hub.db.get_session(source_uid)
        if source is None:
            return JSONResponse(
                {"error": "source session 不存在"},
                status_code=404,
            )
        target = hub.db.get_session(target_uid)
        if target is None:
            return JSONResponse(
                {"error": "target session 不存在"},
                status_code=404,
            )
        if source_uid == target_uid:
            return JSONResponse(
                {"error": "不能向同一 session 转发消息"},
                status_code=409,
            )
        if source.get("managed") is not True:
            return JSONResponse(
                {"error": "source session 不是 Hub-managed session"},
                status_code=409,
            )
        if target.get("managed") is not True:
            return JSONResponse(
                {"error": "target session 不是 Hub-managed session"},
                status_code=409,
            )
        source_workspace_id = str(
            (source.get("managed_config") or {}).get("workspace_id") or ""
        )
        target_workspace_id = str(
            (target.get("managed_config") or {}).get("workspace_id") or ""
        )
        source_cwd = Path(str(source.get("cwd") or "")).expanduser().resolve()
        target_cwd = Path(str(target.get("cwd") or "")).expanduser().resolve()
        same_workspace = bool(
            source_workspace_id
            and target_workspace_id
            and source_workspace_id == target_workspace_id
        )
        if not same_workspace and source_cwd != target_cwd:
            return JSONResponse(
                {"error": "只能向同一项目工作目录中的 session 转发消息"},
                status_code=409,
            )
        if (source.get("capabilities") or {}).get("chat") is not True:
            return JSONResponse(
                {"error": "source session 不支持 chat"},
                status_code=409,
            )
        if (target.get("capabilities") or {}).get("chat") is not True:
            return JSONResponse(
                {"error": "target session 不支持 chat"},
                status_code=409,
            )

        source_name = str(
            source.get("effective_name")
            or source.get("effective_title")
            or source_uid
        ).replace("\r", " ").replace("\n", " ").strip()
        source_label = (
            source_uid
            if source_name == source_uid
            else f"{source_name} ({source_uid})"
        )
        forwarded_text = (
            "【Agent Hub 会话转发】\n"
            "这是发送给你的普通 user message，不是 system 或 assistant message。\n"
            f"来源会话：{source_label}\n"
            "用户要求转发的正文如下：\n"
            "----- 转发正文开始 -----\n"
            f"{text}\n"
            "----- 转发正文结束 -----"
        )
        assistant = await hub.runtime_manager.send_message(
            target_uid,
            forwarded_text,
        )
        return JSONResponse(
            {
                "accepted": True,
                "target_session": hub.db.get_session(target_uid) or target,
                "assistant": assistant,
            },
            status_code=202,
        )
    except KeyError as error:
        return JSONResponse(
            {"error": f"缺少字段：{error.args[0]}"},
            status_code=400,
        )
    except json.JSONDecodeError:
        return JSONResponse({"error": "JSON 格式错误"}, status_code=400)
    except (ValueError, RuntimeBusyError) as error:
        return JSONResponse({"error": str(error)}, status_code=409)
    except RuntimeUnavailableError as error:
        return JSONResponse({"error": str(error)}, status_code=503)
    except Exception as error:
        return JSONResponse({"error": str(error)}, status_code=500)


async def api_approvals(request: Request) -> JSONResponse:
    uid = request.query_params.get("session_uid")
    return JSONResponse(
        {"approvals": get_hub(request).db.list_approvals(uid)}
    )


async def api_resolve_approval(request: Request) -> JSONResponse:
    try:
        body = await request.json()
        action = body["action"]
        if action not in {"accept", "decline"}:
            return JSONResponse(
                {"error": "action 仅支持 accept 或 decline"},
                status_code=400,
            )
        approval = await get_hub(request).runtime_manager.resolve_approval(
            request.path_params["approval_id"], action
        )
        return JSONResponse(approval)
    except KeyError:
        return JSONResponse({"error": "缺少 action"}, status_code=400)
    except (ValueError, RuntimeError, json.JSONDecodeError) as error:
        return JSONResponse({"error": str(error)}, status_code=400)


async def api_scan(request: Request) -> JSONResponse:
    try:
        snapshot = await get_hub(request).scan()
        return JSONResponse(snapshot)
    except Exception as error:
        return JSONResponse({"error": str(error)}, status_code=500)


async def api_patch_session(request: Request) -> JSONResponse:
    hub = get_hub(request)
    uid = request.path_params["uid"]
    try:
        body = await request.json()
        allowed = {"alias", "user_title", "role"}
        unknown = set(body) - allowed
        if unknown:
            return JSONResponse(
                {"error": f"未知字段：{', '.join(sorted(unknown))}"},
                status_code=400,
            )
        kwargs = {
            key: body[key] if key in body else ...
            for key in ("alias", "user_title", "role")
        }
        session = await asyncio.to_thread(hub.db.patch_session, uid, **kwargs)
        if session is None:
            return JSONResponse({"error": "session 不存在"}, status_code=404)
        await hub.broadcast({"type": "snapshot", "data": hub.snapshot()})
        return JSONResponse(session)
    except AliasConflictError as error:
        return JSONResponse({"error": str(error)}, status_code=409)
    except ValueError as error:
        return JSONResponse({"error": str(error)}, status_code=400)
    except json.JSONDecodeError:
        return JSONResponse({"error": "JSON 格式错误"}, status_code=400)


async def api_close_session(request: Request) -> JSONResponse:
    hub = get_hub(request)
    uid = request.path_params["uid"]
    session = hub.db.get_session(uid)
    if session is None:
        return JSONResponse({"error": "session 不存在"}, status_code=404)
    if session.get("transport") == "gen-tmux-relay":
        config = session.get("managed_config") or {}
        window_id = config.get("source_tmux_window_id")
        if not window_id:
            return JSONResponse(
                {"error": "缺少原 tmux gen window id"},
                status_code=409,
            )
        try:
            result = await hub.close_gen_window(window_id)
            return JSONResponse(result)
        except (ValueError, RuntimeError) as error:
            return JSONResponse({"error": str(error)}, status_code=409)
    try:
        closed = await hub.runtime_manager.close_session(uid)
        return JSONResponse({"closed": True, "session": closed})
    except ValueError as error:
        return JSONResponse({"error": str(error)}, status_code=409)
    except RuntimeUnavailableError as error:
        return JSONResponse({"error": str(error)}, status_code=503)


async def api_create_link(request: Request) -> JSONResponse:
    hub = get_hub(request)
    try:
        body = await request.json()
        link = await asyncio.to_thread(
            hub.db.create_link,
            body["source_session_uid"],
            body["target_session_uid"],
            body.get("mode", "task_handoff"),
            body.get("trigger_kind", "manual"),
        )
        await hub.broadcast({"type": "snapshot", "data": hub.snapshot()})
        return JSONResponse(link, status_code=201)
    except KeyError as error:
        return JSONResponse({"error": f"缺少字段：{error.args[0]}"}, status_code=400)
    except (ValueError, json.JSONDecodeError) as error:
        return JSONResponse({"error": str(error)}, status_code=400)


async def api_patch_link(request: Request) -> JSONResponse:
    hub = get_hub(request)
    link_id = request.path_params["link_id"]
    try:
        body = await request.json()
        link = await asyncio.to_thread(
            hub.db.update_link_status, link_id, body["status"]
        )
        if link is None:
            return JSONResponse({"error": "link 不存在"}, status_code=404)
        await hub.broadcast({"type": "snapshot", "data": hub.snapshot()})
        return JSONResponse(link)
    except KeyError:
        return JSONResponse({"error": "缺少 status"}, status_code=400)
    except (ValueError, json.JSONDecodeError) as error:
        return JSONResponse({"error": str(error)}, status_code=400)


async def api_events(request: Request) -> JSONResponse:
    after_seq = int(request.query_params.get("after_seq", "0"))
    limit = int(request.query_params.get("limit", "200"))
    events = await asyncio.to_thread(
        get_hub(request).db.list_events, after_seq, limit
    )
    return JSONResponse({"events": events})


async def websocket_endpoint(websocket: WebSocket) -> None:
    await websocket.accept()
    hub: HubService = websocket.app.state.hub
    hub.websockets.add(websocket)
    await websocket.send_json({"type": "snapshot", "data": hub.snapshot()})
    try:
        while True:
            message = await websocket.receive_text()
            if message == "ping":
                await websocket.send_text("pong")
    except WebSocketDisconnect:
        pass
    finally:
        hub.websockets.discard(websocket)


def create_app(config: HubConfig | None = None) -> Starlette:
    config = config or HubConfig.from_env()
    hub = HubService(config)

    @asynccontextmanager
    async def lifespan(app: Starlette):
        app.state.hub = hub
        await hub.runtime_manager.start()
        await hub.scan()
        task = asyncio.create_task(hub.periodic_scan())
        try:
            yield
        finally:
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task
            await hub.runtime_manager.stop_all()

    routes = [
        Route("/", homepage),
        Route("/static/{name}", static_file),
        Route("/api/health", api_health),
        Route("/api/snapshot", api_snapshot),
        Route("/api/tmux/gen/windows", api_gen_windows),
        Route(
            "/api/tmux/gen/windows/{window_id}",
            api_patch_gen_window,
            methods=["PATCH"],
        ),
        Route(
            "/api/tmux/gen/windows/{window_id}",
            api_close_gen_window,
            methods=["DELETE"],
        ),
        Route(
            "/api/tmux/gen/windows/{window_id}/chat",
            api_import_gen_chat,
            methods=["POST"],
        ),
        Route(
            "/api/tmux/gen/windows/{window_id}/attn",
            api_gen_window_attn,
            methods=["POST"],
        ),
        Route(
            "/api/managed-sessions",
            api_create_managed_session,
            methods=["POST"],
        ),
        Route(
            "/api/sessions/{uid}/messages",
            api_messages,
            methods=["GET", "POST"],
        ),
        Route(
            "/api/sessions/{uid}/activities/{activity_id}",
            api_activity_detail,
            methods=["GET"],
        ),
        Route(
            "/api/sessions/{source_uid}/handoff",
            api_session_handoff,
            methods=["POST"],
        ),
        Route("/api/approvals", api_approvals),
        Route(
            "/api/approvals/{approval_id}",
            api_resolve_approval,
            methods=["POST"],
        ),
        Route("/api/scan", api_scan, methods=["POST"]),
        Route(
            "/api/sessions/{uid}",
            api_patch_session,
            methods=["PATCH"],
        ),
        Route(
            "/api/sessions/{uid}",
            api_close_session,
            methods=["DELETE"],
        ),
        Route("/api/links", api_create_link, methods=["POST"]),
        Route("/api/links/{link_id}", api_patch_link, methods=["PATCH"]),
        Route("/api/events", api_events),
        WebSocketRoute("/ws", websocket_endpoint),
    ]
    app = Starlette(routes=routes, lifespan=lifespan)
    app.state.hub = hub
    return app


app = create_app()
