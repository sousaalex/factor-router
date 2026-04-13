"""Testes do router híbrido/local."""
from __future__ import annotations

import asyncio
import unittest
from unittest.mock import AsyncMock, patch

from src.router import router as router_mod
from src.router.router import route


class TestRouterHeuristic(unittest.TestCase):
    def test_code_request_prefers_qwen36(self) -> None:
        async def run() -> str:
            with patch.object(router_mod, "ROUTER_DECISION_MODE", "hybrid"):
                rr = await route("please fix this python bug and refactor the endpoint")
                return rr.model_id

        self.assertEqual(asyncio.run(run()), "qwen/qwen3.6-plus")

    def test_ambiguous_request_uses_llm_in_hybrid_mode(self) -> None:
        async def run() -> str:
            with patch.object(router_mod, "ROUTER_DECISION_MODE", "hybrid"):
                with patch.object(router_mod, "OLLAMA_BASE_URL", "http://localhost:11434"):
                    with patch.object(
                        router_mod,
                        "_call_classifier",
                        new=AsyncMock(return_value=('{"model": "qwen/qwen3.5-plus-02-15"}', 10, 4, 1.0)),
                    ):
                        rr = await route("need a good model for this")
                return rr.model_id

        self.assertEqual(asyncio.run(run()), "qwen/qwen3.5-plus-02-15")

    def test_required_tool_choice_forces_gpt41mini(self) -> None:
        async def run() -> str:
            with patch.object(router_mod, "ROUTER_DECISION_MODE", "hybrid"):
                rr = await route(
                    "find customer and create invoice",
                    tool_choice="required",
                )
                return rr.model_id

        self.assertEqual(asyncio.run(run()), "openai/gpt-4.1-mini")

    def test_tier3_with_multimodal_prefers_mimo(self) -> None:
        async def run() -> str:
            with patch.object(router_mod, "ROUTER_DECISION_MODE", "llm"):
                with patch.object(router_mod, "OLLAMA_BASE_URL", "http://localhost:11434"):
                    with patch.object(
                        router_mod,
                        "_call_classifier",
                        new=AsyncMock(return_value=('{"tier": 3}', 10, 4, 1.0)),
                    ):
                        rr = await route(
                            [
                                {"type": "text", "text": "analyze this video and image with tools"},
                                {"type": "image_url", "image_url": {"url": "https://example.com/x.png"}},
                            ]
                        )
                return rr.model_id

        self.assertEqual(asyncio.run(run()), "xiaomi/mimo-v2-omni")

    def test_required_tool_choice_forces_gpt41mini_even_in_llm_mode(self) -> None:
        async def run() -> str:
            with patch.object(router_mod, "ROUTER_DECISION_MODE", "llm"):
                with patch.object(router_mod, "OLLAMA_BASE_URL", "http://localhost:11434"):
                    with patch.object(
                        router_mod,
                        "_call_classifier",
                        new=AsyncMock(return_value=('{"tier": 3}', 10, 4, 1.0)),
                    ):
                        rr = await route(
                            "create invoice and resolve many2one",
                            tool_choice="required",
                        )
                return rr.model_id

        self.assertEqual(asyncio.run(run()), "openai/gpt-4.1-mini")

    def test_llm_mode_parse_error_falls_back_to_heuristic(self) -> None:
        async def run() -> str:
            with patch.object(router_mod, "ROUTER_DECISION_MODE", "llm"):
                with patch.object(router_mod, "OLLAMA_BASE_URL", "http://localhost:11434"):
                    with patch.object(
                        router_mod,
                        "_call_classifier",
                        new=AsyncMock(return_value=("not-json", 10, 4, 1.0)),
                    ):
                        rr = await route("please fix this python bug and refactor the endpoint")
                return rr.model_id

        self.assertEqual(asyncio.run(run()), "qwen/qwen3.6-plus")

    def test_llm_mode_timeout_falls_back_to_heuristic(self) -> None:
        async def run() -> str:
            with patch.object(router_mod, "ROUTER_DECISION_MODE", "llm"):
                with patch.object(router_mod, "OLLAMA_BASE_URL", "http://localhost:11434"):
                    with patch.object(
                        router_mod,
                        "_call_classifier",
                        new=AsyncMock(side_effect=router_mod.httpx.TimeoutException("timeout")),
                    ):
                        rr = await route("please fix this python bug and refactor the endpoint")
                return rr.model_id

        self.assertEqual(asyncio.run(run()), "qwen/qwen3.6-plus")


if __name__ == "__main__":
    unittest.main()
