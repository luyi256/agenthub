from __future__ import annotations

import json
import sqlite3
import threading
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

from .naming import auto_native_name, normalize_alias, project_name, session_uid


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


class AliasConflictError(ValueError):
    pass


class HubDatabase:
    def __init__(self, path: Path):
        self.path = path
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._initialize()

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.path, timeout=10)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute("PRAGMA busy_timeout = 5000")
        return connection

    def _initialize(self) -> None:
        with self._lock, self._connect() as connection:
            connection.executescript(
                """
                PRAGMA journal_mode = WAL;

                CREATE TABLE IF NOT EXISTS sessions (
                    session_uid TEXT PRIMARY KEY,
                    runtime TEXT NOT NULL,
                    runtime_id TEXT NOT NULL,
                    runtime_version TEXT,
                    native_name TEXT,
                    auto_native_name TEXT NOT NULL,
                    alias TEXT,
                    user_title TEXT,
                    discovered_title TEXT,
                    role TEXT,
                    cwd TEXT,
                    project TEXT NOT NULL,
                    pid INTEGER,
                    process_kind TEXT,
                    status TEXT NOT NULL,
                    presence TEXT NOT NULL,
                    attach_state TEXT NOT NULL,
                    capabilities_json TEXT NOT NULL DEFAULT '{}',
                    metadata_json TEXT NOT NULL DEFAULT '{}',
                    managed INTEGER NOT NULL DEFAULT 0,
                    transport TEXT,
                    managed_config_json TEXT NOT NULL DEFAULT '{}',
                    first_seen_at TEXT NOT NULL,
                    last_seen_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    UNIQUE(runtime, runtime_id)
                );

                CREATE UNIQUE INDEX IF NOT EXISTS sessions_alias_unique
                ON sessions(alias) WHERE alias IS NOT NULL;

                CREATE TABLE IF NOT EXISTS links (
                    link_id TEXT PRIMARY KEY,
                    source_session_uid TEXT NOT NULL,
                    target_session_uid TEXT NOT NULL,
                    mode TEXT NOT NULL,
                    trigger_kind TEXT NOT NULL,
                    status TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    FOREIGN KEY(source_session_uid) REFERENCES sessions(session_uid),
                    FOREIGN KEY(target_session_uid) REFERENCES sessions(session_uid)
                );

                CREATE TABLE IF NOT EXISTS events (
                    seq INTEGER PRIMARY KEY AUTOINCREMENT,
                    event_type TEXT NOT NULL,
                    session_uid TEXT,
                    link_id TEXT,
                    payload_json TEXT NOT NULL DEFAULT '{}',
                    created_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS messages (
                    seq INTEGER PRIMARY KEY AUTOINCREMENT,
                    message_id TEXT NOT NULL UNIQUE,
                    session_uid TEXT NOT NULL,
                    role TEXT NOT NULL,
                    content TEXT NOT NULL DEFAULT '',
                    status TEXT NOT NULL,
                    metadata_json TEXT NOT NULL DEFAULT '{}',
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    FOREIGN KEY(session_uid) REFERENCES sessions(session_uid)
                );

                CREATE INDEX IF NOT EXISTS messages_session_seq
                ON messages(session_uid, seq);

                CREATE TABLE IF NOT EXISTS approvals (
                    approval_id TEXT PRIMARY KEY,
                    session_uid TEXT NOT NULL,
                    runtime_request_id TEXT NOT NULL,
                    method TEXT NOT NULL,
                    params_json TEXT NOT NULL DEFAULT '{}',
                    status TEXT NOT NULL,
                    decision_json TEXT,
                    created_at TEXT NOT NULL,
                    resolved_at TEXT,
                    FOREIGN KEY(session_uid) REFERENCES sessions(session_uid)
                );
                """
            )
            self._ensure_column(
                connection, "sessions", "managed", "INTEGER NOT NULL DEFAULT 0"
            )
            self._ensure_column(connection, "sessions", "transport", "TEXT")
            self._ensure_column(
                connection,
                "sessions",
                "managed_config_json",
                "TEXT NOT NULL DEFAULT '{}'",
            )

    @staticmethod
    def _ensure_column(
        connection: sqlite3.Connection, table: str, column: str, definition: str
    ) -> None:
        columns = {
            row["name"]
            for row in connection.execute(f"PRAGMA table_info({table})").fetchall()
        }
        if column not in columns:
            connection.execute(
                f"ALTER TABLE {table} ADD COLUMN {column} {definition}"
            )

    def replace_discovery(
        self,
        records: Iterable[dict[str, Any]],
        scanned_runtimes: Iterable[str],
    ) -> bool:
        records = list(records)
        runtimes = sorted(set(scanned_runtimes))
        timestamp = now_iso()
        changed = False
        with self._lock, self._connect() as connection:
            for runtime in runtimes:
                cursor = connection.execute(
                    """
                    UPDATE sessions
                    SET presence = 'offline',
                        status = CASE
                            WHEN presence = 'history' THEN status
                            ELSE 'offline'
                        END,
                        updated_at = ?
                    WHERE runtime = ? AND managed = 0
                      AND presence NOT IN ('history', 'offline')
                    """,
                    (timestamp, runtime),
                )
                changed = changed or cursor.rowcount > 0

            for record in records:
                runtime = str(record["runtime"])
                runtime_id = str(record["runtime_id"])
                uid = session_uid(runtime, runtime_id)
                cwd = record.get("cwd")
                project = project_name(cwd)
                auto_name = auto_native_name(runtime, cwd, uid)
                capabilities_json = json.dumps(
                    record.get("capabilities") or {},
                    ensure_ascii=False,
                    sort_keys=True,
                )
                metadata_json = json.dumps(
                    record.get("metadata") or {},
                    ensure_ascii=False,
                    sort_keys=True,
                )
                existing = connection.execute(
                    "SELECT * FROM sessions WHERE session_uid = ?", (uid,)
                ).fetchone()
                if existing is not None and existing["managed"]:
                    connection.execute(
                        """
                        UPDATE sessions
                        SET last_seen_at = ?, updated_at = ?
                        WHERE session_uid = ?
                        """,
                        (timestamp, timestamp, uid),
                    )
                    continue
                values = (
                    record.get("runtime_version"),
                    record.get("native_name"),
                    auto_name,
                    record.get("discovered_title"),
                    cwd,
                    project,
                    record.get("pid"),
                    record.get("process_kind"),
                    record.get("status") or "unknown",
                    record.get("presence") or "observable",
                    record.get("attach_state") or "observable",
                    capabilities_json,
                    metadata_json,
                    timestamp,
                    timestamp,
                    uid,
                )
                if existing is None:
                    connection.execute(
                        """
                        INSERT INTO sessions (
                            session_uid, runtime, runtime_id, runtime_version,
                            native_name, auto_native_name, discovered_title,
                            cwd, project, pid, process_kind, status, presence,
                            attach_state, capabilities_json, metadata_json,
                            first_seen_at, last_seen_at, updated_at
                        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                        """,
                        (
                            uid,
                            runtime,
                            runtime_id,
                            record.get("runtime_version"),
                            record.get("native_name"),
                            auto_name,
                            record.get("discovered_title"),
                            cwd,
                            project,
                            record.get("pid"),
                            record.get("process_kind"),
                            record.get("status") or "unknown",
                            record.get("presence") or "observable",
                            record.get("attach_state") or "observable",
                            capabilities_json,
                            metadata_json,
                            timestamp,
                            timestamp,
                            timestamp,
                        ),
                    )
                    self._append_event_tx(
                        connection,
                        "session.discovered",
                        session_uid=uid,
                        payload={"runtime": runtime, "runtime_id": runtime_id},
                    )
                    changed = True
                else:
                    compare = {
                        "runtime_version": record.get("runtime_version"),
                        "native_name": record.get("native_name"),
                        "auto_native_name": auto_name,
                        "discovered_title": record.get("discovered_title"),
                        "cwd": cwd,
                        "project": project,
                        "pid": record.get("pid"),
                        "process_kind": record.get("process_kind"),
                        "status": record.get("status") or "unknown",
                        "presence": record.get("presence") or "observable",
                        "attach_state": record.get("attach_state") or "observable",
                        "capabilities_json": capabilities_json,
                        "metadata_json": metadata_json,
                    }
                    changed = changed or any(existing[key] != value for key, value in compare.items())
                    connection.execute(
                        """
                        UPDATE sessions SET
                            runtime_version = ?,
                            native_name = ?,
                            auto_native_name = ?,
                            discovered_title = ?,
                            cwd = ?,
                            project = ?,
                            pid = ?,
                            process_kind = ?,
                            status = ?,
                            presence = ?,
                            attach_state = ?,
                            capabilities_json = ?,
                            metadata_json = ?,
                            last_seen_at = ?,
                            updated_at = ?
                        WHERE session_uid = ?
                        """,
                        values,
                    )
            cleanup = connection.execute(
                """
                DELETE FROM sessions
                WHERE runtime_id LIKE 'process:%'
                  AND presence = 'offline'
                  AND alias IS NULL
                  AND user_title IS NULL
                  AND role IS NULL
                  AND NOT EXISTS (
                      SELECT 1 FROM links
                      WHERE links.source_session_uid = sessions.session_uid
                         OR links.target_session_uid = sessions.session_uid
                  )
                """
            )
            changed = changed or cleanup.rowcount > 0
        return changed

    def _session_dict(self, row: sqlite3.Row) -> dict[str, Any]:
        result = dict(row)
        result["capabilities"] = json.loads(result.pop("capabilities_json") or "{}")
        result["metadata"] = json.loads(result.pop("metadata_json") or "{}")
        result["managed_config"] = json.loads(
            result.pop("managed_config_json") or "{}"
        )
        result["managed"] = bool(result.get("managed"))
        result["effective_title"] = (
            result.get("user_title")
            or result.get("discovered_title")
            or result.get("native_name")
            or result.get("auto_native_name")
        )
        result["effective_name"] = (
            result.get("alias")
            or result.get("native_name")
            or result.get("auto_native_name")
        )
        return result

    def list_sessions(self) -> list[dict[str, Any]]:
        with self._lock, self._connect() as connection:
            rows = connection.execute(
                """
                SELECT * FROM sessions
                ORDER BY
                    CASE presence
                        WHEN 'online' THEN 0
                        WHEN 'messageable' THEN 1
                        WHEN 'observable' THEN 2
                        WHEN 'history' THEN 3
                        ELSE 4
                    END,
                    last_seen_at DESC
                """
            ).fetchall()
        return [self._session_dict(row) for row in rows]

    def get_session(self, uid: str) -> dict[str, Any] | None:
        with self._lock, self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM sessions WHERE session_uid = ?", (uid,)
            ).fetchone()
        return self._session_dict(row) if row else None

    def assert_alias_available(
        self, alias: str | None, *, exclude_uid: str | None = None
    ) -> None:
        if not alias:
            return
        normalized = normalize_alias(alias)
        query = "SELECT session_uid FROM sessions WHERE alias = ?"
        values: list[Any] = [normalized]
        if exclude_uid:
            query += " AND session_uid != ?"
            values.append(exclude_uid)
        with self._lock, self._connect() as connection:
            row = connection.execute(query, values).fetchone()
        if row:
            raise AliasConflictError("该 alias 已被其他 session 使用")

    def register_managed_session(
        self,
        *,
        runtime: str,
        runtime_id: str,
        runtime_version: str | None,
        native_name: str,
        alias: str | None,
        user_title: str | None,
        role: str | None,
        cwd: str,
        transport: str,
        managed_config: dict[str, Any],
        capabilities: dict[str, Any],
        pid: int | None = None,
    ) -> dict[str, Any]:
        uid = session_uid(runtime, runtime_id)
        timestamp = now_iso()
        normalized_alias = normalize_alias(alias) if alias else None
        auto_name = auto_native_name(runtime, cwd, uid)
        try:
            with self._lock, self._connect() as connection:
                connection.execute(
                    """
                    INSERT INTO sessions (
                        session_uid, runtime, runtime_id, runtime_version,
                        native_name, auto_native_name, alias, user_title, role,
                        cwd, project, pid, process_kind, status, presence,
                        attach_state, capabilities_json, metadata_json,
                        managed, transport, managed_config_json,
                        first_seen_at, last_seen_at, updated_at
                    ) VALUES (
                        ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'interactive',
                        'idle', 'online', 'managed', ?, '{}', 1, ?, ?, ?, ?, ?
                    )
                    ON CONFLICT(session_uid) DO UPDATE SET
                        runtime_version = excluded.runtime_version,
                        native_name = excluded.native_name,
                        alias = COALESCE(excluded.alias, sessions.alias),
                        user_title = COALESCE(excluded.user_title, sessions.user_title),
                        role = COALESCE(excluded.role, sessions.role),
                        cwd = excluded.cwd,
                        project = excluded.project,
                        pid = excluded.pid,
                        status = 'idle',
                        presence = 'online',
                        attach_state = 'managed',
                        capabilities_json = excluded.capabilities_json,
                        managed = 1,
                        transport = excluded.transport,
                        managed_config_json = excluded.managed_config_json,
                        last_seen_at = excluded.last_seen_at,
                        updated_at = excluded.updated_at
                    """,
                    (
                        uid,
                        runtime,
                        runtime_id,
                        runtime_version,
                        native_name,
                        auto_name,
                        normalized_alias,
                        user_title,
                        role,
                        cwd,
                        project_name(cwd),
                        pid,
                        json.dumps(capabilities, ensure_ascii=False, sort_keys=True),
                        transport,
                        json.dumps(
                            managed_config, ensure_ascii=False, sort_keys=True
                        ),
                        timestamp,
                        timestamp,
                        timestamp,
                    ),
                )
                self._append_event_tx(
                    connection,
                    "managed_session.registered",
                    session_uid=uid,
                    payload={
                        "runtime": runtime,
                        "runtime_id": runtime_id,
                        "transport": transport,
                    },
                )
        except sqlite3.IntegrityError as error:
            raise AliasConflictError("该 alias 已被其他 session 使用") from error
        return self.get_session(uid) or {}

    def update_session_runtime_state(
        self,
        uid: str,
        *,
        status: str | None = None,
        presence: str | None = None,
        pid: int | None | object = ...,
        metadata_patch: dict[str, Any] | None = None,
    ) -> dict[str, Any] | None:
        with self._lock, self._connect() as connection:
            row = connection.execute(
                "SELECT metadata_json FROM sessions WHERE session_uid = ?", (uid,)
            ).fetchone()
            if row is None:
                return None
            metadata = json.loads(row["metadata_json"] or "{}")
            metadata.update(metadata_patch or {})
            if status is not None and status != "closed":
                metadata.pop("closed", None)
            updates = ["metadata_json = ?", "last_seen_at = ?", "updated_at = ?"]
            values: list[Any] = [
                json.dumps(metadata, ensure_ascii=False, sort_keys=True),
                now_iso(),
                now_iso(),
            ]
            if status is not None:
                updates.append("status = ?")
                values.append(status)
            if presence is not None:
                updates.append("presence = ?")
                values.append(presence)
            if pid is not ...:
                updates.append("pid = ?")
                values.append(pid)
            values.append(uid)
            connection.execute(
                f"UPDATE sessions SET {', '.join(updates)} WHERE session_uid = ?",
                values,
            )
        return self.get_session(uid)

    def close_session(
        self,
        uid: str,
        *,
        reason: str,
    ) -> dict[str, Any] | None:
        with self._lock, self._connect() as connection:
            row = connection.execute(
                "SELECT metadata_json FROM sessions WHERE session_uid = ?",
                (uid,),
            ).fetchone()
            if row is None:
                return None
            metadata = json.loads(row["metadata_json"] or "{}")
            metadata["closed"] = {
                "reason": reason,
                "at": now_iso(),
            }
            timestamp = now_iso()
            connection.execute(
                """
                UPDATE sessions SET
                    status = 'closed',
                    presence = 'offline',
                    pid = NULL,
                    metadata_json = ?,
                    last_seen_at = ?,
                    updated_at = ?
                WHERE session_uid = ?
                """,
                (
                    json.dumps(
                        metadata,
                        ensure_ascii=False,
                        sort_keys=True,
                    ),
                    timestamp,
                    timestamp,
                    uid,
                ),
            )
            connection.execute(
                """
                UPDATE messages
                SET status = 'interrupted', updated_at = ?
                WHERE session_uid = ? AND status = 'streaming'
                """,
                (timestamp, uid),
            )
            connection.execute(
                """
                UPDATE messages
                SET status = 'cancelled', updated_at = ?
                WHERE session_uid = ? AND status = 'queued'
                """,
                (timestamp, uid),
            )
            connection.execute(
                """
                UPDATE approvals
                SET status = 'expired', resolved_at = ?
                WHERE session_uid = ? AND status = 'pending'
                """,
                (timestamp, uid),
            )
            self._append_event_tx(
                connection,
                "session.closed",
                session_uid=uid,
                payload={"reason": reason},
            )
        return self.get_session(uid)

    def mark_managed_sessions_stopped(self) -> None:
        with self._lock, self._connect() as connection:
            timestamp = now_iso()
            connection.execute(
                """
                UPDATE sessions
                SET status = 'stopped', presence = 'offline', pid = NULL,
                    updated_at = ?
                WHERE managed = 1 AND status != 'closed'
                """,
                (timestamp,),
            )
            connection.execute(
                """
                UPDATE messages
                SET status = 'interrupted', updated_at = ?
                WHERE status = 'streaming'
                  AND session_uid IN (
                      SELECT session_uid FROM sessions
                      WHERE managed = 1 AND status != 'closed'
                  )
                """,
                (timestamp,),
            )
            connection.execute(
                """
                UPDATE approvals
                SET status = 'expired', resolved_at = ?
                WHERE status = 'pending'
                  AND session_uid IN (
                      SELECT session_uid FROM sessions
                      WHERE managed = 1 AND status != 'closed'
                  )
                """,
                (timestamp,),
            )

    def add_message(
        self,
        session_uid_value: str,
        role: str,
        content: str = "",
        *,
        status: str = "completed",
        metadata: dict[str, Any] | None = None,
        message_id: str | None = None,
    ) -> dict[str, Any]:
        message_id = message_id or f"msg_{uuid.uuid4().hex}"
        timestamp = now_iso()
        with self._lock, self._connect() as connection:
            connection.execute(
                """
                INSERT INTO messages (
                    message_id, session_uid, role, content, status,
                    metadata_json, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    message_id,
                    session_uid_value,
                    role,
                    content,
                    status,
                    json.dumps(metadata or {}, ensure_ascii=False, sort_keys=True),
                    timestamp,
                    timestamp,
                ),
            )
        return self.get_message(message_id) or {}

    def import_messages(
        self,
        session_uid_value: str,
        messages: Iterable[dict[str, Any]],
    ) -> int:
        imported = 0
        with self._lock, self._connect() as connection:
            for item in messages:
                cursor = connection.execute(
                    """
                    INSERT OR IGNORE INTO messages (
                        message_id, session_uid, role, content, status,
                        metadata_json, created_at, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        item["message_id"],
                        session_uid_value,
                        item["role"],
                        item.get("content") or "",
                        item.get("status") or "completed",
                        json.dumps(
                            item.get("metadata") or {},
                            ensure_ascii=False,
                            sort_keys=True,
                        ),
                        item["created_at"],
                        item.get("updated_at") or item["created_at"],
                    ),
                )
                imported += max(cursor.rowcount, 0)
        return imported

    def append_message_delta(self, message_id: str, delta: str) -> dict[str, Any] | None:
        with self._lock, self._connect() as connection:
            cursor = connection.execute(
                """
                UPDATE messages
                SET content = content || ?, updated_at = ?
                WHERE message_id = ?
                """,
                (delta, now_iso(), message_id),
            )
            if cursor.rowcount == 0:
                return None
        return self.get_message(message_id)

    def complete_message(
        self,
        message_id: str,
        *,
        content_if_empty: str | None = None,
        status: str = "completed",
        metadata_patch: dict[str, Any] | None = None,
    ) -> dict[str, Any] | None:
        with self._lock, self._connect() as connection:
            row = connection.execute(
                "SELECT content, metadata_json FROM messages WHERE message_id = ?",
                (message_id,),
            ).fetchone()
            if row is None:
                return None
            content = row["content"]
            if not content and content_if_empty:
                content = content_if_empty
            metadata = json.loads(row["metadata_json"] or "{}")
            metadata.update(metadata_patch or {})
            connection.execute(
                """
                UPDATE messages SET
                    content = ?, status = ?, metadata_json = ?, updated_at = ?
                WHERE message_id = ?
                """,
                (
                    content,
                    status,
                    json.dumps(metadata, ensure_ascii=False, sort_keys=True),
                    now_iso(),
                    message_id,
                ),
            )
        return self.get_message(message_id)

    def sync_message(
        self,
        message_id: str,
        *,
        content: str,
        status: str,
        metadata_patch: dict[str, Any] | None = None,
    ) -> dict[str, Any] | None:
        with self._lock, self._connect() as connection:
            row = connection.execute(
                "SELECT metadata_json FROM messages WHERE message_id = ?",
                (message_id,),
            ).fetchone()
            if row is None:
                return None
            metadata = json.loads(row["metadata_json"] or "{}")
            metadata.update(metadata_patch or {})
            connection.execute(
                """
                UPDATE messages
                SET content = ?, status = ?, metadata_json = ?, updated_at = ?
                WHERE message_id = ?
                """,
                (
                    content,
                    status,
                    json.dumps(metadata, ensure_ascii=False, sort_keys=True),
                    now_iso(),
                    message_id,
                ),
            )
        return self.get_message(message_id)

    def get_message(self, message_id: str) -> dict[str, Any] | None:
        with self._lock, self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM messages WHERE message_id = ?", (message_id,)
            ).fetchone()
        return self._message_dict(row) if row else None

    @staticmethod
    def _message_dict(row: sqlite3.Row) -> dict[str, Any]:
        item = dict(row)
        item["metadata"] = json.loads(item.pop("metadata_json") or "{}")
        return item

    def list_messages(
        self, session_uid_value: str, after_seq: int = 0, limit: int = 500
    ) -> list[dict[str, Any]]:
        with self._lock, self._connect() as connection:
            rows = connection.execute(
                """
                SELECT * FROM messages
                WHERE session_uid = ? AND seq > ?
                ORDER BY seq ASC
                LIMIT ?
                """,
                (session_uid_value, after_seq, min(max(limit, 1), 2000)),
            ).fetchall()
        return [self._message_dict(row) for row in rows]

    def search_messages(
        self,
        *,
        cwd: str,
        query: str,
        workspace_id: str | None = None,
        role: str | None = None,
        limit: int = 50,
    ) -> list[dict[str, Any]]:
        normalized = query.strip()
        if not normalized:
            return []
        workspace_clause = "sessions.cwd = ?"
        values: list[Any] = [cwd]
        if workspace_id:
            workspace_clause = (
                "(sessions.cwd = ? OR "
                "json_extract(sessions.managed_config_json, '$.workspace_id') = ?)"
            )
            values.append(workspace_id)
        clauses = [
            workspace_clause,
            "messages.content != ''",
            "instr(lower(messages.content), lower(?)) > 0",
        ]
        values.append(normalized)
        if role:
            clauses.append("messages.role = ?")
            values.append(role)
        values.append(min(max(limit, 1), 100))
        with self._lock, self._connect() as connection:
            rows = connection.execute(
                f"""
                SELECT
                    messages.*,
                    sessions.runtime,
                    sessions.runtime_id,
                    sessions.alias,
                    sessions.native_name,
                    sessions.auto_native_name,
                    sessions.user_title,
                    sessions.discovered_title,
                    sessions.status AS session_status,
                    sessions.presence AS session_presence,
                    sessions.transport,
                    sessions.managed
                FROM messages
                JOIN sessions
                  ON sessions.session_uid = messages.session_uid
                WHERE {' AND '.join(clauses)}
                ORDER BY messages.seq DESC
                LIMIT ?
                """,
                values,
            ).fetchall()
        results: list[dict[str, Any]] = []
        for row in rows:
            item = dict(row)
            item["metadata"] = json.loads(item.pop("metadata_json") or "{}")
            item["managed"] = bool(item.get("managed"))
            item["session_name"] = (
                item.get("alias")
                or item.get("native_name")
                or item.get("auto_native_name")
                or "未命名会话"
            )
            item["session_title"] = (
                item.get("user_title")
                or item.get("discovered_title")
                or item.get("native_name")
                or item.get("auto_native_name")
                or item["session_name"]
            )
            item["excerpt"] = self._search_excerpt(
                item.get("content") or "", normalized
            )
            if len(item["content"]) > 12_000:
                item["content"] = item["content"][:12_000] + "\n…"
            results.append(item)
        return results

    @staticmethod
    def _search_excerpt(content: str, query: str, radius: int = 110) -> str:
        folded_content = content.lower()
        index = folded_content.find(query.lower())
        if index < 0:
            return content[: radius * 2]
        start = max(0, index - radius)
        end = min(len(content), index + len(query) + radius)
        prefix = "…" if start else ""
        suffix = "…" if end < len(content) else ""
        return prefix + content[start:end].strip() + suffix

    def add_approval(
        self,
        *,
        session_uid_value: str,
        runtime_request_id: str,
        method: str,
        params: dict[str, Any],
        approval_id: str | None = None,
    ) -> dict[str, Any]:
        approval_id = approval_id or f"apr_{uuid.uuid4().hex}"
        with self._lock, self._connect() as connection:
            connection.execute(
                """
                INSERT INTO approvals (
                    approval_id, session_uid, runtime_request_id, method,
                    params_json, status, created_at
                ) VALUES (?, ?, ?, ?, ?, 'pending', ?)
                ON CONFLICT(approval_id) DO UPDATE SET
                    runtime_request_id = excluded.runtime_request_id,
                    method = excluded.method,
                    params_json = excluded.params_json,
                    status = 'pending',
                    decision_json = NULL,
                    resolved_at = NULL
                """,
                (
                    approval_id,
                    session_uid_value,
                    runtime_request_id,
                    method,
                    json.dumps(params, ensure_ascii=False, sort_keys=True),
                    now_iso(),
                ),
            )
        return self.get_approval(approval_id) or {}

    @staticmethod
    def _approval_dict(row: sqlite3.Row) -> dict[str, Any]:
        item = dict(row)
        item["params"] = json.loads(item.pop("params_json") or "{}")
        item["decision"] = (
            json.loads(item.pop("decision_json")) if item.get("decision_json") else None
        )
        return item

    def get_approval(self, approval_id: str) -> dict[str, Any] | None:
        with self._lock, self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM approvals WHERE approval_id = ?", (approval_id,)
            ).fetchone()
        return self._approval_dict(row) if row else None

    def list_approvals(
        self, session_uid_value: str | None = None, status: str | None = "pending"
    ) -> list[dict[str, Any]]:
        clauses: list[str] = []
        values: list[Any] = []
        if session_uid_value:
            clauses.append("session_uid = ?")
            values.append(session_uid_value)
        if status:
            clauses.append("status = ?")
            values.append(status)
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        with self._lock, self._connect() as connection:
            rows = connection.execute(
                f"SELECT * FROM approvals {where} ORDER BY created_at ASC", values
            ).fetchall()
        return [self._approval_dict(row) for row in rows]

    def resolve_approval(
        self, approval_id: str, status: str, decision: dict[str, Any]
    ) -> dict[str, Any] | None:
        with self._lock, self._connect() as connection:
            cursor = connection.execute(
                """
                UPDATE approvals SET
                    status = ?, decision_json = ?, resolved_at = ?
                WHERE approval_id = ?
                """,
                (
                    status,
                    json.dumps(decision, ensure_ascii=False, sort_keys=True),
                    now_iso(),
                    approval_id,
                ),
            )
            if cursor.rowcount == 0:
                return None
        return self.get_approval(approval_id)

    def patch_session(
        self,
        uid: str,
        *,
        alias: str | None | object = ...,
        user_title: str | None | object = ...,
        role: str | None | object = ...,
    ) -> dict[str, Any] | None:
        updates: list[str] = []
        values: list[Any] = []
        payload: dict[str, Any] = {}
        if alias is not ...:
            normalized = normalize_alias(alias) if alias else None
            updates.append("alias = ?")
            values.append(normalized)
            payload["alias"] = normalized
        if user_title is not ...:
            title = str(user_title).strip() if user_title else None
            updates.append("user_title = ?")
            values.append(title)
            payload["user_title"] = title
        if role is not ...:
            normalized_role = str(role).strip() if role else None
            updates.append("role = ?")
            values.append(normalized_role)
            payload["role"] = normalized_role
        if not updates:
            return self.get_session(uid)
        updates.append("updated_at = ?")
        values.append(now_iso())
        values.append(uid)
        with self._lock, self._connect() as connection:
            try:
                cursor = connection.execute(
                    f"UPDATE sessions SET {', '.join(updates)} WHERE session_uid = ?",
                    values,
                )
            except sqlite3.IntegrityError as error:
                raise AliasConflictError("该 alias 已被其他 session 使用") from error
            if cursor.rowcount == 0:
                return None
            self._append_event_tx(
                connection,
                "session.updated",
                session_uid=uid,
                payload=payload,
            )
        return self.get_session(uid)

    def create_link(
        self,
        source_uid: str,
        target_uid: str,
        mode: str,
        trigger_kind: str,
    ) -> dict[str, Any]:
        if source_uid == target_uid:
            raise ValueError("不能把 session 连接到自身")
        if mode not in {"notify", "task_handoff", "discussion"}:
            raise ValueError("不支持的 link mode")
        if trigger_kind not in {"manual", "artifact.ready", "task.completed"}:
            raise ValueError("不支持的 trigger")
        timestamp = now_iso()
        link_id = f"lnk_{session_uid(source_uid, target_uid).removeprefix('ses_')[:24]}"
        with self._lock, self._connect() as connection:
            source = connection.execute(
                "SELECT 1 FROM sessions WHERE session_uid = ?", (source_uid,)
            ).fetchone()
            target = connection.execute(
                "SELECT 1 FROM sessions WHERE session_uid = ?", (target_uid,)
            ).fetchone()
            if not source or not target:
                raise ValueError("source 或 target session 不存在")
            connection.execute(
                """
                INSERT INTO links (
                    link_id, source_session_uid, target_session_uid,
                    mode, trigger_kind, status, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, 'draft', ?, ?)
                ON CONFLICT(link_id) DO UPDATE SET
                    mode = excluded.mode,
                    trigger_kind = excluded.trigger_kind,
                    status = 'draft',
                    updated_at = excluded.updated_at
                """,
                (
                    link_id,
                    source_uid,
                    target_uid,
                    mode,
                    trigger_kind,
                    timestamp,
                    timestamp,
                ),
            )
            self._append_event_tx(
                connection,
                "link.created",
                link_id=link_id,
                payload={
                    "source_session_uid": source_uid,
                    "target_session_uid": target_uid,
                    "mode": mode,
                    "trigger_kind": trigger_kind,
                    "status": "draft",
                },
            )
        return self.get_link(link_id) or {}

    def update_link_status(self, link_id: str, status: str) -> dict[str, Any] | None:
        if status not in {"draft", "active", "paused", "closed"}:
            raise ValueError("不支持的 link status")
        with self._lock, self._connect() as connection:
            cursor = connection.execute(
                "UPDATE links SET status = ?, updated_at = ? WHERE link_id = ?",
                (status, now_iso(), link_id),
            )
            if cursor.rowcount == 0:
                return None
            self._append_event_tx(
                connection,
                "link.status_changed",
                link_id=link_id,
                payload={"status": status},
            )
        return self.get_link(link_id)

    def _link_dict(self, row: sqlite3.Row) -> dict[str, Any]:
        return dict(row)

    def get_link(self, link_id: str) -> dict[str, Any] | None:
        with self._lock, self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM links WHERE link_id = ?", (link_id,)
            ).fetchone()
        return self._link_dict(row) if row else None

    def list_links(self) -> list[dict[str, Any]]:
        with self._lock, self._connect() as connection:
            rows = connection.execute(
                "SELECT * FROM links ORDER BY created_at DESC"
            ).fetchall()
        return [self._link_dict(row) for row in rows]

    def _append_event_tx(
        self,
        connection: sqlite3.Connection,
        event_type: str,
        *,
        session_uid: str | None = None,
        link_id: str | None = None,
        payload: dict[str, Any] | None = None,
    ) -> None:
        connection.execute(
            """
            INSERT INTO events (
                event_type, session_uid, link_id, payload_json, created_at
            ) VALUES (?, ?, ?, ?, ?)
            """,
            (
                event_type,
                session_uid,
                link_id,
                json.dumps(payload or {}, ensure_ascii=False, sort_keys=True),
                now_iso(),
            ),
        )

    def list_events(self, after_seq: int = 0, limit: int = 200) -> list[dict[str, Any]]:
        with self._lock, self._connect() as connection:
            rows = connection.execute(
                """
                SELECT * FROM events
                WHERE seq > ?
                ORDER BY seq ASC
                LIMIT ?
                """,
                (after_seq, min(max(limit, 1), 1000)),
            ).fetchall()
        events = []
        for row in rows:
            item = dict(row)
            item["payload"] = json.loads(item.pop("payload_json") or "{}")
            events.append(item)
        return events
