-- =====================================================================
--  ESQUEMA BD  —  DIDs propios y clientes
--  Motor recomendado: MariaDB 10.x / MySQL 8.x
--  Principio: la tabla client_dids es la UNICA fuente de verdad de
--  que numeros puede usar cada cliente como Caller ID. Solo mete aqui
--  numeros que compraste legalmente en tus proveedores.
-- =====================================================================

CREATE DATABASE IF NOT EXISTS asterisk CHARACTER SET utf8mb4;
USE asterisk;

-- Clientes / call centers
CREATE TABLE IF NOT EXISTS clients (
  id           INT AUTO_INCREMENT PRIMARY KEY,
  client       VARCHAR(64) NOT NULL UNIQUE,   -- coincide con pjsip (client1, ...)
  name         VARCHAR(128) NOT NULL,
  max_channels INT NOT NULL DEFAULT 10,        -- canales simultaneos
  active       TINYINT(1) NOT NULL DEFAULT 1,
  created_at   TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

-- Pool de DIDs PROPIOS por cliente (un cliente puede tener muchos numeros)
CREATE TABLE IF NOT EXISTS client_dids (
  id           INT AUTO_INCREMENT PRIMARY KEY,
  client       VARCHAR(64) NOT NULL,
  number       VARCHAR(32) NOT NULL UNIQUE,    -- E.164 sin '+', ej: 15551234567
  provider     VARCHAR(32) DEFAULT NULL,       -- de que proveedor lo compraste
  active       TINYINT(1) NOT NULL DEFAULT 1,
  created_at   TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
  INDEX idx_client (client),
  INDEX idx_active (active),
  CONSTRAINT fk_did_client FOREIGN KEY (client) REFERENCES clients(client)
);

-- (Opcional) Registro de destinos permitidos por cliente, para whitelisting
CREATE TABLE IF NOT EXISTS client_allowed_prefixes (
  id      INT AUTO_INCREMENT PRIMARY KEY,
  client  VARCHAR(64) NOT NULL,
  prefix  VARCHAR(16) NOT NULL,   -- ej: 1  (USA/Canada), 34 (Espana)
  INDEX idx_client (client)
);

-- =====================================================================
--  DATOS DE EJEMPLO (reemplaza por tus numeros reales comprados)
-- =====================================================================
INSERT INTO clients (client, name, max_channels) VALUES
  ('client1', 'Call Center Alfa', 10),
  ('client2', 'Call Center Beta', 20)
ON DUPLICATE KEY UPDATE name=VALUES(name);

-- Numeros de EJEMPLO. Cambialos por DIDs que realmente posees.
INSERT INTO client_dids (client, number, provider) VALUES
  ('client1', '15551110001', 'telnyx'),
  ('client1', '15551110002', 'telnyx'),
  ('client2', '15552220001', 'providerB')
ON DUPLICATE KEY UPDATE provider=VALUES(provider);

-- Usuario de BD de solo lectura para Asterisk (menor privilegio):
--   CREATE USER 'asterisk_ro'@'localhost' IDENTIFIED BY 'CAMBIA_ESTO_PASS_BD';
--   GRANT SELECT ON asterisk.* TO 'asterisk_ro'@'localhost';
--   FLUSH PRIVILEGES;
