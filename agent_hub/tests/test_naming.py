from __future__ import annotations

import unittest

from agent_hub.naming import (
    ascii_slug,
    auto_native_name,
    normalize_alias,
    session_uid,
)


class NamingTests(unittest.TestCase):
    def test_session_uid_is_stable_and_runtime_scoped(self) -> None:
        first = session_uid("tcodex", "thread-1")
        self.assertEqual(first, session_uid("tcodex", "thread-1"))
        self.assertNotEqual(first, session_uid("codex", "thread-1"))

    def test_ascii_slug_has_fallback(self) -> None:
        self.assertEqual(ascii_slug("生成"), "workspace")
        self.assertEqual(ascii_slug("Creative Agent"), "creative-agent")

    def test_alias_validation(self) -> None:
        self.assertEqual(normalize_alias("Generation/Video-2"), "generation/video-2")
        with self.assertRaises(ValueError):
            normalize_alias("generation video")

    def test_auto_native_name_has_short_uid(self) -> None:
        uid = session_uid("tcodex", "thread-1")
        name = auto_native_name("tcodex", "/home/luyi/generation", uid)
        self.assertTrue(name.startswith("generation-tcodex-"))


if __name__ == "__main__":
    unittest.main()
