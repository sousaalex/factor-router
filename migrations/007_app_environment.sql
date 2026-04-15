-- Ambiente da app no gateway (origem de roteamento de chave upstream).
-- Regra de negócio: app é dev ou prod; keys herdam este ambiente.

ALTER TABLE gateway_apps
    ADD COLUMN IF NOT EXISTS environment TEXT;

UPDATE gateway_apps
SET environment = COALESCE(NULLIF(TRIM(environment), ''), 'prod')
WHERE environment IS NULL OR TRIM(environment) = '';

ALTER TABLE gateway_apps
    ALTER COLUMN environment SET NOT NULL;

ALTER TABLE gateway_apps
    ALTER COLUMN environment SET DEFAULT 'dev';

DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1
        FROM pg_constraint
        WHERE conname = 'gateway_apps_environment_check'
    ) THEN
        ALTER TABLE gateway_apps
            ADD CONSTRAINT gateway_apps_environment_check
            CHECK (environment IN ('dev', 'prod'));
    END IF;
END $$;

COMMENT ON COLUMN gateway_apps.environment IS
    'Ambiente comercial da app (dev|prod). API keys desta app herdam este valor.';
