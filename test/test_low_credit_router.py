"""Router económico: teto de modelo + prompt budget (classifier)."""
from __future__ import annotations

import unittest

from src.gateway.model_policy import cap_model_for_low_openrouter_credit
from src.router.classifier_prompt import build_classifier_prompt


class TestCapLowCredit(unittest.TestCase):
    def test_disabled_unchanged(self) -> None:
        self.assertEqual(
            cap_model_for_low_openrouter_credit("openai/gpt-5.4-mini", balance_low=False),
            "openai/gpt-5.4-mini",
        )

    def test_complex_to_kimi(self) -> None:
        self.assertEqual(
            cap_model_for_low_openrouter_credit("openai/gpt-5.4-mini", balance_low=True),
            "moonshotai/kimi-k2.5",
        )

    def test_frontier_to_kimi(self) -> None:
        self.assertEqual(
            cap_model_for_low_openrouter_credit(
                "openai/gpt-5.4-mini",
                balance_low=True,
            ),
            "moonshotai/kimi-k2.5",
        )

    def test_reasoning_unchanged(self) -> None:
        self.assertEqual(
            cap_model_for_low_openrouter_credit("qwen/qwen3.6-plus", balance_low=True),
            "qwen/qwen3.6-plus",
        )

    def test_simple_tier_unchanged_under_cap(self) -> None:
        self.assertEqual(
            cap_model_for_low_openrouter_credit("qwen/qwen3.6-plus", balance_low=True),
            "qwen/qwen3.6-plus",
        )

    def test_reasoning_plus_unchanged(self) -> None:
        self.assertEqual(
            cap_model_for_low_openrouter_credit("moonshotai/kimi-k2.5", balance_low=True),
            "moonshotai/kimi-k2.5",
        )


class TestClassifierBudgetPrompt(unittest.TestCase):
    def test_low_balance_appends_block(self) -> None:
        sys_low, _ = build_classifier_prompt(
            user_message="hi",
            models=[{"id": "qwen/qwen3.6-plus", "tier": "reasoning", "pricing": {}, "description": "x"}],
            default_model="qwen/qwen3.6-plus",
            openrouter_balance_low=True,
        )
        self.assertIn("OPENROUTER PREPAID BALANCE IS LOW", sys_low)

    def test_normal_no_block(self) -> None:
        sys_ok, _ = build_classifier_prompt(
            user_message="hi",
            models=[{"id": "qwen/qwen3.6-plus", "tier": "reasoning", "pricing": {}, "description": "x"}],
            default_model="qwen/qwen3.6-plus",
            openrouter_balance_low=False,
        )
        self.assertNotIn("OPENROUTER PREPAID BALANCE IS LOW", sys_ok)


if __name__ == "__main__":
    unittest.main()
