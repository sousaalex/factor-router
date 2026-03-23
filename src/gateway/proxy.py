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
    5. Faz proxy ao OpenRouter (stream SSE ou JSON completo)
    6. Extrai tokens da resposta e regista no acumulador
    7. Se finish_reason=stop → flush do balde → grava no Postgres via usage/service.py
"""
from __future__ import annotations

import asyncio
import json
import logging
from typing import AsyncIterator, TYPE_CHECKING

import httpx
from fastapi import HTTPException, status
from fastapi.responses import JSONResponse, StreamingResponse

from src.gateway.accumulator import get_accumulator
from src.gateway.config import Settings
from src.router.router import route as router_route

if TYPE_CHECKING:
    from src.gateway.context import GatewayContext

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


def _is_final_chunk(chunk_data: dict) -> bool:
    """
    Verifica se este chunk SSE é o último do turno.
    finish_reason=stop   → resposta final ao utilizador
    finish_reason=length → limite de tokens atingido (também fecha o turno)
    finish_reason=tool_calls NÃO fecha — o agente vai fazer outro call com tool_result
    """
    choices = chunk_data.get("choices") or []
    for choice in choices:
        finish_reason = choice.get("finish_reason")
        if finish_reason in ("stop", "end_turn", "length"):
            return True
    return False


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
) -> StreamingResponse:
    """
    Faz proxy de um request com stream=True.
    Passa chunks SSE ao agente sem buffering.
    Extrai tokens e detecta o fim do turno (finish_reason=stop).
    """
    accumulator = get_accumulator()

    async def generate() -> AsyncIterator[bytes]:
        #print(f"[Proxy] generate() called for turn [{ctx.turn_id[:8]}] — new stream connection")

        total_prompt      = 0
        total_completion  = 0
        total_tool_calls  = 0
        tool_call_indices: set[int] = set()  # índices únicos de tool_calls no stream
        is_last_call      = False

        try:
            async with httpx.AsyncClient(timeout=settings.upstream_timeout) as client:
                async with client.stream(
                    "POST",
                    f"{settings.upstream_url}/chat/completions",
                    headers={"Authorization": f"Bearer {settings.openrouter_api_key}"},
                    json=body,
                ) as upstream:

                    if upstream.status_code >= 400:
                        error_body = await upstream.aread()
                        # print(
                        #     "Upstream error %d for turn [%s]: %s",
                        #     upstream.status_code,
                        #     ctx.turn_id[:8],
                        #     error_body[:200],
                        # )
                        yield error_body
                        # Força flush mesmo em erro — tokens foram consumidos
                        # O balde pode ter tokens de calls anteriores do mesmo turno
                        is_last_call = True
                        return

                    async for raw_line in upstream.aiter_lines():
                        if not raw_line:
                            yield b"\n"
                            continue

                        # Passa o chunk ao agente imediatamente
                        yield (raw_line + "\n\n").encode()

                        # Tenta extrair tokens e detectar fim do turno
                        if raw_line.startswith("data: "):
                            data_str = raw_line[6:].strip()
                            if data_str == "[DONE]":
                                continue  # fim do stream SSE — não fecha o turno
                                          # quem fecha é o finish_reason=stop no chunk anterior
                            try:
                                chunk_data = json.loads(data_str)
                                choices = chunk_data.get("choices") or []
                                for choice in choices:
                                    fr = choice.get("finish_reason")
                                    #if fr:
                                    #    print(f"[Proxy] finish_reason='{fr}' for turn [{ctx.turn_id[:8]}]")
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

                                if _is_final_chunk(chunk_data):
                                    is_last_call = True
                                    #print(f"[Proxy] FINAL CHUNK detected for turn [{ctx.turn_id[:8]}] finish_reason=stop")

                                #print(f"[Proxy] FINALLY turn [{ctx.turn_id[:8]}] is_last_call={is_last_call} total_prompt={total_prompt} total_completion={total_completion} total_tool_calls={total_tool_calls}")

                            except json.JSONDecodeError:
                                pass

        except httpx.TimeoutException:
            #print("Timeout no upstream para turno [%s]", ctx.turn_id[:8])
            yield b'data: {"error": "upstream_timeout"}\n\n'

        finally:
            # Regista tokens deste call no acumulador
            await accumulator.record(
                turn_id=ctx.turn_id,
                prompt_tokens=total_prompt,
                completion_tokens=total_completion,
                tool_calls_in_call=total_tool_calls,
            )
            # finish_reason=stop → turno terminou → flush assíncrono
            # finish_reason=tool_calls → agente vai fazer outro call → NÃO flush
            if is_last_call:
                _create_flush_task(ctx.turn_id)

    return StreamingResponse(
        generate(),
        media_type="text/event-stream",
        headers={
            "Cache-Control":     "no-cache",
            "X-Accel-Buffering": "no",
        },
    )


# ─────────────────────────────────────────────────────────────────────────────
# Non-streaming proxy
# ─────────────────────────────────────────────────────────────────────────────

async def _proxy_json(
    body: dict,
    ctx: GatewayContext,
    settings: Settings,
) -> JSONResponse:
    """
    Faz proxy de um request com stream=False.
    Aguarda resposta completa, extrai tokens, faz flush.
    """
    accumulator = get_accumulator()

    try:
        async with httpx.AsyncClient(timeout=settings.upstream_timeout) as client:
            upstream = await client.post(
                f"{settings.upstream_url}/chat/completions",
                headers={"Authorization": f"Bearer {settings.openrouter_api_key}"},
                json=body,
            )
    except httpx.TimeoutException:
        raise HTTPException(
            status_code=status.HTTP_504_GATEWAY_TIMEOUT,
            detail={
                "error": "upstream_timeout",
                "message": f"O provider não respondeu em {settings.upstream_timeout}s.",
            },
        )

    if upstream.status_code >= 400:
        # print(
        #     "Upstream error %d for turn [%s]",
        #     upstream.status_code,
        #     ctx.turn_id[:8],
        # )
        # Força flush mesmo em erro — tokens foram consumidos pelo provider
        # O balde pode ter tokens de calls anteriores do mesmo turno agentic
        _create_flush_task(ctx.turn_id)
        return JSONResponse(
            status_code=upstream.status_code,
            content=upstream.json(),
        )

    response_data = upstream.json()
    p, c, t = _extract_usage_from_response(response_data)

    await accumulator.record(
        turn_id=ctx.turn_id,
        prompt_tokens=p,
        completion_tokens=c,
        tool_calls_in_call=t,
    )

    # Só faz flush quando o turno terminou (finish_reason=stop ou length).
    # Se for tool_calls, o agente vai fazer mais calls com tool_results
    # no mesmo X-Turn-Id — o balde mantém-se aberto para acumular.
    finish_reason = (
        (response_data.get("choices") or [{}])[0]
        .get("finish_reason", "stop")
    )
    if finish_reason in ("stop", "end_turn", "length"):
        _create_flush_task(ctx.turn_id)

    return JSONResponse(content=response_data)


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
       - X-Turn-Id novo     → chama router UMA VEZ → abre balde
       - X-Turn-Id existente → usa model_id do balde (router ignorado)
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

    # ── 2. Decide model_id ───────────────────────────────────────────────────
    accumulator = get_accumulator()
    model_id = await accumulator.get_model_id_if_known(ctx.turn_id)

    if model_id is None:
        # Primeiro call deste turno → chama router UMA VEZ
        # print(
        #     "Turno novo [%s] — a chamar router | app=%s session=%s",
        #     ctx.turn_id[:8],
        #     ctx.app_id,
        #     ctx.session_id,
        # )

        # Extrai a mensagem do utilizador para o router classificar
        user_message = next(
            (
                m.get("content", "")
                for m in reversed(messages)
                if m.get("role") == "user"
            ),
            ctx.user_message,  # fallback: preview do header X-User-Message
        )

        router_result = await router_route(user_message)
        model_id = router_result.model_id

        await accumulator.open(
            ctx=ctx,
            model_id=model_id,
            router_est_input_tokens=router_result.estimated_input_tokens,
            router_est_output_tokens=router_result.estimated_output_tokens,
        )

        # print(
        #     "Turno [%s] app=%s company=%s → model=%s (est ~%d tokens)",
        #     ctx.turn_id[:8],
        #     ctx.app_id,
        #     ctx.company_id,
        #     model_id,
        #     router_result.estimated_input_tokens + router_result.estimated_output_tokens,
        # )

    else:
        # Call seguinte do mesmo turno → router NÃO é chamado
        bucket = accumulator._buckets.get(ctx.turn_id)
        call_num = (bucket.llm_calls_count + 1) if bucket else "?"
        print(
            f"Turno em curso [{ctx.turn_id[:8]}] call #{call_num} → model={model_id} (router ignorado)",
        )

    # ── 3. Prepara body para o upstream ─────────────────────────────────────
    upstream_body = {
        **body,
        "model": model_id,                           # substitui o model da app
        "stream_options": {"include_usage": True},   # tokens reais no chunk final
    }

    # ── 4. Proxy ─────────────────────────────────────────────────────────────
    if is_stream:
        return await _proxy_stream(upstream_body, ctx, settings)
    else:
        return await _proxy_json(upstream_body, ctx, settings)