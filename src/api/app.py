"""
src/api/app.py

Ponto de entrada da aplicação FastAPI.
Monta os routers, regista os error handlers globais,
configura o Swagger UI e o CORS.
"""
from __future__ import annotations

import logging
import time
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request, status
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from src.gateway.config import get_settings

logger = logging.getLogger(__name__)


# ─────────────────────────────────────────────────────────────────────────────
# Lifespan — startup / shutdown
# ─────────────────────────────────────────────────────────────────────────────

async def _cleanup_loop() -> None:
    """
    Background task — corre a cada 15 segundos.
    Remove baldes expirados do acumulador e grava-os no DB.
    Garante que turnos abandonados (agente crashou, loop infinito de tools)
    ficam registados no centro de custos.
    """
    import asyncio
    from src.gateway.accumulator import get_accumulator
    from src.usage.service import record_turn_usage

    while True:
        await asyncio.sleep(15)
        try:
            accumulator = get_accumulator()
            records = await accumulator.cleanup_expired()
            for record in records:
                try:
                    await record_turn_usage(**record)
                    print(f"[Cleanup] TTL record saved to DB: turn={record['turn_id'][:8]} tokens={record['total_tokens']}")
                except Exception as e:
                    print(f"[Cleanup] FAILED to save TTL record: {e} — record={record.get('turn_id','?')[:8]}")
        except Exception as e:
            print(f"[Cleanup] Cleanup loop error: {e}")


@asynccontextmanager
async def lifespan(app: FastAPI):
    import asyncio
    settings = get_settings()
    from src.gateway.key_store import init_key_store
    key_store = init_key_store(settings.database_url)
    await key_store.startup()
    logger.info(
        "FactorRouter a arrancar | upstream=%s | port=%s | %d keys carregadas",
        settings.upstream_url,
        settings.port,
        key_store.cache_size,
    )
    # Inicia background task de cleanup de baldes expirados
    cleanup_task = asyncio.create_task(_cleanup_loop())
   # print("[Cleanup] Background cleanup task started — interval=15s TTL=30s")
    yield
    cleanup_task.cancel()
    await key_store.shutdown()
    logger.info("FactorRouter a encerrar.")


# ─────────────────────────────────────────────────────────────────────────────
# App
# ─────────────────────────────────────────────────────────────────────────────

settings = get_settings()

app = FastAPI(
    title="FactorRouter",
    description="""
## O que é isto?

Drop-in replacement para o OpenAI SDK com routing automático de modelos e
centro de custos centralizado.

As tuas apps apontam para este gateway em vez do OpenRouter/OpenAI diretamente.
O gateway decide o modelo, faz o proxy do stream, e regista o custo.

## Autenticação

Todos os endpoints requerem `Authorization: Bearer <key>`.
Obtém a tua API Key com o administrador do gateway.

## Headers obrigatórios

Todos os requests a `/v1/*` devem incluir:

| Header | Tipo | Descrição |
|---|---|---|
| `X-App-Id` | string | Identificador da app (ex: `severino-wa`) |
| `X-Turn-Id` | UUID v4 | Gerado pelo agente no início de cada turno |
| `X-Session-Id` | string | ID da sessão de chat |
| `X-Conversation-Id` | string ou `"null"` | ID da conversa, se existir |
| `X-User-Message` | string | Primeiros 300 chars da mensagem do utilizador |
| `X-User-Id` | string ou `"null"` | ID do utilizador |
| `X-User-Name` | string ou `"null"` | Nome do utilizador |
| `X-User-Email` | string ou `"null"` | Email do utilizador |
| `X-Company-Id` | string ou `"null"` | ID da empresa |
| `X-Company-Name` | string ou `"null"` | Nome da empresa |

> **Nota:** Header ausente = `400`. Valor desconhecido = enviar `"null"` como string.
    """,
    version="2.0.0",
    lifespan=lifespan,
    docs_url="/docs",
    redoc_url="/redoc",
    openapi_tags=[
        {
            "name": "proxy",
            "description": "Endpoint principal — compatível com OpenAI SDK.",
        },
        {
            "name": "usage",
            "description": "Centro de custos — leitura de logs e estatísticas.",
        },
        {
            "name": "system",
            "description": "Health check e informação do sistema.",
        },
    ],
)


# ─────────────────────────────────────────────────────────────────────────────
# CORS
# ─────────────────────────────────────────────────────────────────────────────

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],       # ajustar em produção
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


# ─────────────────────────────────────────────────────────────────────────────
# Middleware — request timing
# ─────────────────────────────────────────────────────────────────────────────

@app.middleware("http")
async def add_timing_header(request: Request, call_next):
    start = time.perf_counter()
    response = await call_next(request)
    elapsed = time.perf_counter() - start
    response.headers["X-Response-Time-Ms"] = str(round(elapsed * 1000, 2))
    return response


# ─────────────────────────────────────────────────────────────────────────────
# Error handlers globais — erros claros e estruturados
# ─────────────────────────────────────────────────────────────────────────────

@app.exception_handler(404)
async def not_found_handler(request: Request, exc):
    return JSONResponse(
        status_code=status.HTTP_404_NOT_FOUND,
        content={
            "error": "not_found",
            "message": f"Endpoint '{request.method} {request.url.path}' não existe.",
            "docs": "/docs",
        },
    )


@app.exception_handler(405)
async def method_not_allowed_handler(request: Request, exc):
    return JSONResponse(
        status_code=status.HTTP_405_METHOD_NOT_ALLOWED,
        content={
            "error": "method_not_allowed",
            "message": f"Método '{request.method}' não permitido neste endpoint.",
        },
    )


@app.exception_handler(500)
async def internal_error_handler(request: Request, exc):
    logger.exception("Erro interno não tratado: %s %s", request.method, request.url.path)
    return JSONResponse(
        status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
        content={
            "error": "internal_server_error",
            "message": "Erro interno no gateway. Consulta os logs.",
        },
    )


# ─────────────────────────────────────────────────────────────────────────────
# Routers
# ─────────────────────────────────────────────────────────────────────────────

from src.api.routes import health, proxy, usage, admin, turns   # noqa: E402

app.include_router(health.router)
app.include_router(proxy.router, prefix="/v1", tags=["proxy"])
app.include_router(usage.router, prefix="/usage", tags=["usage"])
app.include_router(admin.router, prefix="/admin", tags=["admin"])
app.include_router(turns.router, prefix="/v1",    tags=["turns"])