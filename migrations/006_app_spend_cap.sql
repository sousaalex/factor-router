-- Quota por app (tenant): quanto cada integração pode consumir em USD (estimado no gateway).
-- Independente do saldo de créditos OpenRouter da organização.
-- spend_cap_usd: teto; spent_usd_total: acumulado (sincronizado em record_turn_usage).

ALTER TABLE gateway_apps
    ADD COLUMN IF NOT EXISTS spend_cap_usd NUMERIC(12, 2) NOT NULL DEFAULT 10,
    ADD COLUMN IF NOT EXISTS spent_usd_total NUMERIC(14, 6) NOT NULL DEFAULT 0;

COMMENT ON COLUMN gateway_apps.spend_cap_usd IS
    'Teto em USD que esta app (ex.: Severino AgiWeb) pode consumir via API no router; default 10.';
COMMENT ON COLUMN gateway_apps.spent_usd_total IS
    'Soma de total_cost_usd (uso registado); actualizado a cada insert em llm_usage_log.';

-- Align counter with historical usage (idempotent if re-run after partial deploy)
UPDATE gateway_apps a
SET spent_usd_total = COALESCE(
    (SELECT SUM(u.total_cost_usd) FROM llm_usage_log u WHERE u.app_id = a.app_id),
    0
);
