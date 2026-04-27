"""
src/gateway/proxy.py

Lógica central do gateway — orquestra router, proxy SSE/JSON e acumulador.

Fluxo por call ao LLM:
    1. Lê o body do request (OpenAI-compat JSON)
    2. Verifica se o X-Turn-Id já tem balde em memória
       - Não tem → primeiro call do turno → chama router UMA VEZ → abre balde
       - Tem     → call seguinte do mesmo turno → usa model_id do balde
    3. Injeta o model_id real no body (substitui o que a app enviou)
    4. Adiciona stream_options para garantir tokens reais no chunk final
    5. Faz proxy ao provider (OpenRouter ou Ollama via ollama/…) — stream SSE ou JSON completo
    6. Extrai tokens da resposta e regista no acumulador
    7. Flush do balde ocorre via:
       - POST /v1/turns/{turn_id}/end (explícito, recomendado para MemGPT/tool-only)
       - TTL cleanup (fallback — 15s de inatividade, ver app.py _cleanup_loop)
"""
from __future__ import annotations

import asyncio
import json
import logging
import time
from typing import AsyncIterator, TYPE_CHECKING

import httpx
from fastapi import HTTPException, Request, status
from fastapi.responses import JSONResponse, PlainTextResponse, Response, StreamingResponse

from src.gateway.accumulator import get_accumulator
from src.gateway.config import Settings
from src.gateway.key_store import get_key_store
from src.gateway.model_policy import (
    apply_premium_model_policy,
    cap_model_for_low_openrouter_credit,
)
from src.gateway.provider_upstream import (
    UpstreamTarget,
    body_for_upstream_proxy,
    resolve_upstream,
)
from src.gateway.openai_message_content import flatten_openai_message_content
from src.router.router import GATEWAY_TITLE_MODEL_ID, route as router_route
from src.usage.openrouter_credits_state import read_remaining_usd_snapshot
from src.gateway.resilience import (
    get_circuit_breaker,
    record_model_failure,
    record_model_success,
    retry_upstream_call,
)

if TYPE_CHECKING:
    from src.gateway.context import GatewayContext

logger = logging.getLogger(__name__)

# ─────────────────────────────────────────────────────────────────────────────
# Quota por app (tenant) em USD — quanto esta integração pode consumir no router.
# É um teto interno do Factor Router; não é o saldo de créditos OpenRouter da org.
# ─────────────────────────────────────────────────────────────────────────────

def _app_budget_exceeded_body(
    app_id: str,
    spend_cap_usd: float,
    spent_usd_total: float,
) -> dict:
    return {
        "error":           "app_budget_exceeded",
        "message": (
            "This app has reached its maximum allowed usage (USD) on this gateway. "
            "This limit is per app/API integration and is not your OpenRouter account balance. "
            "Raise spend_cap_usd via PATCH /admin/apps/{app_id} or contact support."
        ),
        "app_id":          app_id,
        "spend_cap_usd":   spend_cap_usd,
        "spent_usd_total": spent_usd_total,
    }


async def _enforce_app_budget_or_raise(ctx: "GatewayContext") -> None:
    """
    Aplica o mesmo teto USD por app usado no chat.
    Lança HTTPException/JSONResponse em caso de app inválida/inativa/excedida.
    """
    try:
        spend_status = await get_key_store().get_app_spend_status(ctx.app_id)
    except Exception as e:
        logger.exception("[Proxy] Falha ao ler orçamento da app %s: %s", ctx.app_id, e)
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail={
                "error": "budget_check_unavailable",
                "message": "Could not verify app usage limit. Try again later.",
            },
        ) from e

    if spend_status is None:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail={
                "error": "app_not_found",
                "message": "App associated with this key was not found.",
            },
        )
    if not spend_status["is_active"]:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail={
                "error": "app_disabled",
                "message": "This app is disabled.",
            },
        )
    if spend_status["spent_usd_total"] >= spend_status["spend_cap_usd"]:
        raise HTTPException(
            status_code=status.HTTP_402_PAYMENT_REQUIRED,
            detail=_app_budget_exceeded_body(
                ctx.app_id,
                spend_status["spend_cap_usd"],
                spend_status["spent_usd_total"],
            ),
        )


# ─────────────────────────────────────────────────────────────────────────────
# Helpers — extração de tokens
# ─────────────────────────────────────────────────────────────────────────────

def _extract_usage_from_chunk(chunk_data: dict) -> tuple[int, int, int]:
    """
    Extrai tokens de um chunk SSE.
    Devolve (prompt_tokens, completion_tokens, tool_calls_count).

    O OpenRouter inclui usage no chunk final quando pedimos
    stream_options={"include_usage": true}.
    """
    usage = chunk_data.get("usage") or {}
    prompt_tokens     = int(usage.get("prompt_tokens", 0))
    completion_tokens = int(usage.get("completion_tokens", 0))

    # tool_calls NÃO são contadas por chunk — cada tool vem fragmentada em
    # múltiplos chunks (um por argumento), o que causaria contagem errada.
    # A contagem real é feita no chunk com finish_reason=tool_calls abaixo.
    return prompt_tokens, completion_tokens, 0


def _extract_usage_from_response(response_data: dict) -> tuple[int, int, int]:
    """
    Extrai tokens de uma resposta JSON completa (stream=false).
    Devolve (prompt_tokens, completion_tokens, tool_calls_count).
    """
    usage = response_data.get("usage") or {}
    prompt_tokens     = int(usage.get("prompt_tokens", 0))
    completion_tokens = int(usage.get("completion_tokens", 0))

    choices = response_data.get("choices") or []
    tool_calls_count = 0
    for choice in choices:
        message = choice.get("message") or {}
        tool_calls_count += len(message.get("tool_calls") or [])

    return prompt_tokens, completion_tokens, tool_calls_count


def _classify_upstream_error(status_code: int, upstream_text: str) -> str:
    """
    Classifica erros do provider para códigos estáveis no cliente.
    """
    text_l = (upstream_text or "").lower()
    if status_code in {401, 403}:
        return "upstream_auth_failed"
    if status_code == 402:
        return "upstream_budget_exhausted"
    if status_code == 429:
        if any(k in text_l for k in ("credit", "credits", "quota", "balance", "insufficient")):
            return "upstream_budget_exhausted"
        return "upstream_rate_limited"
    return "upstream_error"


def _sse_data_event(payload: dict) -> bytes:
    """Serializa um evento SSE `data:` com JSON válido."""
    return f"data: {json.dumps(payload, ensure_ascii=False)}\n\n".encode("utf-8")


def _sse_done_event() -> bytes:
    """Evento SSE de fim de stream."""
    return b"data: [DONE]\n\n"


def _build_non_sse_stream_payload(
    *,
    upstream_status: int,
    upstream_content_type: str,
    upstream_text: str,
    model_id: str,
) -> dict:
    """
    Constrói um payload SSE seguro quando o upstream não devolve `text/event-stream`.
    Se o upstream devolveu uma resposta JSON completa, tentamos preservá-la como um
    único chunk SSE. Caso contrário, devolvemos um erro normalizado.
    """
    try:
        parsed = json.loads(upstream_text)
    except json.JSONDecodeError:
        parsed = None

    if isinstance(parsed, dict):
        choices = parsed.get("choices") or []
        if choices:
            first_choice = choices[0] if isinstance(choices[0], dict) else {}
            message = first_choice.get("message") or {}
            content = message.get("content")
            if isinstance(content, str) and content:
                chunk: dict = {
                    "id": parsed.get("id"),
                    "object": "chat.completion.chunk",
                    "created": parsed.get("created") or int(time.time()),
                    "model": parsed.get("model") or model_id,
                    "choices": [
                        {
                            "index": 0,
                            "delta": {"content": content},
                            "finish_reason": first_choice.get("finish_reason") or "stop",
                        }
                    ],
                }
                usage = parsed.get("usage")
                if isinstance(usage, dict):
                    chunk["usage"] = usage
                return {
                    "kind": "chunk",
                    "chunk": chunk,
                }

        payload = {
            "error": "upstream_non_sse_response",
            "message": "Upstream returned JSON instead of SSE during a streaming request.",
            "upstream_status": upstream_status,
            "upstream_content_type": upstream_content_type or "unknown",
            "upstream_body": upstream_text[:500],
        }
        if "error" in parsed:
            payload["upstream_error"] = parsed["error"]
        return {
            "kind": "error",
            "payload": payload,
        }

    return {
        "kind": "error",
        "payload": {
            "error": "upstream_non_sse_response",
            "message": "Upstream returned a non-SSE response during a streaming request.",
            "upstream_status": upstream_status,
            "upstream_content_type": upstream_content_type or "unknown",
            "upstream_body": upstream_text[:500],
        },
    }





# ─────────────────────────────────────────────────────────────────────────────
# Flush helper — grava no DB após fim do turno
# ─────────────────────────────────────────────────────────────────────────────

async def _flush_and_record(turn_id: str) -> None:
    """
    Faz flush do balde e grava o registo no Postgres.
    Chamado via asyncio.create_task — não bloqueia o agente.
    Logging completo — qualquer falha é visível nos logs.
    """
    accumulator = get_accumulator()
    #print("[Flush] Starting flush for turn [%s]", turn_id[:8])

    record = await accumulator.flush(turn_id)
    if record is None:
        #print("[Flush] No bucket found for turn [%s] — already flushed or never opened", turn_id[:8])
        return

    # print(
    #     "[Flush] Bucket ready [%s] — %d tokens, %d llm_calls, source=%s",
    #     turn_id[:8],
    #     record.get("total_tokens", 0),
    #     record.get("meta", {}).get("llm_calls_count", 0),
    #     record.get("meta", {}).get("source", "?"),
    # )

    try:
        from src.usage.service import record_turn_usage
        await record_turn_usage(**record)
        #print("[Flush] Successfully written to DB for turn [%s]", turn_id[:8])
    except Exception as e:
        print(
            "[Flush] FAILED to write turn [%s] to DB: %s — record: %s",
            turn_id[:8],
            e,
            {k: v for k, v in record.items() if k != "meta"},
        )


def _create_flush_task(turn_id: str) -> None:
    """
    Cria a task de flush com handler de excepção explícito.
    asyncio.create_task descarta excepções silenciosamente — isto evita isso.
    """
    task = asyncio.create_task(_flush_and_record(turn_id))

    def _on_error(t: asyncio.Task) -> None:
        if t.cancelled():
            return
        exc = t.exception()
        # if exc:
        #     print(
        #         "[Flush] Unhandled exception in flush task for turn [%s]: %s",
        #         turn_id[:8],
        #         exc,
        #     )

    task.add_done_callback(_on_error)


# ─────────────────────────────────────────────────────────────────────────────
# Streaming proxy
# ─────────────────────────────────────────────────────────────────────────────

async def _proxy_stream(
    body: dict,
    ctx: GatewayContext,
    settings: Settings,
    upstream_target: UpstreamTarget,
    upstream_env: str,
) -> StreamingResponse:
    """
    Faz proxy de um request com stream=True.
    Passa chunks SSE ao agente sem buffering.
    Extrai tokens para o acumulador.
    
    NOTA: O fim do turno NÃO é detectado automaticamente via finish_reason.
    O agente deve chamar POST /v1/turns/{turn_id}/end quando terminar,
    ou o TTL cleanup irá gravar o turno após 15s de inatividade.
    """
    accumulator = get_accumulator()

    async def generate() -> AsyncIterator[bytes]:
        #print(f"[Proxy] generate() called for turn [{ctx.turn_id[:8]}] — new stream connection")

        total_prompt      = 0
        total_completion  = 0
        total_tool_calls  = 0
        tool_call_indices: set[int] = set()  # índices únicos de tool_calls no stream
        last_stream_touch = 0.0

        work = body_for_upstream_proxy(body, upstream_target)
        cb = get_circuit_breaker()
        model_id = body.get("model", "unknown")
        
        # Check circuit breaker before attempting
        if cb.is_open(model_id):
            logger.warning("[Proxy] Circuit OPEN for model %s — returning 503", model_id)
            payload = {
                "error": "circuit_breaker_open",
                "message": f"Model {model_id} is temporarily unavailable due to repeated failures. Try again later.",
                "model": model_id,
            }
            yield f"data: {json.dumps(payload, ensure_ascii=False)}\n\n".encode("utf-8")
            return
        
        async def _do_stream_request() -> httpx.Response:
            client = httpx.AsyncClient(timeout=settings.upstream_timeout)
            try:
                response = await client.stream(
                    "POST",
                    upstream_target.chat_completions_url,
                    headers=upstream_target.headers,
                    json=work,
                )
                return response
            except Exception:
                await client.aclose()
                raise
        
        # For streaming, we can't use retry_upstream_call directly because
        # we need to handle the streaming response. We do a single attempt
        # with circuit breaker tracking.
        try:
            async with httpx.AsyncClient(timeout=settings.upstream_timeout) as client:
                async with client.stream(
                    "POST",
                    upstream_target.chat_completions_url,
                    headers=upstream_target.headers,
                    json=work,
                ) as upstream:

                    if upstream.status_code >= 400:
                        error_body = await upstream.aread()
                        raw_error_text = error_body.decode("utf-8", errors="replace").strip()
                        error_code = _classify_upstream_error(
                            upstream.status_code,
                            raw_error_text,
                        )
                        
                        # Track failure for circuit breaker and fallback
                        cb.record_failure(model_id)
                        fallback = record_model_failure(model_id)
                        
                        print(
                            "[ProxyUpstreamError] app_id=%s env=%s source=%s status=%s body=%s"
                            % (
                                ctx.app_id,
                                upstream_env,
                                upstream_target.api_key_source or "unknown",
                                upstream.status_code,
                                error_body[:300],
                            )
                        )
                        
                        # If we have a fallback model, include it in the error
                        payload = {
                            "error": error_code,
                            "upstream_status": upstream.status_code,
                            "upstream_env": upstream_env,
                            "api_key_source": upstream_target.api_key_source or "unknown",
                            "message": "Upstream provider returned an error.",
                            "upstream_body": raw_error_text[:500],
                        }
                        if fallback:
                            payload["suggested_fallback"] = fallback
                            payload["message"] += f" Consider retrying with model: {fallback}"
                        
                        yield f"data: {json.dumps(payload, ensure_ascii=False)}\n\n".encode("utf-8")
                        return
                    
                    # Success — record it
                    cb.record_success(model_id)
                    record_model_success(model_id)

                    content_type = (upstream.headers.get("content-type") or "").lower()
                    if "text/event-stream" not in content_type:
                        raw_body = await upstream.aread()
                        raw_text = raw_body.decode("utf-8", errors="replace").strip()
                        non_sse = _build_non_sse_stream_payload(
                            upstream_status=upstream.status_code,
                            upstream_content_type=content_type,
                            upstream_text=raw_text,
                            model_id=model_id,
                        )
                        if non_sse["kind"] == "chunk":
                            yield _sse_data_event(non_sse["chunk"])
                            yield _sse_done_event()
                        else:
                            yield _sse_data_event(non_sse["payload"])
                        return

                    bid = ctx.accumulator_bucket_id
                    await accumulator.touch_activity(bid)
                    last_stream_touch = time.monotonic()

                    # Buffer para reconstruir linhas SSE a partir de bytes brutos.
                    # aiter_lines() faz decode UTF-8 (perde bytes inválidos → "???")
                    # e adiciona \n\n a cada linha, corrompendo o formato SSE original.
                    # aiter_bytes() preserva os bytes exatos do upstream.
                    sse_buffer = b""
                    async for chunk in upstream.aiter_bytes():
                        now = time.monotonic()
                        if now - last_stream_touch >= 15:
                            last_stream_touch = now
                            await accumulator.touch_activity(bid)

                        # Passa os bytes brutos ao cliente imediatamente (pass-through fiel)
                        yield chunk

                        # Acumula no buffer para extração de tokens
                        sse_buffer += chunk

                        # Processa linhas completas do buffer (separadas por \n)
                        while b"\n" in sse_buffer:
                            line, sse_buffer = sse_buffer.split(b"\n", 1)
                            line_str = line.decode("utf-8", errors="replace").strip()

                            if not line_str:
                                continue

                            if line_str.startswith("data: "):
                                data_str = line_str[6:].strip()
                                if data_str == "[DONE]":
                                    continue  # fim do stream SSE
                                try:
                                    chunk_data = json.loads(data_str)
                                    p, c, t = _extract_usage_from_chunk(chunk_data)
                                    total_prompt     += p
                                    total_completion += c

                                    # Rastreia índices únicos de tool_calls no stream
                                    # Cada tool_call tem um índice único — mesmo que venha
                                    # fragmentada em muitos chunks, o índice não muda
                                    choices = chunk_data.get("choices") or []
                                    for choice in choices:
                                        delta = choice.get("delta") or {}
                                        for tc in (delta.get("tool_calls") or []):
                                            idx = tc.get("index")
                                            if idx is not None:
                                                tool_call_indices.add(idx)
                                    total_tool_calls = len(tool_call_indices)

                                except json.JSONDecodeError:
                                    pass

        except httpx.TimeoutException:
            #print("Timeout no upstream para turno [%s]", ctx.turn_id[:8])
            yield b'data: {"error": "upstream_timeout"}\n\n'

        finally:
            # Regista tokens deste call no acumulador
            bid = ctx.accumulator_bucket_id
            await accumulator.record(
                turn_id=bid,
                prompt_tokens=total_prompt,
                completion_tokens=total_completion,
                tool_calls_in_call=total_tool_calls,
            )
            # NOTA: flush automático por finish_reason foi removido.
            # O turno só é fechado via:
            #   1. POST /v1/turns/{turn_id}/end (explícito, recomendado para MemGPT)
            #   2. TTL cleanup (fallback de segurança — 15s de inatividade)

    return StreamingResponse(
        generate(),
        media_type="text/event-stream",
        headers={
            "Cache-Control":     "no-cache",
            "X-Accel-Buffering": "no",
            "X-Factor-Upstream-Env": upstream_env,
        },
    )


# ─────────────────────────────────────────────────────────────────────────────
# Non-streaming proxy
# ─────────────────────────────────────────────────────────────────────────────

async def _periodic_bucket_touch(turn_id: str, accumulator) -> None:
    """Mantém o balde vivo durante um POST longo (stream=False)."""
    try:
        while True:
            await asyncio.sleep(15)
            await accumulator.touch_activity(turn_id)
    except asyncio.CancelledError:
        return


async def _proxy_json(
    body: dict,
    ctx: GatewayContext,
    settings: Settings,
    upstream_target: UpstreamTarget,
    upstream_env: str,
) -> JSONResponse:
    """
    Faz proxy de um request com stream=False.
    Aguarda resposta completa e extrai tokens para o acumulador.
    
    NOTA: O fim do turno NÃO é detectado automaticamente via finish_reason.
    O agente deve chamar POST /v1/turns/{turn_id}/end quando terminar,
    ou o TTL cleanup irá gravar o turno após 15s de inatividade.
    """
    accumulator = get_accumulator()
    bid = ctx.accumulator_bucket_id
    await accumulator.touch_activity(bid)
    touch_task = asyncio.create_task(
        _periodic_bucket_touch(bid, accumulator)
    )

    work = body_for_upstream_proxy(body, upstream_target)
    cb = get_circuit_breaker()
    model_id = body.get("model", "unknown")
    
    # Check circuit breaker before attempting
    if cb.is_open(model_id):
        touch_task.cancel()
        try:
            await touch_task
        except asyncio.CancelledError:
            pass
        logger.warning("[Proxy] Circuit OPEN for model %s — returning 503", model_id)
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail={
                "error": "circuit_breaker_open",
                "message": f"Model {model_id} is temporarily unavailable due to repeated failures. Try again later.",
                "model": model_id,
            },
        )
    
    async def _do_post() -> httpx.Response:
        async with httpx.AsyncClient(timeout=settings.upstream_timeout) as client:
            return await client.post(
                upstream_target.chat_completions_url,
                headers=upstream_target.headers,
                json=work,
            )
    
    try:
        upstream = await retry_upstream_call(_do_post, max_retries=2, base_delay=1.0)
    except httpx.TimeoutException:
        touch_task.cancel()
        try:
            await touch_task
        except asyncio.CancelledError:
            pass
        cb.record_failure(model_id)
        record_model_failure(model_id)
        raise HTTPException(
            status_code=status.HTTP_504_GATEWAY_TIMEOUT,
            detail={
                "error": "upstream_timeout",
                "message": f"O provider não respondeu em {settings.upstream_timeout}s.",
            },
        )
    except httpx.HTTPError as e:
        touch_task.cancel()
        try:
            await touch_task
        except asyncio.CancelledError:
            pass
        cb.record_failure(model_id)
        record_model_failure(model_id)
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail={
                "error": "upstream_unreachable",
                "message": f"Falha ao contactar upstream: {e}",
            },
        )
    finally:
        touch_task.cancel()
        try:
            await touch_task
        except asyncio.CancelledError:
            pass

    if upstream.status_code >= 400:
        raw_error_text = upstream.text
        error_code = _classify_upstream_error(upstream.status_code, raw_error_text)
        
        # Track failure for circuit breaker and fallback
        cb.record_failure(model_id)
        fallback = record_model_failure(model_id)
        
        print(
            "[ProxyUpstreamError] app_id=%s env=%s source=%s status=%s body=%s"
            % (
                ctx.app_id,
                upstream_env,
                upstream_target.api_key_source or "unknown",
                upstream.status_code,
                raw_error_text[:300],
            )
        )
        
        try:
            payload = upstream.json()
        except Exception:
            payload = {"message": raw_error_text}
        if isinstance(payload, dict):
            payload.setdefault("factor_error", error_code)
            payload.setdefault("factor_upstream_status", upstream.status_code)
            payload.setdefault("factor_upstream_env", upstream_env)
            payload.setdefault(
                "factor_api_key_source",
                upstream_target.api_key_source or "unknown",
            )
            if fallback:
                payload["suggested_fallback"] = fallback
                payload["message"] = payload.get("message", "") + f" Consider retrying with model: {fallback}"
        return JSONResponse(
            status_code=upstream.status_code,
            content=payload,
            headers={"X-Factor-Upstream-Env": upstream_env},
        )

    # Success — record it
    cb.record_success(model_id)
    record_model_success(model_id)

    response_data = upstream.json()
    p, c, t = _extract_usage_from_response(response_data)

    await accumulator.record(
        turn_id=bid,
        prompt_tokens=p,
        completion_tokens=c,
        tool_calls_in_call=t,
    )

    return JSONResponse(
        content=response_data,
        headers={"X-Factor-Upstream-Env": upstream_env},
    )


# ─────────────────────────────────────────────────────────────────────────────
# Ponto de entrada — chamado por src/api/routes/proxy.py
# ─────────────────────────────────────────────────────────────────────────────

async def handle_chat_completions(
    request,
    ctx: GatewayContext,
    settings: Settings,
) -> StreamingResponse | JSONResponse:
    """
    Orquestra o fluxo completo de um call ao LLM:

    1. Lê e valida o body
    2. Decide model_id:
       - X-Conversation-Id: generate-title → modelo fixo (sem router), balde separado por turno
       - X-Turn-Id novo (chat) → chama router UMA VEZ → abre balde
       - mesmo balde (chat ou título) → usa model_id do balde (router ignorado)
    3. Prepara body para o upstream (injeta model_id, stream_options)
    4. Delega para _proxy_stream ou _proxy_json
    """

    # ── 1. Lê o body ────────────────────────────────────────────────────────
    try:
        body: dict = await request.json()
    except Exception:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail={
                "error": "invalid_body",
                "message": "O body deve ser JSON válido no formato OpenAI.",
            },
        )

    messages: list = body.get("messages") or []
    if not messages:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail={
                "error": "missing_messages",
                "message": "O campo 'messages' é obrigatório e não pode estar vazio.",
            },
        )

    is_stream: bool = bool(body.get("stream", False))

    # ── 1b. Quota USD por app (tenant) — teto interno; consumo em gateway_apps ───
    try:
        spend_status = await get_key_store().get_app_spend_status(ctx.app_id)
    except Exception as e:
        logger.exception("[Proxy] Falha ao ler orçamento da app %s: %s", ctx.app_id, e)
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail={
                "error":   "budget_check_unavailable",
                "message": "Could not verify app usage limit. Try again later.",
            },
        )
    if spend_status is None:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail={
                "error":   "app_not_found",
                "message": "App associated with this key was not found.",
            },
        )
    if not spend_status["is_active"]:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail={
                "error":   "app_disabled",
                "message": "This app is disabled.",
            },
        )
    if spend_status["spent_usd_total"] >= spend_status["spend_cap_usd"]:
        return JSONResponse(
            status_code=status.HTTP_402_PAYMENT_REQUIRED,
            content=_app_budget_exceeded_body(
                ctx.app_id,
                spend_status["spend_cap_usd"],
                spend_status["spent_usd_total"],
            ),
        )

    # ── 2. Decide model_id ───────────────────────────────────────────────────
    budget_thr = settings.openrouter_router_budget_threshold_usd
    if budget_thr is None:
        budget_thr = float(settings.openrouter_credits_alert_threshold_usd)
    else:
        budget_thr = float(budget_thr)

    openrouter_balance_low = False
    if settings.openrouter_router_budget_enabled:
        snap_remaining = await read_remaining_usd_snapshot()
        if snap_remaining is not None and snap_remaining <= budget_thr:
            openrouter_balance_low = True
            logger.info(
                "[Router] Modo económico OpenRouter (remaining_usd=%.4f <= %.4f)",
                snap_remaining,
                budget_thr,
            )

    accumulator = get_accumulator()
    bid = ctx.accumulator_bucket_id
    is_title = ctx.is_title_generation_request

    model_id = await accumulator.get_model_id_if_known(bid)

    if model_id is None:
        if is_title:
            model_id = GATEWAY_TITLE_MODEL_ID
            _raw_title_user = next(
                (
                    m.get("content")
                    for m in reversed(messages)
                    if m.get("role") == "user"
                ),
                None,
            )
            _title_usage_msg = (
                flatten_openai_message_content(_raw_title_user).strip()
                or (ctx.user_message or "").strip()
            )
            await accumulator.open(
                ctx=ctx,
                model_id=model_id,
                router_est_input_tokens=0,
                router_est_output_tokens=0,
                usage_user_message=_title_usage_msg or None,
            )
        else:
            # Primeiro call deste turno → chama router UMA VEZ
            raw_content = next(
                (
                    m.get("content")
                    for m in reversed(messages)
                    if m.get("role") == "user"
                ),
                None,
            )
            user_message = flatten_openai_message_content(raw_content)
            if not user_message:
                user_message = flatten_openai_message_content(ctx.user_message)

            router_result = await router_route(
                raw_content if raw_content is not None else user_message,
                openrouter_balance_low=openrouter_balance_low,
                tool_choice=body.get("tool_choice"),
            )
            model_id = router_result.model_id
            model_id = apply_premium_model_policy(settings, ctx, model_id)
            model_id = cap_model_for_low_openrouter_credit(
                model_id,
                balance_low=openrouter_balance_low,
            )

            await accumulator.open(
                ctx=ctx,
                model_id=model_id,
                router_est_input_tokens=router_result.estimated_input_tokens,
                router_est_output_tokens=router_result.estimated_output_tokens,
                usage_user_message=user_message.strip() or None,
            )

    else:
        bucket = accumulator._buckets.get(bid)
        call_num = (bucket.llm_calls_count + 1) if bucket else "?"
        print(
            f"Turno em curso [{ctx.turn_id[:8]}] bucket={bid[:24]}... "
            f"call #{call_num} → model={model_id} (router ignorado)",
        )

    await accumulator.touch_activity(bid)

    if not is_title:
        resolved = apply_premium_model_policy(settings, ctx, model_id)
        if resolved != model_id:
            await accumulator.set_bucket_model_id(bid, resolved)
            model_id = resolved

        capped = cap_model_for_low_openrouter_credit(
            model_id,
            balance_low=openrouter_balance_low,
        )
        if capped != model_id:
            await accumulator.set_bucket_model_id(bid, capped)
            model_id = capped
        
        # Check if this model has failed recently and should use fallback
        from src.gateway.resilience import record_model_failure, get_fallback_model
        cb = get_circuit_breaker()
        if cb.is_open(model_id):
            fallback = get_fallback_model(model_id)
            if fallback and fallback != model_id:
                logger.warning(
                    "[Proxy] Model %s circuit open — using fallback %s",
                    model_id,
                    fallback,
                )
                model_id = fallback
                await accumulator.set_bucket_model_id(bid, model_id)

    # FIX: Modelos Qwen/Alibaba rejeitam function.arguments vazio ou não-JSON.
    # Se o agente enviar um assistant message com tool_calls em que os argumentos são "",
    # forçamos "{}" para garantir que o upstream não devolve 400.
    for m in messages:
        if m.get("role") == "assistant" and "tool_calls" in m:
            for tc in m["tool_calls"]:
                func = tc.get("function")
                if isinstance(func, dict):
                    args = func.get("arguments")
                    if not args or str(args).strip() == "":
                        func["arguments"] = "{}"

    # ── 3. Prepara body para o upstream ─────────────────────────────────────
    upstream_body = {
        **body,
        "model": model_id,                           # substitui o model da app
        "stream_options": {"include_usage": True},   # tokens reais no chunk final (OpenRouter)
        "messages": messages,                        # passa as mensagens corrigidas
    }
    # Regra explícita: quando o agente exigir tool_choice=required,
    # roteamos imediatamente para GPT-4.1 Mini.
    tool_choice = upstream_body.get("tool_choice")
    if tool_choice == "required" and not is_title:
        forced_model = "openai/gpt-4.1-mini"
        if model_id != forced_model:
            logger.info(
                "[Proxy] tool_choice=required detectado; a forçar modelo %s (antes=%s).",
                forced_model,
                model_id,
            )
            model_id = forced_model
            upstream_body["model"] = model_id
            await accumulator.set_bucket_model_id(bid, model_id)

    # FIX: Alguns modelos do catálogo rejeitam tool_choice=required ou tool_choice como object.
    # Se isso acontecer, preferimos um fallback compatível do próprio catálogo.
    model_l = str(model_id).lower()
    has_image_input = any(
        isinstance(part, dict) and (
            str(part.get("type") or "").lower() in {"image", "image_url", "input_image"}
            or (
                isinstance(part.get("image_url"), dict)
                and part.get("image_url", {}).get("url")
            )
        )
        for msg in messages
        if isinstance(msg, dict) and msg.get("role") == "user"
        for part in (msg.get("content") if isinstance(msg.get("content"), list) else [])
    )
    if ("alibaba" in model_l or "qwen" in model_l) and (
        tool_choice == "required" or isinstance(tool_choice, dict)
    ):
        fallback_model = "moonshotai/kimi-k2.5" if has_image_input else "qwen/qwen3.6-plus"
        logger.warning(
            "[Proxy] tool_choice=%r incompatível com model=%s; "
            "a usar fallback compatível %s.",
            tool_choice,
            model_id,
            fallback_model,
        )
        model_id = fallback_model
        upstream_body["model"] = model_id
        await accumulator.set_bucket_model_id(bid, model_id)

    upstream_target = resolve_upstream(
        model_id,
        settings,
        preferred_env=ctx.upstream_env,
    )
    logger.info(
        "[Proxy] app_id=%s model=%s upstream_env=%s api_key_source=%s",
        ctx.app_id,
        model_id,
        upstream_target.selected_env or "unmapped",
        upstream_target.api_key_source or "unknown",
    )
    print(
        "[ProxyRoute] app_id=%s model=%s upstream_env=%s api_key_source=%s"
        % (
            ctx.app_id,
            model_id,
            upstream_target.selected_env or "unmapped",
            upstream_target.api_key_source or "unknown",
        )
    )

    # ── 4. Proxy ─────────────────────────────────────────────────────────────
    if is_stream:
        return await _proxy_stream(
            upstream_body,
            ctx,
            settings,
            upstream_target,
            upstream_target.selected_env or "unmapped",
        )
    else:
        return await _proxy_json(
            upstream_body,
            ctx,
            settings,
            upstream_target,
            upstream_target.selected_env or "unmapped",
        )


async def handle_audio_transcriptions(
    request: Request,
    ctx: "GatewayContext",
    settings: Settings,
) -> JSONResponse | PlainTextResponse:
    """
    Proxy OpenAI-compatible para POST /v1/audio/transcriptions (Factor Whisper).
    Exige os mesmos headers X-* para manter rastreio de custos por turno.
    """
    await _enforce_app_budget_or_raise(ctx)

    # Debug: log request details
    content_type = request.headers.get("content-type", "not set")
    logger.info(f"[AudioTranscription] Content-Type: {content_type}")
    logger.info(f"[AudioTranscription] Method: {request.method}")
    logger.info(f"[AudioTranscription] URL: {request.url}")
    
    # Check if content-type is multipart
    if not content_type.startswith("multipart/form-data"):
        logger.error(f"[AudioTranscription] Invalid Content-Type: {content_type}")
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail={
                "error": "invalid_content_type",
                "message": f"Content-Type must be multipart/form-data, got: {content_type}",
            },
        )

    try:
        # Debug: try to read raw body first
        raw_body = await request.body()
        logger.info(f"[AudioTranscription] Raw body length: {len(raw_body)} bytes")
        
        # Now try to parse form
        form = await request.form()
        logger.info(f"[AudioTranscription] Form keys: {list(form.keys())}")
    except Exception as e:
        logger.exception(f"[AudioTranscription] Form parse error: {e}")
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail={
                "error": "invalid_multipart",
                "message": f"Body deve ser multipart/form-data válido. Error: {str(e)}",
            },
        ) from e

    upload = form.get("file")
    if upload is None or not hasattr(upload, "filename"):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail={
                "error": "missing_file",
                "message": "Campo 'file' é obrigatório em multipart/form-data.",
            },
        )

    # Campos OpenAI-compat aceites pelo upstream.
    data: dict[str, str] = {}
    for key in ("model", "response_format", "language", "prompt", "temperature"):
        value = form.get(key)
        if value is not None:
            data[key] = str(value)
    if "model" not in data:
        data["model"] = "whisper-1"

    # Starlette UploadFile
    audio_bytes = await upload.read()
    if not audio_bytes:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail={"error": "empty_file", "message": "O ficheiro de áudio está vazio."},
        )

    files = {
        "file": (
            upload.filename or "audio.bin",
            audio_bytes,
            getattr(upload, "content_type", None) or "application/octet-stream",
        )
    }

    try:
        async with httpx.AsyncClient(timeout=settings.whisper_upstream_timeout) as client:
            upstream = await client.post(
                settings.whisper_upstream_url,
                data=data,
                files=files,
            )
    except httpx.TimeoutException as e:
        raise HTTPException(
            status_code=status.HTTP_504_GATEWAY_TIMEOUT,
            detail={
                "error": "upstream_timeout",
                "message": f"Whisper upstream não respondeu em {settings.whisper_upstream_timeout}s.",
            },
        ) from e
    except httpx.HTTPError as e:
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail={
                "error": "upstream_unreachable",
                "message": f"Falha ao contactar Whisper upstream: {e}",
            },
        ) from e

    if upstream.status_code >= 400:
        content_type = upstream.headers.get("content-type", "")
        if "application/json" in content_type:
            try:
                payload = upstream.json()
            except Exception:
                payload = {"error": "upstream_error", "message": upstream.text}
            return JSONResponse(status_code=upstream.status_code, content=payload)
        return JSONResponse(
            status_code=upstream.status_code,
            content={"error": "upstream_error", "message": upstream.text},
        )

    response_format = (data.get("response_format") or "json").lower()
    model_id = "whisper/whisper-1"
    prompt_tokens = completion_tokens = total_tokens = 0
    meta: dict[str, object] = {
        "source": "whisper_upstream",
        "whisper_upstream_url": settings.whisper_upstream_url,
        "audio_size_bytes": len(audio_bytes),
        "response_format": response_format,
    }

    if response_format == "text":
        text_out = upstream.text
        completion_tokens = len(text_out.split())
        total_tokens = completion_tokens
        meta["text_chars"] = len(text_out)
        response = PlainTextResponse(content=text_out, status_code=upstream.status_code)
    else:
        payload = upstream.json()
        usage = payload.get("usage") or {}
        prompt_tokens = int(usage.get("prompt_tokens_estimated") or 0)
        completion_tokens = int(usage.get("completion_tokens_estimated") or 0)
        total_tokens = int(
            usage.get("total_tokens_estimated")
            or (prompt_tokens + completion_tokens)
        )
        model_id = f"whisper/{payload.get('model') or 'whisper-1'}"
        for k in ("language_detected", "duration_seconds", "audio_size_bytes"):
            if k in payload:
                meta[k] = payload[k]
        response = JSONResponse(content=payload, status_code=upstream.status_code)

    try:
        from src.usage.service import record_turn_usage

        await record_turn_usage(
            turn_id=ctx.accumulator_bucket_id,
            app_id=ctx.app_id,
            chat_session_id=ctx.session_id,
            conversation_id=ctx.conversation_id,
            user_message=ctx.user_message or "(audio transcription)",
            user_id=ctx.user_id,
            user_name=ctx.user_name,
            user_email=ctx.user_email,
            company_id=ctx.company_id,
            company_name=ctx.company_name,
            model_id=model_id,
            prompt_tokens=prompt_tokens,
            completion_tokens=completion_tokens,
            total_tokens=total_tokens,
            tool_calls_count=0,
            meta=meta,
        )
    except Exception as e:
        logger.warning("[AudioProxy] Falha ao registar usage turn=%s: %s", ctx.turn_id[:8], e)

    return response


async def handle_audio_speech(
    request: Request,
    ctx: "GatewayContext",
    settings: Settings,
) -> Response:
    """
    Proxy OpenAI-compatible para POST /v1/audio/speech (Text-to-Speech).
    Exige os mesmos headers X-* para manter rastreio de custos por turno.
    Retorna áudio binário (mp3, wav, etc.) conforme response_format.
    """
    await _enforce_app_budget_or_raise(ctx)

    # Verificar se TTS está configurado
    if not settings.speech_upstream_url:
        raise HTTPException(
            status_code=status.HTTP_501_NOT_IMPLEMENTED,
            detail={
                "error": "tts_not_configured",
                "message": "TTS upstream não configurado. Defina SPEECH_UPSTREAM_URL.",
            },
        )

    # Debug: log request details
    content_type = request.headers.get("content-type", "not set")
    logger.info(f"[AudioSpeech] Content-Type: {content_type}")
    logger.info(f"[AudioSpeech] Method: {request.method}")

    if not content_type.lower().startswith("application/json"):
        logger.error(f"[AudioSpeech] Invalid Content-Type: {content_type}")
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail={
                "error": "invalid_content_type",
                "message": f"Content-Type must be application/json, got: {content_type}",
            },
        )

    # Parse JSON body
    try:
        body = await request.json()
    except Exception as e:
        logger.exception(f"[AudioSpeech] JSON parse error: {e}")
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail={
                "error": "invalid_json",
                "message": f"Body deve ser JSON válido. Error: {str(e)}",
            },
        ) from e

    # Validar campos obrigatórios
    model = body.get("model")
    if not model:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail={
                "error": "missing_model",
                "message": "Campo 'model' é obrigatório.",
            },
        )

    input_text = body.get("input")
    if not input_text:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail={
                "error": "missing_input",
                "message": "Campo 'input' (texto a sintetizar) é obrigatório.",
            },
        )

    # Campos opcionais com defaults OpenAI-compat
    voice = body.get("voice", "alloy")
    response_format = body.get("response_format", "mp3")
    speed = body.get("speed", 1.0)

    # Construir payload para o upstream
    upstream_body = {
        "model": model,
        "input": input_text,
        "voice": voice,
        "response_format": response_format,
        "speed": speed,
    }

    logger.info(
        "[AudioSpeech] Request: model=%s, voice=%s, format=%s, speed=%s, input_len=%d",
        model,
        voice,
        response_format,
        speed,
        len(input_text),
    )

    try:
        async with httpx.AsyncClient(timeout=settings.speech_upstream_timeout) as client:
            upstream = await client.post(
                settings.speech_upstream_url,
                json=upstream_body,
            )
    except httpx.TimeoutException as e:
        raise HTTPException(
            status_code=status.HTTP_504_GATEWAY_TIMEOUT,
            detail={
                "error": "upstream_timeout",
                "message": f"TTS upstream não respondeu em {settings.speech_upstream_timeout}s.",
            },
        ) from e
    except httpx.HTTPError as e:
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail={
                "error": "upstream_unreachable",
                "message": f"Falha ao contactar TTS upstream: {e}",
            },
        ) from e

    # Tratar erros do upstream
    if upstream.status_code >= 400:
        content_type_upstream = upstream.headers.get("content-type", "")
        if "application/json" in content_type_upstream:
            try:
                payload = upstream.json()
            except Exception:
                payload = {"error": "upstream_error", "message": upstream.text}
            return JSONResponse(status_code=upstream.status_code, content=payload)
        return JSONResponse(
            status_code=upstream.status_code,
            content={"error": "upstream_error", "message": upstream.text},
        )

    # Mapear content-type conforme formato de resposta
    content_type_map = {
        "mp3": "audio/mpeg",
        "wav": "audio/wav",
        "flac": "audio/flac",
        "ogg": "audio/ogg",
        "aac": "audio/aac",
        "opus": "audio/opus",
    }
    audio_content_type = content_type_map.get(response_format, "audio/mpeg")

    # Resposta binária de áudio
    audio_bytes = upstream.content
    audio_size_bytes = len(audio_bytes)

    logger.info(
        "[AudioSpeech] Success: format=%s, size=%d bytes",
        response_format,
        audio_size_bytes,
    )

    # Registar usage (TTS conta como completion tokens baseados no comprimento do texto)
    model_id = f"tts/{model}"
    # Estimativa: ~1 token por 4 caracteres (aproximação OpenAI)
    completion_tokens = len(input_text) // 4
    total_tokens = completion_tokens
    meta: dict[str, object] = {
        "source": "speech_upstream",
        "speech_upstream_url": settings.speech_upstream_url,
        "voice": voice,
        "response_format": response_format,
        "speed": speed,
        "input_chars": len(input_text),
        "audio_size_bytes": audio_size_bytes,
    }

    try:
        from src.usage.service import record_turn_usage

        await record_turn_usage(
            turn_id=ctx.accumulator_bucket_id,
            app_id=ctx.app_id,
            chat_session_id=ctx.session_id,
            conversation_id=ctx.conversation_id,
            user_message=ctx.user_message or "(text-to-speech)",
            user_id=ctx.user_id,
            user_name=ctx.user_name,
            user_email=ctx.user_email,
            company_id=ctx.company_id,
            company_name=ctx.company_name,
            model_id=model_id,
            prompt_tokens=0,  # TTS não tem prompt tokens no sentido tradicional
            completion_tokens=completion_tokens,
            total_tokens=total_tokens,
            tool_calls_count=0,
            meta=meta,
        )
    except Exception as e:
        logger.warning("[AudioSpeech] Falha ao registar usage turn=%s: %s", ctx.turn_id[:8], e)

    return Response(
        content=audio_bytes,
        media_type=audio_content_type,
        headers={
            "Content-Disposition": f'attachment; filename="speech.{response_format}"',
        },
    )
