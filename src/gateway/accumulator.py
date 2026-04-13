"""
src/gateway/accumulator.py

Acumulador de tokens por turno (X-Turn-Id).

Problema que resolve:
    Um agente agentic faz múltiplos calls ao LLM dentro do mesmo turno
    (loop tool_call → tool_result → tool_call → ...).
    Cada call consome tokens. Precisamos de 1 único registo no DB
    com o total de tokens do turno inteiro — não um registo por call.

Como funciona:
    1. No início do turno o agente gera um X-Turn-Id (UUID v4).
    2. Cada call ao LLM dentro do loop usa o mesmo X-Turn-Id.
    3. O gateway acumula tokens em memória por X-Turn-Id.
    4. Quando o stream termina com finish_reason="stop" (resposta final),
       o acumulador devolve os totais e limpa o balde.
    5. Esses totais vão para o usage/service.py → 1 linha no Postgres.

Fallback:
    Se o provider não devolver tokens reais (chunk.usage ausente),
    usamos a estimativa do router. O campo meta.source indica a origem:
    - "usage_real"               → tokens reais do provider
    - "router_estimate_fallback" → estimativa do router
"""
from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from src.gateway.context import GatewayContext

logger = logging.getLogger(__name__)

# TTL efectivo: Settings.accumulator_idle_ttl_seconds (env ACCUMULATOR_IDLE_TTL_SECONDS).


# ─────────────────────────────────────────────────────────────────────────────
# TurnBucket — o "balde" de um turno
# ─────────────────────────────────────────────────────────────────────────────

@dataclass
class TurnBucket:
    """
    Estado acumulado de um turno em curso.
    Criado no primeiro call ao LLM do turno, destruído após flush.
    """

    # contexto do agente (dos headers X-*)
    turn_id: str
    app_id: str
    session_id: str
    conversation_id: str | None
    user_message: str
    user_id: str | None
    user_name: str | None
    user_email: str | None
    company_id: str | None
    company_name: str | None

    # modelo escolhido pelo router para este turno
    model_id: str

    # estimativa do router (fallback se tokens reais não chegarem)
    router_est_input_tokens: int = 0
    router_est_output_tokens: int = 0

    # acumuladores — somam em cada call ao LLM dentro do loop
    prompt_tokens: int = 0
    completion_tokens: int = 0
    llm_calls_count: int = 0
    tool_calls_count: int = 0

    # controlo interno
    has_real_usage: bool = False   # True se já recebemos tokens reais do provider
    created_at: float = field(default_factory=time.monotonic)
    last_activity_at: float = field(default_factory=time.monotonic)

    @property
    def total_tokens(self) -> int:
        return self.prompt_tokens + self.completion_tokens

    @property
    def is_expired(self) -> bool:
        from src.gateway.config import get_settings

        ttl = get_settings().accumulator_idle_ttl_seconds
        return (time.monotonic() - self.last_activity_at) > ttl

    @property
    def source(self) -> str:
        return "usage_real" if self.has_real_usage else "router_estimate_fallback"

    def add_llm_call(
        self,
        prompt_tokens: int,
        completion_tokens: int,
        tool_calls_in_call: int = 0,
    ) -> None:
        """
        Soma os tokens de um call ao LLM neste balde.
        Chamado após cada call ao LLM dentro do loop agentic.
        """
        if prompt_tokens > 0 or completion_tokens > 0:
            self.has_real_usage = True

        self.prompt_tokens     += prompt_tokens
        self.completion_tokens += completion_tokens
        self.tool_calls_count  += tool_calls_in_call
        self.llm_calls_count   += 1
        self.last_activity_at = time.monotonic()

        #print(f"[Accumulator] [{self.turn_id[:8]}] call #{self.llm_calls_count} | +{prompt_tokens} prompt +{completion_tokens} completion | total: {self.total_tokens} tokens")

    def to_usage_record(self) -> dict:
        """
        Serializa o balde para o formato que o usage/service.py espera.
        Se não houver tokens reais, usa a estimativa do router como fallback.
        """
        if self.has_real_usage:
            prompt_tokens     = self.prompt_tokens
            completion_tokens = self.completion_tokens
        else:
            prompt_tokens     = self.router_est_input_tokens
            completion_tokens = self.router_est_output_tokens

        return {
            "turn_id":          self.turn_id,
            "app_id":           self.app_id,
            "chat_session_id":  self.session_id,
            "conversation_id":  self.conversation_id,
            "user_message":     self.user_message,
            "user_id":          self.user_id,
            "user_name":        self.user_name,
            "user_email":       self.user_email,
            "company_id":       self.company_id,
            "company_name":     self.company_name,
            "model_id":         self.model_id,
            "prompt_tokens":    prompt_tokens,
            "completion_tokens": completion_tokens,
            "total_tokens":     prompt_tokens + completion_tokens,
            "tool_calls_count": self.tool_calls_count,
            "meta": {
                "source":          self.source,
                "llm_calls_count": self.llm_calls_count,
            },
        }


# ─────────────────────────────────────────────────────────────────────────────
# TurnAccumulator — singleton que gere todos os baldes em memória
# ─────────────────────────────────────────────────────────────────────────────

class TurnAccumulator:
    """
    Gere os baldes de todos os turnos em curso.

    Thread-safe via asyncio.Lock por turn_id.
    Um único singleton partilhado por toda a aplicação (ver get_accumulator()).

    Ciclo de vida de um balde:
        open()     → cria o balde no início do turno
        record()   → soma tokens de cada call ao LLM
        flush()    → devolve os totais e destrói o balde
        cleanup()  → remove baldes expirados (chamado periodicamente)
    """

    def __init__(self) -> None:
        self._buckets: dict[str, TurnBucket] = {}
        self._lock = asyncio.Lock()

    async def open(
        self,
        ctx: "GatewayContext",
        model_id: str,
        router_est_input_tokens: int = 0,
        router_est_output_tokens: int = 0,
        *,
        usage_user_message: str | None = None,
    ) -> TurnBucket:
        """
        Abre (ou reabre) um balde para o turn_id dado.

        Se o balde já existir (agente faz 2º call do mesmo turno),
        não recria — apenas devolve o existente.

        usage_user_message:
            Texto completo da última mensagem user (ex.: extraído do JSON do body).
            Se vazio/None, usa ctx.user_message (header X-User-Message).
        """
        bucket_id = ctx.accumulator_bucket_id
        recorded_msg = (usage_user_message or "").strip() or (ctx.user_message or "")
        async with self._lock:
            if bucket_id not in self._buckets:
                self._buckets[bucket_id] = TurnBucket(
                    turn_id=bucket_id,
                    app_id=ctx.app_id,
                    session_id=ctx.session_id,
                    conversation_id=ctx.conversation_id,
                    user_message=recorded_msg,
                    user_id=ctx.user_id,
                    user_name=ctx.user_name,
                    user_email=ctx.user_email,
                    company_id=ctx.company_id,
                    company_name=ctx.company_name,
                    model_id=model_id,
                    router_est_input_tokens=router_est_input_tokens,
                    router_est_output_tokens=router_est_output_tokens,
                )
               # print(f"[Accumulator] Bucket opened [{bucket_id[:16]}] app={ctx.app_id} session={ctx.session_id} model={model_id}")
            return self._buckets[bucket_id]

    async def record(
        self,
        turn_id: str,
        prompt_tokens: int,
        completion_tokens: int,
        tool_calls_in_call: int = 0,
    ) -> None:
        """
        Soma tokens de um call ao LLM no balde do turno.
        Se o balde não existir (foi flushed ou nunca aberto), ignora com aviso.
        """
        async with self._lock:
            bucket = self._buckets.get(turn_id)
            if bucket is None:
               # print(f"[Accumulator] WARNING: record() called for unknown/already-flushed turn [{turn_id[:8]}]")
                return
            bucket.add_llm_call(
                prompt_tokens=prompt_tokens,
                completion_tokens=completion_tokens,
                tool_calls_in_call=tool_calls_in_call,
            )

    async def touch_activity(self, turn_id: str) -> None:
        """Atualiza last_activity_at (ex.: stream longo antes de record())."""
        async with self._lock:
            bucket = self._buckets.get(turn_id)
            if bucket is not None:
                bucket.last_activity_at = time.monotonic()

    async def set_bucket_model_id(self, turn_id: str, model_id: str) -> None:
        """Actualiza o model_id do balde (ex.: downgrade Sonnet → Kimi após política)."""
        async with self._lock:
            bucket = self._buckets.get(turn_id)
            if bucket is not None:
                bucket.model_id = model_id

    async def flush(self, turn_id: str) -> dict | None:
        """
        Termina o turno: devolve os totais acumulados e destrói o balde.

        Retorna None se o balde não existir (já flushed ou nunca aberto).
        O caller (proxy.py) é responsável por passar o resultado ao usage/service.py.
        """
        async with self._lock:
            bucket = self._buckets.pop(turn_id, None)
            if bucket is None:
               # print(f"[Accumulator] WARNING: flush() called for unknown/already-flushed turn [{turn_id[:8]}]")
                return None

            record = bucket.to_usage_record()
           # print(f"[Accumulator] FLUSH [{turn_id[:8]}] app={bucket.app_id} | {record['total_tokens']} tokens ({record['prompt_tokens']} prompt + {record['completion_tokens']} completion) | {record['meta']['llm_calls_count']} llm_calls | {record['tool_calls_count']} tool_calls | source={record['meta']['source']}")
            return record

    async def get_model_id_if_known(self, turn_id: str) -> str | None:
        """
        Devolve o model_id do balde se o turno já estava em curso.
        Devolve None se for o primeiro call deste turno.

        Usado pelo proxy.py para decidir se chama o router ou não:

            model_id = await acc.get_model_id_if_known(ctx.turn_id)
            if model_id is None:
                # primeiro call do turno — chama o router UMA VEZ
                model_id, estimates = router.recommend(messages)
                await acc.open(ctx, model_id, ...)
            # calls seguintes do mesmo turno — router NÃO é chamado
        """
        async with self._lock:
            bucket = self._buckets.get(turn_id)
            return bucket.model_id if bucket is not None else None

    async def cleanup_expired(self) -> list[dict]:
        """
        Remove baldes expirados (TTL ultrapassado) e grava-os no DB.
        Deve ser chamado periodicamente — ex: a cada 15 segundos via background task.
        Retorna lista de records para gravar no DB.
        """
        records_to_save = []
        async with self._lock:
            expired = [
                turn_id
                for turn_id, bucket in self._buckets.items()
                if bucket.is_expired
            ]
            for turn_id in expired:
                bucket = self._buckets.pop(turn_id)
                # print(
                #     f"[Accumulator] TTL expired [{turn_id[:8]}] app={bucket.app_id} "
                #     f"tokens={bucket.total_tokens} llm_calls={bucket.llm_calls_count} — flushing to DB"
                # )
                if bucket.total_tokens > 0:
                    records_to_save.append(bucket.to_usage_record())
        return records_to_save

    @property
    def active_turns(self) -> int:
        """Número de turnos atualmente em curso. Útil para métricas."""
        return len(self._buckets)


# ─────────────────────────────────────────────────────────────────────────────
# Singleton global
# ─────────────────────────────────────────────────────────────────────────────

_accumulator: TurnAccumulator | None = None


def get_accumulator() -> TurnAccumulator:
    """
    Devolve o singleton do acumulador.
    Criado na primeira chamada, partilhado por toda a aplicação.

    Usar via Depends(get_accumulator) nos endpoints FastAPI,
    ou importar diretamente no proxy.py.
    """
    global _accumulator
    if _accumulator is None:
        _accumulator = TurnAccumulator()
    return _accumulator