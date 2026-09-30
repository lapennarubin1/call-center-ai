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
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;


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
