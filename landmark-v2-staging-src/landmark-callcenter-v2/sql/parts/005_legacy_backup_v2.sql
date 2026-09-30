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
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

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
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;


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
