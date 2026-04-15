"""
Resolve o endpoint real e o nome do modelo na API a partir do model_id interno.

Convenção:
  - ollama/<name>     → POST {OLLAMA_BASE_URL}/v1/chat/completions, model=<name>
  - openrouter/<id>   → POST {UPSTREAM_URL}/chat/completions, model=<id>
  - <id> (sem prefixo) → OpenRouter (compatível com configs antigas), model=<id>
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from fastapi import HTTPException, status

if TYPE_CHECKING:
    from src.gateway.config import Settings


@dataclass(frozen=True)
class UpstreamTarget:
    """Destino HTTP para chat/completions (API estilo OpenAI)."""

    chat_completions_url: str
    api_model: str
    headers: dict[str, str]
    omit_stream_options: bool = False
    selected_env: str = ""
    api_key_source: str = ""


def _resolve_openrouter_api_key(
    settings: "Settings",
    preferred_env: str | None = None,
) -> tuple[str, str, str]:
    env = (preferred_env or "").strip().lower()
    if env == "dev":
        return (settings.openrouter_api_dev or "").strip(), "dev", "OPENROUTER_API_DEV"
    if env == "prod":
        return (settings.openrouter_api_prod or "").strip(), "prod", "OPENROUTER_API_PROD"
    return "", env, ""


def resolve_upstream(
    model_id: str,
    settings: "Settings",
    preferred_env: str | None = None,
) -> UpstreamTarget:
    mid = (model_id or "").strip()
    if not mid:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail={"error": "missing_model", "message": "model_id vazio."},
        )

    if mid.startswith("ollama/"):
        base = (settings.ollama_base_url or "").strip().rstrip("/")
        if not base:
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail={
                    "error": "ollama_not_configured",
                    "message": (
                        "Modelo ollama/… escolhido mas OLLAMA_BASE_URL não está definido "
                        "no gateway (.env). Ex.: http://localhost:11434 ou "
                        "http://host.docker.internal:11434 em Docker."
                    ),
                },
            )
        name = mid[len("ollama/") :].strip()
        if not name:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail={
                    "error": "invalid_ollama_model",
                    "message": "ID inválido: use ollama/<nome> (ex. ollama/gemma4:latest).",
                },
            )
        url = f"{base}/v1/chat/completions"
        return UpstreamTarget(
            chat_completions_url=url,
            api_model=name,
            headers={},
            omit_stream_options=bool(
                getattr(settings, "ollama_legacy_strip_stream_options", False)
            ),
        )

    if mid.startswith("openrouter/"):
        rest = mid[len("openrouter/") :].strip()
        if not rest:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail={
                    "error": "invalid_openrouter_model",
                    "message": "ID inválido: use openrouter/<modelo_openrouter>.",
                },
            )
        mid = rest

    ur = settings.upstream_url.strip().rstrip("/")
    api_key, env, api_key_source = _resolve_openrouter_api_key(
        settings,
        preferred_env=preferred_env,
    )
    if env not in {"dev", "prod"}:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail={
                "error": "upstream_env_not_mapped",
                "message": (
                    "API key sem ambiente mapeado (permitidos: dev|prod). "
                    "Contacte o administrador do FactorRouter para corrigir a configuração da key."
                ),
            },
        )
    if not api_key:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail={
                "error": "openrouter_api_key_not_configured",
                "message": (
                    "Nenhuma key OpenRouter disponível para este ambiente. "
                    "Configure OPENROUTER_API_DEV para dev ou OPENROUTER_API_PROD para prod."
                ),
            },
        )
    return UpstreamTarget(
        chat_completions_url=f"{ur}/chat/completions",
        api_model=mid,
        headers={"Authorization": f"Bearer {api_key}"},
        omit_stream_options=False,
        selected_env=env,
        api_key_source=api_key_source,
    )


def body_for_upstream_proxy(body: dict, target: UpstreamTarget) -> dict:
    """
    Copia o body OpenAI com model ajustado. stream_options só em stream=true
    (API OpenAI-compat; Ollama antigo pode falhar com stream_options em stream=false).
    """
    out = {**body, "model": target.api_model}
    if not bool(out.get("stream")):
        out.pop("stream_options", None)
    elif target.omit_stream_options:
        out.pop("stream_options", None)
    return out
