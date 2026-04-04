"""
Políticas opcionais por modelo (ex.: Gemini premium só para X-User-Id na allowlist;
outros utilizadores fazem downgrade para Kimi via OpenRouter, ou modelo configurado).
"""
from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from fastapi import HTTPException, status

if TYPE_CHECKING:
    from src.gateway.config import Settings
    from src.gateway.context import GatewayContext

logger = logging.getLogger(__name__)


def _parse_allowlist(raw: str) -> set[str]:
    return {x.strip() for x in (raw or "").split(",") if x.strip()}


def _user_in_premium_allowlist(settings: "Settings", ctx: "GatewayContext") -> bool:
    allowed_raw = _parse_allowlist(settings.gateway_premium_model_user_allowlist)
    if not allowed_raw:
        return False
    allowed_lower = {x.lower() for x in allowed_raw}
    uid = ctx.user_id
    if uid is None:
        return False
    return str(uid).strip().lower() in allowed_lower


def apply_premium_model_policy(settings: "Settings", ctx: "GatewayContext", model_id: str) -> str:
    """
    Se o router escolheu o modelo premium (ex. Gemini Pro) e o utilizador não está
    na allowlist, devolve GATEWAY_PREMIUM_MODEL_FALLBACK (Kimi por defeito) — sem 403.

    Allowlist vazia com premium definido → 503 (config inválida).
    """
    premium = (settings.gateway_premium_model or "").strip()
    if not premium or model_id != premium:
        return model_id

    allowed_raw = _parse_allowlist(settings.gateway_premium_model_user_allowlist)
    if not allowed_raw:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail={
                "error": "premium_model_misconfigured",
                "message": (
                    "Modelo premium configurado sem GATEWAY_PREMIUM_MODEL_USER_ALLOWLIST. "
                    "Define os X-User-Id permitidos no .env."
                ),
            },
        )

    if _user_in_premium_allowlist(settings, ctx):
        return model_id

    fb = (settings.gateway_premium_model_fallback or "").strip() or "openrouter/moonshotai/kimi-k2.5"
    logger.info(
        "[ModelPolicy] Downgrade %s → %s (user_id=%r não está na allowlist premium)",
        model_id,
        fb,
        ctx.user_id,
    )
    return fb


def cap_model_for_low_openrouter_credit(model_id: str, *, balance_low: bool) -> str:
    """
    Com saldo OpenRouter baixo (snapshot BD), não usar tiers caros alojados no OpenRouter.
    Modelos nativos fora do OpenRouter (ex.: gemini/* direto) não são afetados.
    """
    if not balance_low or not model_id.startswith("openrouter/"):
        return model_id
    from src.router.router import get_model_info

    info = get_model_info(model_id)
    tier = (info or {}).get("tier")
    if tier in ("complex", "frontier"):
        return "openrouter/moonshotai/kimi-k2.5"
    return model_id
