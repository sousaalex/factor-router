"""
src/usage/service.py

Registo e leitura de uso de tokens por turno.

Diferenças em relação ao llm_usage_service.py do Severino:
  - Usa asyncpg direto (sem SQLAlchemy) — consistente com o key_store.py
  - Sem lógica de Odoo — os dados de utilizador/empresa vêm sempre
    dos headers X-* enviados pelo agente
  - Preços lidos do router (get_model_info) — igual ao Severino
  - app_id incluído — novo campo que identifica qual app gerou o custo
"""
from __future__ import annotations

import logging
from typing import Any, Optional

from src.gateway.key_store import get_key_store
from src.router.router import get_model_info

logger = logging.getLogger(__name__)


# ─────────────────────────────────────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────────────────────────────────────

def _calc_costs(
    prompt_tokens: int,
    completion_tokens: int,
    input_price_per_1m: float,
    output_price_per_1m: float,
) -> dict[str, float]:
    input_cost  = (prompt_tokens     / 1_000_000) * input_price_per_1m
    output_cost = (completion_tokens / 1_000_000) * output_price_per_1m
    return {
        "input_cost_usd":  input_cost,
        "output_cost_usd": output_cost,
        "total_cost_usd":  input_cost + output_cost,
    }


def _get_pool():
    """Reutiliza o pool do KeyStore — já inicializado no arranque."""
    return get_key_store()._pool


# ─────────────────────────────────────────────────────────────────────────────
# record_turn_usage — chamado pelo proxy após fim do turno
# ─────────────────────────────────────────────────────────────────────────────

async def record_turn_usage(
    *,
    turn_id: str,
    app_id: str,
    chat_session_id: str,
    conversation_id: Optional[str],
    user_message: str,
    user_id: Optional[str],
    user_name: Optional[str],
    user_email: Optional[str],
    company_id: Optional[str],
    company_name: Optional[str],
    model_id: str,
    prompt_tokens: int,
    completion_tokens: int,
    total_tokens: int,
    tool_calls_count: int = 0,
    meta: Optional[dict[str, Any]] = None,
) -> None:
    """
    Persiste um registo de turno no Postgres.

    Chamado de forma assíncrona pelo proxy após o stream terminar.
    Nunca bloqueia o agente — falhas são logged mas não propagadas.
    """
    import json

    # Preços do modelo a partir do router
    model_info      = get_model_info(model_id) or {}
    input_price     = float(model_info.get("input_per_1m_tokens",  0.0))
    output_price    = float(model_info.get("output_per_1m_tokens", 0.0))
    costs           = _calc_costs(prompt_tokens, completion_tokens, input_price, output_price)

    stored_msg = (user_message or "").strip() or "(empty)"
    meta_json       = json.dumps(meta or {})

    # Não gravar se não há tokens reais e a fonte é estimativa do router
    # Evita registos duplicados quando o endpoint /turns/{id}/end é chamado
    # múltiplas vezes para o mesmo turno
    source = (meta or {}).get("source", "")
    if total_tokens == 0 and source == "router_estimate_fallback":
        print(f"[Usage] Skipping zero-token router_estimate_fallback record for turn [{turn_id[:8]}]")
        return

    total_cost = costs["total_cost_usd"]
    try:
        pool = _get_pool()
        async with pool.acquire() as conn:
            async with conn.transaction():
                ins = await conn.fetchrow(
                    """
                    INSERT INTO llm_usage_log (
                        turn_id,
                        app_id, chat_session_id, user_id, user_name, user_email,
                        company_id, company_name, conversation_id, user_message,
                        model_id, prompt_tokens, completion_tokens, total_tokens,
                        input_price_per_1m, output_price_per_1m,
                        input_cost_usd, output_cost_usd, total_cost_usd,
                        tool_calls_count, meta
                    ) VALUES (
                        $1,
                        $2,  $3,  $4,  $5,  $6,
                        $7,  $8,  $9,  $10,
                        $11, $12, $13, $14,
                        $15, $16,
                        $17, $18, $19,
                        $20, $21::jsonb
                    )
                    ON CONFLICT (turn_id) DO NOTHING
                    RETURNING id
                    """,
                    turn_id,
                    app_id, chat_session_id, user_id, user_name, user_email,
                    company_id, company_name, conversation_id, stored_msg,
                    model_id, prompt_tokens, completion_tokens, total_tokens,
                    input_price, output_price,
                    costs["input_cost_usd"], costs["output_cost_usd"], total_cost,
                    tool_calls_count, meta_json,
                )
                if ins is not None and app_id:
                    await conn.execute(
                        """
                        UPDATE gateway_apps
                        SET spent_usd_total = spent_usd_total + $1::double precision
                        WHERE app_id = $2
                        """,
                        total_cost,
                        app_id,
                    )
        logger.info(
            "Turno registado [%s] app=%s model=%s tokens=%d cost=$%.6f source=%s",
            turn_id[:8],
            app_id,
            model_id,
            total_tokens,
            total_cost,
            (meta or {}).get("source", "?"),
        )
    except Exception as e:
        logger.warning(
            "Falha ao gravar turno [%s] no Postgres: %s",
            turn_id[:8],
            e,
        )


# ─────────────────────────────────────────────────────────────────────────────
# get_usage_logs — leitura para relatórios
# ─────────────────────────────────────────────────────────────────────────────

async def get_usage_logs(
    *,
    company_id: Optional[str]      = None,
    app_id: Optional[str]          = None,
    session_id: Optional[str]      = None,
    date_from: Optional[str]       = None,
    date_to: Optional[str]         = None,
    limit: int                     = 50,
    offset: int                    = 0,
) -> dict[str, Any]:
    """
    Lista registos de uso com filtros opcionais.
    Devolve { items, limit, offset, count }.
    """
    conditions = []
    params     = []
    idx        = 1

    if company_id:
        conditions.append(f"company_id = ${idx}"); params.append(company_id); idx += 1
    if app_id:
        conditions.append(f"app_id = ${idx}");     params.append(app_id);     idx += 1
    if session_id:
        conditions.append(f"chat_session_id = ${idx}"); params.append(session_id); idx += 1
    if date_from:
        conditions.append(f"created_at >= ${idx}::timestamptz"); params.append(date_from); idx += 1
    if date_to:
        conditions.append(f"created_at <= ${idx}::timestamptz"); params.append(date_to);   idx += 1

    where  = ("WHERE " + " AND ".join(conditions)) if conditions else ""
    params += [limit, offset]

    query = f"""
        SELECT
            id, created_at, turn_id, app_id, chat_session_id,
            user_id, user_name, user_email,
            company_id, company_name, conversation_id,
            user_message, model_id,
            prompt_tokens, completion_tokens, total_tokens,
            input_price_per_1m, output_price_per_1m,
            input_cost_usd, output_cost_usd, total_cost_usd,
            tool_calls_count, meta
        FROM llm_usage_log
        {where}
        ORDER BY created_at DESC
        LIMIT ${idx} OFFSET ${idx + 1}
    """

    pool = _get_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch(query, *params)

    items = [
        {
            "id":                  row["id"],
            "created_at":          row["created_at"].isoformat(),
            "turn_id":             row["turn_id"],
            "app_id":              row["app_id"],
            "chat_session_id":     row["chat_session_id"],
            "user_id":             row["user_id"],
            "user_name":           row["user_name"],
            "user_email":          row["user_email"],
            "company_id":          row["company_id"],
            "company_name":        row["company_name"],
            "conversation_id":     row["conversation_id"],
            "user_message":        row["user_message"],
            "model_id":            row["model_id"],
            "prompt_tokens":       row["prompt_tokens"],
            "completion_tokens":   row["completion_tokens"],
            "total_tokens":        row["total_tokens"],
            "input_price_per_1m":  float(row["input_price_per_1m"]),
            "output_price_per_1m": float(row["output_price_per_1m"]),
            "input_cost_usd":      float(row["input_cost_usd"]),
            "output_cost_usd":     float(row["output_cost_usd"]),
            "total_cost_usd":      float(row["total_cost_usd"]),
            "tool_calls_count":    row["tool_calls_count"],
            "meta":                row["meta"],
        }
        for row in rows
    ]

    return {"items": items, "limit": limit, "offset": offset, "count": len(items)}


# ─────────────────────────────────────────────────────────────────────────────
# get_usage_stats — agregados para dashboard
# ─────────────────────────────────────────────────────────────────────────────

async def get_usage_stats(
    *,
    company_id: Optional[str] = None,
    app_id: Optional[str]     = None,
    date_from: Optional[str]  = None,
    date_to: Optional[str]    = None,
) -> dict[str, Any]:
    """
    Estatísticas agregadas: total tokens, custo USD, breakdown por modelo.
    """
    conditions = []
    params     = []
    idx        = 1

    if company_id:
        conditions.append(f"company_id = ${idx}"); params.append(company_id); idx += 1
    if app_id:
        conditions.append(f"app_id = ${idx}");     params.append(app_id);     idx += 1
    if date_from:
        conditions.append(f"created_at >= ${idx}::timestamptz"); params.append(date_from); idx += 1
    if date_to:
        conditions.append(f"created_at <= ${idx}::timestamptz"); params.append(date_to);   idx += 1

    where = ("WHERE " + " AND ".join(conditions)) if conditions else ""

    pool = _get_pool()
    async with pool.acquire() as conn:

        # Totais globais
        totals = await conn.fetchrow(
            f"""
            SELECT
                COALESCE(SUM(total_tokens),   0) AS total_tokens,
                COALESCE(SUM(total_cost_usd), 0) AS total_cost_usd
            FROM llm_usage_log {where}
            """,
            *params,
        )

        # Breakdown por modelo
        model_rows = await conn.fetch(
            f"""
            SELECT
                model_id,
                COALESCE(SUM(total_tokens),   0) AS total_tokens,
                COALESCE(SUM(total_cost_usd), 0) AS total_cost_usd
            FROM llm_usage_log {where}
            GROUP BY model_id
            ORDER BY total_cost_usd DESC
            """,
            *params,
        )

        # Breakdown por app
        app_rows = await conn.fetch(
            f"""
            SELECT
                app_id,
                COALESCE(SUM(total_tokens),   0) AS total_tokens,
                COALESCE(SUM(total_cost_usd), 0) AS total_cost_usd
            FROM llm_usage_log {where}
            GROUP BY app_id
            ORDER BY total_cost_usd DESC
            """,
            *params,
        )

    return {
        "total_tokens":   int(totals["total_tokens"]),
        "total_cost_usd": float(totals["total_cost_usd"]),
        "by_model": [
            {
                "model_id":       r["model_id"],
                "total_tokens":   int(r["total_tokens"]),
                "total_cost_usd": float(r["total_cost_usd"]),
            }
            for r in model_rows
        ],
        "by_app": [
            {
                "app_id":         r["app_id"],
                "total_tokens":   int(r["total_tokens"]),
                "total_cost_usd": float(r["total_cost_usd"]),
            }
            for r in app_rows
        ],
        "filters": {
            "company_id": company_id,
            "app_id":     app_id,
            "date_from":  date_from,
            "date_to":    date_to,
        },
    }