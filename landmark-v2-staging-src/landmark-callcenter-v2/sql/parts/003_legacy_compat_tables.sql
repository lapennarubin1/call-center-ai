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
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

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
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

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
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

-- L4 · panel_sync_log — bitácora de sincronizaciones (WF14 v1 y pantallas del panel)
CREATE TABLE IF NOT EXISTS panel_sync_log (
  id        BIGINT AUTO_INCREMENT PRIMARY KEY,
  source    VARCHAR(32) NOT NULL,
  rows_in   INT         NOT NULL DEFAULT 0,
  status    VARCHAR(16) NOT NULL DEFAULT 'ok',
  synced_at DATETIME    NOT NULL DEFAULT CURRENT_TIMESTAMP,
  KEY idx_source_when (source, synced_at)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

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
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

-- L6 · wf2_provider_config — interruptor de proveedores de v1.
--      V2 no lo usa NUNCA (sus interruptores son countries/voice_providers/
--      call_routes). Se crea para que el ROLLBACK a v1 tenga dónde apoyarse.
CREATE TABLE IF NOT EXISTS wf2_provider_config (
  provider VARCHAR(32) NOT NULL PRIMARY KEY,
  enabled  TINYINT     NOT NULL DEFAULT 1
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

INSERT INTO schema_migrations (migration_id, notes)
SELECT @MIG3, 'Tablas de compatibilidad v1 (no-op en produccion)'
FROM DUAL WHERE NOT EXISTS (SELECT 1 FROM schema_migrations WHERE migration_id=@MIG3);
