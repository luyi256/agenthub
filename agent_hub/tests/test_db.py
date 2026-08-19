from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from agent_hub.db import AliasConflictError, HubDatabase


def record(runtime: str, runtime_id: str, cwd: str) -> dict:
    return {
        "runtime": runtime,
        "runtime_id": runtime_id,
        "cwd": cwd,
        "status": "online",
        "presence": "online",
        "attach_state": "observable",
        "capabilities": {"observable": True},
        "metadata": {},
    }


class DatabaseTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.db = HubDatabase(Path(self.tmp.name) / "hub.sqlite3")
        self.db.replace_discovery(
            [
                record("claude", "a", "/tmp/project"),
                record("tcodex", "b", "/tmp/project"),
            ],
            ["claude", "tcodex"],
        )

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def test_alias_survives_discovery_refresh(self) -> None:
        first = self.db.list_sessions()[0]
        self.db.patch_session(first["session_uid"], alias="project/video")
        self.db.replace_discovery(
            [
                record("claude", "a", "/tmp/project"),
                record("tcodex", "b", "/tmp/project"),
            ],
            ["claude", "tcodex"],
        )
        self.assertEqual(
            self.db.get_session(first["session_uid"])["alias"], "project/video"
        )

    def test_alias_is_unique(self) -> None:
        first, second = self.db.list_sessions()
        self.db.patch_session(first["session_uid"], alias="project/video")
        with self.assertRaises(AliasConflictError):
            self.db.patch_session(second["session_uid"], alias="project/video")

    def test_link_uses_session_uid(self) -> None:
        first, second = self.db.list_sessions()
        link = self.db.create_link(
            first["session_uid"],
            second["session_uid"],
            "task_handoff",
            "artifact.ready",
        )
        self.assertEqual(link["source_session_uid"], first["session_uid"])
        self.assertEqual(link["status"], "draft")

    def test_managed_session_and_messages(self) -> None:
        session = self.db.register_managed_session(
            runtime="tcodex",
            runtime_id="managed-thread",
            runtime_version="1.0.0",
            native_name="project-video-abcd",
            alias="project/video",
            user_title="Video generation",
            role="video_generator",
            cwd="/tmp/project",
            transport="app-server",
            managed_config={"permission_profile": "safe"},
            capabilities={"chat": True},
        )
        self.assertTrue(session["managed"])
        message = self.db.add_message(
            session["session_uid"], "assistant", status="streaming"
        )
        self.db.append_message_delta(message["message_id"], "hello")
        completed = self.db.complete_message(message["message_id"])
        self.assertEqual(completed["content"], "hello")
        self.assertEqual(completed["status"], "completed")
        synced = self.db.sync_message(
            message["message_id"],
            content="replayed",
            status="completed",
            metadata_patch={"source": "worker-state"},
        )
        self.assertEqual(synced["content"], "replayed")
        self.assertEqual(synced["metadata"]["source"], "worker-state")

    def test_close_session_preserves_history_and_finalizes_pending_state(
        self,
    ) -> None:
        session = self.db.register_managed_session(
            runtime="tcodex",
            runtime_id="close-thread",
            runtime_version="1.0.0",
            native_name="close-unit",
            alias="close/unit",
            user_title=None,
            role=None,
            cwd="/tmp/project",
            transport="tmux-worker",
            managed_config={"permission_profile": "safe"},
            capabilities={"chat": True},
        )
        streaming = self.db.add_message(
            session["session_uid"],
            "assistant",
            status="streaming",
        )
        queued = self.db.add_message(
            session["session_uid"],
            "human",
            "later",
            status="queued",
        )

        closed = self.db.close_session(
            session["session_uid"],
            reason="unit_test",
        )

        assert closed
        self.assertEqual(closed["status"], "closed")
        self.assertEqual(closed["presence"], "offline")
        self.assertEqual(
            closed["metadata"]["closed"]["reason"],
            "unit_test",
        )
        self.assertEqual(
            self.db.get_message(streaming["message_id"])["status"],
            "interrupted",
        )
        self.assertEqual(
            self.db.get_message(queued["message_id"])["status"],
            "cancelled",
        )


if __name__ == "__main__":
    unittest.main()
