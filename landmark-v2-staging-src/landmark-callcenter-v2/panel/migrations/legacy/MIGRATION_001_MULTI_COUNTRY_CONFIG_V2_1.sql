-- ═══════════════════════════════════════════════════════════════════════════════
--  MIGRATION_001_MULTI_COUNTRY_CONFIG_V2_1.sql
--  Landmark Markets · Template V2.1 · Fundación de configuración multi-país
--
--  ESTADO: PROPUESTA — NO EJECUTADA EN PRODUCCIÓN
--  Reemplaza a MIGRATION_001_MULTI_COUNTRY_CONFIG.sql (V2), que NUNCA se aplicó
--  en ningún entorno real y tenía cuatro defectos confirmados en MariaDB 10.11:
--    1. re-ejecutarla sobrescribía capacity_default editado desde el panel
--    2. re-ejecutarla sobrescribía notes
--    3. re-ejecutarla REACTIVABA destinos de Telegram deshabilitados
--    4. el upgrade parcial creaba route_key='' en varias filas y el UNIQUE
--       fallaba con ERROR 1062, dejando la migración cortada a medias
--
--  GARANTÍAS
--    · Idempotente de verdad: la 2ª ejecución es NO-OP funcional.
--    · El SEED corre UNA sola vez en la vida de la base, protegido por el
--      marcador '001_multi_country_config_v2_1:seed' en schema_migrations.
--      Después de eso, nada de lo que edites desde el panel vuelve a pisarse:
--      ni capacidades, ni agentes, ni Telegram (tampoco resucita un destino que
--      borraste), ni políticas, ni tools.
--    · Además cada INSERT del seed chequea su clave natural: si una ejecución
--      se corta a mitad del seed, la siguiente completa solo lo que faltó.
--    · Sin DROP, sin TRUNCATE, sin DELETE ejecutables.
--    · No altera ninguna tabla productiva existente.
--    · Upgrade parcial seguro: route_key se agrega NULL, se rellena con valores
--      únicos deterministas y recién después se crea el UNIQUE y el NOT NULL.
--
--  ESCENARIOS SOPORTADOS (todos probados en MariaDB 10.11 — ver TEST_PLAN)
--    A · base limpia
--    B · re-ejecución sobre V2.1 completa           → no-op
--    C · re-ejecución tras ediciones del panel      → no pisa nada
--    D · ejecución interrumpida y reanudada         → completa lo faltante
--    E · call_routes previa SIN route_key, N filas  → backfill único + UNIQUE
--    F · base con la V2 aplicada (solo existió en pruebas)
--
--  TABLAS
--    schema_migrations        ledger de migraciones y del seed
--    countries                NUEVA · datos de país (prefijo, huso, idioma)
--    voice_providers          proveedores de voz contratados
--    followup_policies        NUEVA · políticas reutilizables por clave
--    call_routes              país × proveedor, varias por país permitidas
--    route_capacity_windows   franjas horarias con capacidad
--    route_telegram_targets   destinos Telegram por ruta y propósito
--    route_audit              auditoría
--    country_tool_configs     NUEVA · CREATE_ACCOUNT / CREATE_PAYMENT_LINK / CALLBACK
--    wf_call_jobs             NUEVA · claim atómico de despacho, UNIQUE(lead_id, attempt)
--    wf_conversation_ledger   idempotencia post-call, PK conversation_id
-- ═══════════════════════════════════════════════════════════════════════════════

SET @MIG  = '001_multi_country_config_v2_1';
SET @SEED = '001_multi_country_config_v2_1:seed';

-- ───────────────────────────────────────────────────────────────────────────────
-- 0 · LEDGER
-- ───────────────────────────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS schema_migrations (
  migration_id VARCHAR(128) NOT NULL PRIMARY KEY,
  applied_at   DATETIME     NOT NULL DEFAULT CURRENT_TIMESTAMP,
  notes        VARCHAR(255) NULL
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;


-- ───────────────────────────────────────────────────────────────────────────────
-- 1 · COUNTRIES
--     El huso horario es del PAÍS: las franjas de todas sus rutas se
--     interpretan en esa zona. Tenerlo por ruta permitía que IN_PROVEEDOR1 e
--     IN_STRINGEE interpretaran "09:00" distinto.
-- ───────────────────────────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS countries (
  iso                 CHAR(2)      NOT NULL PRIMARY KEY,
  country_name        VARCHAR(64)  NOT NULL,
  dial_prefix         VARCHAR(8)   NOT NULL,
  national_number_len TINYINT      NULL,
  timezone            VARCHAR(64)  NOT NULL,
  language            VARCHAR(16)  NOT NULL DEFAULT 'en',
  archived_at         DATETIME     NULL,
  notes               VARCHAR(255) NULL,
  created_at          DATETIME     NOT NULL DEFAULT CURRENT_TIMESTAMP,
  updated_at          DATETIME     NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;


-- ───────────────────────────────────────────────────────────────────────────────
-- 2 · VOICE_PROVIDERS
--     Un proveedor SOLO LLAMA. Asterisk no aparece: es gateway.
--     Sin credenciales. endpoint es la URL del adapter cuando aplica.
-- ───────────────────────────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS voice_providers (
  id            INT AUTO_INCREMENT PRIMARY KEY,
  code          VARCHAR(32)  NOT NULL,
  display_name  VARCHAR(64)  NOT NULL,
  provider_kind ENUM('sip','stringee') NOT NULL,
  endpoint      VARCHAR(255) NULL,
  account_ref   VARCHAR(64)  NULL,
  enabled       TINYINT      NOT NULL DEFAULT 1,
  notes         VARCHAR(255) NULL,
  created_at    DATETIME     NOT NULL DEFAULT CURRENT_TIMESTAMP,
  updated_at    DATETIME     NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
  UNIQUE KEY uq_provider_code (code)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;


-- ───────────────────────────────────────────────────────────────────────────────
-- 3 · FOLLOWUP_POLICIES
--     La política NO pertenece al proveedor. Varias rutas comparten una.
--     policy_json tiene reglas explícitas (result, attempt) → acción.
--     Sin módulo, sin %3, sin "if provider", sin "if India".
-- ───────────────────────────────────────────────────────────────────────────────
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


-- ───────────────────────────────────────────────────────────────────────────────
-- 4 · CALL_ROUTES
--     Un país puede tener VARIAS rutas activas a la vez (IN_PROVEEDOR1 +
--     IN_STRINGEE). No hay exclusividad país-proveedor: por eso ya NO existe
--     UNIQUE(iso, provider_id).
--     Sin hard delete: archived_at. Un route_key archivado sigue resolviendo
--     para el post-call que llega minutos después, y no se reutiliza jamás
--     (lo garantiza el UNIQUE sobre route_key, que incluye las archivadas).
-- ───────────────────────────────────────────────────────────────────────────────
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
  KEY idx_enabled (enabled),
  KEY idx_archived (archived_at)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;


-- ── 4b · UPGRADE SEGURO DE call_routes PREEXISTENTE ────────────────────────────
-- Cada paso chequea information_schema y solo actúa si hace falta.
-- Patrón: SET @ddl := IF(condición, 'ALTER …', 'DO 0'); PREPARE; EXECUTE.

-- route_key: se agrega NULL (NUNCA '' con NOT NULL + DEFAULT, que era el bug V2)
SET @c := (SELECT COUNT(*) FROM information_schema.COLUMNS
           WHERE TABLE_SCHEMA=DATABASE() AND TABLE_NAME='call_routes' AND COLUMN_NAME='route_key');
SET @ddl := IF(@c=0, 'ALTER TABLE call_routes ADD COLUMN route_key VARCHAR(64) NULL AFTER id', 'DO 0');
PREPARE s FROM @ddl; EXECUTE s; DEALLOCATE PREPARE s;

-- backfill determinista: ISO_PROVEEDOR para cada fila sin clave
UPDATE call_routes r
LEFT JOIN voice_providers p ON p.id = r.provider_id
   SET r.route_key = UPPER(CONCAT(r.iso, '_', COALESCE(p.code, CONCAT('ROUTE', r.id))))
 WHERE r.route_key IS NULL OR r.route_key = '';

-- desempate: si dos filas quedaron con la misma clave, todas menos la de menor
-- id reciben el sufijo _<id>. La tabla derivada evita el error 1093.
UPDATE call_routes r
JOIN (SELECT route_key, MIN(id) AS keep_id FROM call_routes GROUP BY route_key) d
  ON d.route_key = r.route_key
   SET r.route_key = CONCAT(r.route_key, '_', r.id)
 WHERE r.id <> d.keep_id;

-- recién ahora, con valores únicos garantizados, el UNIQUE
SET @c := (SELECT COUNT(*) FROM information_schema.STATISTICS
           WHERE TABLE_SCHEMA=DATABASE() AND TABLE_NAME='call_routes' AND INDEX_NAME='uq_route_key');
SET @ddl := IF(@c=0, 'ALTER TABLE call_routes ADD UNIQUE KEY uq_route_key (route_key)', 'DO 0');
PREPARE s FROM @ddl; EXECUTE s; DEALLOCATE PREPARE s;

-- y el NOT NULL, solo si todavía es nullable
SET @c := (SELECT COUNT(*) FROM information_schema.COLUMNS
           WHERE TABLE_SCHEMA=DATABASE() AND TABLE_NAME='call_routes'
             AND COLUMN_NAME='route_key' AND IS_NULLABLE='YES');
SET @ddl := IF(@c=1, 'ALTER TABLE call_routes MODIFY route_key VARCHAR(64) NOT NULL', 'DO 0');
PREPARE s FROM @ddl; EXECUTE s; DEALLOCATE PREPARE s;

-- columnas nuevas de V2.1 sobre una call_routes anterior
SET @c := (SELECT COUNT(*) FROM information_schema.COLUMNS
           WHERE TABLE_SCHEMA=DATABASE() AND TABLE_NAME='call_routes' AND COLUMN_NAME='archived_at');
SET @ddl := IF(@c=0, 'ALTER TABLE call_routes ADD COLUMN archived_at DATETIME NULL', 'DO 0');
PREPARE s FROM @ddl; EXECUTE s; DEALLOCATE PREPARE s;

SET @c := (SELECT COUNT(*) FROM information_schema.COLUMNS
           WHERE TABLE_SCHEMA=DATABASE() AND TABLE_NAME='call_routes' AND COLUMN_NAME='followup_policy_id');
SET @ddl := IF(@c=0, 'ALTER TABLE call_routes ADD COLUMN followup_policy_id INT NULL', 'DO 0');
PREPARE s FROM @ddl; EXECUTE s; DEALLOCATE PREPARE s;

SET @c := (SELECT COUNT(*) FROM information_schema.COLUMNS
           WHERE TABLE_SCHEMA=DATABASE() AND TABLE_NAME='call_routes' AND COLUMN_NAME='recording_upload_crm');
SET @ddl := IF(@c=0, 'ALTER TABLE call_routes ADD COLUMN recording_upload_crm TINYINT NOT NULL DEFAULT 1', 'DO 0');
PREPARE s FROM @ddl; EXECUTE s; DEALLOCATE PREPARE s;

SET @c := (SELECT COUNT(*) FROM information_schema.COLUMNS
           WHERE TABLE_SCHEMA=DATABASE() AND TABLE_NAME='call_routes' AND COLUMN_NAME='recording_telegram');
SET @ddl := IF(@c=0, 'ALTER TABLE call_routes ADD COLUMN recording_telegram TINYINT NOT NULL DEFAULT 1', 'DO 0');
PREPARE s FROM @ddl; EXECUTE s; DEALLOCATE PREPARE s;

SET @c := (SELECT COUNT(*) FROM information_schema.STATISTICS
           WHERE TABLE_SCHEMA=DATABASE() AND TABLE_NAME='call_routes' AND INDEX_NAME='idx_archived');
SET @ddl := IF(@c=0, 'ALTER TABLE call_routes ADD KEY idx_archived (archived_at)', 'DO 0');
PREPARE s FROM @ddl; EXECUTE s; DEALLOCATE PREPARE s;

SET @c := (SELECT COUNT(*) FROM information_schema.STATISTICS
           WHERE TABLE_SCHEMA=DATABASE() AND TABLE_NAME='call_routes' AND INDEX_NAME='idx_iso');
SET @ddl := IF(@c=0, 'ALTER TABLE call_routes ADD KEY idx_iso (iso)', 'DO 0');
PREPARE s FROM @ddl; EXECUTE s; DEALLOCATE PREPARE s;

-- ── 4c · UPGRADE DESDE V2 (columnas de país que ahora viven en countries) ─────
-- V2 guardaba country_name / dial_prefix / timezone en la ruta, NOT NULL sin
-- default. Si se dejaran así, el panel V2.1 no podría insertar rutas nuevas.
-- No se borran (sin DROP): se vuelven NULL-ables y quedan como legado.

-- 4c.1 · rellenar countries desde las rutas V2, solo si esas columnas existen
SET @c := (SELECT COUNT(*) FROM information_schema.COLUMNS
           WHERE TABLE_SCHEMA=DATABASE() AND TABLE_NAME='call_routes' AND COLUMN_NAME='timezone');
SET @ddl := IF(@c=1,
  'INSERT INTO countries (iso, country_name, dial_prefix, national_number_len, timezone, language)
   SELECT r.iso, MIN(r.country_name), MIN(r.dial_prefix), MIN(r.national_number_len),
          MIN(r.timezone), MIN(r.language)
     FROM call_routes r
    WHERE r.iso NOT IN (SELECT iso FROM countries)
      AND r.timezone IS NOT NULL AND r.dial_prefix IS NOT NULL
    GROUP BY r.iso',
  'DO 0');
PREPARE s FROM @ddl; EXECUTE s; DEALLOCATE PREPARE s;

-- 4c.2 · relajar NOT NULL de las columnas heredadas (una por una, si existen y son NOT NULL)
SET @c := (SELECT COUNT(*) FROM information_schema.COLUMNS WHERE TABLE_SCHEMA=DATABASE()
           AND TABLE_NAME='call_routes' AND COLUMN_NAME='country_name' AND IS_NULLABLE='NO');
SET @ddl := IF(@c=1, 'ALTER TABLE call_routes MODIFY country_name VARCHAR(64) NULL', 'DO 0');
PREPARE s FROM @ddl; EXECUTE s; DEALLOCATE PREPARE s;

SET @c := (SELECT COUNT(*) FROM information_schema.COLUMNS WHERE TABLE_SCHEMA=DATABASE()
           AND TABLE_NAME='call_routes' AND COLUMN_NAME='dial_prefix' AND IS_NULLABLE='NO');
SET @ddl := IF(@c=1, 'ALTER TABLE call_routes MODIFY dial_prefix VARCHAR(8) NULL', 'DO 0');
PREPARE s FROM @ddl; EXECUTE s; DEALLOCATE PREPARE s;

SET @c := (SELECT COUNT(*) FROM information_schema.COLUMNS WHERE TABLE_SCHEMA=DATABASE()
           AND TABLE_NAME='call_routes' AND COLUMN_NAME='timezone' AND IS_NULLABLE='NO');
SET @ddl := IF(@c=1, 'ALTER TABLE call_routes MODIFY timezone VARCHAR(64) NULL', 'DO 0');
PREPARE s FROM @ddl; EXECUTE s; DEALLOCATE PREPARE s;

-- NOTA upgrade desde V2: V2 tenía UNIQUE(iso, provider_id). V2.1 lo elimina del
-- diseño (varias rutas del mismo par deben poder coexistir, sobre todo tras un
-- archivado). Como esta migración no ejecuta DROP, en una base V2 ese índice
-- permanece. V2 solo existió en bases de prueba: recrearlas desde cero es lo
-- recomendado. PENDING_VERIFICATION: confirmar que ninguna base real tiene V2.


-- ───────────────────────────────────────────────────────────────────────────────
-- 5 · ROUTE_CAPACITY_WINDOWS
--     Horas en hora LOCAL del país. end < start ⇒ cruza medianoche, y el tramo
--     posterior a las 00:00 pertenece al día en que la franja EMPIEZA.
-- ───────────────────────────────────────────────────────────────────────────────
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


-- ───────────────────────────────────────────────────────────────────────────────
-- 6 · ROUTE_TELEGRAM_TARGETS
-- ───────────────────────────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS route_telegram_targets (
  id         INT AUTO_INCREMENT PRIMARY KEY,
  route_id   INT NOT NULL,
  purpose    ENUM('recording','account','payment','alert') NOT NULL,
  chat_id    VARCHAR(32) NOT NULL,
  enabled    TINYINT NOT NULL DEFAULT 1,
  created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
  UNIQUE KEY uq_route_purpose_chat (route_id, purpose, chat_id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;


-- ───────────────────────────────────────────────────────────────────────────────
-- 7 · ROUTE_AUDIT
-- ───────────────────────────────────────────────────────────────────────────────
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


-- ───────────────────────────────────────────────────────────────────────────────
-- 8 · COUNTRY_TOOL_CONFIGS
--     Tools de ElevenLabs por país. Dos modos:
--       CONFIG_ROUTER   → endpoint universal nuestro; la config resuelve el resto
--       CUSTOM_ENDPOINT → API propia del país
--     credential_ref es un NOMBRE lógico de credential de n8n. Nunca el secreto.
--     Una tool disabled no bloquea llamadas; solo se valida si está enabled.
-- ───────────────────────────────────────────────────────────────────────────────
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


-- ───────────────────────────────────────────────────────────────────────────────
-- 9 · WF_CALL_JOBS — claim atómico de despacho
--
--     Resuelve la race condition: dos rutas del mismo país leen el mismo lead
--     en NOT_CONTACTED antes de que ninguna cambie su estado.
--
--     UNIQUE(lead_id, attempt): para un mismo intento de un mismo lead, gana
--     UN solo INSERT, sin importar cuántos workers o rutas compitan.
--
--     Flujo WF2 (futuro):
--       INSERT IGNORE … state='CLAIMED'
--       SELECT call_job_id WHERE lead_id=? AND attempt=?
--         → es MI call_job_id: gano · otro: SKIP
--       (NO usar affectedRows: con el driver mysql2 de n8n su significado
--        depende del flag FOUND_ROWS)
--       PATCH LeadStudio → ATTEMPTING
--       state='DISPATCHING'  (ANTES de llamar al proveedor)
--       dispatch
--       state='DISPATCHED' + ids  |  'FAILED' + error
--
--     Un job en DISPATCHING sin respuesta registrada significa que n8n pudo
--     caerse DESPUÉS de mandar la llamada: pasa a UNKNOWN/NEEDS_RECONCILIATION.
--     NUNCA se re-despacha automáticamente.
-- ───────────────────────────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS wf_call_jobs (
  id                   BIGINT AUTO_INCREMENT PRIMARY KEY,
  call_job_id          VARCHAR(96)  NOT NULL,
  lead_id              VARCHAR(64)  NOT NULL,
  route_id             INT          NOT NULL,
  route_key            VARCHAR(64)  NOT NULL,
  country_iso          CHAR(2)      NOT NULL,
  provider             VARCHAR(32)  NOT NULL,
  attempt              INT          NOT NULL,
  execution_id         VARCHAR(64)  NULL,
  state                ENUM('CLAIMED','DISPATCHING','DISPATCHED','UNKNOWN','FAILED',
                            'COMPLETED','NEEDS_RECONCILIATION') NOT NULL DEFAULT 'CLAIMED',
  conversation_id      VARCHAR(128) NULL,
  provider_job_id      VARCHAR(128) NULL,
  provider_call_id     VARCHAR(128) NULL,
  dispatch_http_status INT          NULL,
  error_code           VARCHAR(32)  NULL,
  error_message        VARCHAR(255) NULL,
  created_at           DATETIME     NOT NULL DEFAULT CURRENT_TIMESTAMP,
  claimed_at           DATETIME     NOT NULL DEFAULT CURRENT_TIMESTAMP,
  dispatched_at        DATETIME     NULL,
  completed_at         DATETIME     NULL,
  updated_at           DATETIME     NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
  UNIQUE KEY uq_call_job_id (call_job_id),
  UNIQUE KEY uq_lead_attempt (lead_id, attempt),
  KEY idx_state (state),
  KEY idx_route (route_key),
  KEY idx_conversation (conversation_id),
  KEY idx_provider_job (provider_job_id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;


-- ───────────────────────────────────────────────────────────────────────────────
-- 10 · WF_CONVERSATION_LEDGER — idempotencia post-call
--
--      conversation_id es la PK. Webhook y polling compiten por el INSERT;
--      gana uno. Sobrevive restart, varios workers y concurrencia.
--
--      NO hay reintento ciego de CLAIMED viejo: si el POST /followups se hizo
--      y n8n cayó antes de guardar followup_id, reintentar podría duplicar el
--      followup. Pasa a NEEDS_RECONCILIATION hasta tener un lookup verificable
--      en LeadStudio (PENDING_VERIFICATION).
-- ───────────────────────────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS wf_conversation_ledger (
  conversation_id VARCHAR(128) NOT NULL PRIMARY KEY,
  call_job_id     VARCHAR(96)  NULL,
  route_key       VARCHAR(64)  NULL,
  lead_id         VARCHAR(64)  NULL,
  provider        VARCHAR(32)  NULL,
  attempt         INT          NULL,
  source          ENUM('webhook','polling','dispatch') NOT NULL,
  claim_token     VARCHAR(64)  NULL,   -- el ganador se verifica releyendo este token
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

-- upgrade desde V2: el ledger V2 tenía estados CLAIMED/DONE/FAILED/SKIPPED.
-- Se amplía el ENUM conservando los viejos (sin pérdida), luego DONE → PROCESSED.
SET @c := (SELECT COUNT(*) FROM information_schema.COLUMNS WHERE TABLE_SCHEMA=DATABASE()
           AND TABLE_NAME='wf_conversation_ledger' AND COLUMN_NAME='state'
           AND COLUMN_TYPE NOT LIKE '%NEEDS_RECONCILIATION%');
SET @ddl := IF(@c=1,
  'ALTER TABLE wf_conversation_ledger MODIFY state
     ENUM(''NEW'',''CLAIMED'',''PROCESSED'',''NEEDS_RECONCILIATION'',''FAILED'',''DONE'',''SKIPPED'')
     NOT NULL DEFAULT ''CLAIMED''',
  'DO 0');
PREPARE s FROM @ddl; EXECUTE s; DEALLOCATE PREPARE s;

SET @c := (SELECT COUNT(*) FROM information_schema.COLUMNS WHERE TABLE_SCHEMA=DATABASE()
           AND TABLE_NAME='wf_conversation_ledger' AND COLUMN_NAME='call_job_id');
SET @ddl := IF(@c=0, 'ALTER TABLE wf_conversation_ledger ADD COLUMN call_job_id VARCHAR(96) NULL', 'DO 0');
PREPARE s FROM @ddl; EXECUTE s; DEALLOCATE PREPARE s;

SET @c := (SELECT COUNT(*) FROM information_schema.COLUMNS WHERE TABLE_SCHEMA=DATABASE()
           AND TABLE_NAME='wf_conversation_ledger' AND COLUMN_NAME='claim_token');
SET @ddl := IF(@c=0, 'ALTER TABLE wf_conversation_ledger ADD COLUMN claim_token VARCHAR(64) NULL AFTER source', 'DO 0');
PREPARE s FROM @ddl; EXECUTE s; DEALLOCATE PREPARE s;

SET @c := (SELECT COUNT(*) FROM information_schema.COLUMNS WHERE TABLE_SCHEMA=DATABASE()
           AND TABLE_NAME='wf_conversation_ledger' AND COLUMN_NAME='source'
           AND COLUMN_TYPE NOT LIKE '%dispatch%');
SET @ddl := IF(@c=1, 'ALTER TABLE wf_conversation_ledger MODIFY source ENUM(''webhook'',''polling'',''dispatch'') NOT NULL', 'DO 0');
PREPARE s FROM @ddl; EXECUTE s; DEALLOCATE PREPARE s;

SET @c := (SELECT COUNT(*) FROM information_schema.COLUMNS WHERE TABLE_SCHEMA=DATABASE()
           AND TABLE_NAME='wf_conversation_ledger' AND COLUMN_NAME='result');
SET @ddl := IF(@c=0, 'ALTER TABLE wf_conversation_ledger ADD COLUMN result VARCHAR(32) NULL', 'DO 0');
PREPARE s FROM @ddl; EXECUTE s; DEALLOCATE PREPARE s;

UPDATE wf_conversation_ledger SET state = 'PROCESSED' WHERE state = 'DONE';


-- ═══════════════════════════════════════════════════════════════════════════════
--  SEED — corre UNA SOLA VEZ en la vida de la base.
--
--  Dos guardas en cada INSERT:
--    (1) NOT EXISTS marcador @SEED  → tras la primera ejecución completa, nunca más
--    (2) NOT EXISTS clave natural   → si se cortó a mitad, completa lo faltante
--
--  Valores = lo que HOY está hardcodeado en los JSON de n8n (fuente de verdad).
-- ═══════════════════════════════════════════════════════════════════════════════

-- ── S1 · países ───────────────────────────────────────────────────────────────
-- IN: WF2 COUNTRY_CFG.india (+91, hi) · WF3 usa Asia/Kolkata en Telegram
-- NP: WF2 COUNTRY_CFG.nepal (+977, hi)
INSERT INTO countries (iso, country_name, dial_prefix, national_number_len, timezone, language, notes)
SELECT 'IN','India','+91',10,'Asia/Kolkata','hi','Seed V2.1 desde WF2 COUNTRY_CFG.india'
FROM DUAL
WHERE NOT EXISTS (SELECT 1 FROM schema_migrations WHERE migration_id=@SEED)
  AND NOT EXISTS (SELECT 1 FROM countries WHERE iso='IN');

INSERT INTO countries (iso, country_name, dial_prefix, national_number_len, timezone, language, notes)
SELECT 'NP','Nepal','+977',NULL,'Asia/Kathmandu','hi','Seed V2.1 desde WF2 COUNTRY_CFG.nepal'
FROM DUAL
WHERE NOT EXISTS (SELECT 1 FROM schema_migrations WHERE migration_id=@SEED)
  AND NOT EXISTS (SELECT 1 FROM countries WHERE iso='NP');


-- ── S2 · proveedores ──────────────────────────────────────────────────────────
INSERT INTO voice_providers (code, display_name, provider_kind, endpoint, account_ref, enabled, notes)
SELECT 'proveedor1','Proveedor SIP 1','sip',NULL,'650098',1,
       'Trunk SIP con rutas por pais. Sale por Asterisk (gateway).'
FROM DUAL
WHERE NOT EXISTS (SELECT 1 FROM schema_migrations WHERE migration_id=@SEED)
  AND NOT EXISTS (SELECT 1 FROM voice_providers WHERE code='proveedor1');

INSERT INTO voice_providers (code, display_name, provider_kind, endpoint, account_ref, enabled, notes)
SELECT 'stringee','Stringee','stringee','http://172.18.0.1:8091',NULL,1,
       'Worker propio. endpoint desde WF2 POST Stringee Worker.'
FROM DUAL
WHERE NOT EXISTS (SELECT 1 FROM schema_migrations WHERE migration_id=@SEED)
  AND NOT EXISTS (SELECT 1 FROM voice_providers WHERE code='stringee');


-- ── S3 · política STANDARD_CALL_RETRY ─────────────────────────────────────────
-- Reproduce WF2 "🔍 Classify Dial Result (Asterisk)1" regla por regla:
--   intento 1 +2h · 2 +3h · 3 +2 días hábiles · 4 +2h · 5 +3h · 6 +3 días hábiles
--   7 +2h · 8 +3h · 9 CLOSE
-- Explícito, sin módulo. Lo que v1 NO define queda como decisión pendiente:
--   · FAILED y UNKNOWN: sin regla → unmatched_action = NONE (se registra, no se
--     reprograma). En v1 WF2 los trataba como UNREACHABLE sin reintento y WF9
--     como NO_ANSWER: los dos workflows se contradecían. DECISIÓN PENDIENTE.
--   · BUSY y VOICEMAIL → alias de NO_ANSWER (WF9 v1 mapeaba VOICEMAIL→NO_ANSWER).
INSERT INTO followup_policies (policy_key, name, description, policy_json)
SELECT 'STANDARD_CALL_RETRY', 'Standard call retry (v1 proveedor1)',
       'Reproduce el ciclo de WF2 v1: 2h/3h/2bd, 2h/3h/3bd, 2h/3h, CLOSE al 9.',
       '{"policy_version":1,"no_answer_sip_codes":["603","408","486"],"result_aliases":{"BUSY":"NO_ANSWER","VOICEMAIL":"NO_ANSWER"},"rules":[{"result":"NO_ANSWER","attempt":1,"action":"RETRY","delay":"+2h"},{"result":"NO_ANSWER","attempt":2,"action":"RETRY","delay":"+3h"},{"result":"NO_ANSWER","attempt":3,"action":"RETRY","delay":"+2bd"},{"result":"NO_ANSWER","attempt":4,"action":"RETRY","delay":"+2h"},{"result":"NO_ANSWER","attempt":5,"action":"RETRY","delay":"+3h"},{"result":"NO_ANSWER","attempt":6,"action":"RETRY","delay":"+3bd"},{"result":"NO_ANSWER","attempt":7,"action":"RETRY","delay":"+2h"},{"result":"NO_ANSWER","attempt":8,"action":"RETRY","delay":"+3h"},{"result":"NO_ANSWER","attempt":9,"action":"CLOSE","delay":null},{"result":"NO_ANSWER","attempt":"*","action":"CLOSE","delay":null},{"result":"ANSWERED","attempt":"*","action":"COMPLETE","delay":null},{"result":"CALLBACK","attempt":"*","action":"CALLBACK","delay":null},{"result":"WRONG_NUMBER","attempt":"*","action":"CLOSE","delay":null},{"result":"DNC","attempt":"*","action":"CLOSE","delay":null}],"callback_default":"+24h","unmatched_action":"NONE"}'
FROM DUAL
WHERE NOT EXISTS (SELECT 1 FROM schema_migrations WHERE migration_id=@SEED)
  AND NOT EXISTS (SELECT 1 FROM followup_policies WHERE policy_key='STANDARD_CALL_RETRY');


-- ── S4 · rutas ────────────────────────────────────────────────────────────────
-- IN_PROVEEDOR1 · capacity 6 ← WF2 "Select Batch + Country Route": BATCH_SIZE = 6
INSERT INTO call_routes (route_key, iso, provider_id, enabled, priority, caller_id,
       elevenlabs_agent_id, elevenlabs_phone_number_id, capacity_default, followup_policy_id,
       recording_enabled, recording_min_secs, recording_upload_crm, recording_telegram, notes)
SELECT 'IN_PROVEEDOR1','IN',p.id,1,10,NULL,
       'agent_5701kramx550e3qs2tm11661b48p','phnum_7801kyktmabxembteqce884tanb6',
       6,(SELECT id FROM followup_policies WHERE policy_key='STANDARD_CALL_RETRY'),
       1,60,1,1,'Seed V2.1 desde WF2/WF10 vigentes.'
FROM voice_providers p
WHERE p.code='proveedor1'
  AND NOT EXISTS (SELECT 1 FROM schema_migrations WHERE migration_id=@SEED)
  AND NOT EXISTS (SELECT 1 FROM call_routes WHERE route_key='IN_PROVEEDOR1');

-- IN_STRINGEE · capacity 1 ← WF2 "Normalize & Select (Stringee)": BATCH_SIZE = 1
--               caller_id  ← WF2 "POST → Stringee Worker": from_number
--               policy     ← la MISMA. Stringee no tiene política especial:
--                            solo conoce el resultado de forma asíncrona.
INSERT INTO call_routes (route_key, iso, provider_id, enabled, priority, caller_id,
       elevenlabs_agent_id, elevenlabs_phone_number_id, capacity_default, followup_policy_id,
       recording_enabled, recording_min_secs, recording_upload_crm, recording_telegram, notes)
SELECT 'IN_STRINGEE','IN',p.id,1,20,'917971730907',
       'agent_5701kramx550e3qs2tm11661b48p',NULL,
       1,(SELECT id FROM followup_policies WHERE policy_key='STANDARD_CALL_RETRY'),
       1,60,1,1,'capacity=1 = capacidad comprada hoy. Subir desde el panel al ampliar.'
FROM voice_providers p
WHERE p.code='stringee'
  AND NOT EXISTS (SELECT 1 FROM schema_migrations WHERE migration_id=@SEED)
  AND NOT EXISTS (SELECT 1 FROM call_routes WHERE route_key='IN_STRINGEE');

-- NP_PROVEEDOR1 · enabled 0 ← nodos Nepal DESACTIVADOS en WF2
INSERT INTO call_routes (route_key, iso, provider_id, enabled, priority, caller_id,
       elevenlabs_agent_id, elevenlabs_phone_number_id, capacity_default, followup_policy_id,
       recording_enabled, recording_min_secs, recording_upload_crm, recording_telegram, notes)
SELECT 'NP_PROVEEDOR1','NP',p.id,0,30,NULL,
       'agent_7601m209ntj3fyrbg0dcq6w507yq','phnum_7801kyktmabxembteqce884tanb6',
       6,(SELECT id FROM followup_policies WHERE policy_key='STANDARD_CALL_RETRY'),
       1,60,1,1,'Deshabilitada: refleja los nodos Nepal desactivados en WF2.'
FROM voice_providers p
WHERE p.code='proveedor1'
  AND NOT EXISTS (SELECT 1 FROM schema_migrations WHERE migration_id=@SEED)
  AND NOT EXISTS (SELECT 1 FROM call_routes WHERE route_key='NP_PROVEEDOR1');

-- upgrade desde V2: rutas que existían sin política asignada reciben la estándar
-- (en V2 las tres tenían exactamente esta lógica embebida en JSON).
-- Solo durante el seed: después, un NULL puesto a propósito no se toca.
UPDATE call_routes
   SET followup_policy_id = (SELECT id FROM (SELECT id FROM followup_policies
                                             WHERE policy_key='STANDARD_CALL_RETRY') x)
 WHERE followup_policy_id IS NULL
   AND NOT EXISTS (SELECT 1 FROM schema_migrations WHERE migration_id=@SEED);


-- ── S5 · Telegram (4 nodos de WF10 + 2 de WF3) ────────────────────────────────
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
      UNION ALL SELECT 'NP_PROVEEDOR1','recording','-1003984044945') t
  ON t.rk = r.route_key
WHERE NOT EXISTS (SELECT 1 FROM schema_migrations WHERE migration_id=@SEED)
  AND NOT EXISTS (SELECT 1 FROM route_telegram_targets x
                  WHERE x.route_id=r.id AND x.purpose=t.purpose AND x.chat_id=t.chat_id);


-- ── S6 · tools por país ───────────────────────────────────────────────────────
-- IN CREATE_ACCOUNT  ← WF3 MARKET_BY_COUNTRY.india = 'IND' (el JSON manda 'IND';
--                      el brief menciona 'ATL_IND' como ejemplo → PENDING_VERIFICATION)
--                      portal_url ← WF3 "Send SMS Credentials", rama no-Nepal
-- IN PAYMENT_LINK    ← WF7 "Guard + Build Order": okpay, INR, min 2000, techo 500000
-- NP CREATE_ACCOUNT  ← WF3 MARKET_BY_COUNTRY.nepal = 'NPL'
-- NP PAYMENT_LINK    ← WF7 rama Monetix DESHABILITADA a propósito (sin contrato).
--                      Se guarda disabled e incompleta: no bloquea llamadas.
-- CALLBACK           ← en v1 el callback NO es una tool: sale del data_collection
--                      del post-call (WF9). Se deja disabled. PENDING_VERIFICATION.
INSERT INTO country_tool_configs (country_iso, tool_type, enabled, mode, provider_key,
       endpoint, http_method, credential_ref, market, currency, config_json, notes)
SELECT t.iso, t.tool, t.en, 'CONFIG_ROUTER', t.prov, NULL, NULL, t.cref, t.mkt, t.cur, t.cfg, t.nt
FROM (SELECT 'IN' iso,'CREATE_ACCOUNT' tool,1 en,'cashstudio' prov,'LEADSTUDIO_API' cref,
             'IND' mkt,NULL cur,'{"portal_url":"https://crm.landmarkmarkets.in/"}' cfg,
             'WF3 v1. market IND segun JSON.' nt
      UNION ALL SELECT 'IN','CREATE_PAYMENT_LINK',1,'okpay','OKPAY_IN',NULL,'INR',
             '{"min_amount":2000,"max_amount":500000}','WF7 v1.'
      UNION ALL SELECT 'IN','CALLBACK',0,NULL,NULL,NULL,NULL,NULL,
             'v1: callback via data_collection post-call, no tool.'
      UNION ALL SELECT 'NP','CREATE_ACCOUNT',1,'cashstudio','LEADSTUDIO_API','NPL',NULL,
             '{"portal_url":"https://crm.lmtradermarkets.com/"}','WF3 v1.'
      UNION ALL SELECT 'NP','CREATE_PAYMENT_LINK',0,'monetix',NULL,NULL,'NPR',NULL,
             'Monetix sin contrato. Disabled.') t
WHERE NOT EXISTS (SELECT 1 FROM schema_migrations WHERE migration_id=@SEED)
  AND NOT EXISTS (SELECT 1 FROM country_tool_configs x
                  WHERE x.country_iso=t.iso AND x.tool_type=t.tool);


-- ── S7 · franjas horarias — DELIBERADAMENTE NO SEMBRADAS ──────────────────────
-- Hoy WF2 corre 24/7. Sembrar franjas cambiaría el comportamiento.
-- Sin franjas: capacity_now = capacity_default las 24 h, igual que hoy.
-- Se cargan desde el panel cuando se decida (ej. IN_PROVEEDOR1 09-14→5,
-- 14-15→8, 15-20→5 · IN_STRINGEE 09-20→3).


-- ── S8 · marcadores — ÚLTIMO paso ─────────────────────────────────────────────
-- Si la ejecución se corta antes de llegar acá, el marcador no existe y la
-- próxima ejecución completa el seed (guarda de clave natural en cada INSERT).
INSERT INTO schema_migrations (migration_id, notes)
SELECT @SEED, 'Seed V2.1: IN x2 rutas, NP disabled, STANDARD_CALL_RETRY, Telegram, tools'
FROM DUAL WHERE NOT EXISTS (SELECT 1 FROM schema_migrations WHERE migration_id=@SEED);

INSERT INTO schema_migrations (migration_id, notes)
SELECT @MIG, 'DDL V2.1: countries, policies, call_routes, tools, call_jobs, ledger'
FROM DUAL WHERE NOT EXISTS (SELECT 1 FROM schema_migrations WHERE migration_id=@MIG);


-- ═══════════════════════════════════════════════════════════════════════════════
--  GRANTS — ajustar al usuario real (PENDING_VERIFICATION PV-9)
-- ═══════════════════════════════════════════════════════════════════════════════
-- Panel:
-- GRANT SELECT, INSERT, UPDATE, DELETE ON asterisk.countries              TO 'panel_rw'@'%';
-- GRANT SELECT, INSERT, UPDATE, DELETE ON asterisk.voice_providers        TO 'panel_rw'@'%';
-- GRANT SELECT, INSERT, UPDATE, DELETE ON asterisk.followup_policies      TO 'panel_rw'@'%';
-- GRANT SELECT, INSERT, UPDATE         ON asterisk.call_routes            TO 'panel_rw'@'%';
-- GRANT SELECT, INSERT, UPDATE, DELETE ON asterisk.route_capacity_windows TO 'panel_rw'@'%';
-- GRANT SELECT, INSERT, UPDATE, DELETE ON asterisk.route_telegram_targets TO 'panel_rw'@'%';
-- GRANT SELECT, INSERT, UPDATE         ON asterisk.country_tool_configs   TO 'panel_rw'@'%';
-- GRANT SELECT, INSERT                 ON asterisk.route_audit            TO 'panel_rw'@'%';
-- GRANT SELECT                         ON asterisk.wf_call_jobs           TO 'panel_rw'@'%';
-- GRANT SELECT                         ON asterisk.wf_conversation_ledger TO 'panel_rw'@'%';
-- n8n (usuario del nodo MySQL):
-- GRANT SELECT, INSERT, UPDATE ON asterisk.wf_call_jobs           TO '<n8n_user>'@'%';
-- GRANT SELECT, INSERT, UPDATE ON asterisk.wf_conversation_ledger TO '<n8n_user>'@'%';
--
-- call_routes sin DELETE: no existe hard delete de rutas, ni siquiera por permiso.


-- ═══════════════════════════════════════════════════════════════════════════════
--  VERIFICACIÓN DE PARIDAD
-- ═══════════════════════════════════════════════════════════════════════════════
-- SELECT r.route_key, p.code, p.provider_kind, r.enabled, r.capacity_default,
--        c.dial_prefix, c.timezone, r.caller_id, fp.policy_key
--   FROM call_routes r
--   JOIN voice_providers p ON p.id = r.provider_id
--   JOIN countries c ON c.iso = r.iso
--   LEFT JOIN followup_policies fp ON fp.id = r.followup_policy_id
--  ORDER BY r.priority;
--
--  IN_PROVEEDOR1 | proveedor1 | sip      | 1 | 6 | +91  | Asia/Kolkata   | NULL         | STANDARD_CALL_RETRY
--  IN_STRINGEE   | stringee   | stringee | 1 | 1 | +91  | Asia/Kolkata   | 917971730907 | STANDARD_CALL_RETRY
--  NP_PROVEEDOR1 | proveedor1 | sip      | 0 | 6 | +977 | Asia/Kathmandu | NULL         | STANDARD_CALL_RETRY
