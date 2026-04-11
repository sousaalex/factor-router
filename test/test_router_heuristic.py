"""Testes do router híbrido/local."""
from __future__ import annotations

import asyncio
import unittest
from unittest.mock import AsyncMock, patch

from src.router import router as router_mod
from src.router.router import route


class TestRouterHeuristic(unittest.TestCase):
    def test_code_request_uses_small_fast_model(self) -> None:
        async def run() -> str:
            with patch.object(router_mod, "ROUTER_DECISION_MODE", "hybrid"):
                rr = await route("please fix this python bug and refactor the endpoint")
                return rr.model_id

        self.assertEqual(asyncio.run(run()), "qwen/qwen3.5-397b-a17b")

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


if __name__ == "__main__":
    unittest.main()
