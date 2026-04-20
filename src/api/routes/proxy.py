"""
src/api/routes/proxy.py

Endpoint principal do gateway — POST /v1/chat/completions.
Compatível com OpenAI SDK (drop-in replacement).

Fluxo:
1. Autentica a API Key (auth.py)
2. Valida e extrai os headers de contexto (context.py)
3. Chama o router para decidir o model_id
4. Faz proxy do request para o OpenRouter (stream ou non-stream)
5. Regista o custo no fim do turno

Este ficheiro define apenas o endpoint.
A lógica de proxy e streaming vive em src/gateway/proxy.py (próximo passo).
"""
from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse, StreamingResponse

from src.gateway.auth import AuthenticatedApp, authenticate
from src.gateway.context import GatewayContext

router = APIRouter()


@router.post(
    "/chat/completions",
    summary="Chat completions (OpenAI-compatible)",
    description="""
Main gateway endpoint. Accepts the same body the OpenAI SDK sends.

**The `model` field in the body is ignored** for normal chat — the gateway uses an internal
router to pick the model. Exception: when **`X-Conversation-Id` is exactly `generate-title`**
(title generation), the gateway skips the router and uses **`google/gemini-2.5-flash-lite`**
(same `POST /v1/chat/completions`; usage is tracked under a separate bucket from the chat turn).

Supports `stream: true` (SSE) and `stream: false` (full JSON).

Requires all `X-*` headers documented in the API description.
    """,
    tags=["proxy"],
)
async def chat_completions(
    request: Request,
    auth: Annotated[AuthenticatedApp, Depends(authenticate)],
    ctx: Annotated[GatewayContext, Depends(GatewayContext.from_headers)],
):
    """
    Proxy OpenAI-compatible: OpenRouter (default) ou Ollama local para model_id `ollama/…`
    quando OLLAMA_BASE_URL está definido. Routing automático de modelos.
    O app_id vem sempre da API Key — nunca de headers enviados pelo agente.
    """
    # Injeta o app_id da API Key no contexto
    ctx.app_id = auth.app_id
    ctx.upstream_env = auth.upstream_env

    from src.gateway.config import get_settings
    from src.gateway.proxy import handle_chat_completions
    return await handle_chat_completions(request, ctx, get_settings())


@router.post(
    "/audio/transcriptions",
    summary="Audio transcriptions (OpenAI-compatible)",
    description="""
Proxy para transcrição de áudio em formato OpenAI.

Requer `Authorization: Bearer <api_key>` e os mesmos headers `X-*` do chat
para rastreio/observabilidade por turno.
    """,
    tags=["proxy"],
)
async def audio_transcriptions(
    request: Request,
    auth: Annotated[AuthenticatedApp, Depends(authenticate)],
    ctx: Annotated[GatewayContext, Depends(GatewayContext.from_headers)],
):
    # Injeta o app_id da API Key no contexto
    ctx.app_id = auth.app_id
    ctx.upstream_env = auth.upstream_env

    from src.gateway.config import get_settings
    from src.gateway.proxy import handle_audio_transcriptions
    return await handle_audio_transcriptions(request, ctx, get_settings())


@router.post(
    "/audio/speech",
    summary="Audio speech / TTS (OpenAI-compatible)",
    description="""
Proxy para síntese de voz em formato OpenAI (`POST /v1/audio/speech`).

Corpo JSON: `model`, `input`, opcional `voice`, `response_format` (ex.: `opus`).

Requer `Authorization: Bearer <api_key>` e os mesmos headers `X-*` do chat
para rastreio e centro de custos.
""",
    tags=["proxy"],
)
async def audio_speech(
    request: Request,
    auth: Annotated[AuthenticatedApp, Depends(authenticate)],
    ctx: Annotated[GatewayContext, Depends(GatewayContext.from_headers)],
):
    ctx.app_id = auth.app_id
    ctx.upstream_env = auth.upstream_env

    from src.gateway.config import get_settings
    from src.gateway.proxy import handle_audio_speech

    return await handle_audio_speech(request, ctx, get_settings())