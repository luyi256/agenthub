from __future__ import annotations

import re
import unicodedata
import uuid
from pathlib import Path


SESSION_NAMESPACE = uuid.UUID("32a4d7a3-e43d-47b0-99aa-03ce0e2ad246")
ALIAS_RE = re.compile(r"^[a-z0-9][a-z0-9._/-]{0,63}$")


def session_uid(runtime: str, runtime_id: str) -> str:
    value = uuid.uuid5(SESSION_NAMESPACE, f"{runtime}:{runtime_id}")
    return f"ses_{value.hex}"


def short_uid(uid: str, length: int = 6) -> str:
    compact = uid.removeprefix("ses_")
    return compact[-length:]


def ascii_slug(value: str, fallback: str = "workspace", max_length: int = 32) -> str:
    normalized = unicodedata.normalize("NFKD", value)
    ascii_value = normalized.encode("ascii", "ignore").decode("ascii").lower()
    ascii_value = re.sub(r"[^a-z0-9]+", "-", ascii_value).strip("-")
    if not ascii_value:
        ascii_value = fallback
    return ascii_value[:max_length].strip("-") or fallback


def project_name(cwd: str | None) -> str:
    if not cwd:
        return "workspace"
    path = Path(cwd)
    return path.name or "workspace"


def auto_native_name(runtime: str, cwd: str | None, uid: str) -> str:
    project = ascii_slug(project_name(cwd))
    runtime_slug = ascii_slug(runtime, fallback="agent", max_length=16)
    return f"{project}-{runtime_slug}-{short_uid(uid, 4)}"


def normalize_alias(value: str) -> str:
    alias = value.strip().lower()
    if not ALIAS_RE.fullmatch(alias):
        raise ValueError(
            "alias 只能包含小写字母、数字、点、下划线、斜杠和连字符，长度 1–64"
        )
    if "//" in alias or alias.endswith("/"):
        raise ValueError("alias 不能包含连续斜杠或以斜杠结尾")
    return alias


def compact_title(value: str | None, max_length: int = 72) -> str | None:
    if not value:
        return None
    title = re.sub(r"\s+", " ", value).strip()
    if not title:
        return None
    if len(title) <= max_length:
        return title
    return title[: max_length - 1].rstrip() + "…"
