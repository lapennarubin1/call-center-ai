# Panel de administración + Facturación por minuto

Aplicación web (Flask) para gestionar toda la plataforma ITSP desde el
navegador y facturar el consumo por minuto. Conecta a la misma base de
datos MariaDB de Asterisk. **Probado:** todas las rutas, plantillas y la
lógica de tarificación pasan pruebas automáticas (ver más abajo).

## Qué puedes hacer desde el panel

- **Clientes**: crear/editar, tipo de facturación (prepago/pospago), saldo,
  límite de crédito, moneda, plan de tarifas, canales máximos, activar/desactivar.
- **Conexiones SIP**: un cliente puede tener **varias** (una por sede, campaña
  u operador), cada una con su usuario/clave, transporte (UDP/TLS), canales,
  IP permitida y ruta de proveedores propia.
- **Números DID**: asignar los números propios que usará como Caller ID.
- **Proveedores**: alta/edición de mayoristas (Telnyx y otros), auth por
  usuario/clave o por IP, prioridad para el enrutamiento de menor costo (LCR).
- **Planes y tarifas**: tarifas por prefijo (precio/min, cargo de conexión,
  duración mínima, incremento). Importación masiva por CSV.
- **Facturación**: tarificar las llamadas y generar facturas por periodo y
  cliente, con desglose por prefijo y margen estimado.
- **CDR y fraude**: navegar el historial de llamadas y los cortes automáticos.
- **Aplicar y recargar**: botón que regenera la config de Asterisk desde la BD
  y la recarga. Todo lo que editas en el panel se refleja en el sistema real.

## Archivos

```
panel/
├── app.py                 # Aplicación Flask (todas las rutas)
├── models.py              # Modelos de datos (SQLAlchemy)
├── config_generator.py    # Genera pjsip/extensions desde la BD y recarga
├── crear_admin.py         # Crea el usuario administrador
├── func_odbc-additions.conf  # Función ODBC CLIENT_ACTIVE para el dialplan
├── requirements.txt
├── itsp-panel.service     # systemd (gunicorn) + timers de rating
├── static/style.css
└── templates/             # 14 plantillas HTML
billing/
├── schema-billing.sql     # Tablas: providers, connections, planes, tarifas, facturas
└── rating.py              # Motor de tarificación (match de prefijo + incrementos)
```

## Instalación

```bash
# 1. Copiar el proyecto al servidor
sudo mkdir -p /opt/itsp
sudo cp -r panel billing /opt/itsp/

# 2. Base de datos (después de schema.sql y schema-monitoring.sql)
sudo mysql asterisk < billing/schema-billing.sql

# 3. Usuario de BD con escritura (el panel y el rating lo usan)
#    (si no lo creaste ya en el README principal)
sudo mysql -e "CREATE USER IF NOT EXISTS 'asterisk_rw'@'localhost' IDENTIFIED BY 'PASS_RW';
               GRANT SELECT,INSERT,UPDATE,DELETE ON asterisk.* TO 'asterisk_rw'@'localhost';
               FLUSH PRIVILEGES;"

# 4. Dependencias Python
cd /opt/itsp/panel
sudo pip install -r requirements.txt --break-system-packages

# 5. Función ODBC que necesita el dialplan generado
cat func_odbc-additions.conf | sudo tee -a /etc/asterisk/func_odbc.conf
sudo asterisk -rx "module reload func_odbc.so"

# 6. Usuario administrador del panel
export ITSP_DB_USER=asterisk_rw ITSP_DB_PASS=PASS_RW
python3 crear_admin.py admin "UNA_CLAVE_FUERTE"

# 7. Servicio web
sudo cp itsp-panel.service /etc/systemd/system/
#    edita las variables (ITSP_DB_PASS, ITSP_SECRET) dentro del service
sudo useradd -r -s /usr/sbin/nologin itsp || true
sudo systemctl daemon-reload
sudo systemctl enable --now itsp-panel
```

El panel queda en `127.0.0.1:8080`. **Ponlo detrás de nginx con HTTPS** — no lo
expongas directo a internet. Ejemplo mínimo de nginx:

```nginx
server {
  listen 443 ssl;
  server_name panel.tudominio.com;
  ssl_certificate     /etc/letsencrypt/live/panel.tudominio.com/fullchain.pem;
  ssl_certificate_key /etc/letsencrypt/live/panel.tudominio.com/privkey.pem;
  location / { proxy_pass http://127.0.0.1:8080; proxy_set_header Host $host; }
}
```

## Permisos para "Aplicar y recargar"

El botón ejecuta `config_generator.py` con sudo (escribe en `/etc/asterisk` y
recarga). Da ese permiso puntual al usuario del panel en `/etc/sudoers.d/itsp`:

```
itsp ALL=(root) NOPASSWD: /usr/bin/python3 /opt/itsp/panel/config_generator.py
```

Deja en `/etc/asterisk/pjsip.conf` y `extensions.conf` solo la parte fija
(transports, globals, subrutinas del dialplan) más:
```
#include "pjsip_gen.conf"
#include "extensions_gen.conf"
```

## Tarificación automática (opcional)

Además del botón "Tarificar" del panel, puedes tarificar en automático cada
5 minutos activando el timer comentado en `itsp-panel.service`
(`itsp-rating.timer`).

## Cómo funciona la facturación por minuto

1. Cada llamada genera un CDR (por el módulo de monitoreo).
2. El motor `rating.py` toma los CDR nuevos y, por cada uno:
   - identifica el cliente (accountcode) y su plan de tarifas,
   - busca el **prefijo más largo** que coincide con el destino,
   - calcula los segundos facturables aplicando duración mínima e incremento
     (ej. 60/60 = por minuto; 30/6 = mínimo 30s luego bloques de 6s),
   - importe = cargo_conexión + precio_min × minutos_facturables,
   - si el cliente es prepago, descuenta del saldo y lo **corta** si llega a 0.
3. "Generar facturas" agrupa esos importes por cliente y periodo en una factura
   con desglose por prefijo y margen (si cargaste tarifas de compra).

## Validación ya realizada

- Las 13 vistas del panel renderizan sin error (prueba automática de rutas y
  plantillas contra BD SQLite).
- Alta de cliente y generación de factura vía POST: OK.
- Lógica de `billed_seconds` e identificación de proveedor: correcta
  (per-minuto, mínimo 30s, incremento 6s verificados).
- El generador produce `pjsip`/`extensions` correctos, incluyendo conexiones
  TLS con SRTP y los `identify` por IP de proveedor.

Lo que debes validar tú en tu servidor (requiere MariaDB + Asterisk reales):
tarificar CDR reales, el botón "Aplicar y recargar" contra tu Asterisk, y el
login del panel detrás de nginx/HTTPS.
