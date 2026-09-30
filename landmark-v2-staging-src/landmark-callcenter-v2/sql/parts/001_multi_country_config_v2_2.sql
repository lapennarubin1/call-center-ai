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
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;


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
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

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
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

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
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;


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
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

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
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

CREATE TABLE IF NOT EXISTS route_telegram_targets (
  id         INT AUTO_INCREMENT PRIMARY KEY,
  route_id   INT NOT NULL,
  purpose    ENUM('recording','account','payment','alert') NOT NULL,
  chat_id    VARCHAR(32) NOT NULL,
  enabled    TINYINT NOT NULL DEFAULT 1,
  created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
  UNIQUE KEY uq_route_purpose_chat (route_id, purpose, chat_id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

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
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;


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
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;


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
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

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
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;


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
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;


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
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;


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
