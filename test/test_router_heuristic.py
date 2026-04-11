"""Testes do router heurístico local."""
from __future__ import annotations

import asyncio
import unittest
from unittest.mock import patch

from src.router import router as router_mod
from src.router.router import route


class TestRouterHeuristic(unittest.TestCase):
    def test_code_request_uses_small_fast_model(self) -> None:
        async def run() -> str:
            with patch.object(router_mod, "ROUTER_DECISION_MODE", "heuristic"):
                rr = await route("please fix this python bug and refactor the endpoint")
                return rr.model_id

        self.assertEqual(asyncio.run(run()), "x-ai/grok-code-fast-1")

    def test_multimodal_request_prefers_plus_model(self) -> None:
        async def run() -> str:
            content = [
                {"type": "text", "text": "analyze the screenshot and explain the issue"},
                {"type": "image_url", "image_url": {"url": "https://example.com/img.png"}},
            ]
            with patch.object(router_mod, "ROUTER_DECISION_MODE", "heuristic"):
                rr = await route(content)
                return rr.model_id

        self.assertEqual(asyncio.run(run()), "qwen/qwen3.5-plus-02-15")


if __name__ == "__main__":
    unittest.main()
