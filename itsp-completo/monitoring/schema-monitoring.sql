-- =====================================================================
--  MODULO 2: esquema para CDR + deteccion de fraude
--  Ejecutar sobre la BD 'asterisk' ya creada por schema.sql
-- =====================================================================
USE asterisk;

-- Tabla de CDR (Call Detail Records) escrita por cdr_adaptive_odbc.
CREATE TABLE IF NOT EXISTS cdr (
  id          BIGINT AUTO_INCREMENT PRIMARY KEY,
  calldate    DATETIME DEFAULT NULL,
  clid        VARCHAR(80)  DEFAULT '',
  src         VARCHAR(80)  DEFAULT '',
  dst         VARCHAR(80)  DEFAULT '',
  dcontext    VARCHAR(80)  DEFAULT '',
  channel     VARCHAR(80)  DEFAULT '',
  dstchannel  VARCHAR(80)  DEFAULT '',
  lastapp     VARCHAR(80)  DEFAULT '',
  lastdata    VARCHAR(80)  DEFAULT '',
  duration    INT DEFAULT 0,
  billsec     INT DEFAULT 0,
  disposition VARCHAR(45)  DEFAULT '',
  accountcode VARCHAR(80)  DEFAULT '',   -- usaremos esto = nombre del cliente
  uniqueid    VARCHAR(150) DEFAULT '',
  INDEX idx_calldate (calldate),
  INDEX idx_account (accountcode),
  INDEX idx_dst (dst)
);

-- Eventos de fraude detectados (auditoria de lo que hizo el monitor).
CREATE TABLE IF NOT EXISTS fraud_events (
  id         BIGINT AUTO_INCREMENT PRIMARY KEY,
  ts         DATETIME DEFAULT CURRENT_TIMESTAMP,
  client     VARCHAR(64),
  rule       VARCHAR(64),        -- que regla se disparo
  detail     VARCHAR(255),
  action     VARCHAR(64),        -- ej: 'client_disabled'
  INDEX idx_client (client),
  INDEX idx_ts (ts)
);

-- Umbrales de fraude por cliente (si no hay fila, se usan los del script).
CREATE TABLE IF NOT EXISTS fraud_limits (
  client              VARCHAR(64) PRIMARY KEY,
  max_calls_per_min   INT DEFAULT 120,   -- llamadas nuevas por minuto
  max_intl_per_hour   INT DEFAULT 300,   -- llamadas internacionales/hora
  max_minutes_per_hour INT DEFAULT 3000, -- minutos facturados/hora
  CONSTRAINT fk_fraud_client FOREIGN KEY (client) REFERENCES clients(client)
);
