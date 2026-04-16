-- migrations/001_gateway_apps.sql
--
-- Tabelas de gestão de apps e API Keys do gateway.
-- A key real NUNCA é guardada — apenas o SHA-256 hash.
--
-- Executar:
--   psql $DATABASE_URL -f migrations/001_gateway_apps.sql

-- ─────────────────────────────────────────────────────────────────────────────
-- Apps registadas no gateway
-- ─────────────────────────────────────────────────────────────────────────────
-- Nota: se uma execução anterior falhar, pode ficar um tipo composto
-- `gateway_apps` no catálogo do Postgres (mesmo que a tabela não exista).
-- Isso faz `CREATE TABLE` falhar com: `type "gateway_apps" already exists`.
-- Para tornar o script re-executável, removemos o tipo apenas quando a tabela não existe.
DO $$
BEGIN
  IF EXISTS (SELECT 1 FROM pg_type WHERE typname = 'gateway_apps')
     AND NOT EXISTS (
       SELECT 1 FROM pg_class
        WHERE relname = 'gateway_apps'
          AND relkind = 'r'
     ) THEN
    EXECUTE 'DROP TYPE gateway_apps';
  END IF;
END $$;

CREATE TABLE IF NOT EXISTS gateway_apps (
    id          UUID        PRIMARY KEY DEFAULT gen_random_uuid(),
    app_id      TEXT        NOT NULL UNIQUE,   -- "severino-wa", "bluma", etc.
    name        TEXT        NOT NULL,           -- nome legível: "Severino WhatsApp"
    description TEXT,                          -- opcional: para que serve esta app
    is_active   BOOLEAN     NOT NULL DEFAULT TRUE,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at  TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_gateway_apps_app_id
    ON gateway_apps (app_id);

CREATE INDEX IF NOT EXISTS idx_gateway_apps_is_active
    ON gateway_apps (is_active);

-- ─────────────────────────────────────────────────────────────────────────────
-- API Keys das apps
-- A key real nunca é guardada — apenas o SHA-256 hex digest.
-- Uma app pode ter múltiplas keys (rotação sem downtime).
-- ─────────────────────────────────────────────────────────────────────────────

CREATE TABLE IF NOT EXISTS gateway_api_keys (
    id          UUID        PRIMARY KEY DEFAULT gen_random_uuid(),
    app_id      TEXT        NOT NULL REFERENCES gateway_apps(app_id)
                                ON DELETE CASCADE,
    key_hash    TEXT        NOT NULL UNIQUE,   -- SHA-256(key_real) em hex
    key_prefix  TEXT        NOT NULL,          -- primeiros 12 chars da key (para display)
                                               -- ex: "sk-gw-bluma-" — permite identificar
                                               -- a key sem a expor
    label       TEXT,                          -- "production", "staging", etc.
    is_active   BOOLEAN     NOT NULL DEFAULT TRUE,
    last_used_at TIMESTAMPTZ,                  -- quando foi usada pela última vez
    created_at  TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    revoked_at  TIMESTAMPTZ                    -- preenchido ao revogar (audit trail)
);

CREATE INDEX IF NOT EXISTS idx_gateway_api_keys_app_id
    ON gateway_api_keys (app_id);

CREATE INDEX IF NOT EXISTS idx_gateway_api_keys_key_hash
    ON gateway_api_keys (key_hash);

CREATE INDEX IF NOT EXISTS idx_gateway_api_keys_is_active
    ON gateway_api_keys (is_active);

-- ─────────────────────────────────────────────────────────────────────────────
-- Trigger: atualiza updated_at automaticamente na gateway_apps
-- ─────────────────────────────────────────────────────────────────────────────

CREATE OR REPLACE FUNCTION update_updated_at()
RETURNS TRIGGER AS $$
BEGIN
    NEW.updated_at = NOW();
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

DROP TRIGGER IF EXISTS trg_gateway_apps_updated_at ON gateway_apps;
CREATE TRIGGER trg_gateway_apps_updated_at
    BEFORE UPDATE ON gateway_apps
    FOR EACH ROW EXECUTE FUNCTION update_updated_at();

-- ─────────────────────────────────────────────────────────────────────────────
-- Dados iniciais de exemplo (comentar em produção)
-- ─────────────────────────────────────────────────────────────────────────────

-- INSERT INTO gateway_apps (app_id, name, description) VALUES
--     ('severino-web', 'Severino AgiWeb',      'Agente ERP web'),
--     ('severino-wa',  'Severino WhatsApp',     'Agente WhatsApp via Evolution API'),
--     ('bluma',        'Bluma',                 'npm lib para agentes externos');