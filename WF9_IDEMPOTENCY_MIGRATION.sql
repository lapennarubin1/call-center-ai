-- ============================================================================
-- WF9_IDEMPOTENCY_MIGRATION.sql
-- Base de datos: asterisk (MariaDB)
--
-- Crea UNA tabla nueva: wf_call_events
--   - inbox durable (callback Stringee, webhook y polling de ElevenLabs)
--   - idempotencia (UNIQUE event_key: 'stringee:<job_id>' / 'elevenlabs:<conversation_id>')
--   - ledger de dispatch Stringee (WF2 inserta cada job aceptado por el worker)
--   - claim/lease atómico (claim_token + lease_until)
--   - retención local del próximo reintento cuando LeadStudio no permite
--     guardarlo sin crear una Activity (next_retry_at, corrección de buzón Asterisk)
--
-- NO modifica, renombra ni borra ninguna tabla existente.
-- NO toca wf_call_followups ni stringee_calls.
-- Es seguro ejecutarlo más de una vez (CREATE TABLE IF NOT EXISTS).
--
-- Todas las columnas de tiempo con lógica (event_at, dispatched_at,
-- first_seen_at, next_attempt_at, lease_until, next_retry_at, processed_at)
-- se escriben SIEMPRE en UTC explícito desde los workflows (UTC_TIMESTAMP(3)
-- o literal UTC). created_at / updated_at son sólo de auditoría.
--
-- Collation utf8mb4_unicode_ci: igual que stringee_calls, para poder unir
-- stringee_call_id = stringee_calls.call_id sin "Illegal mix of collations".
-- ============================================================================

CREATE TABLE IF NOT EXISTS wf_call_events (
  id                 BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,
  event_key          VARCHAR(191)    NOT NULL,              -- stringee:<job_id> | elevenlabs:<conversation_id>
  source             VARCHAR(20)     NOT NULL,              -- stringee | elevenlabs
  provider           VARCHAR(20)     NULL,                  -- stringee | asterisk (NULL hasta conocerlo)
  external_id        VARCHAR(128)    NOT NULL,              -- job_id | conversation_id
  lead_id            VARCHAR(64)     NULL,
  phone              VARCHAR(32)     NULL,
  call_attempts      INT             NULL,                  -- intento enviado al worker / dynamic variable
  crm_attempt        INT             NULL,                  -- posición real en el contador del CRM
  event_at           DATETIME(3)     NULL,                  -- inicio de la llamada (UTC)
  dispatched_at      DATETIME(3)     NULL,                  -- dispatch WF2 (UTC)
  first_seen_at      DATETIME(3)     NULL,                  -- primera vez que el evento llegó (UTC)
  conversation_id    VARCHAR(128)    NULL,
  stringee_call_id   VARCHAR(128)    NULL,
  state              VARCHAR(16)     NOT NULL DEFAULT 'RECEIVED',  -- RECEIVED|PROCESSING|DONE|FAILED|SKIPPED|STALE
  resolution         VARCHAR(48)     NULL,
  followup_id        VARCHAR(64)     NULL,
  lifecycle_applied  TINYINT(1)      NOT NULL DEFAULT 0,
  next_retry_at      DATETIME(3)     NULL,                  -- reintento retenido localmente (UTC)
  payload            LONGTEXT        NULL,
  process_attempts   INT             NOT NULL DEFAULT 0,    -- veces reclamado
  error_count        INT             NOT NULL DEFAULT 0,    -- veces que terminó en FAILED
  next_attempt_at    DATETIME(3)     NOT NULL DEFAULT '1970-01-01 00:00:00.000',  -- siempre se escribe explícito en UTC
  claim_token        VARCHAR(64)     NULL,
  lease_until        DATETIME(3)     NULL,
  last_error         TEXT            NULL,
  processed_at       DATETIME(3)     NULL,
  created_at         DATETIME(3)     NOT NULL DEFAULT CURRENT_TIMESTAMP(3),
  updated_at         DATETIME(3)     NOT NULL DEFAULT CURRENT_TIMESTAMP(3) ON UPDATE CURRENT_TIMESTAMP(3),
  PRIMARY KEY (id),
  UNIQUE KEY uq_wf_call_events_event_key (event_key),
  KEY idx_wce_state_next (state, next_attempt_at),
  KEY idx_wce_state_lease (state, lease_until),
  KEY idx_wce_claim (claim_token),
  KEY idx_wce_lead_state (lead_id, state),
  KEY idx_wce_lead_lifecycle (lead_id, lifecycle_applied, event_at),
  KEY idx_wce_conversation (conversation_id),
  KEY idx_wce_stringee_call (stringee_call_id),
  KEY idx_wce_hold (lead_id, next_retry_at),
  KEY idx_wce_processed (processed_at)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

-- ----------------------------------------------------------------------------
-- Verificación (solo lectura). Debe mostrar la tabla y 0 filas.
-- ----------------------------------------------------------------------------
SHOW CREATE TABLE wf_call_events\G
SELECT COUNT(*) AS filas FROM wf_call_events;

-- ----------------------------------------------------------------------------
-- Permisos (solo lectura): ¿el usuario de n8n tiene permisos a nivel de BD?
-- Si el usuario de la credencial "MySQL account" aparece SOLO en
-- TABLE_PRIVILEGES (no en SCHEMA_PRIVILEGES para 'asterisk'), hay que darle
-- permisos sobre la tabla nueva. Ver el bloque GRANT comentado al final.
-- ----------------------------------------------------------------------------
SELECT GRANTEE, PRIVILEGE_TYPE FROM information_schema.SCHEMA_PRIVILEGES
 WHERE TABLE_SCHEMA = 'asterisk' ORDER BY GRANTEE, PRIVILEGE_TYPE;
SELECT GRANTEE, TABLE_NAME, PRIVILEGE_TYPE FROM information_schema.TABLE_PRIVILEGES
 WHERE TABLE_SCHEMA = 'asterisk' AND TABLE_NAME IN ('wf_call_followups','stringee_calls','wf2_provider_config','wf_call_events')
 ORDER BY GRANTEE, TABLE_NAME, PRIVILEGE_TYPE;

-- ----------------------------------------------------------------------------
-- SOLO si el usuario de n8n tiene permisos por tabla (no por base de datos):
-- descomentar reemplazando <usuario_n8n> y <host> por los valores que
-- devolvió la consulta anterior para wf_call_followups.
-- ----------------------------------------------------------------------------
-- GRANT SELECT, INSERT, UPDATE ON asterisk.wf_call_events TO '<usuario_n8n>'@'<host>';
