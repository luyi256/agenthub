from __future__ import annotations

import unittest

from agent_hub.runtime_options import (
    runtime_options,
    validate_runtime_selection,
)


class RuntimeOptionsTests(unittest.TestCase):
    def test_tcodex_options_include_catalog_models(self) -> None:
        options = runtime_options()["tcodex"]
        model_ids = {item["id"] for item in options["models"]}
        self.assertIn("gpt-5.6-luna", model_ids)

    def test_validation_accepts_default_and_supported_selection(self) -> None:
        self.assertEqual(
            validate_runtime_selection("tcodex", None, None),
            (None, None),
        )
        self.assertEqual(
            validate_runtime_selection(
                "tcodex", "gpt-5.6-luna", "high"
            ),
            ("gpt-5.6-luna", "high"),
        )

    def test_validation_rejects_unknown_catalog_model(self) -> None:
        with self.assertRaisesRegex(ValueError, "当前不支持模型"):
            validate_runtime_selection(
                "tcodex", "does-not-exist", "medium"
            )
