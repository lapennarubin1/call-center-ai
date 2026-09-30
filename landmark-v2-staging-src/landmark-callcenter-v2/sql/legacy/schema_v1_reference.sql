-- ════════════════════════════════════════════════════════════════
--  REFERENCIA: schema.sql del panel REAL (panel-share/deploy/schema.sql)
--
--  Se incluye para documentar las tablas de v1. NO forma parte de la
--  migración V2 y NO se ejecuta: sql/migration.sql es lo ejecutable.
--
--  CAMBIO RESPECTO DEL ORIGINAL: la línea CREATE USER traía la
--  contraseña de panel_rw en claro. Se sustituyó por un marcador.
--  El original NO debe distribuirse — ver docs/PENDING_VERIFICATION.md (PV-2).
-- ════════════════════════════════════════════════════════════════

-- ════════════════════════════════════════════════════════════════
--  Landmark Markets — Panel de Control
--  Tablas de apoyo. El CDR de Asterisk ya existe y NO se toca.
--
--  Ejecutar:  mysql asterisk < schema.sql
-- ════════════════════════════════════════════════════════════════

-- Espejo de la hoja `leads` de Google Sheets. Lo llena WF14 cada 15 min.
-- Existe para que el panel nunca consulte Google Sheets en vivo:
-- la cuota de la API es de 60 lecturas/minuto y WF2 ya la consume casi entera.
CREATE TABLE IF NOT EXISTS panel_leads (
    lead_id          VARCHAR(64)  NOT NULL,
    full_name        VARCHAR(160) DEFAULT NULL,
    phone            VARCHAR(32)  DEFAULT NULL,
    country          VARCHAR(64)  DEFAULT NULL,
    language         VARCHAR(16)  DEFAULT NULL,
    status           VARCHAR(48)  DEFAULT NULL,
    last_call_status VARCHAR(48)  DEFAULT NULL,
    call_attempts    INT          DEFAULT 0,
    last_call_time   DATETIME     DEFAULT NULL,
    duration_secs    INT          DEFAULT 0,
    synced_at        DATETIME     DEFAULT NULL,
    PRIMARY KEY (lead_id),
    KEY idx_status         (status),
    KEY idx_last_call_time (last_call_time),
    KEY idx_phone          (phone)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

-- Espejo de la hoja `conversions` (cuentas abiertas en Atlantis).
CREATE TABLE IF NOT EXISTS panel_conversions (
    lead_id       VARCHAR(64)  NOT NULL,
    full_name     VARCHAR(160) DEFAULT NULL,
    phone         VARCHAR(32)  DEFAULT NULL,
    atlantis_user VARCHAR(80)  DEFAULT NULL,
    account_id    VARCHAR(80)  DEFAULT NULL,
    created_at    DATETIME     DEFAULT NULL,
    synced_at     DATETIME     DEFAULT NULL,
    PRIMARY KEY (lead_id),
    KEY idx_created (created_at)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

-- Bitácora de sincronización: permite ver desde el panel si WF14 dejó de correr.
CREATE TABLE IF NOT EXISTS panel_sync_log (
    id         INT AUTO_INCREMENT PRIMARY KEY,
    source     VARCHAR(32)  NOT NULL,
    rows_in    INT          DEFAULT 0,
    status     VARCHAR(16)  DEFAULT 'ok',
    message    VARCHAR(255) DEFAULT NULL,
    synced_at  DATETIME     NOT NULL,
    KEY idx_synced (synced_at)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

-- ── Índice sobre el CDR ─────────────────────────────────────────
-- El panel filtra SIEMPRE por rango de calldate. Sin este índice, cada
-- consulta hace un escaneo completo de la tabla (cientos de miles de filas).
-- Se crea sólo si no existe; en MySQL 8 basta con ignorar el error 1061.
CREATE INDEX idx_cdr_calldate ON cdr (calldate);
CREATE INDEX idx_cdr_disp_date ON cdr (disposition, calldate);

-- ── Usuario del panel (sólo lectura del CDR, escritura en sus tablas) ──
CREATE USER IF NOT EXISTS 'panel_rw'@'localhost' IDENTIFIED BY '<CLAVE_DE_panel_rw — NO VERSIONAR>';
GRANT SELECT                     ON asterisk.cdr               TO 'panel_rw'@'localhost';
GRANT SELECT, INSERT, UPDATE, DELETE ON asterisk.panel_leads       TO 'panel_rw'@'localhost';
GRANT SELECT, INSERT, UPDATE, DELETE ON asterisk.panel_conversions TO 'panel_rw'@'localhost';
GRANT SELECT, INSERT, DELETE     ON asterisk.panel_sync_log    TO 'panel_rw'@'localhost';
FLUSH PRIVILEGES;
