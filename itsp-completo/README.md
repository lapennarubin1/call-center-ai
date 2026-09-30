# Plataforma ITSP / Reseller de voz — Sistema completo

## Estructura del paquete

```
itsp-completo/
├── config/                  # Configuración de Asterisk
│   ├── pjsip.conf           # Transports, proveedores, clientes
│   ├── extensions.conf      # Dialplan: LCR, límites, caller ID, anti-fraude
│   ├── func_odbc.conf       # Consultas a BD (DIDs, estado cliente)
│   ├── res_odbc.conf        # Conexión Asterisk -> MariaDB
│   ├── rtp.conf             # Rango RTP + RTP estricto
│   ├── manager.conf         # AMI solo localhost
│   └── logger.conf          # Log de seguridad (para fail2ban)
├── security/                # Firewall y anti fuerza bruta
│   ├── nftables-sip.sh      # Firewall deny-by-default
│   ├── jail.local           # fail2ban (SIP + SSH)
│   └── asterisk-security.conf  # Filtro fail2ban para Asterisk
├── sql/
│   ├── schema.sql           # Tablas base: clients, client_dids
│   └── (ver monitoring/)    # Tablas CDR y fraude en monitoring/
├── tls/                     # Cifrado señalización + audio
│   ├── generar-certificados.sh   # Genera CA + cert del servidor
│   └── pjsip-tls-srtp.conf       # Transport TLS + cliente cifrado
└── monitoring/              # Monitoreo y corte automático de fraude
    ├── cdr_adaptive_odbc.conf    # Asterisk graba CDR en BD
    ├── schema-monitoring.sql     # Tablas cdr, fraud_events, fraud_limits
    ├── fraud-monitor.py          # Script de detección y corte
    ├── fraud-monitor.service     # systemd service
    ├── fraud-monitor.timer       # systemd timer (cada minuto)
    └── integracion-extensions.conf  # Cambios al dialplan para CDR + bloqueo
```

---

## Funcionalidades incluidas

- **Multi-proveedor**: Telnyx, ProveedorB, ProveedorC (ampliable sin límite). Cada proveedor soporta auth por credenciales o por IP.
- **Selección manual de proveedor por cliente** + **failover automático (LCR)**: si el proveedor principal cae, pasa al siguiente en cadena.
- **Multi-cliente aislado**: cada cliente tiene su propio contexto, sus propios DIDs y sus propios límites.
- **Un cliente, múltiples números**: pool de DIDs en BD con rotación aleatoria.
- **Caller ID controlado**: solo se emite un número si está en tu tabla `client_dids`. Si el cliente no tiene DID asignado, la llamada se rechaza. Nunca sale un Caller ID que no sea tuyo.
- **Canales simultáneos por cliente**: límite configurable por cliente, ajustable en el dialplan.
- **Anti-fraude de prefijos**: bloquea destinos premium/satelitales en el dialplan.
- **TLS + SRTP**: cifrado de señalización y audio. Permite clientes con IP dinámica sin necesidad de whitelist de IP en el firewall.
- **Firewall deny-by-default** (nftables): solo entran SIP desde IPs de proveedores/clientes, RTP en rango definido, SSH desde IPs de admin.
- **fail2ban**: banea IPs que fallan autenticación SIP repetidamente.
- **CDR en BD**: registro completo de cada llamada en MariaDB.
- **Monitor antifraude automático**: detecta picos anómalos y deshabilita al cliente + cuelga llamadas + alerta. Umbrales ajustables por cliente.

---

## Instalación paso a paso

### 1. Servidor recomendado
Ubuntu 22.04/24.04 LTS, IP pública fija.

| Canales simultáneos | vCPU | RAM  |
|---------------------|------|------|
| hasta 30            | 2    | 2 GB |
| 30–100              | 4    | 4 GB |
| 100–300             | 8    | 8 GB |

### 2. Paquetes del sistema
```bash
sudo apt update
sudo apt install -y asterisk mariadb-server unixodbc odbc-mariadb \
                    fail2ban nftables python3 python3-pip
pip install pymysql requests
```

### 3. ODBC — conectar Asterisk a MariaDB

`/etc/odbcinst.ini`
```ini
[MariaDB]
Description = MariaDB ODBC Driver
Driver      = /usr/lib/x86_64-linux-gnu/odbc/libmaodbc.so
```

`/etc/odbc.ini`
```ini
[asterisk]
Description = Asterisk DB
Driver      = MariaDB
Server      = localhost
Database    = asterisk
Port        = 3306
```

### 4. Base de datos
```bash
sudo mysql < sql/schema.sql
sudo mysql < monitoring/schema-monitoring.sql
```

Crea el usuario de solo lectura (para DIDs) y uno de escritura (para el monitor):
```sql
CREATE USER 'asterisk_ro'@'localhost' IDENTIFIED BY 'PASS_RO_FUERTE';
GRANT SELECT ON asterisk.* TO 'asterisk_ro'@'localhost';

CREATE USER 'asterisk_rw'@'localhost' IDENTIFIED BY 'PASS_RW_FUERTE';
GRANT SELECT, INSERT, UPDATE ON asterisk.* TO 'asterisk_rw'@'localhost';

FLUSH PRIVILEGES;
```

Carga tus DIDs reales en `client_dids` (los números que compraste en Telnyx u otro proveedor).

### 5. Configuración de Asterisk
```bash
sudo cp config/pjsip.conf        /etc/asterisk/
sudo cp config/extensions.conf   /etc/asterisk/
sudo cp config/func_odbc.conf    /etc/asterisk/
sudo cp config/res_odbc.conf     /etc/asterisk/
sudo cp config/rtp.conf          /etc/asterisk/
sudo cp config/manager.conf      /etc/asterisk/
sudo cp config/logger.conf       /etc/asterisk/
sudo cp monitoring/cdr_adaptive_odbc.conf /etc/asterisk/
```

**Rellena todos los placeholders** (`REEMPLAZA_*`, `CAMBIA_ESTO_*`, `TU_IP_*`).
Genera contraseñas fuertes: `openssl rand -base64 24`

En `integracion-extensions.conf` están los cambios que debes aplicar a cada contexto `[from-clientN]` del `extensions.conf` (añade `accountcode` y el bloqueo de cliente deshabilitado).

### 6. TLS + certificados
```bash
# Edita SERVER_CN dentro del script con tu FQDN real
sudo bash tls/generar-certificados.sh
sudo cp tls/pjsip-tls-srtp.conf /etc/asterisk/
# Añade al final del pjsip.conf:
echo '#include "pjsip-tls-srtp.conf"' | sudo tee -a /etc/asterisk/pjsip.conf
```

### 7. Firewall
```bash
# Edita nftables-sip.sh: IPs de admin, proveedores y clientes
sudo bash security/nftables-sip.sh
sudo nft list ruleset | sudo tee /etc/nftables.conf
sudo systemctl enable nftables
```

### 8. fail2ban
```bash
sudo cp security/jail.local /etc/fail2ban/jail.local
sudo cp security/asterisk-security.conf /etc/fail2ban/filter.d/
sudo systemctl restart fail2ban
```

### 9. Monitor antifraude
```bash
sudo mkdir -p /opt/itsp
sudo cp monitoring/fraud-monitor.py /opt/itsp/
# Edita en el script: DB password RW, AMI password, webhook opcional

sudo cp monitoring/fraud-monitor.service /etc/systemd/system/
sudo cp monitoring/fraud-monitor.timer   /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now fraud-monitor.timer
```

### 10. Arrancar todo
```bash
sudo systemctl restart mariadb
sudo systemctl restart asterisk
sudo systemctl restart fail2ban
sudo asterisk -rx "core reload"
```

---

## Checklist de validación (antes de producción)

```bash
# Conexión ODBC
sudo asterisk -rx "odbc show"                      # Connected: yes

# Registros hacia proveedores
sudo asterisk -rx "pjsip show registrations"       # Registered

# Endpoints cargados
sudo asterisk -rx "pjsip show endpoints"

# Dialplan
sudo asterisk -rx "dialplan show from-client1"

# TLS
sudo asterisk -rx "pjsip show transports"          # transport-tls presente

# Firewall
sudo nft list ruleset

# fail2ban
sudo fail2ban-client status asterisk

# CDR en BD (haz una llamada de prueba primero)
mysql asterisk -e "SELECT calldate,accountcode,dst,billsec FROM cdr ORDER BY id DESC LIMIT 5;"

# Monitor antifraude
sudo python3 /opt/itsp/fraud-monitor.py            # sin errores
sudo systemctl list-timers | grep fraud

# Prueba en vivo con logs
sudo asterisk -rvvvvv
# Registra un softphone, llama, verifica: proveedor correcto, Caller ID = tu DID, audio ok
```

---

## Operación diaria

### Añadir un proveedor nuevo
1. Copia un bloque de proveedor en `pjsip.conf` (5 secciones: reg/auth/aor/endpoint/identify).
2. Añade su IP al firewall (`nftables-sip.sh` y recarga).
3. Añade su nombre a `PROVIDERS` del cliente o a `LCR_DEFAULT`.
4. `sudo asterisk -rx "core reload"`

### Añadir un cliente nuevo
1. `pjsip.conf`: copia bloque de client1 → clientN, nuevo usuario/clave.
2. `extensions.conf`: copia contexto `[from-client1]` → `[from-clientN]`, ajusta CLIENT/MAXCHANS/PROVIDERS.
3. BD: `INSERT INTO clients ...` y `INSERT INTO client_dids ...` con sus números.
4. Firewall: añade su IP (o usa TLS para IP dinámica).
5. `sudo asterisk -rx "core reload"`

### Reactivar un cliente cortado por fraude
```bash
# Investiga primero en fraud_events qué pasó.
# Si fue legítimo:
mysql asterisk -e "UPDATE clients SET active=1 WHERE client='clientX';"
sudo asterisk -rx "core reload"
```

---

## Notas de cumplimiento
- Emite Caller ID **solo** desde números que posees (el sistema lo fuerza).
- **STIR/SHAKEN**: obligatorio para tráfico hacia EE.UU. Telnyx lo firma si tu cuenta y DIDs están en regla. Verifica en el portal.
- Revisa los requisitos de licencia de operador en tu jurisdicción.
