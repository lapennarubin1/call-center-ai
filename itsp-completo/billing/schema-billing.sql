-- =====================================================================
--  ESQUEMA PANEL + FACTURACION
--  Ejecutar DESPUES de sql/schema.sql y monitoring/schema-monitoring.sql
--  Añade: proveedores, conexiones SIP por cliente, planes y tarifas,
--         registros de facturacion, facturas y usuarios del panel.
-- =====================================================================
USE asterisk;

-- ---------- Ampliaciones a la tabla clients ----------
ALTER TABLE clients
  ADD COLUMN company       VARCHAR(128) DEFAULT NULL,
  ADD COLUMN email         VARCHAR(128) DEFAULT NULL,
  ADD COLUMN billing_type  ENUM('prepaid','postpaid') NOT NULL DEFAULT 'postpaid',
  ADD COLUMN balance       DECIMAL(12,4) NOT NULL DEFAULT 0,     -- saldo (prepago)
  ADD COLUMN credit_limit  DECIMAL(12,4) NOT NULL DEFAULT 0,     -- limite (pospago)
  ADD COLUMN rate_plan_id  INT DEFAULT NULL,
  ADD COLUMN currency      VARCHAR(3) NOT NULL DEFAULT 'USD';

-- ---------- PROVEEDORES (editables desde el panel) ----------
CREATE TABLE IF NOT EXISTS providers (
  id         INT AUTO_INCREMENT PRIMARY KEY,
  name       VARCHAR(64) NOT NULL UNIQUE,       -- ej: telnyx
  host       VARCHAR(128) NOT NULL,             -- ej: sip.telnyx.com
  port       INT NOT NULL DEFAULT 5060,
  auth_type  ENUM('userpass','ip') NOT NULL DEFAULT 'userpass',
  username   VARCHAR(128) DEFAULT NULL,
  password   VARCHAR(128) DEFAULT NULL,
  transport  ENUM('udp','tcp','tls') NOT NULL DEFAULT 'udp',
  from_domain VARCHAR(128) DEFAULT NULL,
  codecs     VARCHAR(128) NOT NULL DEFAULT 'ulaw,alaw',
  ip_cidrs   VARCHAR(255) DEFAULT NULL,         -- CIDRs para identify/firewall (coma)
  priority   INT NOT NULL DEFAULT 100,          -- orden LCR (menor = primero)
  active     TINYINT(1) NOT NULL DEFAULT 1,
  created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

-- ---------- CONEXIONES SIP por cliente (varias por cliente) ----------
-- Cada conexion = un login SIP (endpoint pjsip). Un cliente puede tener
-- muchas: p.ej. una por sede, por campaña o por operador.
CREATE TABLE IF NOT EXISTS connections (
  id           INT AUTO_INCREMENT PRIMARY KEY,
  client       VARCHAR(64) NOT NULL,
  label        VARCHAR(64) DEFAULT NULL,        -- nombre amigable
  username     VARCHAR(64) NOT NULL UNIQUE,     -- usuario SIP
  password     VARCHAR(128) NOT NULL,
  transport    ENUM('udp','tcp','tls') NOT NULL DEFAULT 'udp',
  max_channels INT NOT NULL DEFAULT 5,          -- canales simultaneos de ESTA conexion
  allowed_ip   VARCHAR(64) DEFAULT NULL,        -- IP fija del cliente (opcional)
  provider_order VARCHAR(255) DEFAULT NULL,     -- override LCR (coma) o NULL=plan del cliente
  active       TINYINT(1) NOT NULL DEFAULT 1,
  created_at   TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
  INDEX idx_client (client),
  CONSTRAINT fk_conn_client FOREIGN KEY (client) REFERENCES clients(client)
);

-- ---------- PLANES DE TARIFAS (venta) ----------
CREATE TABLE IF NOT EXISTS rate_plans (
  id          INT AUTO_INCREMENT PRIMARY KEY,
  name        VARCHAR(64) NOT NULL UNIQUE,
  currency    VARCHAR(3) NOT NULL DEFAULT 'USD',
  description VARCHAR(255) DEFAULT NULL,
  active      TINYINT(1) NOT NULL DEFAULT 1,
  created_at  TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

-- ---------- TARIFAS DE VENTA (precio al cliente por prefijo) ----------
CREATE TABLE IF NOT EXISTS sell_rates (
  id           INT AUTO_INCREMENT PRIMARY KEY,
  plan_id      INT NOT NULL,
  prefix       VARCHAR(24) NOT NULL,            -- ej: 1, 34, 3491, 521
  description  VARCHAR(128) DEFAULT NULL,       -- ej: USA, Spain Mobile
  rate_per_min DECIMAL(10,5) NOT NULL,          -- precio por minuto
  connect_fee  DECIMAL(10,5) NOT NULL DEFAULT 0,-- cargo por conexion
  min_seconds  INT NOT NULL DEFAULT 0,          -- duracion minima facturable
  increment_seconds INT NOT NULL DEFAULT 60,    -- bloque de facturacion (60=por minuto)
  INDEX idx_plan_prefix (plan_id, prefix),
  CONSTRAINT fk_sell_plan FOREIGN KEY (plan_id) REFERENCES rate_plans(id) ON DELETE CASCADE
);

-- ---------- TARIFAS DE COMPRA (costo del proveedor, para margen) ----------
CREATE TABLE IF NOT EXISTS buy_rates (
  id           INT AUTO_INCREMENT PRIMARY KEY,
  provider     VARCHAR(64) NOT NULL,
  prefix       VARCHAR(24) NOT NULL,
  description  VARCHAR(128) DEFAULT NULL,
  rate_per_min DECIMAL(10,5) NOT NULL,
  increment_seconds INT NOT NULL DEFAULT 60,
  INDEX idx_prov_prefix (provider, prefix)
);

-- ---------- REGISTROS DE FACTURACION (una fila por llamada tarificada) ----------
CREATE TABLE IF NOT EXISTS billing_records (
  id             BIGINT AUTO_INCREMENT PRIMARY KEY,
  cdr_id         BIGINT NOT NULL UNIQUE,        -- evita tarificar dos veces
  client         VARCHAR(64) NOT NULL,
  dst            VARCHAR(80) DEFAULT '',
  prefix_matched VARCHAR(24) DEFAULT NULL,
  plan_id        INT DEFAULT NULL,
  billsec        INT NOT NULL DEFAULT 0,        -- segundos reales
  billed_seconds INT NOT NULL DEFAULT 0,        -- segundos facturados (con incremento)
  rate_per_min   DECIMAL(10,5) NOT NULL DEFAULT 0,
  amount         DECIMAL(12,5) NOT NULL DEFAULT 0, -- precio de venta
  cost           DECIMAL(12,5) DEFAULT NULL,       -- costo (si hay buy_rate)
  currency       VARCHAR(3) NOT NULL DEFAULT 'USD',
  invoice_id     INT DEFAULT NULL,
  created_at     TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
  INDEX idx_client (client),
  INDEX idx_invoice (invoice_id),
  INDEX idx_created (created_at)
);

-- ---------- FACTURAS ----------
CREATE TABLE IF NOT EXISTS invoices (
  id           INT AUTO_INCREMENT PRIMARY KEY,
  client       VARCHAR(64) NOT NULL,
  period_start DATE NOT NULL,
  period_end   DATE NOT NULL,
  currency     VARCHAR(3) NOT NULL DEFAULT 'USD',
  total_calls  INT NOT NULL DEFAULT 0,
  total_minutes DECIMAL(12,2) NOT NULL DEFAULT 0,
  total_amount DECIMAL(12,4) NOT NULL DEFAULT 0,
  total_cost   DECIMAL(12,4) DEFAULT NULL,
  status       ENUM('draft','issued','paid') NOT NULL DEFAULT 'draft',
  created_at   TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
  INDEX idx_client (client)
);

-- ---------- USUARIOS DEL PANEL ----------
CREATE TABLE IF NOT EXISTS admin_users (
  id            INT AUTO_INCREMENT PRIMARY KEY,
  username      VARCHAR(64) NOT NULL UNIQUE,
  password_hash VARCHAR(255) NOT NULL,
  created_at    TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);
-- El usuario admin inicial se crea con el script panel/crear_admin.py
