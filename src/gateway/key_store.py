"""
src/gateway/key_store.py

Gestão de API Keys do gateway.

Como a Anthropic e a OpenAI fazem:
    - A key real NUNCA é guardada na base de dados
    - Guardamos apenas SHA-256(key) em hex
    - Em cada request fazemos sha256(key_recebida) e procuramos no DB
    - Cache em memória (TTL 5min) — zero I/O ao DB por request
    - Se o DB for comprometido, as keys reais continuam seguras

Quota de LLM (USD) no router é por APP (gateway_apps): todas as keys da mesma app
partilham spend_cap_usd e spent_usd_total — a key só identifica a app.

Ciclo de vida de uma key:
    1. Admin chama POST /admin/apps/{app_id}/keys
    2. Gateway gera key = "sk-gw-{app_id}-{secrets.token_hex(24)}"
    3. Calcula hash = sha256(key)
    4. Guarda {key_hash, key_prefix, label} no Postgres
    5. Devolve a key UMA ÚNICA VEZ ao admin — nunca mais é recuperável
    6. Admin guarda a key em segurança (ex: variável de ambiente da app)
    7. Em cada request: sha256(key_recebida) → lookup no cache → app_id

Revogação:
    - DELETE /admin/apps/{app_id}/keys/{key_id}
    - Marca is_active=False e revoked_at=NOW() no Postgres
    - Invalida o cache imediatamente
    - O audit trail fica no revoked_at
"""
from __future__ import annotations

import asyncio
import hashlib
import logging
import secrets
import time
from dataclasses import dataclass
from typing import Optional

import asyncpg

logger = logging.getLogger(__name__)

# TTL do cache em memória (segundos)
# Após este tempo, o cache é recarregado do Postgres
# Significa: uma key revogada demora no máximo este tempo a ser bloqueada
_CACHE_TTL_SECONDS = 300  # 5 minutos


# ─────────────────────────────────────────────────────────────────────────────
# Helpers de hashing
# ─────────────────────────────────────────────────────────────────────────────

def hash_key(api_key: str) -> str:
    """
    Calcula SHA-256(api_key) e devolve o hex digest.
    É o único valor guardado na base de dados.

    Exemplo:
        hash_key("sk-gw-bluma-abc123") → "e3b0c44298fc..."
    """
    return hashlib.sha256(api_key.encode("utf-8")).hexdigest()


def generate_api_key(app_id: str) -> tuple[str, str, str]:
    """
    Gera uma nova API Key para uma app.

    Devolve:
        (api_key, key_hash, key_prefix)

    - api_key    → devolvida UMA VEZ ao admin, nunca guardada
    - key_hash   → guardado no Postgres (SHA-256 da key)
    - key_prefix → primeiros 10 chars para display (ex: "sk-fai-e56")

    Formato: sk-fai-{48 chars hex aleatório}
    Exemplo: sk-fai-e5627b264cf469b5d8dbe06c415dcf74a2f36947c61ce131
    """
    random_part = secrets.token_hex(24)   # 48 chars hex = 192 bits de entropia
    api_key     = f"sk-fai-{random_part}"
    key_hash    = hash_key(api_key)
    key_prefix  = api_key[:14]  # "sk-fai-e5627b" — suficiente para identificar
    return api_key, key_hash, key_prefix


def looks_like_gateway_api_key(api_key: str) -> bool:
    """Evita reload extra do cache em tentativas óbvias com chave inválida."""
    s = (api_key or "").strip()
    return s.startswith("sk-fai-") and len(s) >= 20


# ─────────────────────────────────────────────────────────────────────────────
# CachedKey — entrada no cache
# ─────────────────────────────────────────────────────────────────────────────

@dataclass
class CachedKey:
    """Entrada no cache em memória."""
    app_id:    str
    key_id:    str   # UUID da linha em gateway_api_keys
    app_name:  str
    is_active: bool
    label: Optional[str] = None


# ─────────────────────────────────────────────────────────────────────────────
# KeyStore — singleton que gere o cache e as operações no DB
# ─────────────────────────────────────────────────────────────────────────────

class KeyStore:
    """
    Gere a validação de API Keys com cache em memória.

    O cache é um dict {key_hash → CachedKey}.
    É carregado do Postgres no arranque e refrescado periodicamente (TTL).

    Validação por request (O(1), zero I/O):
        hash = sha256(api_key_recebida)
        entry = cache.get(hash)
        if entry and entry.is_active: autenticado

    Thread-safe via asyncio.Lock.
    """

    def __init__(self, database_url: str) -> None:
        self._database_url = database_url
        self._cache: dict[str, CachedKey] = {}
        self._cache_loaded_at: float = 0.0
        self._lock = asyncio.Lock()
        self._pool: Optional[asyncpg.Pool] = None

    async def startup(self) -> None:
        """
        Inicializa o pool de ligações ao Postgres e carrega o cache.
        Chamado no lifespan do FastAPI.
        """
        self._pool = await asyncpg.create_pool(
            self._database_url,
            min_size=1,
            max_size=5,
        )
        await self._reload_cache()
        logger.info(
            "KeyStore started — %d active keys loaded from Postgres",
            len(self._cache),
        )

    async def shutdown(self) -> None:
        """Fecha o pool de ligações. Chamado no shutdown do FastAPI."""
        if self._pool:
            await self._pool.close()

    # ── Validação (caminho crítico — chamado em CADA request) ────────────────

    async def validate(self, api_key: str) -> Optional[CachedKey]:
        """
        Valida uma API Key.
        Devolve CachedKey se válida e ativa, None caso contrário.

        Fluxo:
            1. Calcula sha256(api_key)
            2. Refresca cache se TTL expirou (**await** — antes era create_task e o mesmo
               pedido via cache velho → 401 após reactivar app / nova key)
            3. Se miss e a string parece key sk-fai-*, faz mais um reload e volta a procurar
               (admin reactivou app ou alterou BD sem passar pelo patch_app)
            4. Lookup O(1); verifica is_active na entrada
        """
        key_hash = hash_key(api_key)

        if self._cache_needs_refresh():
            await self._reload_cache()

        entry = self._cache.get(key_hash)
        if entry is None and looks_like_gateway_api_key(api_key):
            await self._reload_cache()
            entry = self._cache.get(key_hash)

        if entry is None or not entry.is_active:
            return None

        # Atualiza last_used_at de forma assíncrona (não bloqueia o request)
        asyncio.create_task(self._update_last_used(entry.key_id))

        return entry

    # ── Operações de gestão (Admin API) ──────────────────────────────────────

    async def create_app(
        self,
        name: str,
        environment: str = "dev",
        description: str | None = None,
        spend_cap_usd: float = 10.0,
    ) -> dict:
        """
        Regista uma nova app no gateway.
        O app_id e gerado automaticamente a partir do name:
            "Severino WhatsApp" -> "severino-whatsapp"
        spend_cap_usd: quota máxima (USD estimados) que esta app pode consumir no router.
        Devolve os dados da app criada.
        """
        import re as _re
        app_id = _re.sub(r"[^a-z0-9]+", "-", name.strip().lower()).strip("-")
        env = (environment or "dev").strip().lower()
        if env not in {"dev", "prod"}:
            raise ValueError("Invalid app environment. Allowed values: dev, prod.")
        async with self._pool.acquire() as conn:
            row = await conn.fetchrow(
                """
                INSERT INTO gateway_apps (app_id, name, environment, description, spend_cap_usd)
                VALUES ($1, $2, $3, $4, $5)
                RETURNING id, app_id, name, environment, description, is_active, created_at,
                          spend_cap_usd, spent_usd_total
                """,
                app_id, name, env, description, spend_cap_usd,
            )
        logger.info("App created: app_id=%s name=%s spend_cap_usd=%s", app_id, name, spend_cap_usd)
        return _serialize_app_row(dict(row))

    async def get_app_spend_status(self, app_id: str) -> Optional[dict]:
        """
        Quota do tenant: teto e consumo acumulado em USD (estimado no gateway) para o proxy.
        Não reflecte saldo OpenRouter — só o que esta app já “gastou” no vosso router.
        Devolve None se a app não existir.
        """
        async with self._pool.acquire() as conn:
            row = await conn.fetchrow(
                """
                SELECT spend_cap_usd, spent_usd_total, is_active
                FROM gateway_apps
                WHERE app_id = $1
                """,
                app_id,
            )
        if row is None:
            return None
        cap = float(row["spend_cap_usd"])
        spent = float(row["spent_usd_total"])
        return {
            "spend_cap_usd":   cap,
            "spent_usd_total": spent,
            "remaining_usd":   max(0.0, cap - spent),
            "is_active":       row["is_active"],
        }

    async def patch_app(
        self,
        app_id: str,
        *,
        spend_cap_usd: float | None = None,
        is_active: bool | None = None,
        environment: str | None = None,
    ) -> Optional[dict]:
        """
        Actualiza teto de gasto e/ou is_active. Devolve a app actualizada ou None.
        """
        if spend_cap_usd is None and is_active is None and environment is None:
            raise ValueError("Nothing to update.")
        sets: list[str] = []
        args: list = []
        idx = 1
        if spend_cap_usd is not None:
            sets.append(f"spend_cap_usd = ${idx}")
            args.append(spend_cap_usd)
            idx += 1
        if is_active is not None:
            sets.append(f"is_active = ${idx}")
            args.append(is_active)
            idx += 1
        if environment is not None:
            env = (environment or "").strip().lower()
            if env not in {"dev", "prod"}:
                raise ValueError("Invalid app environment. Allowed values: dev, prod.")
            sets.append(f"environment = ${idx}")
            args.append(env)
            idx += 1
        args.append(app_id)
        q = f"""
            UPDATE gateway_apps
            SET {", ".join(sets)}
            WHERE app_id = ${idx}
            RETURNING id, app_id, name, environment, description, is_active, created_at,
                      spend_cap_usd, spent_usd_total
        """
        async with self._pool.acquire() as conn:
            row = await conn.fetchrow(q, *args)
            if row is not None and environment is not None:
                await conn.execute(
                    """
                    UPDATE gateway_api_keys
                    SET label = $1
                    WHERE app_id = $2
                    """,
                    env, app_id,
                )
        if row is None:
            return None
        out = _serialize_app_row(dict(row))
        # is_active / keys válidas mudam — recarregar cache para não ficar 401 até ao TTL (5 min)
        await self._reload_cache()
        return out

    async def create_key(
        self,
        app_id: str,
        label: str | None = None,
    ) -> dict:
        """
        Gera uma nova API Key para uma app.

        A key real é devolvida UMA ÚNICA VEZ.
        O Postgres guarda apenas o hash.

        Devolve:
            {
                "api_key":    "sk-gw-bluma-...",   ← guardar em segurança
                "key_id":     "uuid...",
                "key_prefix": "sk-gw-bluma-a3f9",  ← para display futuro
                "app_id":     "bluma",
                "label":      "production",
            }
        """
        # Verifica que a app existe e está ativa
        async with self._pool.acquire() as conn:
            app = await conn.fetchrow(
                "SELECT app_id, name, environment, is_active FROM gateway_apps WHERE app_id = $1",
                app_id,
            )
            if not app:
                raise ValueError(f"App '{app_id}' not found.")
            if not app["is_active"]:
                raise ValueError(f"App '{app_id}' is disabled.")
            app_env = str(app["environment"]).strip().lower()
            if app_env not in {"dev", "prod"}:
                raise ValueError(
                    f"App '{app_id}' has invalid environment '{app_env}'. Contact FactorRouter admin."
                )
            display_name = (label or "").strip()
            if ":" in display_name:
                raise ValueError("Key name cannot contain ':'.")
            stored_label = app_env if not display_name else f"{app_env}:{display_name}"

            # Gera key, hash e prefix
            api_key, key_hash, key_prefix = generate_api_key(app_id)

            # Guarda APENAS o hash no Postgres
            row = await conn.fetchrow(
                """
                INSERT INTO gateway_api_keys (app_id, key_hash, key_prefix, label)
                VALUES ($1, $2, $3, $4)
                RETURNING id, app_id, key_prefix, label, is_active, created_at
                """,
                app_id, key_hash, key_prefix, stored_label,
            )

        # Atualiza cache imediatamente (sem esperar pelo TTL)
        async with self._lock:
            self._cache[key_hash] = CachedKey(
                app_id=app_id,
                key_id=str(row["id"]),
                app_name=app["name"],
                is_active=True,
                label=row["label"],
            )

        logger.info(
            "Key created: app_id=%s prefix=%s label=%s",
            app_id, key_prefix, label,
        )

        return {
            "api_key":    api_key,       # ← devolvida UMA VEZ, guardar em segurança
            "key_id":     str(row["id"]),
            "key_prefix": key_prefix,
            "app_id":     app_id,
            "label":      stored_label,
            "created_at": row["created_at"].isoformat(),
            "warning":    "Store this key securely — it will not be shown again.",
        }

    async def revoke_key(self, key_id: str, app_id: str) -> dict:
        """
        Revoga uma API Key. Efeito imediato — remove do cache.
        O registo fica no Postgres para audit trail (revoked_at preenchido).
        """
        async with self._pool.acquire() as conn:
            row = await conn.fetchrow(
                """
                UPDATE gateway_api_keys
                SET is_active = FALSE, revoked_at = NOW()
                WHERE id = $1 AND app_id = $2
                RETURNING id, app_id, key_prefix, label, revoked_at
                """,
                key_id, app_id,
            )
            if not row:
                raise ValueError(
                    f"Key '{key_id}' not found for app '{app_id}'."
                )

        # Remove do cache imediatamente
        async with self._lock:
            self._cache = {
                h: e for h, e in self._cache.items()
                if e.key_id != key_id
            }

        logger.warning(
            "Key revoked: app_id=%s prefix=%s key_id=%s",
            app_id, row["key_prefix"], key_id,
        )
        return {
            "key_id":     str(row["id"]),
            "app_id":     row["app_id"],
            "key_prefix": row["key_prefix"],
            "label":      row["label"],
            "revoked_at": row["revoked_at"].isoformat(),
        }

    async def patch_key_label(self, app_id: str, key_id: str, label: str) -> dict:
        """
        Atualiza o label (ambiente) de uma key existente.
        Label suportado: dev | prod.
        """
        label_norm = (label or "").strip().lower()
        if label_norm not in {"dev", "prod"}:
            raise ValueError("Invalid key label. Allowed values: dev, prod.")

        async with self._pool.acquire() as conn:
            row = await conn.fetchrow(
                """
                UPDATE gateway_api_keys
                SET label = $1
                WHERE id = $2 AND app_id = $3
                RETURNING id, app_id, key_prefix, label, is_active, created_at, revoked_at, last_used_at
                """,
                label_norm, key_id, app_id,
            )
            if not row:
                raise ValueError(f"Key '{key_id}' not found for app '{app_id}'.")

        await self._reload_cache()
        return dict(row)

    async def list_apps(self) -> list[dict]:
        """Lista todas as apps com contagem de keys ativas."""
        async with self._pool.acquire() as conn:
            rows = await conn.fetch(
                """
                SELECT
                    a.id, a.app_id, a.name, a.environment, a.description,
                    a.is_active, a.created_at,
                    a.spend_cap_usd, a.spent_usd_total,
                    COUNT(k.id) FILTER (WHERE k.is_active) AS active_keys
                FROM gateway_apps a
                LEFT JOIN gateway_api_keys k ON k.app_id = a.app_id
                GROUP BY a.id, a.app_id, a.name, a.environment, a.description,
                         a.is_active, a.created_at,
                         a.spend_cap_usd, a.spent_usd_total
                ORDER BY a.created_at DESC
                """
            )
        return [_serialize_app_row(dict(r)) for r in rows]

    async def list_keys(self, app_id: str) -> list[dict]:
        """Lista as keys de uma app (sem expor o hash — só o prefix e metadata)."""
        async with self._pool.acquire() as conn:
            rows = await conn.fetch(
                """
                SELECT id, app_id, key_prefix, label,
                       is_active, last_used_at, created_at, revoked_at
                FROM gateway_api_keys
                WHERE app_id = $1
                ORDER BY created_at DESC
                """,
                app_id,
            )
        return [dict(r) for r in rows]

    # ── Cache interno ────────────────────────────────────────────────────────

    def _cache_needs_refresh(self) -> bool:
        return (time.monotonic() - self._cache_loaded_at) > _CACHE_TTL_SECONDS

    async def _reload_cache(self) -> None:
        """
        Recarrega o cache do Postgres.
        Substitui o dict atomicamente — zero downtime durante o reload.
        """
        if not self._pool:
            return
        try:
            async with self._pool.acquire() as conn:
                rows = await conn.fetch(
                    """
                    SELECT k.key_hash, k.id, k.app_id, k.is_active, k.label, a.name
                    FROM gateway_api_keys k
                    JOIN gateway_apps a ON a.app_id = k.app_id
                    WHERE k.is_active = TRUE AND a.is_active = TRUE
                    """
                )

            new_cache = {
                row["key_hash"]: CachedKey(
                    app_id=row["app_id"],
                    key_id=str(row["id"]),
                    app_name=row["name"],
                    is_active=row["is_active"],
                    label=row["label"],
                )
                for row in rows
            }

            async with self._lock:
                self._cache = new_cache
                self._cache_loaded_at = time.monotonic()

            logger.debug(
                "Cache reloaded — %d active keys",
                len(new_cache),
            )

        except Exception as e:
            logger.error("Failed to reload key cache: %s", e)

    async def _update_last_used(self, key_id: str) -> None:
        """Atualiza last_used_at de forma assíncrona — não bloqueia o request."""
        if not self._pool:
            return
        try:
            async with self._pool.acquire() as conn:
                await conn.execute(
                    "UPDATE gateway_api_keys SET last_used_at = NOW() WHERE id = $1",
                    key_id,
                )
        except Exception as e:
            logger.debug("Failed to update last_used_at for key %s: %s", key_id, e)

    @property
    def cache_size(self) -> int:
        """Número de keys em cache. Útil para métricas e health check."""
        return len(self._cache)


def _serialize_app_row(d: dict) -> dict:
    """Normaliza tipos NUMERIC/datetime para JSON-friendly."""
    out = dict(d)
    if "created_at" in out and out["created_at"] is not None:
        out["created_at"] = out["created_at"].isoformat()
    for k in ("spend_cap_usd", "spent_usd_total"):
        if k in out and out[k] is not None:
            out[k] = float(out[k])
    if "active_keys" in out and out["active_keys"] is not None:
        out["active_keys"] = int(out["active_keys"])
    cap = out.get("spend_cap_usd")
    spent = out.get("spent_usd_total")
    if isinstance(cap, (int, float)) and isinstance(spent, (int, float)):
        out["remaining_usd"] = max(0.0, float(cap) - float(spent))
    return out


# ─────────────────────────────────────────────────────────────────────────────
# Singleton global
# ─────────────────────────────────────────────────────────────────────────────

_key_store: Optional[KeyStore] = None


def init_key_store(database_url: str) -> KeyStore:
    """Inicializa o singleton. Chamado no lifespan do app.py."""
    global _key_store
    _key_store = KeyStore(database_url)
    return _key_store


def get_key_store() -> KeyStore:
    """
    Devolve o singleton do KeyStore.
    Usar via Depends(get_key_store) nos endpoints FastAPI.
    """
    if _key_store is None:
        raise RuntimeError(
            "KeyStore not initialized. "
            "Ensure init_key_store() is called from the FastAPI lifespan."
        )
    return _key_store