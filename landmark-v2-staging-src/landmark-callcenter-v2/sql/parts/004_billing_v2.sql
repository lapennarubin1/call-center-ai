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
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;


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
