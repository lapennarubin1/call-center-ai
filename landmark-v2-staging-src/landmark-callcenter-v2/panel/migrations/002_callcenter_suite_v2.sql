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
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;


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
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;


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
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;


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
