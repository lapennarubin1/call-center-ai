-- ═══════════════════════════════════════════════════════════════════════════════
--  LANDMARK CALL CENTER V2 · MIGRACIÓN COMPLETA (r2)
--
--  Concatenación de sql/parts/. Ejecutar ENTERO y en este orden:
--
--    001  fundación V2.2 — países, proveedores, rutas, políticas, ejecución
--    002  suite V2       — ajustes, grabaciones, tools, índices
--    003  compat legacy  — tablas de v1 que WF14/WF10 leen (no-op si ya están)
--    004  facturación    — un proveedor, 5 modelos, precio por ruta
--    005  Legacy Backup  — clasificación de grupos y modo de operación
--    006  pagos          — DIRECT_PROVIDER / UNIVERSAL_ROUTER
--
--  Propiedades verificadas contra MariaDB 10.11 real:
--    · idempotente      correr dos veces deja el mismo estado
--    · re-ejecutable    no falla en la segunda pasada
--    · no destructiva   no borra ni vacía ninguna tabla
--    · no pisa ediciones: precios, cuotas, modos y configuración de país
--      editados desde el panel sobreviven a una re-ejecución
--
--  Lo que NO hace:
--    · no enciende rutas, proveedores ni países
--    · no cambia el modo de operación
--    · no toca n8n_switches, n8n_switch_schedules, sip_deposits,
--      sip_provider_pricing ni ninguna tabla de v1
--
--    mysql <base> < migration.sql
-- ═══════════════════════════════════════════════════════════════════════════════


-- ┌────────────────────────────────────────────────────────────────────────┐
-- │ PARTE: 001_multi_country_config_v2_2.sql                              │
-- └────────────────────────────────────────────────────────────────────────┘

-- ═══════════════════════════════════════════════════════════════════════════════
--  MIGRATION_001_MULTI_COUNTRY_CONFIG_V2_2.sql
--  Landmark Markets · Fundación multi-país V2.2
--
--  ESTADO: PROPUESTA — NO EJECUTADA EN PRODUCCIÓN.
--  Reemplaza a V2.1 (nunca desplegada). Una instalación limpia crea directamente
--  el esquema V2.2. Una base con V2.1 aplicada se actualiza en el lugar.
--
--  GARANTÍAS
--    · Idempotente: la 2ª ejecución es NO-OP funcional.
--    · Seed de UNA sola vez (marcador en schema_migrations). Si la base ya tiene
--      el seed de V2.1, el de V2.2 no corre: nunca pisa configuración del panel.
--    · Sin DROP / TRUNCATE / DELETE ejecutables.
--    · No altera ninguna tabla productiva existente (crm_leads, crm_conversions,
--      stringee_calls, wf_call_followups, wf10_sent_recordings,
--      wf2_provider_config, n8n_switches, sip_*, app_settings…).
--
--  NOVEDADES V2.2
--    · countries.enabled                → interruptor por PAÍS
--    · voice_providers.adapter_key      → VARCHAR, separa proveedor comercial de
--                                         implementación técnica (sin ENUM)
--    · wf_call_jobs                     → hecho por llamada: resultado, duración,
--                                         reintento TÉCNICO separado del intento de
--                                         NEGOCIO, estado RELEASED, y a lo sumo UNA
--                                         llamada en vuelo por lead (UNIQUE generado)
--    · wf_events                        → event store local, idempotente por event_key
--    · wf_reconciliation_issues         → diferencias local ↔ CRM/proveedor
--
--  Motor objetivo: MariaDB 10.11 (probado). Usa columnas generadas STORED.
-- ═══════════════════════════════════════════════════════════════════════════════

SET @MIG       = '001_multi_country_config_v2_2';
SET @SEED      = '001_multi_country_config_v2_2:seed';
SET @SEED_V21  = '001_multi_country_config_v2_1:seed';

CREATE TABLE IF NOT EXISTS schema_migrations (
  migration_id VARCHAR(128) NOT NULL PRIMARY KEY,
  applied_at   DATETIME     NOT NULL DEFAULT CURRENT_TIMESTAMP,
  notes        VARCHAR(255) NULL
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;


-- ═══════════════════════════════════════════════════════════════════════════════
--  A · CONFIGURACIÓN
-- ═══════════════════════════════════════════════════════════════════════════════

-- A1 · countries — interruptor de PAÍS. Nace apagado (fail-closed).
CREATE TABLE IF NOT EXISTS countries (
  iso                 CHAR(2)      NOT NULL PRIMARY KEY,
  country_name        VARCHAR(64)  NOT NULL,
  enabled             TINYINT      NOT NULL DEFAULT 0,
  dial_prefix         VARCHAR(8)   NOT NULL,
  national_number_len TINYINT      NULL,
  timezone            VARCHAR(64)  NOT NULL,
  language            VARCHAR(16)  NOT NULL DEFAULT 'en',
  archived_at         DATETIME     NULL,
  notes               VARCHAR(255) NULL,
  created_at          DATETIME     NOT NULL DEFAULT CURRENT_TIMESTAMP,
  updated_at          DATETIME     NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

-- upgrade V2.1: el país no tenía interruptor → las filas existentes estaban
-- "encendidas" de hecho. Se agrega con DEFAULT 1 para NO cambiar comportamiento,
-- y luego el default pasa a 0 para las altas nuevas.
SET @c := (SELECT COUNT(*) FROM information_schema.COLUMNS
           WHERE TABLE_SCHEMA=DATABASE() AND TABLE_NAME='countries' AND COLUMN_NAME='enabled');
SET @ddl := IF(@c=0, 'ALTER TABLE countries ADD COLUMN enabled TINYINT NOT NULL DEFAULT 1 AFTER country_name', 'DO 0');
PREPARE s FROM @ddl; EXECUTE s; DEALLOCATE PREPARE s;
SET @c := (SELECT COUNT(*) FROM information_schema.COLUMNS WHERE TABLE_SCHEMA=DATABASE()
           AND TABLE_NAME='countries' AND COLUMN_NAME='enabled' AND COLUMN_DEFAULT <> '0');
SET @ddl := IF(@c=1, 'ALTER TABLE countries ALTER COLUMN enabled SET DEFAULT 0', 'DO 0');
PREPARE s FROM @ddl; EXECUTE s; DEALLOCATE PREPARE s;


-- A2 · voice_providers — proveedor COMERCIAL. adapter_key = implementación técnica.
--      VARCHAR, no ENUM: el catálogo de adapters soportados vive en la aplicación
--      (routes_config.ADAPTERS). Un adapter desconocido no puede activarse.
--      Interruptor de PROVEEDOR: enabled=0 apaga todas sus rutas en todos los países.
CREATE TABLE IF NOT EXISTS voice_providers (
  id           INT AUTO_INCREMENT PRIMARY KEY,
  code         VARCHAR(32)  NOT NULL,
  display_name VARCHAR(64)  NOT NULL,
  adapter_key  VARCHAR(64)  NOT NULL,
  endpoint     VARCHAR(255) NULL,
  account_ref  VARCHAR(64)  NULL,
  enabled      TINYINT      NOT NULL DEFAULT 0,
  notes        VARCHAR(255) NULL,
  created_at   DATETIME     NOT NULL DEFAULT CURRENT_TIMESTAMP,
  updated_at   DATETIME     NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
  UNIQUE KEY uq_provider_code (code),
  KEY idx_adapter (adapter_key)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

-- upgrade V2.1: provider_kind ENUM → adapter_key VARCHAR
SET @c := (SELECT COUNT(*) FROM information_schema.COLUMNS
           WHERE TABLE_SCHEMA=DATABASE() AND TABLE_NAME='voice_providers' AND COLUMN_NAME='adapter_key');
SET @ddl := IF(@c=0, 'ALTER TABLE voice_providers ADD COLUMN adapter_key VARCHAR(64) NULL AFTER display_name', 'DO 0');
PREPARE s FROM @ddl; EXECUTE s; DEALLOCATE PREPARE s;

SET @c := (SELECT COUNT(*) FROM information_schema.COLUMNS
           WHERE TABLE_SCHEMA=DATABASE() AND TABLE_NAME='voice_providers' AND COLUMN_NAME='provider_kind');
SET @ddl := IF(@c=1,
  'UPDATE voice_providers SET adapter_key = CASE provider_kind
       WHEN ''sip'' THEN ''ELEVENLABS_SIP'' WHEN ''stringee'' THEN ''STRINGEE_WORKER'' END
    WHERE adapter_key IS NULL OR adapter_key = ''''',
  'DO 0');
PREPARE s FROM @ddl; EXECUTE s; DEALLOCATE PREPARE s;

SET @ddl := IF(@c=1, 'ALTER TABLE voice_providers MODIFY provider_kind VARCHAR(16) NULL', 'DO 0');
PREPARE s FROM @ddl; EXECUTE s; DEALLOCATE PREPARE s;

SET @c := (SELECT COUNT(*) FROM information_schema.COLUMNS WHERE TABLE_SCHEMA=DATABASE()
           AND TABLE_NAME='voice_providers' AND COLUMN_NAME='adapter_key' AND IS_NULLABLE='YES');
SET @n := (SELECT COUNT(*) FROM voice_providers WHERE adapter_key IS NULL OR adapter_key='');
SET @ddl := IF(@c=1 AND @n=0, 'ALTER TABLE voice_providers MODIFY adapter_key VARCHAR(64) NOT NULL', 'DO 0');
PREPARE s FROM @ddl; EXECUTE s; DEALLOCATE PREPARE s;

SET @c := (SELECT COUNT(*) FROM information_schema.STATISTICS
           WHERE TABLE_SCHEMA=DATABASE() AND TABLE_NAME='voice_providers' AND INDEX_NAME='idx_adapter');
SET @ddl := IF(@c=0, 'ALTER TABLE voice_providers ADD KEY idx_adapter (adapter_key)', 'DO 0');
PREPARE s FROM @ddl; EXECUTE s; DEALLOCATE PREPARE s;


-- A3 · followup_policies — sin cambios respecto de V2.1
CREATE TABLE IF NOT EXISTS followup_policies (
  id          INT AUTO_INCREMENT PRIMARY KEY,
  policy_key  VARCHAR(64)  NOT NULL,
  name        VARCHAR(128) NOT NULL,
  description VARCHAR(255) NULL,
  policy_json TEXT         NOT NULL,
  archived_at DATETIME     NULL,
  created_at  DATETIME     NOT NULL DEFAULT CURRENT_TIMESTAMP,
  updated_at  DATETIME     NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
  UNIQUE KEY uq_policy_key (policy_key)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;


-- A4 · call_routes — país × proveedor. Varias rutas por país Y varias por
--      proveedor (PROVEEDOR1 → IN, NP, MX, CO, VE…). Sin UNIQUE(iso, provider).
--      Interruptor de RUTA: enabled. Sin hard delete: archived_at.
CREATE TABLE IF NOT EXISTS call_routes (
  id                         INT AUTO_INCREMENT PRIMARY KEY,
  route_key                  VARCHAR(64)  NOT NULL,
  iso                        CHAR(2)      NOT NULL,
  provider_id                INT          NOT NULL,
  enabled                    TINYINT      NOT NULL DEFAULT 0,
  archived_at                DATETIME     NULL,
  priority                   INT          NOT NULL DEFAULT 100,
  caller_id                  VARCHAR(32)  NULL,
  elevenlabs_agent_id        VARCHAR(64)  NULL,
  elevenlabs_phone_number_id VARCHAR(64)  NULL,
  capacity_default           INT          NOT NULL DEFAULT 1,
  followup_policy_id         INT          NULL,
  recording_enabled          TINYINT      NOT NULL DEFAULT 1,
  recording_min_secs         INT          NOT NULL DEFAULT 60,
  recording_upload_crm       TINYINT      NOT NULL DEFAULT 1,
  recording_telegram         TINYINT      NOT NULL DEFAULT 1,
  notes                      VARCHAR(255) NULL,
  created_at                 DATETIME     NOT NULL DEFAULT CURRENT_TIMESTAMP,
  updated_at                 DATETIME     NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
  UNIQUE KEY uq_route_key (route_key),
  KEY idx_iso (iso),
  KEY idx_provider (provider_id),
  KEY idx_enabled (enabled),
  KEY idx_archived (archived_at)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

SET @c := (SELECT COUNT(*) FROM information_schema.STATISTICS
           WHERE TABLE_SCHEMA=DATABASE() AND TABLE_NAME='call_routes' AND INDEX_NAME='idx_provider');
SET @ddl := IF(@c=0, 'ALTER TABLE call_routes ADD KEY idx_provider (provider_id)', 'DO 0');
PREPARE s FROM @ddl; EXECUTE s; DEALLOCATE PREPARE s;


-- A5 · franjas, Telegram, auditoría — sin cambios respecto de V2.1
CREATE TABLE IF NOT EXISTS route_capacity_windows (
  id          INT AUTO_INCREMENT PRIMARY KEY,
  route_id    INT         NOT NULL,
  day_mask    VARCHAR(27) NOT NULL DEFAULT 'mon,tue,wed,thu,fri',
  start_local TIME        NOT NULL,
  end_local   TIME        NOT NULL,
  capacity    INT         NOT NULL,
  created_at  DATETIME    NOT NULL DEFAULT CURRENT_TIMESTAMP,
  updated_at  DATETIME    NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
  KEY idx_route (route_id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

CREATE TABLE IF NOT EXISTS route_telegram_targets (
  id         INT AUTO_INCREMENT PRIMARY KEY,
  route_id   INT NOT NULL,
  purpose    ENUM('recording','account','payment','alert') NOT NULL,
  chat_id    VARCHAR(32) NOT NULL,
  enabled    TINYINT NOT NULL DEFAULT 1,
  created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
  UNIQUE KEY uq_route_purpose_chat (route_id, purpose, chat_id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

CREATE TABLE IF NOT EXISTS route_audit (
  id         INT AUTO_INCREMENT PRIMARY KEY,
  route_id   INT          NULL,
  route_key  VARCHAR(64)  NULL,
  action     VARCHAR(32)  NOT NULL,
  field      VARCHAR(64)  NULL,
  old_value  VARCHAR(255) NULL,
  new_value  VARCHAR(255) NULL,
  actor      VARCHAR(64)  NOT NULL,
  changed_at DATETIME     NOT NULL DEFAULT CURRENT_TIMESTAMP,
  KEY idx_route (route_id),
  KEY idx_when (changed_at)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;


-- A6 · country_tool_configs — las tools pertenecen al PAÍS, no al proveedor de voz
CREATE TABLE IF NOT EXISTS country_tool_configs (
  id             INT AUTO_INCREMENT PRIMARY KEY,
  country_iso    CHAR(2)      NOT NULL,
  tool_type      ENUM('CREATE_ACCOUNT','CREATE_PAYMENT_LINK','CALLBACK') NOT NULL,
  enabled        TINYINT      NOT NULL DEFAULT 0,
  mode           ENUM('CONFIG_ROUTER','CUSTOM_ENDPOINT') NOT NULL DEFAULT 'CONFIG_ROUTER',
  provider_key   VARCHAR(64)  NULL,
  endpoint       VARCHAR(255) NULL,
  http_method    VARCHAR(8)   NULL,
  credential_ref VARCHAR(64)  NULL,
  market         VARCHAR(16)  NULL,
  currency       CHAR(3)      NULL,
  config_json    TEXT         NULL,
  notes          VARCHAR(255) NULL,
  created_at     DATETIME     NOT NULL DEFAULT CURRENT_TIMESTAMP,
  updated_at     DATETIME     NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
  UNIQUE KEY uq_country_tool (country_iso, tool_type)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;


-- ═══════════════════════════════════════════════════════════════════════════════
--  B · EJECUCIÓN Y ANALYTICS LOCALES
-- ═══════════════════════════════════════════════════════════════════════════════

-- B1 · wf_call_jobs — UNA fila por llamada. Es a la vez:
--        · el claim atómico del despacho
--        · el HECHO de analytics de llamadas (resultado y duración viven acá,
--          una sola vez: un evento duplicado no puede duplicar minutos)
--
--      attempt            = intento de NEGOCIO (el que ve la política de follow-up)
--      tech_retry_count   = reintentos TÉCNICOS del mismo intento de negocio
--
--      Estados:
--        CLAIMED → DISPATCHING → DISPATCHED → COMPLETED
--                              ↘ COMPLETED (resultado inmediato, p. ej. SIP 603)
--                              ↘ UNKNOWN → NEEDS_RECONCILIATION   (jamás re-dispatch)
--        CLAIMED | DISPATCHING → RELEASED  (error TÉCNICO sin llamada emitida:
--                                           401/403, config, endpoint caído antes
--                                           de enviar. NO consume el intento de
--                                           negocio; reintento con backoff y tope)
--        RELEASED agotado → NEEDS_RECONCILIATION
--        FAILED              (terminal, sin reintento automático)
--
--      inflight_lead: columna generada = lead_id mientras la llamada está "en
--      vuelo" (o pendiente de reconciliar). UNIQUE ⇒ un lead NUNCA tiene dos
--      llamadas en vuelo, en ningún intento, desde ninguna ruta.
CREATE TABLE IF NOT EXISTS wf_call_jobs (
  id                   BIGINT AUTO_INCREMENT PRIMARY KEY,
  call_job_id          VARCHAR(96)  NOT NULL,
  lead_id              VARCHAR(64)  NOT NULL,
  route_id             INT          NOT NULL,
  route_key            VARCHAR(64)  NOT NULL,
  country_iso          CHAR(2)      NOT NULL,
  provider             VARCHAR(32)  NOT NULL,
  adapter_key          VARCHAR(64)  NULL,
  attempt              INT          NOT NULL,
  execution_id         VARCHAR(64)  NULL,
  state                ENUM('CLAIMED','DISPATCHING','DISPATCHED','UNKNOWN','RELEASED',
                            'FAILED','COMPLETED','NEEDS_RECONCILIATION') NOT NULL DEFAULT 'CLAIMED',
  tech_retry_count     INT          NOT NULL DEFAULT 0,
  next_tech_retry_at   DATETIME     NULL,
  error_class          VARCHAR(16)  NULL,
  conversation_id      VARCHAR(128) NULL,
  provider_job_id      VARCHAR(128) NULL,
  provider_call_id     VARCHAR(128) NULL,
  dispatch_http_status INT          NULL,
  sip_code             VARCHAR(8)   NULL,
  result               VARCHAR(24)  NULL,
  duration_seconds     INT          NULL,
  callback_at          DATETIME     NULL,
  followup_id          VARCHAR(64)  NULL,
  error_code           VARCHAR(32)  NULL,
  error_message        VARCHAR(255) NULL,
  created_at           DATETIME     NOT NULL DEFAULT CURRENT_TIMESTAMP,
  claimed_at           DATETIME     NOT NULL DEFAULT CURRENT_TIMESTAMP,
  dispatched_at        DATETIME     NULL,
  completed_at         DATETIME     NULL,
  updated_at           DATETIME     NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
  inflight_lead        VARCHAR(64) AS (CASE WHEN state IN
                         ('CLAIMED','DISPATCHING','DISPATCHED','UNKNOWN','NEEDS_RECONCILIATION')
                         THEN lead_id ELSE NULL END) STORED,
  UNIQUE KEY uq_call_job_id (call_job_id),
  UNIQUE KEY uq_lead_attempt (lead_id, attempt),
  UNIQUE KEY uq_inflight_lead (inflight_lead),
  KEY idx_state (state),
  -- índice CUBRIENTE de las métricas del dashboard: las consultas por rango de
  -- fechas leen solo el índice (medido: 30 días sobre 5,2 M filas 5,6 s → 0,5 s)
  KEY idx_metrics (created_at, country_iso, route_key, provider, state, result,
                   duration_seconds, dispatched_at),
  KEY idx_country_created (country_iso, created_at),
  KEY idx_route_created (route_key, created_at),
  KEY idx_provider_created (provider, created_at),
  KEY idx_completed (completed_at),
  KEY idx_conversation (conversation_id),
  KEY idx_provider_job (provider_job_id),
  KEY idx_lead (lead_id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

-- ── upgrade V2.1 de wf_call_jobs ──────────────────────────────────────────────
SET @c := (SELECT COUNT(*) FROM information_schema.COLUMNS WHERE TABLE_SCHEMA=DATABASE()
           AND TABLE_NAME='wf_call_jobs' AND COLUMN_NAME='state' AND COLUMN_TYPE NOT LIKE '%RELEASED%');
SET @ddl := IF(@c=1, 'ALTER TABLE wf_call_jobs MODIFY state ENUM(''CLAIMED'',''DISPATCHING'',''DISPATCHED'',''UNKNOWN'',''RELEASED'',''FAILED'',''COMPLETED'',''NEEDS_RECONCILIATION'') NOT NULL DEFAULT ''CLAIMED''', 'DO 0');
PREPARE s FROM @ddl; EXECUTE s; DEALLOCATE PREPARE s;

-- columnas nuevas (una sentencia guardada por columna)
SET @t := 'wf_call_jobs';
SET @c := (SELECT COUNT(*) FROM information_schema.COLUMNS WHERE TABLE_SCHEMA=DATABASE() AND TABLE_NAME=@t AND COLUMN_NAME='adapter_key');
SET @ddl := IF(@c=0, 'ALTER TABLE wf_call_jobs ADD COLUMN adapter_key VARCHAR(64) NULL AFTER provider', 'DO 0');
PREPARE s FROM @ddl; EXECUTE s; DEALLOCATE PREPARE s;
SET @c := (SELECT COUNT(*) FROM information_schema.COLUMNS WHERE TABLE_SCHEMA=DATABASE() AND TABLE_NAME=@t AND COLUMN_NAME='tech_retry_count');
SET @ddl := IF(@c=0, 'ALTER TABLE wf_call_jobs ADD COLUMN tech_retry_count INT NOT NULL DEFAULT 0 AFTER state', 'DO 0');
PREPARE s FROM @ddl; EXECUTE s; DEALLOCATE PREPARE s;
SET @c := (SELECT COUNT(*) FROM information_schema.COLUMNS WHERE TABLE_SCHEMA=DATABASE() AND TABLE_NAME=@t AND COLUMN_NAME='next_tech_retry_at');
SET @ddl := IF(@c=0, 'ALTER TABLE wf_call_jobs ADD COLUMN next_tech_retry_at DATETIME NULL AFTER tech_retry_count', 'DO 0');
PREPARE s FROM @ddl; EXECUTE s; DEALLOCATE PREPARE s;
SET @c := (SELECT COUNT(*) FROM information_schema.COLUMNS WHERE TABLE_SCHEMA=DATABASE() AND TABLE_NAME=@t AND COLUMN_NAME='error_class');
SET @ddl := IF(@c=0, 'ALTER TABLE wf_call_jobs ADD COLUMN error_class VARCHAR(16) NULL AFTER next_tech_retry_at', 'DO 0');
PREPARE s FROM @ddl; EXECUTE s; DEALLOCATE PREPARE s;
SET @c := (SELECT COUNT(*) FROM information_schema.COLUMNS WHERE TABLE_SCHEMA=DATABASE() AND TABLE_NAME=@t AND COLUMN_NAME='sip_code');
SET @ddl := IF(@c=0, 'ALTER TABLE wf_call_jobs ADD COLUMN sip_code VARCHAR(8) NULL AFTER dispatch_http_status', 'DO 0');
PREPARE s FROM @ddl; EXECUTE s; DEALLOCATE PREPARE s;
SET @c := (SELECT COUNT(*) FROM information_schema.COLUMNS WHERE TABLE_SCHEMA=DATABASE() AND TABLE_NAME=@t AND COLUMN_NAME='result');
SET @ddl := IF(@c=0, 'ALTER TABLE wf_call_jobs ADD COLUMN result VARCHAR(24) NULL AFTER sip_code', 'DO 0');
PREPARE s FROM @ddl; EXECUTE s; DEALLOCATE PREPARE s;
SET @c := (SELECT COUNT(*) FROM information_schema.COLUMNS WHERE TABLE_SCHEMA=DATABASE() AND TABLE_NAME=@t AND COLUMN_NAME='duration_seconds');
SET @ddl := IF(@c=0, 'ALTER TABLE wf_call_jobs ADD COLUMN duration_seconds INT NULL AFTER result', 'DO 0');
PREPARE s FROM @ddl; EXECUTE s; DEALLOCATE PREPARE s;
SET @c := (SELECT COUNT(*) FROM information_schema.COLUMNS WHERE TABLE_SCHEMA=DATABASE() AND TABLE_NAME=@t AND COLUMN_NAME='callback_at');
SET @ddl := IF(@c=0, 'ALTER TABLE wf_call_jobs ADD COLUMN callback_at DATETIME NULL AFTER duration_seconds', 'DO 0');
PREPARE s FROM @ddl; EXECUTE s; DEALLOCATE PREPARE s;
SET @c := (SELECT COUNT(*) FROM information_schema.COLUMNS WHERE TABLE_SCHEMA=DATABASE() AND TABLE_NAME=@t AND COLUMN_NAME='followup_id');
SET @ddl := IF(@c=0, 'ALTER TABLE wf_call_jobs ADD COLUMN followup_id VARCHAR(64) NULL AFTER callback_at', 'DO 0');
PREPARE s FROM @ddl; EXECUTE s; DEALLOCATE PREPARE s;

-- V2.1 marcaba el fallo previo al despacho como FAILED + error_code PRE_DISPATCH.
-- En V2.2 ese caso es RELEASED (no consume el intento de negocio).
UPDATE wf_call_jobs SET state='RELEASED', error_class='TECHNICAL'
 WHERE state='FAILED' AND error_code='PRE_DISPATCH' AND dispatched_at IS NULL;

-- un lead con dos llamadas en vuelo haría fallar el UNIQUE: se aborta con un
-- mensaje explícito en vez de "arreglar" datos que pueden representar llamadas reales
SET @c := (SELECT COUNT(*) FROM information_schema.COLUMNS WHERE TABLE_SCHEMA=DATABASE()
           AND TABLE_NAME='wf_call_jobs' AND COLUMN_NAME='inflight_lead');
SET @dups := IF(@c=0, (SELECT COUNT(*) FROM (SELECT lead_id FROM wf_call_jobs
           WHERE state IN ('CLAIMED','DISPATCHING','DISPATCHED','UNKNOWN','NEEDS_RECONCILIATION')
           GROUP BY lead_id HAVING COUNT(*) > 1) d), 0);
SET @ddl := IF(@dups > 0,
  'SELECT * FROM `ABORT_lead_has_2_inflight_calls_fix_wf_call_jobs`', 'DO 0');
PREPARE s FROM @ddl; EXECUTE s; DEALLOCATE PREPARE s;

SET @ddl := IF(@c=0, 'ALTER TABLE wf_call_jobs ADD COLUMN inflight_lead VARCHAR(64) AS (CASE WHEN state IN (''CLAIMED'',''DISPATCHING'',''DISPATCHED'',''UNKNOWN'',''NEEDS_RECONCILIATION'') THEN lead_id ELSE NULL END) STORED', 'DO 0');
PREPARE s FROM @ddl; EXECUTE s; DEALLOCATE PREPARE s;

-- índices nuevos
SET @c := (SELECT COUNT(*) FROM information_schema.STATISTICS WHERE TABLE_SCHEMA=DATABASE() AND TABLE_NAME=@t AND INDEX_NAME='uq_inflight_lead');
SET @ddl := IF(@c=0, 'ALTER TABLE wf_call_jobs ADD UNIQUE KEY uq_inflight_lead (inflight_lead)', 'DO 0');
PREPARE s FROM @ddl; EXECUTE s; DEALLOCATE PREPARE s;
SET @c := (SELECT COUNT(*) FROM information_schema.STATISTICS WHERE TABLE_SCHEMA=DATABASE() AND TABLE_NAME=@t AND INDEX_NAME='idx_metrics');
SET @ddl := IF(@c=0, 'ALTER TABLE wf_call_jobs ADD KEY idx_metrics (created_at, country_iso, route_key, provider, state, result, duration_seconds, dispatched_at)', 'DO 0');
PREPARE s FROM @ddl; EXECUTE s; DEALLOCATE PREPARE s;
SET @c := (SELECT COUNT(*) FROM information_schema.STATISTICS WHERE TABLE_SCHEMA=DATABASE() AND TABLE_NAME=@t AND INDEX_NAME='idx_country_created');
SET @ddl := IF(@c=0, 'ALTER TABLE wf_call_jobs ADD KEY idx_country_created (country_iso, created_at)', 'DO 0');
PREPARE s FROM @ddl; EXECUTE s; DEALLOCATE PREPARE s;
SET @c := (SELECT COUNT(*) FROM information_schema.STATISTICS WHERE TABLE_SCHEMA=DATABASE() AND TABLE_NAME=@t AND INDEX_NAME='idx_route_created');
SET @ddl := IF(@c=0, 'ALTER TABLE wf_call_jobs ADD KEY idx_route_created (route_key, created_at)', 'DO 0');
PREPARE s FROM @ddl; EXECUTE s; DEALLOCATE PREPARE s;
SET @c := (SELECT COUNT(*) FROM information_schema.STATISTICS WHERE TABLE_SCHEMA=DATABASE() AND TABLE_NAME=@t AND INDEX_NAME='idx_provider_created');
SET @ddl := IF(@c=0, 'ALTER TABLE wf_call_jobs ADD KEY idx_provider_created (provider, created_at)', 'DO 0');
PREPARE s FROM @ddl; EXECUTE s; DEALLOCATE PREPARE s;
SET @c := (SELECT COUNT(*) FROM information_schema.STATISTICS WHERE TABLE_SCHEMA=DATABASE() AND TABLE_NAME=@t AND INDEX_NAME='idx_completed');
SET @ddl := IF(@c=0, 'ALTER TABLE wf_call_jobs ADD KEY idx_completed (completed_at)', 'DO 0');
PREPARE s FROM @ddl; EXECUTE s; DEALLOCATE PREPARE s;
SET @c := (SELECT COUNT(*) FROM information_schema.STATISTICS WHERE TABLE_SCHEMA=DATABASE() AND TABLE_NAME=@t AND INDEX_NAME='idx_lead');
SET @ddl := IF(@c=0, 'ALTER TABLE wf_call_jobs ADD KEY idx_lead (lead_id)', 'DO 0');
PREPARE s FROM @ddl; EXECUTE s; DEALLOCATE PREPARE s;


-- B2 · wf_conversation_ledger — idempotencia post-call (sin cambios desde V2.1)
CREATE TABLE IF NOT EXISTS wf_conversation_ledger (
  conversation_id VARCHAR(128) NOT NULL PRIMARY KEY,
  call_job_id     VARCHAR(96)  NULL,
  route_key       VARCHAR(64)  NULL,
  lead_id         VARCHAR(64)  NULL,
  provider        VARCHAR(32)  NULL,
  attempt         INT          NULL,
  source          ENUM('webhook','polling','dispatch') NOT NULL,
  claim_token     VARCHAR(64)  NULL,
  state           ENUM('NEW','CLAIMED','PROCESSED','NEEDS_RECONCILIATION','FAILED')
                  NOT NULL DEFAULT 'CLAIMED',
  result          VARCHAR(32)  NULL,
  followup_id     VARCHAR(64)  NULL,
  error_code      VARCHAR(32)  NULL,
  error_detail    VARCHAR(255) NULL,
  execution_id    VARCHAR(64)  NULL,
  claimed_at      DATETIME     NOT NULL DEFAULT CURRENT_TIMESTAMP,
  completed_at    DATETIME     NULL,
  updated_at      DATETIME     NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
  KEY idx_state (state),
  KEY idx_lead (lead_id),
  KEY idx_job (call_job_id),
  KEY idx_followup (followup_id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;


-- B3 · wf_events — EVENT STORE local, append-only, idempotente.
--
--      event_key UNIQUE, DETERMINISTA a partir de la identidad del hecho:
--        CALL_RESULT:{call_job_id}
--        ACCOUNT_CREATED:{account_provider}:{lead_id}
--        PAYMENT_LINK_CREATED:{payment_provider}:{order_ref}
--      El mismo hecho registrado dos veces (webhook + polling, reintento de
--      n8n) choca con el UNIQUE y no se cuenta dos veces.
--
--      Las métricas de LLAMADAS (intentos, respuesta, duración) se calculan de
--      wf_call_jobs (una fila por llamada). wf_events aporta trazabilidad y las
--      métricas de NEGOCIO (cuentas, pagos, tools, follow-ups, grabaciones).
CREATE TABLE IF NOT EXISTS wf_events (
  id               BIGINT AUTO_INCREMENT PRIMARY KEY,
  event_key        VARCHAR(191) NOT NULL,
  event_type       VARCHAR(48)  NOT NULL,
  event_domain     VARCHAR(16)  NOT NULL,
  occurred_at      DATETIME     NOT NULL,
  call_job_id      VARCHAR(96)  NULL,
  lead_id          VARCHAR(64)  NULL,
  country_iso      CHAR(2)      NULL,
  route_key        VARCHAR(64)  NULL,
  provider         VARCHAR(32)  NULL,
  adapter_key      VARCHAR(64)  NULL,
  attempt          INT          NULL,
  conversation_id  VARCHAR(128) NULL,
  provider_job_id  VARCHAR(128) NULL,
  followup_id      VARCHAR(64)  NULL,
  result           VARCHAR(32)  NULL,
  duration_seconds INT          NULL,
  amount           DECIMAL(14,2) NULL,
  currency         CHAR(3)      NULL,
  source_workflow  VARCHAR(48)  NULL,
  execution_id     VARCHAR(64)  NULL,
  metadata_json    TEXT         NULL,
  created_at       DATETIME     NOT NULL DEFAULT CURRENT_TIMESTAMP,
  UNIQUE KEY uq_event_key (event_key),
  KEY idx_occurred (occurred_at),
  KEY idx_type_occurred (event_type, occurred_at),
  -- cubriente para business_metrics (medido: 30 días, 1,35 M eventos 2,3 s → 0,5 s)
  KEY idx_ev_metrics (event_type, occurred_at, country_iso, route_key, provider, result),
  KEY idx_country_occurred (country_iso, occurred_at),
  KEY idx_route_occurred (route_key, occurred_at),
  KEY idx_provider_occurred (provider, occurred_at),
  KEY idx_lead (lead_id),
  KEY idx_job (call_job_id),
  KEY idx_conversation (conversation_id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;


-- B4 · wf_reconciliation_issues — diferencias entre lo local y el CRM/proveedor.
--      Tabla aparte (y no eventos) porque un issue tiene ciclo de vida:
--      OPEN → RESOLVED / IGNORED. issue_key UNIQUE: el mismo desajuste detectado
--      en cada corrida de WF14 incrementa occurrences, no crea filas nuevas.
CREATE TABLE IF NOT EXISTS wf_reconciliation_issues (
  id             BIGINT AUTO_INCREMENT PRIMARY KEY,
  issue_key      VARCHAR(191) NOT NULL,
  issue_type     VARCHAR(48)  NOT NULL,
  entity_type    VARCHAR(24)  NOT NULL,
  entity_id      VARCHAR(128) NOT NULL,
  lead_id        VARCHAR(64)  NULL,
  country_iso    CHAR(2)      NULL,
  severity       VARCHAR(8)   NOT NULL DEFAULT 'WARN',
  state          ENUM('OPEN','RESOLVED','IGNORED') NOT NULL DEFAULT 'OPEN',
  detail_json    TEXT         NULL,
  occurrences    INT          NOT NULL DEFAULT 1,
  first_seen_at  DATETIME     NOT NULL DEFAULT CURRENT_TIMESTAMP,
  last_seen_at   DATETIME     NOT NULL DEFAULT CURRENT_TIMESTAMP,
  resolved_at    DATETIME     NULL,
  resolved_by    VARCHAR(64)  NULL,
  UNIQUE KEY uq_issue_key (issue_key),
  KEY idx_state (state, last_seen_at),
  KEY idx_type (issue_type),
  KEY idx_lead (lead_id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;


-- ═══════════════════════════════════════════════════════════════════════════════
--  SEED — una sola vez en la vida de la base.
--  Guardas: sin marcador V2.2, sin marcador V2.1, y clave natural ausente.
--  Valores = lo que HOY está hardcodeado en los JSON de n8n.
-- ═══════════════════════════════════════════════════════════════════════════════

SET @do_seed := (SELECT COUNT(*) = 0 FROM schema_migrations WHERE migration_id IN (@SEED, @SEED_V21));

-- S1 · países (ambos encendidos: la ruta de Nepal es la que está apagada, como en WF2)
INSERT INTO countries (iso, country_name, enabled, dial_prefix, national_number_len, timezone, language, notes)
SELECT 'IN','India',1,'+91',10,'Asia/Kolkata','hi','Seed desde WF2 COUNTRY_CFG.india'
FROM DUAL WHERE @do_seed AND NOT EXISTS (SELECT 1 FROM countries WHERE iso='IN');

INSERT INTO countries (iso, country_name, enabled, dial_prefix, national_number_len, timezone, language, notes)
SELECT 'NP','Nepal',1,'+977',NULL,'Asia/Kathmandu','hi','Seed desde WF2 COUNTRY_CFG.nepal'
FROM DUAL WHERE @do_seed AND NOT EXISTS (SELECT 1 FROM countries WHERE iso='NP');

-- S2 · proveedores: UN proveedor SIP con rutas en varios países; adapter separado
INSERT INTO voice_providers (code, display_name, adapter_key, endpoint, account_ref, enabled, notes)
SELECT 'proveedor1','PROVEEDOR1','ELEVENLABS_SIP',NULL,'650098',1,
       'Proveedor SIP. Una ruta por pais (IN, NP, MX, CO, VE...). Sale por Asterisk.'
FROM DUAL WHERE @do_seed AND NOT EXISTS (SELECT 1 FROM voice_providers WHERE code='proveedor1');

INSERT INTO voice_providers (code, display_name, adapter_key, endpoint, account_ref, enabled, notes)
SELECT 'stringee','STRINGEE','STRINGEE_WORKER','http://172.18.0.1:8091',NULL,1,
       'Worker propio. endpoint desde WF2 POST Stringee Worker.'
FROM DUAL WHERE @do_seed AND NOT EXISTS (SELECT 1 FROM voice_providers WHERE code='stringee');

-- S3 · política STANDARD_CALL_RETRY (WF2 v1, regla por regla, sin módulo)
INSERT INTO followup_policies (policy_key, name, description, policy_json)
SELECT 'STANDARD_CALL_RETRY', 'Standard call retry',
       'Ciclo de WF2 v1: 2h/3h/2bd, 2h/3h/3bd, 2h/3h, CLOSE al 9. Compartida por todos los proveedores.',
       '{"policy_version":1,"no_answer_sip_codes":["603","408","486"],"result_aliases":{"BUSY":"NO_ANSWER","VOICEMAIL":"NO_ANSWER"},"rules":[{"result":"NO_ANSWER","attempt":1,"action":"RETRY","delay":"+2h"},{"result":"NO_ANSWER","attempt":2,"action":"RETRY","delay":"+3h"},{"result":"NO_ANSWER","attempt":3,"action":"RETRY","delay":"+2bd"},{"result":"NO_ANSWER","attempt":4,"action":"RETRY","delay":"+2h"},{"result":"NO_ANSWER","attempt":5,"action":"RETRY","delay":"+3h"},{"result":"NO_ANSWER","attempt":6,"action":"RETRY","delay":"+3bd"},{"result":"NO_ANSWER","attempt":7,"action":"RETRY","delay":"+2h"},{"result":"NO_ANSWER","attempt":8,"action":"RETRY","delay":"+3h"},{"result":"NO_ANSWER","attempt":9,"action":"CLOSE","delay":null},{"result":"NO_ANSWER","attempt":"*","action":"CLOSE","delay":null},{"result":"ANSWERED","attempt":"*","action":"COMPLETE","delay":null},{"result":"CALLBACK","attempt":"*","action":"CALLBACK","delay":null},{"result":"WRONG_NUMBER","attempt":"*","action":"CLOSE","delay":null},{"result":"DNC","attempt":"*","action":"CLOSE","delay":null}],"callback_default":"+24h","unmatched_action":"NONE"}'
FROM DUAL WHERE @do_seed AND NOT EXISTS (SELECT 1 FROM followup_policies WHERE policy_key='STANDARD_CALL_RETRY');

-- S4 · rutas
INSERT INTO call_routes (route_key, iso, provider_id, enabled, priority, caller_id,
       elevenlabs_agent_id, elevenlabs_phone_number_id, capacity_default, followup_policy_id, notes)
SELECT 'IN_PROVEEDOR1','IN',p.id,1,10,NULL,
       'agent_5701kramx550e3qs2tm11661b48p','phnum_7801kyktmabxembteqce884tanb6',6,
       (SELECT id FROM followup_policies WHERE policy_key='STANDARD_CALL_RETRY'),
       'capacity 6 = BATCH_SIZE de WF2 v1'
FROM voice_providers p
WHERE p.code='proveedor1' AND @do_seed AND NOT EXISTS (SELECT 1 FROM call_routes WHERE route_key='IN_PROVEEDOR1');

INSERT INTO call_routes (route_key, iso, provider_id, enabled, priority, caller_id,
       elevenlabs_agent_id, elevenlabs_phone_number_id, capacity_default, followup_policy_id, notes)
SELECT 'IN_STRINGEE','IN',p.id,1,20,'917971730907',
       'agent_5701kramx550e3qs2tm11661b48p',NULL,1,
       (SELECT id FROM followup_policies WHERE policy_key='STANDARD_CALL_RETRY'),
       'capacity 1 = capacidad comprada hoy. Se sube desde el panel.'
FROM voice_providers p
WHERE p.code='stringee' AND @do_seed AND NOT EXISTS (SELECT 1 FROM call_routes WHERE route_key='IN_STRINGEE');

INSERT INTO call_routes (route_key, iso, provider_id, enabled, priority, caller_id,
       elevenlabs_agent_id, elevenlabs_phone_number_id, capacity_default, followup_policy_id, notes)
SELECT 'NP_PROVEEDOR1','NP',p.id,0,30,NULL,
       'agent_7601m209ntj3fyrbg0dcq6w507yq','phnum_7801kyktmabxembteqce884tanb6',6,
       (SELECT id FROM followup_policies WHERE policy_key='STANDARD_CALL_RETRY'),
       'Deshabilitada: nodos Nepal desactivados en WF2 v1.'
FROM voice_providers p
WHERE p.code='proveedor1' AND @do_seed AND NOT EXISTS (SELECT 1 FROM call_routes WHERE route_key='NP_PROVEEDOR1');

-- S5 · Telegram (WF10 ×4 nodos, WF3 ×2)
INSERT INTO route_telegram_targets (route_id, purpose, chat_id)
SELECT r.id, t.purpose, t.chat_id
FROM call_routes r
JOIN (SELECT 'IN_PROVEEDOR1' rk,'recording' purpose,'-1004454561082' chat_id
      UNION ALL SELECT 'IN_PROVEEDOR1','recording','-1003984044945'
      UNION ALL SELECT 'IN_PROVEEDOR1','account','-1004454561082'
      UNION ALL SELECT 'IN_PROVEEDOR1','account','-1003984044945'
      UNION ALL SELECT 'IN_STRINGEE','recording','-1004454561082'
      UNION ALL SELECT 'IN_STRINGEE','recording','-1003984044945'
      UNION ALL SELECT 'NP_PROVEEDOR1','recording','-1004454561082'
      UNION ALL SELECT 'NP_PROVEEDOR1','recording','-1003984044945') t ON t.rk = r.route_key
WHERE @do_seed
  AND NOT EXISTS (SELECT 1 FROM route_telegram_targets x
                  WHERE x.route_id=r.id AND x.purpose=t.purpose AND x.chat_id=t.chat_id);

-- S6 · tools del PAÍS (IN_PROVEEDOR1 e IN_STRINGEE usan las mismas)
INSERT INTO country_tool_configs (country_iso, tool_type, enabled, mode, provider_key,
       credential_ref, market, currency, config_json, notes)
SELECT t.iso, t.tool, t.en, 'CONFIG_ROUTER', t.prov, t.cref, t.mkt, t.cur, t.cfg, t.nt
FROM (SELECT 'IN' iso,'CREATE_ACCOUNT' tool,1 en,'cashstudio' prov,'LEADSTUDIO_API' cref,'IND' mkt,
             NULL cur,'{"portal_url":"https://crm.landmarkmarkets.in/"}' cfg,'WF3 v1' nt
      UNION ALL SELECT 'IN','CREATE_PAYMENT_LINK',1,'okpay','OKPAY_IN',NULL,'INR',
             '{"min_amount":2000,"max_amount":500000}','WF7 v1'
      UNION ALL SELECT 'IN','CALLBACK',0,NULL,NULL,NULL,NULL,NULL,'v1: callback por data_collection, no tool'
      UNION ALL SELECT 'NP','CREATE_ACCOUNT',1,'cashstudio','LEADSTUDIO_API','NPL',NULL,
             '{"portal_url":"https://crm.lmtradermarkets.com/"}','WF3 v1'
      UNION ALL SELECT 'NP','CREATE_PAYMENT_LINK',0,'monetix',NULL,NULL,'NPR',NULL,'Monetix sin contrato') t
WHERE @do_seed
  AND NOT EXISTS (SELECT 1 FROM country_tool_configs x WHERE x.country_iso=t.iso AND x.tool_type=t.tool);

-- S7 · franjas: NO se siembran (hoy WF2 llama 24/7; sembrarlas cambiaría el comportamiento)
-- S8 · MX / CO / VE: NO se siembran. Se dan de alta desde el panel con sus
--      agentes reales de ElevenLabs; la migración no inventa agent_ids.

-- marcadores (último paso)
INSERT INTO schema_migrations (migration_id, notes)
SELECT @SEED, 'Seed V2.2' FROM DUAL
WHERE @do_seed AND NOT EXISTS (SELECT 1 FROM schema_migrations WHERE migration_id=@SEED);

INSERT INTO schema_migrations (migration_id, notes)
SELECT @MIG, 'DDL V2.2: country switch, adapter_key, call facts, event store, reconciliation'
FROM DUAL WHERE NOT EXISTS (SELECT 1 FROM schema_migrations WHERE migration_id=@MIG);


-- ═══════════════════════════════════════════════════════════════════════════════
--  GRANTS — PENDING_VERIFICATION (usuario real del panel y de n8n)
-- ═══════════════════════════════════════════════════════════════════════════════
-- Panel (lee todo; escribe configuración y resuelve issues):
-- GRANT SELECT, INSERT, UPDATE ON asterisk.countries, voice_providers, followup_policies,
--       call_routes, country_tool_configs TO 'panel_rw'@'%';
-- GRANT SELECT, INSERT, UPDATE, DELETE ON asterisk.route_capacity_windows, route_telegram_targets TO 'panel_rw'@'%';
-- GRANT SELECT, INSERT ON asterisk.route_audit TO 'panel_rw'@'%';
-- GRANT SELECT ON asterisk.wf_call_jobs, wf_conversation_ledger, wf_events TO 'panel_rw'@'%';
-- GRANT SELECT, UPDATE ON asterisk.wf_reconciliation_issues TO 'panel_rw'@'%';
-- n8n (escribe ejecución y eventos; NUNCA configuración):
-- GRANT SELECT, INSERT, UPDATE ON asterisk.wf_call_jobs, wf_conversation_ledger,
--       wf_reconciliation_issues TO '<n8n_user>'@'%';
-- GRANT SELECT, INSERT ON asterisk.wf_events TO '<n8n_user>'@'%';   -- append-only
-- Sin DELETE sobre call_routes ni wf_events para nadie.

-- ┌────────────────────────────────────────────────────────────────────────┐
-- │ PARTE: 002_callcenter_suite_v2.sql                                    │
-- └────────────────────────────────────────────────────────────────────────┘

-- ═══════════════════════════════════════════════════════════════════════════════
--  002_callcenter_suite_v2.sql
--  Landmark Markets · Call Center V2 — añadidos de la fase BUILD
--
--  ESTADO: PROPUESTA — NO EJECUTADA EN PRODUCCIÓN.
--  Se aplica DESPUÉS de 001_multi_country_config_v2_2.sql (la fundación V2.2,
--  aprobada). Esta migración NO redefine nada de 001: solo agrega lo que los
--  siete templates de la suite necesitan y que la fundación no cubría.
--
--  GARANTÍAS (idénticas a las de 001)
--    · Idempotente: la 2ª ejecución es NO-OP funcional.
--    · Seed de UNA sola vez (marcador en schema_migrations). Nunca pisa
--      configuración editada desde el panel.
--    · Sin DROP / TRUNCATE / DELETE ejecutables.
--    · No altera ninguna tabla productiva existente (crm_leads, crm_conversions,
--      stringee_calls, wf_call_followups, wf10_sent_recordings,
--      wf2_provider_config, n8n_switches, sip_*, app_settings…).
--
--  QUÉ AGREGA
--    · wf_settings              → parámetros operativos del suite (sin secretos)
--    · wf_recording_ledger      → idempotencia y correlación de WF10 V2
--                                 (cadena call_job_id → conversation_id → followup_id)
--    · wf_tool_requests         → claim idempotente de las tools de país (WF3/WF7)
--    · call_routes.recording_*  → ya existen en 001; acá solo se agregan los
--                                 campos que faltaban para WF10 V2
--    · countries.operating_*    → NO se agrega: la ventana operativa es
--                                 route_capacity_windows (ver CONFIGURATION_GUIDE)
--
--  Motor objetivo: MariaDB 10.11 (probado). Compatible con el estilo de 001.
-- ═══════════════════════════════════════════════════════════════════════════════

SET @MIG2  = '002_callcenter_suite_v2';
SET @SEED2 = '002_callcenter_suite_v2:seed';

-- 001 debe estar aplicada. Si no, se aborta con un nombre de tabla explícito
-- (mismo patrón defensivo que usa 001 para el caso de dos llamadas en vuelo).
SET @c := (SELECT COUNT(*) FROM information_schema.TABLES
           WHERE TABLE_SCHEMA=DATABASE() AND TABLE_NAME='wf_call_jobs');
SET @ddl := IF(@c=0,
  'SELECT * FROM `ABORT_run_001_multi_country_config_v2_2_first`', 'DO 0');
PREPARE s FROM @ddl; EXECUTE s; DEALLOCATE PREPARE s;


-- ═══════════════════════════════════════════════════════════════════════════════
--  C1 · wf_settings — parámetros operativos del suite
--
--  Por qué existe: los templates V2 no pueden llevar literales de negocio
--  (N8N_TEMPLATE_STANDARD §1). Cosas como "cuántos minutos antes de considerar
--  obsoleto un DISPATCHING" o "cuál es la ruta de compatibilidad para un
--  post-call de v1 sin route_key" son configuración, no código.
--
--  NUNCA guarda secretos: solo REFERENCIAS lógicas (credential_ref). El panel
--  rechaza valores cuya clave parezca un secreto, igual que en country_tool_configs.
-- ═══════════════════════════════════════════════════════════════════════════════
CREATE TABLE IF NOT EXISTS wf_settings (
  setting_key   VARCHAR(64)  NOT NULL PRIMARY KEY,
  setting_value VARCHAR(255) NULL,
  value_type    ENUM('int','string','bool','json') NOT NULL DEFAULT 'string',
  scope         VARCHAR(32)  NOT NULL DEFAULT 'suite',
  description   VARCHAR(255) NULL,
  created_at    DATETIME     NOT NULL DEFAULT CURRENT_TIMESTAMP,
  updated_at    DATETIME     NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;


-- ═══════════════════════════════════════════════════════════════════════════════
--  C2 · wf_recording_ledger — WF10 V2
--
--  v1 correlacionaba grabaciones por RIGHT(phone,10) contra wf_call_followups.
--  Eso mezcla llamadas de distintos intentos y de distintas rutas del mismo
--  número. V2 correlaciona por la cadena:
--        call_job_id → conversation_id → followup_id → grabación
--
--  recording_ref es la identidad de la grabación en su origen:
--    · Stringee local : el filename del worker (stringee-<phone>-<ms>.wav)
--    · ElevenLabs     : el conversation_id
--  UNIQUE ⇒ la misma grabación no se sube ni se reenvía dos veces, aunque el
--  workflow se re-ejecute o dos corridas se solapen.
--
--  La tabla v1 wf10_sent_recordings NO se toca ni se migra: queda como está
--  mientras WF10 v1 siga activo. V2 escribe solo acá.
-- ═══════════════════════════════════════════════════════════════════════════════
CREATE TABLE IF NOT EXISTS wf_recording_ledger (
  id                BIGINT AUTO_INCREMENT PRIMARY KEY,
  recording_ref     VARCHAR(191) NOT NULL,
  source            VARCHAR(32)  NOT NULL,
  call_job_id       VARCHAR(96)  NULL,
  conversation_id   VARCHAR(128) NULL,
  followup_id       VARCHAR(64)  NULL,
  lead_id           VARCHAR(64)  NULL,
  route_key         VARCHAR(64)  NULL,
  country_iso       CHAR(2)      NULL,
  provider          VARCHAR(32)  NULL,
  duration_seconds  INT          NULL,
  size_bytes        BIGINT       NULL,
  correlation       VARCHAR(24)  NULL,
  state             ENUM('CLAIMED','UPLOADED','SENT','SKIPPED_SHORT','ORPHAN','FAILED')
                    NOT NULL DEFAULT 'CLAIMED',
  claim_token       VARCHAR(64)  NULL,
  crm_uploaded      TINYINT      NOT NULL DEFAULT 0,
  telegram_sent     TINYINT      NOT NULL DEFAULT 0,
  error_code        VARCHAR(32)  NULL,
  error_message     VARCHAR(255) NULL,
  execution_id      VARCHAR(64)  NULL,
  claimed_at        DATETIME     NOT NULL DEFAULT CURRENT_TIMESTAMP,
  completed_at      DATETIME     NULL,
  updated_at        DATETIME     NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
  UNIQUE KEY uq_recording_ref (recording_ref),
  KEY idx_state (state),
  KEY idx_job (call_job_id),
  KEY idx_conversation (conversation_id),
  KEY idx_route_claimed (route_key, claimed_at),
  KEY idx_lead (lead_id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;


-- ═══════════════════════════════════════════════════════════════════════════════
--  C3 · wf_tool_requests — claim idempotente de las tools de país (WF3 / WF7)
--
--  Problema real de v1: ElevenLabs puede reintentar un tool-call, y el bot de
--  WhatsApp puede disparar el mismo pedido. WF3 v1 se apoyaba en el 409 de
--  CashStudio (que llega DESPUÉS de haber pedido la cuenta) y WF7 v1 en un
--  X-Idempotency-Key que el proveedor puede ignorar.
--
--  request_key es DETERMINISTA: {TOOL}:{lead_id}:{request_ref}
--  request_ref = conversation_id de la llamada que pidió la tool, o el
--  execution_id si no hay conversación (mismo criterio que
--  ANALYTICS_EVENT_CONTRACT · key_parts).
--
--  El claim se gana por relectura del token propio, igual que wf_call_jobs.
--  El ganador ejecuta la tool; el perdedor devuelve el resultado ya registrado
--  en vez de volver a crear una cuenta o un link de pago.
-- ═══════════════════════════════════════════════════════════════════════════════
CREATE TABLE IF NOT EXISTS wf_tool_requests (
  id              BIGINT AUTO_INCREMENT PRIMARY KEY,
  request_key     VARCHAR(191) NOT NULL,
  tool_type       VARCHAR(32)  NOT NULL,
  lead_id         VARCHAR(64)  NOT NULL,
  country_iso     CHAR(2)      NULL,
  request_ref     VARCHAR(128) NULL,
  call_job_id     VARCHAR(96)  NULL,
  conversation_id VARCHAR(128) NULL,
  route_key       VARCHAR(64)  NULL,
  tool_provider   VARCHAR(64)  NULL,
  mode            VARCHAR(24)  NULL,
  order_ref       VARCHAR(128) NULL,
  amount          DECIMAL(14,2) NULL,
  currency        CHAR(3)      NULL,
  claim_token     VARCHAR(64)  NULL,
  state           ENUM('CLAIMED','SUCCEEDED','ALREADY_EXISTS','FAILED',
                       'NEEDS_RECONCILIATION') NOT NULL DEFAULT 'CLAIMED',
  result_ref      VARCHAR(128) NULL,
  error_code      VARCHAR(32)  NULL,
  error_message   VARCHAR(255) NULL,
  execution_id    VARCHAR(64)  NULL,
  claimed_at      DATETIME     NOT NULL DEFAULT CURRENT_TIMESTAMP,
  completed_at    DATETIME     NULL,
  updated_at      DATETIME     NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
  UNIQUE KEY uq_request_key (request_key),
  KEY idx_state (state),
  KEY idx_lead_tool (lead_id, tool_type),
  KEY idx_order (order_ref),
  KEY idx_claimed (claimed_at)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;


-- ═══════════════════════════════════════════════════════════════════════════════
--  C4 · columnas que WF10 V2 necesita en call_routes
--
--  001 ya creó recording_enabled / recording_min_secs / recording_upload_crm /
--  recording_telegram. Falta el origen: una ruta Stringee toma la grabación del
--  worker local; una ruta SIP la toma de ElevenLabs. Eso es configuración de
--  ruta, no un "if provider" en el workflow.
-- ═══════════════════════════════════════════════════════════════════════════════
SET @t := 'call_routes';
SET @c := (SELECT COUNT(*) FROM information_schema.COLUMNS WHERE TABLE_SCHEMA=DATABASE()
           AND TABLE_NAME=@t AND COLUMN_NAME='recording_source');
SET @ddl := IF(@c=0,
  'ALTER TABLE call_routes ADD COLUMN recording_source VARCHAR(32) NULL AFTER recording_min_secs',
  'DO 0');
PREPARE s FROM @ddl; EXECUTE s; DEALLOCATE PREPARE s;

-- ventana de recogida de grabaciones, en horas, por ruta (WF10 no puede llevarla
-- hardcodeada; v1 usaba "solo las de hoy", que perdía las de medianoche)
SET @c := (SELECT COUNT(*) FROM information_schema.COLUMNS WHERE TABLE_SCHEMA=DATABASE()
           AND TABLE_NAME=@t AND COLUMN_NAME='recording_lookback_hours');
SET @ddl := IF(@c=0,
  'ALTER TABLE call_routes ADD COLUMN recording_lookback_hours INT NOT NULL DEFAULT 48 AFTER recording_source',
  'DO 0');
PREPARE s FROM @ddl; EXECUTE s; DEALLOCATE PREPARE s;


-- ═══════════════════════════════════════════════════════════════════════════════
--  C5 · índice de apoyo para el reconciliador de WF14
--  (jobs pendientes de reconciliar, ordenados por antigüedad)
-- ═══════════════════════════════════════════════════════════════════════════════
SET @c := (SELECT COUNT(*) FROM information_schema.STATISTICS WHERE TABLE_SCHEMA=DATABASE()
           AND TABLE_NAME='wf_call_jobs' AND INDEX_NAME='idx_state_updated');
SET @ddl := IF(@c=0, 'ALTER TABLE wf_call_jobs ADD KEY idx_state_updated (state, updated_at)', 'DO 0');
PREPARE s FROM @ddl; EXECUTE s; DEALLOCATE PREPARE s;

SET @c := (SELECT COUNT(*) FROM information_schema.STATISTICS WHERE TABLE_SCHEMA=DATABASE()
           AND TABLE_NAME='wf_call_jobs' AND INDEX_NAME='idx_tech_retry');
SET @ddl := IF(@c=0,
  'ALTER TABLE wf_call_jobs ADD KEY idx_tech_retry (state, next_tech_retry_at)', 'DO 0');
PREPARE s FROM @ddl; EXECUTE s; DEALLOCATE PREPARE s;


-- ═══════════════════════════════════════════════════════════════════════════════
--  SEED — una sola vez. Valores = los umbrales que hoy están implícitos en el
--  código de v1 y en call_jobs.reconcile_stale_jobs(). Ninguno es un secreto.
-- ═══════════════════════════════════════════════════════════════════════════════
SET @do_seed2 := (SELECT COUNT(*) = 0 FROM schema_migrations WHERE migration_id = @SEED2);

INSERT INTO wf_settings (setting_key, setting_value, value_type, scope, description)
SELECT t.k, t.v, t.ty, t.sc, t.d FROM (
      SELECT 'reconcile_dispatching_minutes' k,'5'   v,'int'    ty,'WF14' sc,
             'DISPATCHING mas viejo que esto -> NEEDS_RECONCILIATION' d
UNION ALL SELECT 'reconcile_unknown_minutes','5','int','WF14',
             'UNKNOWN mas viejo que esto -> NEEDS_RECONCILIATION'
UNION ALL SELECT 'reconcile_dispatched_minutes','60','int','WF14',
             'DISPATCHED sin post-call mas viejo que esto -> NEEDS_RECONCILIATION'
UNION ALL SELECT 'reconcile_claimed_minutes','10','int','WF14',
             'CLAIMED que nunca despacho -> RELEASED (reintento tecnico)'
UNION ALL SELECT 'reconcile_ledger_minutes','15','int','WF14',
             'ledger CLAIMED sin followup_id -> NEEDS_RECONCILIATION'
UNION ALL SELECT 'tech_retry_max','8','int','WF2',
             'tope de reintentos tecnicos por intento de negocio; agotado -> NEEDS_RECONCILIATION'
UNION ALL SELECT 'tech_retry_backoff_cap_minutes','60','int','WF2',
             'tope del backoff exponencial 1,2,4...'
UNION ALL SELECT 'postcall_polling_window_minutes','20','int','WF9',
             'ventana de la red de seguridad por polling de ElevenLabs'
UNION ALL SELECT 'legacy_compat_route_key','','string','WF9',
             'ruta de compatibilidad para post-calls de v1 sin route_key. VACIO = no se procesan (fail-closed)'
UNION ALL SELECT 'crm_notes_language','en','string','SUITE',
             'idioma OBLIGATORIO de todo texto humano enviado a LeadStudio. Ver CRM_ENGLISH_RULE.md'
UNION ALL SELECT 'recording_default_min_secs','60','int','WF10',
             'minimo por defecto si la ruta no define recording_min_secs'
UNION ALL SELECT 'wf14_leadstudio_page_size','200','int','WF14',
             'tamano de pagina de GET /api/leads en la reconciliacion'
) t
WHERE @do_seed2 AND NOT EXISTS (SELECT 1 FROM wf_settings x WHERE x.setting_key = t.k);

-- recording_source de las rutas sembradas por 001: se deduce del adapter del
-- proveedor UNA sola vez, y solo si la columna sigue vacía (nunca pisa una
-- edición del panel).
UPDATE call_routes r
  JOIN voice_providers p ON p.id = r.provider_id
   SET r.recording_source = CASE p.adapter_key
         WHEN 'STRINGEE_WORKER' THEN 'STRINGEE_WORKER'
         WHEN 'ELEVENLABS_SIP'  THEN 'ELEVENLABS_API'
         ELSE NULL END
 WHERE @do_seed2 AND (r.recording_source IS NULL OR r.recording_source = '');

-- marcadores (último paso)
INSERT INTO schema_migrations (migration_id, notes)
SELECT @SEED2, 'Seed V2 suite: wf_settings + recording_source' FROM DUAL
WHERE @do_seed2 AND NOT EXISTS (SELECT 1 FROM schema_migrations WHERE migration_id=@SEED2);

INSERT INTO schema_migrations (migration_id, notes)
SELECT @MIG2, 'DDL suite V2: wf_settings, wf_recording_ledger, wf_tool_requests, recording_source'
FROM DUAL WHERE NOT EXISTS (SELECT 1 FROM schema_migrations WHERE migration_id=@MIG2);


-- ═══════════════════════════════════════════════════════════════════════════════
--  GRANTS — PENDING_VERIFICATION (usuario real del panel y de n8n)
-- ═══════════════════════════════════════════════════════════════════════════════
-- Panel (lee todo; edita wf_settings; resuelve issues):
-- GRANT SELECT, INSERT, UPDATE ON asterisk.wf_settings TO 'panel_rw'@'%';
-- GRANT SELECT ON asterisk.wf_recording_ledger, asterisk.wf_tool_requests TO 'panel_rw'@'%';
-- n8n (escribe ejecución; NUNCA configuración):
-- GRANT SELECT ON asterisk.wf_settings TO '<n8n_user>'@'%';          -- solo lectura
-- GRANT SELECT, INSERT, UPDATE ON asterisk.wf_recording_ledger,
--       asterisk.wf_tool_requests TO '<n8n_user>'@'%';
-- Sin DELETE sobre wf_recording_ledger ni wf_tool_requests para nadie.

-- ┌────────────────────────────────────────────────────────────────────────┐
-- │ PARTE: 003_legacy_compat_tables.sql                                   │
-- └────────────────────────────────────────────────────────────────────────┘

-- ═══════════════════════════════════════════════════════════════════════════════
--  003_legacy_compat_tables.sql
--  Landmark Markets · Tablas de compatibilidad con v1
--
--  ESTADO: PROPUESTA — NO EJECUTADA EN PRODUCCIÓN.
--
--  POR QUÉ EXISTE ESTE ARCHIVO
--  ───────────────────────────
--  WF14 V2 conserva la sincronización `LeadStudio -> crm_leads` y el volcado del
--  call-log a `stringee_calls`, y WF10 V2 usa `crm_leads` en su ruta de
--  correlación LEGACY por teléfono. Esas tablas las creó el sistema v1: la
--  migración 001/002 NO las toca, a propósito.
--
--  En PRODUCCIÓN ya existen y este archivo es un NO-OP completo (todo es
--  CREATE TABLE IF NOT EXISTS).
--
--  En STAGING, una base limpia con 001+002 NO las tiene, y esas sentencias de
--  WF14/WF10 fallan con "table doesn't exist". Este archivo las crea para que
--  el suite se pueda probar entero contra una base nueva.
--
--  ⚠️ PENDING_VERIFICATION PV-14
--  Las definiciones de abajo están RECONSTRUIDAS a partir del SQL que los JSON
--  de v1 ejecutan (columnas leídas y escritas), no de un `SHOW CREATE TABLE` de
--  producción. Antes de dar por buena la instalación en staging, comparar con:
--
--      SHOW CREATE TABLE crm_leads\G
--      SHOW CREATE TABLE crm_conversions\G
--      SHOW CREATE TABLE stringee_calls\G
--      SHOW CREATE TABLE panel_sync_log\G
--      SHOW CREATE TABLE wf10_sent_recordings\G
--
--  Si alguna difiere, gana la de producción: ajustar ESTE archivo, nunca la
--  tabla productiva.
--
--  GARANTÍAS: idempotente · sin DROP/TRUNCATE/DELETE · no altera ninguna tabla
--  existente (ni siquiera con ALTER: si la tabla está, no se toca).
-- ═══════════════════════════════════════════════════════════════════════════════

SET @MIG3 = '003_legacy_compat_tables';

-- L1 · crm_leads — copia local del CRM (la escribe WF14; la lee WF10 y el panel)
--      Columnas tomadas de WF14 v1 "🧱 Lotes de 500 (crm_leads)".
CREATE TABLE IF NOT EXISTS crm_leads (
  lead_id           VARCHAR(64)  NOT NULL PRIMARY KEY,
  full_name         VARCHAR(160) NULL,
  phone             VARCHAR(32)  NULL,
  country           VARCHAR(32)  NULL,
  country_code      VARCHAR(8)   NULL,
  language          VARCHAR(16)  NULL,
  status            VARCHAR(48)  NULL,
  stage             VARCHAR(48)  NULL,
  call_attempts     INT          NOT NULL DEFAULT 0,
  do_not_call       TINYINT      NOT NULL DEFAULT 0,
  last_contacted_at DATETIME     NULL,
  next_follow_up_at DATETIME     NULL,
  provider          VARCHAR(32)  NULL,
  synced_at         DATETIME     NULL,
  KEY idx_phone_last10 (phone),
  KEY idx_status (status),
  KEY idx_country (country)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

-- L2 · crm_conversions — cuentas abiertas según el CRM (WF14 v1)
CREATE TABLE IF NOT EXISTS crm_conversions (
  lead_id    VARCHAR(64)  NOT NULL PRIMARY KEY,
  full_name  VARCHAR(160) NULL,
  phone      VARCHAR(32)  NULL,
  country    VARCHAR(32)  NULL,
  provider   VARCHAR(32)  NULL,
  stage      VARCHAR(48)  NULL,
  created_at DATETIME     NULL,
  synced_at  DATETIME     NULL,
  KEY idx_country (country),
  KEY idx_created (created_at)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

-- L3 · stringee_calls — call-log del proveedor de cuota fija (WF14 v1)
CREATE TABLE IF NOT EXISTS stringee_calls (
  call_id         VARCHAR(64) NOT NULL PRIMARY KEY,
  lead_id         VARCHAR(64) NULL,
  phone           VARCHAR(32) NULL,
  country         VARCHAR(32) NULL,
  conversation_id VARCHAR(128) NULL,
  answered        TINYINT     NOT NULL DEFAULT 0,
  duration_secs   INT         NOT NULL DEFAULT 0,
  start_time      BIGINT      NOT NULL DEFAULT 0,
  answer_time     BIGINT      NOT NULL DEFAULT 0,
  stop_time       BIGINT      NOT NULL DEFAULT 0,
  KEY idx_lead (lead_id),
  KEY idx_phone (phone),
  KEY idx_start (start_time),
  KEY idx_conversation (conversation_id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

-- L4 · panel_sync_log — bitácora de sincronizaciones (WF14 v1 y pantallas del panel)
CREATE TABLE IF NOT EXISTS panel_sync_log (
  id        BIGINT AUTO_INCREMENT PRIMARY KEY,
  source    VARCHAR(32) NOT NULL,
  rows_in   INT         NOT NULL DEFAULT 0,
  status    VARCHAR(16) NOT NULL DEFAULT 'ok',
  synced_at DATETIME    NOT NULL DEFAULT CURRENT_TIMESTAMP,
  KEY idx_source_when (source, synced_at)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

-- L5 · wf10_sent_recordings — ledger de WF10 v1.
--      V2 NO escribe acá (usa wf_recording_ledger). Se crea solo para que una
--      base de staging pueda correr v1 y V2 en paralelo durante la comparación.
CREATE TABLE IF NOT EXISTS wf10_sent_recordings (
  filename      VARCHAR(191) NOT NULL PRIMARY KEY,
  phone         VARCHAR(32)  NULL,
  country       VARCHAR(32)  NULL,
  duration_secs INT          NOT NULL DEFAULT 0,
  sent_at       DATETIME     NOT NULL DEFAULT CURRENT_TIMESTAMP,
  KEY idx_sent (sent_at)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

-- L6 · wf2_provider_config — interruptor de proveedores de v1.
--      V2 no lo usa NUNCA (sus interruptores son countries/voice_providers/
--      call_routes). Se crea para que el ROLLBACK a v1 tenga dónde apoyarse.
CREATE TABLE IF NOT EXISTS wf2_provider_config (
  provider VARCHAR(32) NOT NULL PRIMARY KEY,
  enabled  TINYINT     NOT NULL DEFAULT 1
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

INSERT INTO schema_migrations (migration_id, notes)
SELECT @MIG3, 'Tablas de compatibilidad v1 (no-op en produccion)'
FROM DUAL WHERE NOT EXISTS (SELECT 1 FROM schema_migrations WHERE migration_id=@MIG3);

-- ┌────────────────────────────────────────────────────────────────────────┐
-- │ PARTE: 004_billing_v2.sql                                             │
-- └────────────────────────────────────────────────────────────────────────┘

-- ═══════════════════════════════════════════════════════════════════════════════
--  004 · BILLING V2 — UNA identidad de proveedor en todo el panel
--
--  Problema que resuelve (§34):
--    El panel tenía DOS registros del mismo proveedor comercial:
--      · SIP Balance  → sip_providers / sip_provider_pricing / sip_deposits
--      · Call Center  → voice_providers / call_routes
--    El usuario tenía que dar de alta Provider1 dos veces.
--
--  Cómo lo resuelve, SIN destruir nada:
--    · voice_providers gana un puntero opcional a la fila legacy
--      (legacy_sip_provider_id). Nada se borra, nada se migra a la fuerza.
--    · SIP Balance sigue siendo el dueño de depósitos, precios históricos y
--      saldo. Esta migración NO toca esas tablas salvo para LEER.
--    · El precio por minuto pasa a poder vivir a nivel de RUTA (§36), que es
--      lo que permite dos rutas del mismo país con tarifas distintas — algo
--      que sip_provider_pricing no puede expresar por su UNIQUE(provider,país).
--
--  Reglas que cumple:
--    §35 billing_model es VARCHAR + registro en la aplicación, no ENUM
--    §37 Stringee es MONTHLY_FLAT y NO exige precio por minuto
--    §40 no se resetean depósitos, precios, historial ni fecha de inicio
--    §41 apagar un proveedor NO borra su información de facturación
--    §64 idempotente, re-ejecutable, no destructiva, sin pisar ediciones
--
--  Requiere: 001 y 002 ya aplicados.
-- ═══════════════════════════════════════════════════════════════════════════════

-- Guarda: si 001 no corrió, abortar con un error legible en vez de fallar feo.
SET @ok := (SELECT COUNT(*) FROM information_schema.TABLES
            WHERE TABLE_SCHEMA=DATABASE() AND TABLE_NAME='voice_providers');
SET @msg := IF(@ok=1, 'DO 0',
  'SELECT * FROM `ABORT_run_001_multi_country_config_v2_2_first`');
PREPARE s FROM @msg; EXECUTE s; DEALLOCATE PREPARE s;


-- ── D1 · voice_providers: facturación del proveedor ───────────────────────────
-- Columnas aditivas. Cada una se añade sólo si no existe, para que la
-- migración se pueda re-ejecutar sin error y sin pisar valores editados.

SET @c := (SELECT COUNT(*) FROM information_schema.COLUMNS
           WHERE TABLE_SCHEMA=DATABASE() AND TABLE_NAME='voice_providers'
             AND COLUMN_NAME='billing_model');
SET @ddl := IF(@c=0,
  'ALTER TABLE voice_providers ADD COLUMN billing_model VARCHAR(32) NOT NULL DEFAULT ''PER_MINUTE'' AFTER enabled',
  'DO 0');
PREPARE s FROM @ddl; EXECUTE s; DEALLOCATE PREPARE s;

SET @c := (SELECT COUNT(*) FROM information_schema.COLUMNS
           WHERE TABLE_SCHEMA=DATABASE() AND TABLE_NAME='voice_providers'
             AND COLUMN_NAME='billing_currency');
SET @ddl := IF(@c=0,
  'ALTER TABLE voice_providers ADD COLUMN billing_currency CHAR(3) NOT NULL DEFAULT ''USD'' AFTER billing_model',
  'DO 0');
PREPARE s FROM @ddl; EXECUTE s; DEALLOCATE PREPARE s;

SET @c := (SELECT COUNT(*) FROM information_schema.COLUMNS
           WHERE TABLE_SCHEMA=DATABASE() AND TABLE_NAME='voice_providers'
             AND COLUMN_NAME='monthly_fee');
SET @ddl := IF(@c=0,
  'ALTER TABLE voice_providers ADD COLUMN monthly_fee DECIMAL(12,2) NULL AFTER billing_currency',
  'DO 0');
PREPARE s FROM @ddl; EXECUTE s; DEALLOCATE PREPARE s;

SET @c := (SELECT COUNT(*) FROM information_schema.COLUMNS
           WHERE TABLE_SCHEMA=DATABASE() AND TABLE_NAME='voice_providers'
             AND COLUMN_NAME='billing_start_date');
SET @ddl := IF(@c=0,
  'ALTER TABLE voice_providers ADD COLUMN billing_start_date DATE NULL AFTER monthly_fee',
  'DO 0');
PREPARE s FROM @ddl; EXECUTE s; DEALLOCATE PREPARE s;

SET @c := (SELECT COUNT(*) FROM information_schema.COLUMNS
           WHERE TABLE_SCHEMA=DATABASE() AND TABLE_NAME='voice_providers'
             AND COLUMN_NAME='billing_notes');
SET @ddl := IF(@c=0,
  'ALTER TABLE voice_providers ADD COLUMN billing_notes VARCHAR(255) NULL AFTER billing_start_date',
  'DO 0');
PREPARE s FROM @ddl; EXECUTE s; DEALLOCATE PREPARE s;

-- El puntero a SIP Balance. NULL = todavía sin vincular; el panel lo muestra
-- como aviso, no como error. Deliberadamente SIN foreign key: sip_providers
-- es una tabla de v1 que esta migración no debe condicionar ni bloquear.
SET @c := (SELECT COUNT(*) FROM information_schema.COLUMNS
           WHERE TABLE_SCHEMA=DATABASE() AND TABLE_NAME='voice_providers'
             AND COLUMN_NAME='legacy_sip_provider_id');
SET @ddl := IF(@c=0,
  'ALTER TABLE voice_providers ADD COLUMN legacy_sip_provider_id INT NULL AFTER billing_notes',
  'DO 0');
PREPARE s FROM @ddl; EXECUTE s; DEALLOCATE PREPARE s;

SET @c := (SELECT COUNT(*) FROM information_schema.STATISTICS
           WHERE TABLE_SCHEMA=DATABASE() AND TABLE_NAME='voice_providers'
             AND INDEX_NAME='idx_legacy_sip');
SET @ddl := IF(@c=0,
  'ALTER TABLE voice_providers ADD KEY idx_legacy_sip (legacy_sip_provider_id)',
  'DO 0');
PREPARE s FROM @ddl; EXECUTE s; DEALLOCATE PREPARE s;


-- ── D2 · call_routes: facturación por RUTA ───────────────────────────────────
-- §36: dos rutas del mismo país pueden tener tarifas distintas. Por eso el
-- precio vive en la ruta y no en (proveedor, país) como en sip_provider_pricing.
-- NULL significa "hereda"; el panel muestra de dónde sale el número.

SET @c := (SELECT COUNT(*) FROM information_schema.COLUMNS
           WHERE TABLE_SCHEMA=DATABASE() AND TABLE_NAME='call_routes'
             AND COLUMN_NAME='price_per_minute');
SET @ddl := IF(@c=0,
  'ALTER TABLE call_routes ADD COLUMN price_per_minute DECIMAL(10,4) NULL AFTER capacity_default',
  'DO 0');
PREPARE s FROM @ddl; EXECUTE s; DEALLOCATE PREPARE s;

SET @c := (SELECT COUNT(*) FROM information_schema.COLUMNS
           WHERE TABLE_SCHEMA=DATABASE() AND TABLE_NAME='call_routes'
             AND COLUMN_NAME='price_per_call');
SET @ddl := IF(@c=0,
  'ALTER TABLE call_routes ADD COLUMN price_per_call DECIMAL(10,4) NULL AFTER price_per_minute',
  'DO 0');
PREPARE s FROM @ddl; EXECUTE s; DEALLOCATE PREPARE s;

SET @c := (SELECT COUNT(*) FROM information_schema.COLUMNS
           WHERE TABLE_SCHEMA=DATABASE() AND TABLE_NAME='call_routes'
             AND COLUMN_NAME='billing_notes');
SET @ddl := IF(@c=0,
  'ALTER TABLE call_routes ADD COLUMN billing_notes VARCHAR(255) NULL AFTER price_per_call',
  'DO 0');
PREPARE s FROM @ddl; EXECUTE s; DEALLOCATE PREPARE s;

-- El trunk pjsip de la ruta. Es lo que permite casar una ruta V2 con la fila
-- de sip_provider_pricing que ya existía (india/proveedor1, mexico/proveedor-mx,
-- nepal/proveedor-nepal — ver pjsip.conf y analytics.ensure_sip_tables).
SET @c := (SELECT COUNT(*) FROM information_schema.COLUMNS
           WHERE TABLE_SCHEMA=DATABASE() AND TABLE_NAME='call_routes'
             AND COLUMN_NAME='trunk_name');
SET @ddl := IF(@c=0,
  'ALTER TABLE call_routes ADD COLUMN trunk_name VARCHAR(100) NULL AFTER caller_id',
  'DO 0');
PREPARE s FROM @ddl; EXECUTE s; DEALLOCATE PREPARE s;


-- ── D3 · billing_audit — quién tocó qué de facturación ───────────────────────
-- §53/§54: los cambios de facturación son sensibles y sólo MASTER. Queda rastro.
-- NUNCA guarda credenciales: sólo el campo, el valor viejo y el nuevo.
CREATE TABLE IF NOT EXISTS billing_audit (
  id           INT AUTO_INCREMENT PRIMARY KEY,
  scope        VARCHAR(16)  NOT NULL,           -- PROVIDER | ROUTE | MODE
  scope_ref    VARCHAR(64)  NULL,               -- code de proveedor o route_key
  action       VARCHAR(32)  NOT NULL,
  field        VARCHAR(64)  NULL,
  old_value    VARCHAR(255) NULL,
  new_value    VARCHAR(255) NULL,
  reason       VARCHAR(255) NULL,
  actor        VARCHAR(64)  NOT NULL,
  changed_at   DATETIME     NOT NULL DEFAULT CURRENT_TIMESTAMP,
  KEY idx_scope (scope, scope_ref),
  KEY idx_when  (changed_at)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;


-- ── D4 · valores iniciales, SIN pisar nada editado ───────────────────────────
-- Sólo se tocan filas cuyo campo sigue en el valor por defecto del ALTER.
-- Una segunda pasada de la migración no cambia nada.

-- Stringee es MONTHLY_FLAT (§37). Se fija SOLO si sigue en el default.
UPDATE voice_providers
   SET billing_model = 'MONTHLY_FLAT'
 WHERE code = 'stringee'
   AND billing_model = 'PER_MINUTE'
   AND monthly_fee IS NULL
   AND NOT EXISTS (SELECT 1 FROM billing_audit
                    WHERE scope='PROVIDER' AND scope_ref='stringee'
                      AND field='billing_model');

-- Provider1 es PER_MINUTE: ya es el default del ALTER, no hace falta tocarlo.

-- Vínculo con SIP Balance. Se hace SOLO si:
--   · la tabla legacy existe
--   · hay EXACTAMENTE una fila de proveedor legacy (el caso real de hoy)
--   · proveedor1 no está ya vinculado
-- Con más de una fila no se adivina: el panel pide elegir. Nunca se desvincula
-- ni se reescribe un vínculo existente.
SET @has_legacy := (SELECT COUNT(*) FROM information_schema.TABLES
                    WHERE TABLE_SCHEMA=DATABASE() AND TABLE_NAME='sip_providers');
SET @sql := IF(@has_legacy=1,
  'SET @legacy_n := (SELECT COUNT(*) FROM sip_providers)', 'SET @legacy_n := 0');
PREPARE s FROM @sql; EXECUTE s; DEALLOCATE PREPARE s;

SET @sql := IF(@has_legacy=1 AND @legacy_n=1,
  'UPDATE voice_providers vp
      SET vp.legacy_sip_provider_id = (SELECT id FROM sip_providers LIMIT 1)
    WHERE vp.code = ''proveedor1'' AND vp.legacy_sip_provider_id IS NULL',
  'DO 0');
PREPARE s FROM @sql; EXECUTE s; DEALLOCATE PREPARE s;

-- Trunk pjsip de cada ruta de Provider1. Son los nombres REALES que ya usan
-- pjsip.conf y sip_provider_pricing. Se rellena sólo si está vacío, para no
-- pisar una corrección hecha desde el panel.
UPDATE call_routes SET trunk_name = 'proveedor1'
 WHERE route_key = 'IN_PROVEEDOR1' AND (trunk_name IS NULL OR trunk_name = '');
UPDATE call_routes SET trunk_name = 'proveedor-nepal'
 WHERE route_key = 'NP_PROVEEDOR1' AND (trunk_name IS NULL OR trunk_name = '');
UPDATE call_routes SET trunk_name = 'proveedor-mx'
 WHERE route_key = 'MX_PROVEEDOR1' AND (trunk_name IS NULL OR trunk_name = '');

-- Precio por ruta heredado del precio legacy de ese país, SOLO la primera vez
-- (price_per_minute IS NULL). Si el usuario ya puso una tarifa de ruta, se
-- respeta. sip_provider_pricing NO se modifica: sólo se lee.
--
-- El emparejamiento es por trunk_name cuando lo hay (exacto), y si no por el
-- nombre del país en minúsculas, que es como sip_provider_pricing lo guarda
-- ('india', 'mexico', 'nepal' — ver analytics.ensure_sip_tables).
SET @sql := IF(@has_legacy=1,
  'UPDATE call_routes r
      JOIN voice_providers vp ON vp.id = r.provider_id
      JOIN countries c ON c.iso = r.iso
      JOIN sip_provider_pricing spp
        ON spp.provider_id = vp.legacy_sip_provider_id
       AND ( spp.trunk_name = r.trunk_name
          OR (r.trunk_name IS NULL AND spp.country = LOWER(c.country_name)) )
      SET r.price_per_minute = spp.price_per_minute
    WHERE r.price_per_minute IS NULL
      AND vp.billing_model = ''PER_MINUTE''',
  'DO 0');
PREPARE s FROM @sql; EXECUTE s; DEALLOCATE PREPARE s;


-- ── D5 · ajustes operativos de facturación ───────────────────────────────────
INSERT INTO wf_settings (setting_key, setting_value, value_type, description)
SELECT * FROM (SELECT
  'billing_effective_cost_enabled' AS k, '1' AS v, 'bool' AS t,
  'Show EFFECTIVE COST for MONTHLY_FLAT providers. It is a calculation, never a contract rate.' AS d) x
WHERE NOT EXISTS (SELECT 1 FROM wf_settings WHERE setting_key='billing_effective_cost_enabled');

INSERT INTO wf_settings (setting_key, setting_value, value_type, description)
SELECT * FROM (SELECT
  'billing_default_currency' AS k, 'USD' AS v, 'string' AS t,
  'Default currency for a new provider billing configuration.' AS d) x
WHERE NOT EXISTS (SELECT 1 FROM wf_settings WHERE setting_key='billing_default_currency');

-- ┌────────────────────────────────────────────────────────────────────────┐
-- │ PARTE: 005_legacy_backup_v2.sql                                       │
-- └────────────────────────────────────────────────────────────────────────┘

-- ═══════════════════════════════════════════════════════════════════════════════
--  005 · LEGACY BACKUP / PLAN B — el call center actual sigue siendo usable
--
--  Qué NO hace esta migración (§46):
--    · no borra n8n_switches ni n8n_switch_schedules
--    · no cambia workflow_ids, etiquetas, estados ON/OFF ni horarios
--    · no apaga ningún workflow
--    · no cambia el modo de operación
--
--  Qué añade:
--    · legacy_group_classification — clasifica cada grupo legacy por
--      categoría y dice si puede convivir con V2 (§51). Tabla APARTE:
--      apunta a n8n_switches, no la modifica.
--    · el modo de operación en app_settings, que ya existía en v1.
--      No se crea una tabla nueva para un único valor.
--
--  Principio de la clasificación: fail-closed. Un grupo que no sabemos
--  clasificar se marca UNKNOWN y coexists_with_v2=0 — se asume que
--  entra en conflicto. Equivocarse hacia "no convive" cuesta una
--  confirmación de más; equivocarse hacia "sí convive" cuesta llamar
--  dos veces al mismo cliente.
--
--  Requiere: 001 y 004 ya aplicados (billing_audit guarda los cambios de modo).
-- ═══════════════════════════════════════════════════════════════════════════════

SET @ok := (SELECT COUNT(*) FROM information_schema.TABLES
            WHERE TABLE_SCHEMA=DATABASE() AND TABLE_NAME='billing_audit');
SET @msg := IF(@ok=1, 'DO 0',
  'SELECT * FROM `ABORT_run_004_billing_v2_first`');
PREPARE s FROM @msg; EXECUTE s; DEALLOCATE PREPARE s;


-- ── E1 · app_settings, tal cual la define v1 ─────────────────────────────────
-- Si el panel v1 todavía no arrancó, la tabla no existe. Se crea con la MISMA
-- definición que analytics.py, para que ambos convivan sin sorpresas.
CREATE TABLE IF NOT EXISTS app_settings (
  setting_key   VARCHAR(100) PRIMARY KEY,
  setting_value TEXT
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

-- El modo arranca en LEGACY_BACKUP. Es deliberado y conservador.
--
-- Por qué NO V2_PRIMARY:
--   Esta migración se instala sobre un sistema que YA TIENE un call center
--   funcionando. Los grupos legacy pueden estar encendidos en n8n ahora
--   mismo. Si el modo arrancara en V2_PRIMARY, un operador podría encender
--   una ruta V2 más adelante SIN pasar nunca por el preflight
--   LEGACY_BACKUP → V2_PRIMARY, que es justo donde se comprueba contra n8n
--   que el despachador viejo está apagado. El enclavamiento existiría pero
--   nadie habría pasado por él.
--
--   Arrancando en LEGACY_BACKUP, encender V2 OBLIGA a un cutover explícito:
--   n8n alcanzable, grupos conflictivos apagados y verificados, grupos sin
--   clasificar resueltos. Un evento deliberado, con confirmación y auditoría.
--
-- LEGACY_BACKUP NO enciende nada. Significa sólo: "el sistema autorizado a
-- llamar es el legacy". Los workflows legacy siguen exactamente como estén;
-- esta migración no los toca. Los de V2 se importan con active:false y sus
-- rutas se siembran apagadas.
--
-- Configurar V2 (países, rutas, tarifas, tools) se puede hacer entero en
-- LEGACY_BACKUP. Lo único que no se puede es llamar.
INSERT INTO app_settings (setting_key, setting_value)
SELECT 'lm_operating_mode', 'LEGACY_BACKUP'
WHERE NOT EXISTS (SELECT 1 FROM app_settings WHERE setting_key='lm_operating_mode');

INSERT INTO app_settings (setting_key, setting_value)
SELECT 'lm_operating_mode_changed_at', ''
WHERE NOT EXISTS (SELECT 1 FROM app_settings WHERE setting_key='lm_operating_mode_changed_at');

INSERT INTO app_settings (setting_key, setting_value)
SELECT 'lm_operating_mode_changed_by', ''
WHERE NOT EXISTS (SELECT 1 FROM app_settings WHERE setting_key='lm_operating_mode_changed_by');


-- ── E2 · clasificación de los grupos legacy ──────────────────────────────────
-- Una fila por grupo de n8n_switches. Se referencia por switch_id Y por
-- etiqueta: el id es estable, pero si alguien recrea un grupo la etiqueta
-- permite reconocerlo. Nunca se borra una clasificación: si el grupo
-- desaparece, la fila queda para el historial.
CREATE TABLE IF NOT EXISTS legacy_group_classification (
  id                INT AUTO_INCREMENT PRIMARY KEY,
  switch_id         INT          NULL,
  switch_label      VARCHAR(80)  NOT NULL,
  category          VARCHAR(24)  NOT NULL DEFAULT 'UNKNOWN',
  coexists_with_v2  TINYINT      NOT NULL DEFAULT 0,
  rationale         VARCHAR(255) NULL,
  classified_by     VARCHAR(64)  NULL,
  updated_at        DATETIME     NOT NULL DEFAULT CURRENT_TIMESTAMP
                                 ON UPDATE CURRENT_TIMESTAMP,
  UNIQUE KEY uq_label (switch_label),
  KEY idx_switch (switch_id),
  KEY idx_coexists (coexists_with_v2)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;


-- ── E3 · clasificación inicial de los grupos que hay hoy ─────────────────────
-- Son los grupos visibles en el panel actual. Se insertan SOLO si no están:
-- una reclasificación hecha por el usuario no se pisa al re-ejecutar (§64).
--
-- Categorías y por qué:
--   DISPATCH    llama. Dos sistemas llamando = llamadas duplicadas al cliente.
--   FOLLOWUP    agenda reintentos. Duplicar = agendar dos veces el mismo lead.
--   ACCOUNT     abre cuentas. Duplicar = dos cuentas para el mismo cliente.
--   PAYMENT     genera links de pago. Duplicar = dos cobros.
--   POST_CALL   procesa resultados. Duplicar = contar dos veces la llamada.
--   RECORDING   sube grabaciones. Duplicar = adjunto repetido, no cobra nada.
--   CRM_SYNC    espeja el CRM a MySQL. Es idempotente por diseño.
--   ANALYTICS   sólo lee y agrega.

INSERT INTO legacy_group_classification
       (switch_label, category, coexists_with_v2, rationale, classified_by)
SELECT * FROM (SELECT
  'INDIA - NEPAL' AS a, 'DISPATCH' AS b, 0 AS c,
  'Calls India/Nepal leads. Running it while V2 dispatches would call the same customer twice.' AS d,
  'migration-005' AS e) x
WHERE NOT EXISTS (SELECT 1 FROM legacy_group_classification WHERE switch_label='INDIA - NEPAL');

INSERT INTO legacy_group_classification
       (switch_label, category, coexists_with_v2, rationale, classified_by)
SELECT * FROM (SELECT
  'MEXICO' AS a, 'DISPATCH' AS b, 0 AS c,
  'Calls Mexico leads. Same duplicate-call risk as any other dispatch group.' AS d,
  'migration-005' AS e) x
WHERE NOT EXISTS (SELECT 1 FROM legacy_group_classification WHERE switch_label='MEXICO');

INSERT INTO legacy_group_classification
       (switch_label, category, coexists_with_v2, rationale, classified_by)
SELECT * FROM (SELECT
  'INDIA - PROVEEDOR STRINGEE' AS a, 'DISPATCH' AS b, 0 AS c,
  'Calls India leads through Stringee. Conflicts with the V2 Stringee route.' AS d,
  'migration-005' AS e) x
WHERE NOT EXISTS (SELECT 1 FROM legacy_group_classification
                   WHERE switch_label='INDIA - PROVEEDOR STRINGEE');

INSERT INTO legacy_group_classification
       (switch_label, category, coexists_with_v2, rationale, classified_by)
SELECT * FROM (SELECT
  'PANEL' AS a, 'CRM_SYNC' AS b, 1 AS c,
  'Mirrors leads into the panel tables. Read-only against the CRM and idempotent, so it can run alongside V2.' AS d,
  'migration-005' AS e) x
WHERE NOT EXISTS (SELECT 1 FROM legacy_group_classification WHERE switch_label='PANEL');

INSERT INTO legacy_group_classification
       (switch_label, category, coexists_with_v2, rationale, classified_by)
SELECT * FROM (SELECT
  'CRM INDIA - NEPAL' AS a, 'CRM_SYNC' AS b, 1 AS c,
  'Mirrors CRM leads for India/Nepal into MySQL. Does not call and does not create follow-ups.' AS d,
  'migration-005' AS e) x
WHERE NOT EXISTS (SELECT 1 FROM legacy_group_classification
                   WHERE switch_label='CRM INDIA - NEPAL');

INSERT INTO legacy_group_classification
       (switch_label, category, coexists_with_v2, rationale, classified_by)
SELECT * FROM (SELECT
  'CRM PANEL' AS a, 'CRM_SYNC' AS b, 1 AS c,
  'Mirrors CRM data into the panel tables. Read-only against the CRM.' AS d,
  'migration-005' AS e) x
WHERE NOT EXISTS (SELECT 1 FROM legacy_group_classification WHERE switch_label='CRM PANEL');

-- Enlaza cada clasificación con el grupo real, si la tabla de v1 ya existe.
-- Por etiqueta, que es lo que el usuario ve. Sólo rellena lo que esté vacío.
SET @has_sw := (SELECT COUNT(*) FROM information_schema.TABLES
                WHERE TABLE_SCHEMA=DATABASE() AND TABLE_NAME='n8n_switches');
SET @sql := IF(@has_sw=1,
  'UPDATE legacy_group_classification g
      JOIN n8n_switches s ON s.label = g.switch_label
      SET g.switch_id = s.id
    WHERE g.switch_id IS NULL',
  'DO 0');
PREPARE s FROM @sql; EXECUTE s; DEALLOCATE PREPARE s;

-- Cualquier grupo que exista en v1 y no esté clasificado entra como UNKNOWN
-- y NO convive: fail-closed. El panel lo muestra en ámbar para que alguien
-- lo clasifique a mano antes de cambiar de modo.
SET @sql := IF(@has_sw=1,
  'INSERT INTO legacy_group_classification
       (switch_id, switch_label, category, coexists_with_v2, rationale, classified_by)
   SELECT s.id, s.label, ''UNKNOWN'', 0,
          ''Not classified yet. Treated as conflicting until someone classifies it.'',
          ''migration-005''
     FROM n8n_switches s
    WHERE NOT EXISTS (SELECT 1 FROM legacy_group_classification g
                       WHERE g.switch_label = s.label)',
  'DO 0');
PREPARE s FROM @sql; EXECUTE s; DEALLOCATE PREPARE s;


-- ── E4 · ajustes del modo ────────────────────────────────────────────────────
INSERT INTO wf_settings (setting_key, setting_value, value_type, description)
SELECT * FROM (SELECT
  'legacy_mode_requires_confirmation' AS k, '1' AS v, 'bool' AS t,
  'Require an explicit typed confirmation before changing the operating mode.' AS d) x
WHERE NOT EXISTS (SELECT 1 FROM wf_settings
                   WHERE setting_key='legacy_mode_requires_confirmation');

INSERT INTO wf_settings (setting_key, setting_value, value_type, description)
SELECT * FROM (SELECT
  'legacy_unknown_blocks_switch' AS k, '1' AS v, 'bool' AS t,
  'Block an operating mode change while any legacy group is still UNKNOWN.' AS d) x
WHERE NOT EXISTS (SELECT 1 FROM wf_settings
                   WHERE setting_key='legacy_unknown_blocks_switch');


-- ── E5 · SEGURIDAD DE INSTALACIÓN: nada queda llamando ───────────────────────
--
--  HALLAZGO de esta fase de corrección.
--
--  El seed de la FUNDACIÓN (001, que se conserva literal) deja encendidos el
--  país IN, los dos proveedores y las rutas IN_PROVEEDOR1 e IN_STRINGEE.
--  Reflejaba el estado de producción. Pero §64 y §77 de esta corrección son
--  explícitos: no encender rutas ni proveedores en silencio, y no activar nada
--  en este paquete. Tal cual, una instalación limpia quedaba en condiciones de
--  llamar en cuanto se importaran los workflows.
--
--  Se corrige aquí en vez de tocar 001, que está aprobado y va literal.
--
--  Se aplica UNA SOLA VEZ y sólo si la instalación está VIRGEN:
--    · es la primera vez que corre 005                  (marcador abajo)
--    · no hay ni una llamada en wf_call_jobs            (nunca despachó)
--    · no hay auditoría de rutas                        (nadie configuró nada)
--
--  En una instalación ya en uso NO hace nada: no apaga lo que alguien
--  encendió a propósito. Volver a encenderlo es un clic en el panel, y
--  queda auditado como cualquier otro cambio.
SET @M005 := '005_legacy_backup_v2:install_safety';

SET @first_run := (SELECT COUNT(*) = 0 FROM schema_migrations
                    WHERE migration_id = @M005);
SET @no_calls  := (SELECT COUNT(*) = 0 FROM wf_call_jobs);
SET @no_audit  := (SELECT COUNT(*) = 0 FROM route_audit);
SET @virgin    := (@first_run AND @no_calls AND @no_audit);

UPDATE call_routes      SET enabled = 0 WHERE @virgin AND enabled = 1;
UPDATE voice_providers  SET enabled = 0 WHERE @virgin AND enabled = 1;
UPDATE countries        SET enabled = 0 WHERE @virgin AND enabled = 1;

-- Queda escrito por qué el panel arranca todo apagado, para que nadie lo
-- lea como un fallo de la migración.
INSERT INTO route_audit (route_id, route_key, action, field, old_value,
                         new_value, actor)
SELECT NULL, NULL, 'INSTALL_SAFETY', 'enabled', '1', '0', 'migration-005'
 WHERE @virgin;

INSERT INTO schema_migrations (migration_id, notes)
SELECT @M005,
       'Fresh install left every country, provider and route disabled (rules 64 and 77).'
 WHERE @first_run
   AND NOT EXISTS (SELECT 1 FROM schema_migrations WHERE migration_id = @M005);

-- ┌────────────────────────────────────────────────────────────────────────┐
-- │ PARTE: 006_payments_v2.sql                                            │
-- └────────────────────────────────────────────────────────────────────────┘

-- ═══════════════════════════════════════════════════════════════════════════════
--  006 · PAGOS V2 — dos modos, ninguno inventado
--
--  Hallazgo que corrige (§25):
--    La versión anterior asumía que LeadStudio expone
--        POST /api/leads/{id}/payment-link
--    Nadie lo verificó. El WF7 REAL de producción NO lo usa: India cobra
--    directamente contra OkPay (api.wpay.one/v1/Collect) y Nepal está
--    marcado [DISABLED - CONFIGURE] en el propio workflow.
--    El endpoint se elimina. No se sustituye por otra suposición.
--
--  Los dos modos que sí existen:
--
--    DIRECT_PROVIDER   el país llama directo a su pasarela, con un
--                      adaptador soportado. Hoy: India → OKPAY_V1.
--
--    UNIVERSAL_ROUTER  una API central recibe la petición normalizada y
--                      decide ella qué pasarela usar. Puede vivir en el
--                      backend de Landmark, en LeadStudio o en otro
--                      cliente: es CONFIGURACIÓN, no está cableado.
--
--  Reglas:
--    §25 nunca hay fallback silencioso de un modo al otro
--    §25 la firma del proveedor vive en el adaptador, NUNCA como JS en la BD
--    §61 ninguna credencial en la base: sólo credential_ref
--
--  Requiere: 001 aplicado.
-- ═══════════════════════════════════════════════════════════════════════════════

SET @ok := (SELECT COUNT(*) FROM information_schema.TABLES
            WHERE TABLE_SCHEMA=DATABASE() AND TABLE_NAME='country_tool_configs');
SET @msg := IF(@ok=1, 'DO 0',
  'SELECT * FROM `ABORT_run_001_multi_country_config_v2_2_first`');
PREPARE s FROM @msg; EXECUTE s; DEALLOCATE PREPARE s;


-- ── F1 · el ENUM de modo admite los dos modos de pago ────────────────────────
-- Se AMPLÍA, no se sustituye: CONFIG_ROUTER y CUSTOM_ENDPOINT siguen siendo
-- válidos para CREATE_ACCOUNT y CALLBACK, que no cambian. Qué modo vale para
-- qué tool lo decide la aplicación (payments.py / routes_config.py), no el
-- ENUM — así añadir un modo no es una migración.
SET @cur := (SELECT COLUMN_TYPE FROM information_schema.COLUMNS
              WHERE TABLE_SCHEMA=DATABASE() AND TABLE_NAME='country_tool_configs'
                AND COLUMN_NAME='mode');
SET @ddl := IF(@cur LIKE '%DIRECT_PROVIDER%', 'DO 0',
  'ALTER TABLE country_tool_configs MODIFY mode
     ENUM(''CONFIG_ROUTER'',''CUSTOM_ENDPOINT'',''DIRECT_PROVIDER'',''UNIVERSAL_ROUTER'')
     NOT NULL DEFAULT ''CONFIG_ROUTER''');
PREPARE s FROM @ddl; EXECUTE s; DEALLOCATE PREPARE s;


-- ── F2 · columnas de pago ────────────────────────────────────────────────────
-- adapter_key : qué adaptador soportado usa el país en DIRECT_PROVIDER
-- router_key  : qué router usa en UNIVERSAL_ROUTER
-- Son dos campos distintos a propósito: un país no puede tener los dos, y
-- tenerlos separados hace imposible "caerse" de un modo al otro por accidente.
SET @c := (SELECT COUNT(*) FROM information_schema.COLUMNS
           WHERE TABLE_SCHEMA=DATABASE() AND TABLE_NAME='country_tool_configs'
             AND COLUMN_NAME='adapter_key');
SET @ddl := IF(@c=0,
  'ALTER TABLE country_tool_configs ADD COLUMN adapter_key VARCHAR(64) NULL AFTER provider_key',
  'DO 0');
PREPARE s FROM @ddl; EXECUTE s; DEALLOCATE PREPARE s;

SET @c := (SELECT COUNT(*) FROM information_schema.COLUMNS
           WHERE TABLE_SCHEMA=DATABASE() AND TABLE_NAME='country_tool_configs'
             AND COLUMN_NAME='router_key');
SET @ddl := IF(@c=0,
  'ALTER TABLE country_tool_configs ADD COLUMN router_key VARCHAR(64) NULL AFTER adapter_key',
  'DO 0');
PREPARE s FROM @ddl; EXECUTE s; DEALLOCATE PREPARE s;

-- URL a la que la pasarela avisa del pago. Es configuración por país porque
-- cada pasarela la registra de su lado.
SET @c := (SELECT COUNT(*) FROM information_schema.COLUMNS
           WHERE TABLE_SCHEMA=DATABASE() AND TABLE_NAME='country_tool_configs'
             AND COLUMN_NAME='callback_url');
SET @ddl := IF(@c=0,
  'ALTER TABLE country_tool_configs ADD COLUMN callback_url VARCHAR(255) NULL AFTER endpoint',
  'DO 0');
PREPARE s FROM @ddl; EXECUTE s; DEALLOCATE PREPARE s;

SET @c := (SELECT COUNT(*) FROM information_schema.COLUMNS
           WHERE TABLE_SCHEMA=DATABASE() AND TABLE_NAME='country_tool_configs'
             AND COLUMN_NAME='return_url');
SET @ddl := IF(@c=0,
  'ALTER TABLE country_tool_configs ADD COLUMN return_url VARCHAR(255) NULL AFTER callback_url',
  'DO 0');
PREPARE s FROM @ddl; EXECUTE s; DEALLOCATE PREPARE s;


-- ── F3 · pedidos de pago ─────────────────────────────────────────────────────
-- Una fila por link de pago pedido. Existe para tres cosas:
--   · idempotencia: out_trade_no es UNIQUE, así que pedir dos veces el mismo
--     link no crea dos cobros
--   · correlación: cuando llega el callback de la pasarela, hay contra qué casarlo
--   · analítica: PAYMENT_LINK_CREATED / PAYMENT_CONFIRMED salen de aquí
--
-- NO guarda credenciales ni firmas. Guarda el identificador del pedido, el de
-- la transacción del proveedor, y el estado.
CREATE TABLE IF NOT EXISTS wf_payment_orders (
  id               INT AUTO_INCREMENT PRIMARY KEY,
  out_trade_no     VARCHAR(64)  NOT NULL,
  lead_id          VARCHAR(64)  NOT NULL,
  country_iso      CHAR(2)      NOT NULL,
  mode             VARCHAR(24)  NOT NULL,
  adapter_key      VARCHAR(64)  NULL,
  router_key       VARCHAR(64)  NULL,
  amount           DECIMAL(14,2) NOT NULL,
  currency         CHAR(3)      NOT NULL,
  state            VARCHAR(24)  NOT NULL DEFAULT 'REQUESTED',
  provider_txn_id  VARCHAR(128) NULL,
  checkout_url     VARCHAR(512) NULL,
  delivery         VARCHAR(16)  NULL,
  error_class      VARCHAR(32)  NULL,
  error_detail     VARCHAR(255) NULL,
  call_job_id      VARCHAR(64)  NULL,
  requested_at     DATETIME     NOT NULL DEFAULT CURRENT_TIMESTAMP,
  confirmed_at     DATETIME     NULL,
  updated_at       DATETIME     NOT NULL DEFAULT CURRENT_TIMESTAMP
                                ON UPDATE CURRENT_TIMESTAMP,
  UNIQUE KEY uq_out_trade_no (out_trade_no),
  KEY idx_lead (lead_id),
  KEY idx_state (state, requested_at),
  KEY idx_country (country_iso),
  KEY idx_txn (provider_txn_id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;


-- ── F4 · configuración de pago de India: OkPay, tal como funciona hoy ────────
-- Se siembra SOLO si India no tiene ya configurado CREATE_PAYMENT_LINK.
--
-- NO se siembran ni mchId ni la clave de firma. En el WF7 de producción la
-- clave de firma viaja EN CLARO dentro del JSON del workflow; copiarla aquí
-- la metería además en la base de datos. §61 lo prohíbe y con razón: quedaría
-- en los backups, en los volcados y en cada copia del paquete.
-- El merchant id y la clave se configuran como credencial de n8n
-- (credential_ref -> OKPAY_API) y el panel nunca los ve.
INSERT INTO country_tool_configs
  (country_iso, tool_type, enabled, mode, provider_key, adapter_key,
   endpoint, callback_url, return_url, http_method, credential_ref,
   currency, config_json, notes)
SELECT * FROM (SELECT
  'IN' AS a, 'CREATE_PAYMENT_LINK' AS b, 0 AS c, 'DIRECT_PROVIDER' AS d,
  'okpay' AS e, 'OKPAY_V1' AS f,
  'https://api.wpay.one/v1/Collect' AS g,
  NULL AS h, NULL AS i, 'POST' AS j, 'OKPAY_API' AS k, 'INR' AS l,
  '{"pay_type":"UPI"}' AS m,
  'Verified against the live WF7. Set callback_url, return_url and the OKPAY_API credential before enabling.' AS n) x
WHERE NOT EXISTS (SELECT 1 FROM country_tool_configs
                   WHERE country_iso='IN' AND tool_type='CREATE_PAYMENT_LINK');

-- Nepal: el WF7 real lo tiene [DISABLED - CONFIGURE]. Se refleja tal cual:
-- deshabilitado y sin adaptador. Deshabilitado NO puede bloquear llamar ni
-- abrir cuentas en Nepal (§25).
INSERT INTO country_tool_configs
  (country_iso, tool_type, enabled, mode, provider_key, adapter_key,
   endpoint, http_method, credential_ref, currency, notes)
SELECT * FROM (SELECT
  'NP' AS a, 'CREATE_PAYMENT_LINK' AS b, 0 AS c, 'DIRECT_PROVIDER' AS d,
  NULL AS e, NULL AS f, NULL AS g, NULL AS h, NULL AS i, 'NPR' AS j,
  'Not configured. The live WF7 marks the Nepal payment branch as DISABLED - CONFIGURE.' AS k) x
WHERE NOT EXISTS (SELECT 1 FROM country_tool_configs
                   WHERE country_iso='NP' AND tool_type='CREATE_PAYMENT_LINK');


-- ── F4b · CORRECCIÓN de lo que sembró la fundación ───────────────────────────
--
--  HALLAZGO de esta fase.
--
--  El seed de 001 (que va literal, aprobado) dejó CREATE_PAYMENT_LINK así:
--      IN  enabled=1  mode=CONFIG_ROUTER  provider_key='okpay'
--      NP  enabled=0  mode=CONFIG_ROUTER  provider_key='monetix'
--
--  Dos problemas:
--    1. CONFIG_ROUTER para pagos significaba el endpoint inventado
--       POST /api/leads/{id}/payment-link. §25 lo elimina.
--    2. India quedaba ENCENDIDA apuntando a ese endpoint inexistente. Un
--       agente pidiendo un link de pago se habría ido contra una URL que
--       no existe, y el cliente se habría quedado esperando un cobro.
--
--  Se corrige UNA sola vez y sólo sobre filas que siguen tal cual las
--  sembró la fundación. Si alguien ya las editó, no se tocan (§64).
SET @M006 := '006_payments_v2:migrate_seeded_payment_mode';
SET @first_run := (SELECT COUNT(*) = 0 FROM schema_migrations
                    WHERE migration_id = @M006);

-- India → DIRECT_PROVIDER / OKPAY_V1, y APAGADA hasta que tenga credencial
-- y callback. Apagar es la única opción honesta: sin la clave de firma no
-- puede cobrar, y dejarla encendida haría fallar al agente delante del cliente.
UPDATE country_tool_configs
   SET mode        = 'DIRECT_PROVIDER',
       adapter_key = 'OKPAY_V1',
       endpoint    = COALESCE(NULLIF(endpoint,''), 'https://api.wpay.one/v1/Collect'),
       http_method = COALESCE(NULLIF(http_method,''), 'POST'),
       credential_ref = COALESCE(NULLIF(credential_ref,''), 'OKPAY_API'),
       enabled     = 0,
       notes       = 'Migrated to DIRECT_PROVIDER/OKPAY_V1 by migration 006. Disabled until the OKPAY_API credential and callback_url are set.'
 WHERE @first_run
   AND country_iso = 'IN' AND tool_type = 'CREATE_PAYMENT_LINK'
   AND mode = 'CONFIG_ROUTER'
   AND provider_key = 'okpay';

-- Nepal → DIRECT_PROVIDER sin adaptador: no hay uno soportado para Monetix.
-- Queda NOT READY, que es lo cierto, en vez de CONFIG_ROUTER, que era falso.
UPDATE country_tool_configs
   SET mode        = 'DIRECT_PROVIDER',
       adapter_key = NULL,
       enabled     = 0,
       notes       = 'Monetix has no supported adapter yet. The live WF7 marks this branch DISABLED - CONFIGURE.'
 WHERE @first_run
   AND country_iso = 'NP' AND tool_type = 'CREATE_PAYMENT_LINK'
   AND mode = 'CONFIG_ROUTER';

INSERT INTO route_audit (route_id, route_key, action, field, old_value,
                         new_value, actor)
SELECT NULL, NULL, 'PAYMENT_MODE_MIGRATION', 'mode', 'CONFIG_ROUTER',
       'DIRECT_PROVIDER', 'migration-006'
 WHERE @first_run;

INSERT INTO schema_migrations (migration_id, notes)
SELECT @M006,
       'Payment tools moved off the unverified CONFIG_ROUTER endpoint; India disabled until credentials are set.'
 WHERE @first_run
   AND NOT EXISTS (SELECT 1 FROM schema_migrations WHERE migration_id = @M006);


-- ── F5 · ajustes ─────────────────────────────────────────────────────────────
INSERT INTO wf_settings (setting_key, setting_value, value_type, description)
SELECT * FROM (SELECT
  'payment_order_ttl_minutes' AS k, '1440' AS v, 'int' AS t,
  'Minutes a REQUESTED payment order waits for a provider callback before reconciliation opens an issue.' AS d) x
WHERE NOT EXISTS (SELECT 1 FROM wf_settings WHERE setting_key='payment_order_ttl_minutes');

INSERT INTO wf_settings (setting_key, setting_value, value_type, description)
SELECT * FROM (SELECT
  'payment_sms_enabled' AS k, '1' AS v, 'bool' AS t,
  'Send the payment link by SMS after it is created.' AS d) x
WHERE NOT EXISTS (SELECT 1 FROM wf_settings WHERE setting_key='payment_sms_enabled');

