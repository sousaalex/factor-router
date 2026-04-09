"""
src/api/routes/turns.py

Turn Lifecycle Protocol — gestão explícita do ciclo de vida de um turno.

Padrão MemGPT (Bluma, agentes tool-only):
  O turno termina quando o agente chama uma tool terminal (ex: message(result)).
  O agente declara o fim explicitamente via:
  → POST /v1/turns/{turn_id}/end

Fallback de segurança:
  Se o agente não chamar o endpoint (crash, bug, rede), o TTL cleanup do gateway
  (app.py _cleanup_loop) grava o turno após 15s de inatividade.

Referência:
  MemGPT / Letta (2023-2025) — arquitectura stateful com send_message tool.
  O agente controla o seu próprio ciclo de vida via request_heartbeat.
  FactorRouter adopta o mesmo princípio: o agente declara o fim do turno.
  
NOTA: A detecção automática via finish_reason=stop foi removida para suportar
agentes que produzem texto de assistente sem terminar o turno.
"""
from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, Path, HTTPException, status
from pydantic import BaseModel

from src.gateway.auth import AuthenticatedApp, authenticate
from src.gateway.context import GatewayContext
from src.gateway.accumulator import get_accumulator
from src.gateway.proxy import _create_flush_task

# Set de turn_ids que já fizeram flush — evita duplicate inserts
# quando o agente chama /end múltiplas vezes para o mesmo turno
_flushed_turns: set[str] = set()

router = APIRouter()


class TurnEndRequest(BaseModel):
    """
    Body opcional do POST /v1/turns/{turn_id}/end.
    Permite ao agente passar metadados sobre como o turno terminou.
    """
    reason: str = "agent_declared"   # como o turno terminou (para logging/debug)


@router.post(
    "/turns/{turn_id}/end",
    summary="Declara o fim de um turno (Turn Lifecycle Protocol)",
    description="""
Declara explicitamente que um turno terminou e força o flush do centro de custos.

**Quando usar:**

Usa este endpoint quando o teu agente segue o padrão **MemGPT/tool-only** —
ou seja, o turno termina quando o agente decide (ex: tool `message(result)`).

Exemplos:
- O Bluma termina quando chama `message(message_type=result)`
- Agentes com `agent_end_turn` tool
- Qualquer agente onde o loop é controlado pelo agente, não pelo modelo

**Importante:**

A detecção automática via `finish_reason=stop` foi **removida**.
Obrigatório chamar este endpoint para fechar o turno explicitamente.

**Garantia:**

Mesmo que o agente não chame este endpoint, o gateway tem fallback por
inatividade (TTL desde o último call ao LLM; ver accumulator) que grava
tokens no DB após 15s de inatividade. Este endpoint garante flush imediato.

**Headers obrigatórios:** os mesmos 9 headers X-* do `/v1/chat/completions`.
    """,
    tags=["turns"],
)
async def end_turn(
    turn_id: Annotated[str, Path(description="X-Turn-Id do turno a terminar")],
    auth:    Annotated[AuthenticatedApp, Depends(authenticate)],
    ctx:     Annotated[GatewayContext, Depends(GatewayContext.from_headers)],
    body:    TurnEndRequest = TurnEndRequest(),
):
    """
    Força o flush do balde de tokens para o turno indicado.
    Idempotente — chamar múltiplas vezes para o mesmo turn_id é seguro.
    """
    # Garante que o app_id vem sempre da key
    ctx.app_id = auth.app_id

    # Idempotência — se já fez flush, ignora silenciosamente
    if turn_id in _flushed_turns:
        print(f"[TurnEnd] Turn [{turn_id[:8]}] — already flushed, ignoring duplicate call")
        return {
            "turn_id": turn_id,
            "status":  "already_flushed",
            "message": "Turn was already flushed.",
        }

    accumulator = get_accumulator()
    bucket = accumulator._buckets.get(turn_id)

    if bucket is None:
        # Balde não existe — já foi flushed ou nunca foi aberto
        # Devolvemos 200 para ser idempotente — o agente não precisa de se preocupar
        print(f"[TurnEnd] Turn [{turn_id[:8]}] — bucket not found (already flushed or never opened)")
        _flushed_turns.add(turn_id)
        return {
            "turn_id": turn_id,
            "status":  "already_flushed",
            "message": "Turn was already flushed or never opened.",
        }

    # Marca como flushed ANTES de lançar a task — evita race condition
    _flushed_turns.add(turn_id)
    # Limpa o set periodicamente para não crescer indefinidamente (max 10000)
    if len(_flushed_turns) > 10000:
        _flushed_turns.clear()

    print(
        f"[TurnEnd] Turn [{turn_id[:8]}] end declared by agent "
        f"app={auth.app_id} reason={body.reason} "
        f"tokens={bucket.total_tokens} llm_calls={bucket.llm_calls_count}"
    )

    # Força flush assíncrono — não bloqueia o agente
    _create_flush_task(turn_id)

    return {
        "turn_id":     turn_id,
        "status":      "flushing",
        "reason":      body.reason,
        "tokens":      bucket.total_tokens,
        "llm_calls":   bucket.llm_calls_count,
        "tool_calls":  bucket.tool_calls_count,
        "message":     "Turn end acknowledged. Usage will be recorded shortly.",
    }