-- ═══════════════════════════════════════════════════════════════════════════════
--  LANDMARK CALL CENTER V2 · ROLLBACK (r2)
--
--  La vuelta atrás es OPERATIVA, no destructiva. El botón real de "parar
--  V2" es apagar los interruptores: eso detiene el discado en el siguiente
--  ciclo de WF2 sin perder un solo dato, y se deshace con otro clic.
--
--  Borrar tablas NO es rollback: es perder la evidencia de lo que pasó
--  mientras V2 estuvo encendido — llamadas, resultados, pedidos de pago,
--  auditoría. Por eso todo lo destructivo está COMENTADO a propósito.
--
--  ORDEN RECOMENDADO ante un problema en producción:
--
--    1. Parte A  — apagar V2 (reversible, segundos)
--    2. Desactivar los 7 workflows en n8n
--    3. Si hace falta volver a llamar: Call Center → Legacy Backup,
--       cambiar a LEGACY_BACKUP y encender los grupos legacy
--    4. Sólo si alguien decide desinstalar V2 del todo: Partes B y C,
--       descomentando a mano y con backup previo
--
--  Lo que este fichero NO toca NUNCA, ni siquiera comentado:
--    n8n_switches · n8n_switch_schedules · sip_providers ·
--    sip_provider_pricing · sip_deposits · sip_extensions ·
--    panel_leads · panel_conversions · support_actions · cdr
-- ═══════════════════════════════════════════════════════════════════════════════


-- ═══════════════════════════════════════════════════════════════════════════════
--  PARTE 0 · INFORME — sólo lee. Correr esto ANTES de decidir nada.
-- ═══════════════════════════════════════════════════════════════════════════════

SELECT 'operating_mode' AS item, setting_value AS value
  FROM app_settings WHERE setting_key = 'lm_operating_mode'
UNION ALL SELECT 'countries_enabled',   CAST(COUNT(*) AS CHAR) FROM countries WHERE enabled = 1
UNION ALL SELECT 'providers_enabled',   CAST(COUNT(*) AS CHAR) FROM voice_providers WHERE enabled = 1
UNION ALL SELECT 'routes_enabled',      CAST(COUNT(*) AS CHAR) FROM call_routes WHERE enabled = 1 AND archived_at IS NULL
UNION ALL SELECT 'call_jobs_total',     CAST(COUNT(*) AS CHAR) FROM wf_call_jobs
UNION ALL SELECT 'call_jobs_in_flight', CAST(COUNT(*) AS CHAR) FROM wf_call_jobs
       WHERE state IN ('CLAIMED','DISPATCHING','DISPATCHED')
UNION ALL SELECT 'needs_reconciliation',CAST(COUNT(*) AS CHAR) FROM wf_call_jobs
       WHERE state IN ('UNKNOWN','NEEDS_RECONCILIATION')
UNION ALL SELECT 'payment_orders_open', CAST(COUNT(*) AS CHAR) FROM wf_payment_orders
       WHERE state = 'REQUESTED'
UNION ALL SELECT 'events_total',        CAST(COUNT(*) AS CHAR) FROM wf_events
UNION ALL SELECT 'issues_open',         CAST(COUNT(*) AS CHAR) FROM wf_reconciliation_issues
       WHERE state = 'OPEN'
UNION ALL SELECT 'legacy_groups',       CAST(COUNT(*) AS CHAR) FROM legacy_group_classification;

-- SIP Balance es de v1 y puede no estar creado todavía (el panel lo crea al
-- arrancar). Se consulta sólo si existe, para que el informe no reviente en
-- una instalación donde aún no se abrió esa pantalla.
SET @has_sip := (SELECT COUNT(*) FROM information_schema.TABLES
                 WHERE TABLE_SCHEMA=DATABASE() AND TABLE_NAME='sip_deposits');
SET @sql := IF(@has_sip=1,
  'SELECT ''sip_deposits_kept'' AS item, CAST(COUNT(*) AS CHAR) AS value FROM sip_deposits',
  'SELECT ''sip_deposits_kept'' AS item, ''(SIP Balance not initialised)'' AS value');
PREPARE s FROM @sql; EXECUTE s; DEALLOCATE PREPARE s;

-- Llamadas a medias: si hay alguna, esperar a que WF14 las reconcilie antes
-- de apagar, o quedarán en NEEDS_RECONCILIATION y habrá que mirarlas a mano.
SELECT call_job_id, lead_id, route_key, state, attempt, created_at
  FROM wf_call_jobs
 WHERE state IN ('CLAIMED','DISPATCHING','DISPATCHED')
 ORDER BY created_at
 LIMIT 50;

-- Pedidos de pago sin confirmar: el cliente puede tener un link abierto.
-- Apagar V2 no los cancela; siguen siendo cobros válidos del lado de la
-- pasarela. Mirar esta lista antes de dar por cerrada la vuelta atrás.
SELECT out_trade_no, lead_id, country_iso, amount, currency, state, requested_at
  FROM wf_payment_orders
 WHERE state = 'REQUESTED'
 ORDER BY requested_at
 LIMIT 50;


-- ═══════════════════════════════════════════════════════════════════════════════
--  PARTE A · APAGADO REVERSIBLE — esto SÍ se ejecuta
--
--  Deja V2 sin capacidad de llamar. No borra nada. Se deshace volviendo a
--  encender desde el panel, ruta por ruta, cuando se quiera.
-- ═══════════════════════════════════════════════════════════════════════════════

UPDATE call_routes     SET enabled = 0 WHERE enabled = 1;
UPDATE voice_providers SET enabled = 0 WHERE enabled = 1;
UPDATE countries       SET enabled = 0 WHERE enabled = 1;

-- Rastro de por qué está todo apagado, para quien lo mire mañana.
INSERT INTO route_audit (route_id, route_key, action, field, old_value, new_value, actor)
VALUES (NULL, NULL, 'ROLLBACK_DISABLE_ALL', 'enabled', '1', '0', 'rollback.sql');

-- El modo NO se cambia aquí. Cambiarlo es una decisión con consecuencias
-- (deja que el legacy vuelva a llamar) y tiene su propio circuito con
-- confirmación y auditoría en Call Center → Legacy Backup. Un script no
-- debería poder poner a llamar a un sistema entero sin que nadie confirme.

SELECT 'V2 disabled. No data was deleted.' AS result,
       (SELECT COUNT(*) FROM call_routes WHERE enabled = 1) AS routes_still_on,
       (SELECT COUNT(*) FROM wf_call_jobs) AS call_jobs_kept,
       (SELECT COUNT(*) FROM wf_events) AS events_kept,
       (SELECT COUNT(*) FROM wf_payment_orders) AS payment_orders_kept;

-- Y la comprobación que más importa: SIP Balance sigue entero.
SET @sql := IF(@has_sip=1,
  'SELECT COUNT(*) AS sip_deposits_kept, COALESCE(SUM(amount_usd),0) AS deposited_usd FROM sip_deposits',
  'SELECT ''(SIP Balance not initialised)'' AS sip_deposits_kept');
PREPARE s FROM @sql; EXECUTE s; DEALLOCATE PREPARE s;


-- ═══════════════════════════════════════════════════════════════════════════════
--  PARTE B · DESINSTALAR EL SUITE V2 — COMENTADO A PROPÓSITO
--
--  Esto BORRA la evidencia operativa de V2: llamadas, resultados, eventos,
--  pedidos de pago, auditoría de facturación y de modo. Después no se puede
--  responder "¿a quién llamamos el martes?" ni "¿quién cambió esa tarifa?".
--
--  Descomentar sólo con un backup hecho y verificado, y sólo si alguien ha
--  decidido explícitamente desinstalar V2.
-- ═══════════════════════════════════════════════════════════════════════════════

-- -- Ejecución y analítica de V2
-- DROP TABLE IF EXISTS wf_payment_orders;
-- DROP TABLE IF EXISTS wf_recording_ledger;
-- DROP TABLE IF EXISTS wf_tool_requests;
-- DROP TABLE IF EXISTS wf_conversation_ledger;
-- DROP TABLE IF EXISTS wf_reconciliation_issues;
-- DROP TABLE IF EXISTS wf_events;
-- DROP TABLE IF EXISTS wf_call_jobs;
--
-- -- Configuración de V2
-- DROP TABLE IF EXISTS route_capacity_windows;
-- DROP TABLE IF EXISTS route_telegram_targets;
-- DROP TABLE IF EXISTS country_tool_configs;
-- DROP TABLE IF EXISTS call_routes;
-- DROP TABLE IF EXISTS followup_policies;
-- DROP TABLE IF EXISTS voice_providers;
-- DROP TABLE IF EXISTS countries;
-- DROP TABLE IF EXISTS route_audit;
-- DROP TABLE IF EXISTS wf_settings;
--
-- -- Facturación y Legacy Backup de r2
-- DROP TABLE IF EXISTS billing_audit;
-- DROP TABLE IF EXISTS legacy_group_classification;
--
-- DELETE FROM schema_migrations WHERE migration_id LIKE '001_multi_country%'
--                                  OR migration_id LIKE '002_callcenter%'
--                                  OR migration_id LIKE '005_legacy%'
--                                  OR migration_id LIKE '006_payments%';
--
-- -- El modo de operación vuelve a no existir; app_settings es de v1 y se queda.
-- DELETE FROM app_settings WHERE setting_key LIKE 'lm_operating_mode%';


-- ═══════════════════════════════════════════════════════════════════════════════
--  PARTE C · QUITAR SÓLO LO QUE AÑADIÓ r2 — COMENTADO A PROPÓSITO
--
--  Deja la FUNDACIÓN V2.2 en pie y revierte únicamente facturación,
--  Legacy Backup y pagos. Útil si se quiere conservar el call center V2
--  pero descartar lo que se añadió en esta corrección.
--
--  Aviso: quitar las columnas de facturación PIERDE las tarifas por ruta y
--  el vínculo con SIP Balance. Las tablas de SIP Balance no se tocan, así
--  que el saldo y los depósitos siguen intactos — pero habrá que volver a
--  vincular Provider1 a mano.
-- ═══════════════════════════════════════════════════════════════════════════════

-- -- 006 pagos
-- DROP TABLE IF EXISTS wf_payment_orders;
-- ALTER TABLE country_tool_configs DROP COLUMN adapter_key;
-- ALTER TABLE country_tool_configs DROP COLUMN router_key;
-- ALTER TABLE country_tool_configs DROP COLUMN callback_url;
-- ALTER TABLE country_tool_configs DROP COLUMN return_url;
-- ALTER TABLE country_tool_configs MODIFY mode
--   ENUM('CONFIG_ROUTER','CUSTOM_ENDPOINT') NOT NULL DEFAULT 'CONFIG_ROUTER';
--
-- -- 005 Legacy Backup  (n8n_switches y n8n_switch_schedules NO se tocan)
-- DROP TABLE IF EXISTS legacy_group_classification;
-- DELETE FROM app_settings WHERE setting_key LIKE 'lm_operating_mode%';
--
-- -- 004 facturación  (sip_* NO se tocan)
-- DROP TABLE IF EXISTS billing_audit;
-- ALTER TABLE voice_providers DROP COLUMN billing_model;
-- ALTER TABLE voice_providers DROP COLUMN billing_currency;
-- ALTER TABLE voice_providers DROP COLUMN monthly_fee;
-- ALTER TABLE voice_providers DROP COLUMN billing_start_date;
-- ALTER TABLE voice_providers DROP COLUMN billing_notes;
-- ALTER TABLE voice_providers DROP COLUMN legacy_sip_provider_id;
-- ALTER TABLE call_routes DROP COLUMN price_per_minute;
-- ALTER TABLE call_routes DROP COLUMN price_per_call;
-- ALTER TABLE call_routes DROP COLUMN billing_notes;
-- ALTER TABLE call_routes DROP COLUMN trunk_name;
--
-- DELETE FROM schema_migrations WHERE migration_id IN
--   ('005_legacy_backup_v2:install_safety',
--    '006_payments_v2:migrate_seeded_payment_mode');
