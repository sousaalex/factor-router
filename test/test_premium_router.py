"""
Testes do classificador (router) + política premium (Claude só na allowlist → Kimi).

Correr na raiz do repo:
    uv run python -m unittest discover -s test -v
    ./test/run_tests.sh
"""
from __future__ import annotations

import asyncio
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from fastapi import HTTPException

from src.gateway.context import GatewayContext
from src.gateway.model_policy import apply_premium_model_policy
from src.router import router as router_mod
from src.router.router import route


PREMIUM = "anthropic/claude-sonnet-4.6"
FALLBACK = "moonshotai/kimi-k2.5"


def _ctx(user_id: str | None) -> GatewayContext:
    return GatewayContext(
        turn_id="00000000-0000-4000-8000-000000000001",
        session_id="s1",
        conversation_id=None,
        user_message="test",
        user_id=user_id,
        user_name=None,
        user_email=None,
        company_id=None,
        company_name=None,
    )


def _settings(
    *,
    premium: str = PREMIUM,
    allowlist: str = "allowed-user,OTHER-MAC",
    fallback: str = FALLBACK,
) -> SimpleNamespace:
    return SimpleNamespace(
        gateway_premium_model=premium,
        gateway_premium_model_user_allowlist=allowlist,
        gateway_premium_model_fallback=fallback,
    )


class TestPremiumModelPolicy(unittest.TestCase):
    """apply_premium_model_policy — sem rede."""

    def test_non_premium_model_unchanged(self) -> None:
        s = _settings()
        ctx = _ctx("anyone")
        out = apply_premium_model_policy(s, ctx, "qwen/qwen3.5-397b-a17b")
        self.assertEqual(out, "qwen/qwen3.5-397b-a17b")

    def test_premium_user_on_allowlist_keeps_claude(self) -> None:
        s = _settings(allowlist="allowed-user,foo")
        ctx = _ctx("allowed-user")
        out = apply_premium_model_policy(s, ctx, PREMIUM)
        self.assertEqual(out, PREMIUM)

    def test_allowlist_case_insensitive(self) -> None:
        s = _settings(allowlist="AbC")
        ctx = _ctx("abc")
        out = apply_premium_model_policy(s, ctx, PREMIUM)
        self.assertEqual(out, PREMIUM)

    def test_premium_user_not_on_allowlist_downgrade_kimi(self) -> None:
        s = _settings(allowlist="only-vip")
        ctx = _ctx("stranger")
        out = apply_premium_model_policy(s, ctx, PREMIUM)
        self.assertEqual(out, FALLBACK)

    def test_premium_user_id_null_downgrades(self) -> None:
        s = _settings(allowlist="vip")
        ctx = _ctx(None)
        out = apply_premium_model_policy(s, ctx, PREMIUM)
        self.assertEqual(out, FALLBACK)

    def test_premium_configured_empty_allowlist_503(self) -> None:
        s = _settings(allowlist="  ,  ")
        ctx = _ctx("any")
        with self.assertRaises(HTTPException) as cm:
            apply_premium_model_policy(s, ctx, PREMIUM)
        self.assertEqual(cm.exception.status_code, 503)
        self.assertIn("premium_model_misconfigured", str(cm.exception.detail))

    def test_premium_disabled_empty_model_skips_policy(self) -> None:
        s = SimpleNamespace(
            gateway_premium_model="",
            gateway_premium_model_user_allowlist="",
            gateway_premium_model_fallback=FALLBACK,
        )
        ctx = _ctx(None)
        out = apply_premium_model_policy(s, ctx, PREMIUM)
        self.assertEqual(out, PREMIUM)


class TestRouterThenPolicy(unittest.TestCase):
    """Simula classificador a devolver Claude; política aplica allowlist."""

    def test_classifier_claude_allowlisted_stays_claude(self) -> None:
        async def run() -> str:
            mock_ret = (f'{{"model": "{PREMIUM}"}}', 10, 10, 1.0)
            with patch.object(router_mod, "OLLAMA_BASE_URL", "http://localhost:11434"):
                with patch.object(
                    router_mod,
                    "_call_classifier",
                    new=AsyncMock(return_value=mock_ret),
                ):
                    rr = await route("preciso de capacidade máxima explícita claude frontier")
                    return apply_premium_model_policy(
                        _settings(allowlist="mac-client"),
                        _ctx("mac-client"),
                        rr.model_id,
                    )

        self.assertEqual(asyncio.run(run()), PREMIUM)

    def test_classifier_claude_not_allowlisted_becomes_kimi(self) -> None:
        async def run() -> str:
            mock_ret = (f'{{"model": "{PREMIUM}"}}', 10, 10, 1.0)
            with patch.object(router_mod, "OLLAMA_BASE_URL", "http://localhost:11434"):
                with patch.object(
                    router_mod,
                    "_call_classifier",
                    new=AsyncMock(return_value=mock_ret),
                ):
                    rr = await route("ignored — mock fixes model")
                    return apply_premium_model_policy(
                        _settings(allowlist="only-vip"),
                        _ctx("other-user"),
                        rr.model_id,
                    )

        self.assertEqual(asyncio.run(run()), FALLBACK)


if __name__ == "__main__":
    unittest.main()
