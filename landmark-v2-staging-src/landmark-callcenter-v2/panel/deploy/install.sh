#!/usr/bin/env bash
# ════════════════════════════════════════════════════════════════
#  Landmark Markets — Panel de Control
#  Instalador para el VPS (Ubuntu 24.04)
#
#  Uso:  sudo bash install.sh
#
#  Es idempotente: se puede volver a ejecutar sin romper nada.
#  No toca Asterisk, ni el CDR, ni n8n.
# ════════════════════════════════════════════════════════════════
set -euo pipefail

APP_DIR=/opt/landmark-panel
SERVICE=landmark-panel
PORT="${LM_PORT:-8080}"
DB_NAME="${LM_DB_NAME:-asterisk}"
DB_USER="${LM_DB_USER:-panel_rw}"
# V2/r2: la contrasena de la BD ya NO trae un valor por defecto en claro.
# Venia fijada aqui y viajaba en cada copia del paquete. Ahora es
# obligatoria por entorno; el instalador se detiene si falta.
DB_PASS="${LM_DB_PASS:-}"
if [[ -z "$DB_PASS" ]]; then
  echo "Falta LM_DB_PASS. Exportala antes de instalar:" >&2
  echo "  sudo LM_DB_PASS='<clave de panel_rw>' bash install.sh" >&2
  echo "Si es una instalacion existente, es la misma clave que ya" >&2
  echo "figura en $APP_DIR/.env (LM_DB_PASS)." >&2
  exit 1
fi
PANEL_USER="${LM_USER:-admin}"
PANEL_PASS="${LM_PASS:-}"

c_ok()   { echo -e "  \033[32m✔\033[0m $*"; }
c_info() { echo -e "  \033[34m→\033[0m $*"; }
c_warn() { echo -e "  \033[33m!\033[0m $*"; }
c_head() { echo -e "\n\033[1m$*\033[0m"; }

[[ $EUID -eq 0 ]] || { echo "Ejecutá con sudo."; exit 1; }

# ── Contraseña del panel ────────────────────────────────────────
if [[ -z "$PANEL_PASS" ]]; then
  if [[ -f "$APP_DIR/.env" ]] && grep -q '^LM_PASS=' "$APP_DIR/.env"; then
    PANEL_PASS=$(grep '^LM_PASS=' "$APP_DIR/.env" | cut -d= -f2-)
    c_info "Conservando la contraseña del panel ya configurada"
  else
    PANEL_PASS=$(tr -dc 'A-Za-z0-9' </dev/urandom | head -c 16)
    NEW_PASS=1
  fi
fi
SECRET=$(tr -dc 'a-f0-9' </dev/urandom | head -c 64)

c_head "1/6  Dependencias del sistema"
export DEBIAN_FRONTEND=noninteractive
apt-get update -qq
apt-get install -y -qq python3-venv python3-pip >/dev/null
c_ok "python3-venv y pip listos"

c_head "2/6  Archivos de la aplicación"
mkdir -p "$APP_DIR"
SRC="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cp -r "$SRC/app/." "$APP_DIR/"
c_ok "Copiado en $APP_DIR"

c_head "3/6  Entorno virtual de Python"
python3 -m venv "$APP_DIR/venv" 2>/dev/null || true
"$APP_DIR/venv/bin/pip" install -q --upgrade pip
"$APP_DIR/venv/bin/pip" install -q -r "$SRC/deploy/requirements.txt"
c_ok "Dependencias instaladas"

c_head "4/6  Base de datos"
if mysql -e "SELECT 1" >/dev/null 2>&1; then
  # Los CREATE INDEX fallan si ya existen (error 1061): es esperado al reinstalar.
  mysql "$DB_NAME" < "$SRC/deploy/schema.sql" 2>/dev/null || {
    c_warn "Algunos índices ya existían — normal si reinstalás"
    grep -v '^CREATE INDEX' "$SRC/deploy/schema.sql" | mysql "$DB_NAME"
  }
  ROWS=$(mysql -N -B "$DB_NAME" -e "SELECT COUNT(*) FROM cdr" 2>/dev/null || echo 0)
  c_ok "Tablas del panel creadas · CDR con $ROWS registros"
else
  c_warn "No pude conectar a MySQL como root. Ejecutá a mano:"
  echo "     mysql $DB_NAME < $SRC/deploy/schema.sql"
fi

c_head "5/6  Configuración"

# Credenciales que hay que reclamarle al operador al terminar.
MISSING_CREDS=()
MISSING_N8N=()

# ── .env: SE FUSIONA, NO SE SOBRESCRIBE ───────────────────────────────
#
# La versión anterior hacía `cat > .env` con nueve variables. Sobre una
# instalación existente eso BORRABA:
#
#     LM_MASTER_PASS · LM_SUPPORT_PASS · LM_ROUTES_API_TOKEN
#     LM_N8N_BASE_URL · LM_N8N_API_KEY · y cualquier otra añadida a mano
#
# y regeneraba LM_SECRET en cada pasada, deslogueando a todo el mundo.
#
# Ahora: se respalda, se conserva TODO lo que hubiera, y sólo se tocan
# las variables que el instalador aporta explícitamente. Una contraseña
# ya configurada nunca se resetea.
ENV_FILE="$APP_DIR/.env"

# Lee el valor actual de una variable del .env, o vacío si no está.
env_get() {
  [[ -f "$ENV_FILE" ]] || { echo ""; return; }
  sed -n "s/^$1=//p" "$ENV_FILE" | tail -1
}

# Fija una variable SÓLO si aún no tiene valor. Nunca pisa lo existente.
env_set_if_empty() {
  local clave="$1" valor="$2"
  local actual; actual=$(env_get "$clave")
  if [[ -n "$actual" ]]; then
    return 0                      # ya configurada: no se toca
  fi
  if grep -q "^$clave=" "$ENV_FILE" 2>/dev/null; then
    sed -i "s|^$clave=.*|$clave=$valor|" "$ENV_FILE"
  else
    echo "$clave=$valor" >> "$ENV_FILE"
  fi
}

# Fija una variable aportada explícitamente por el instalador.
env_set() {
  local clave="$1" valor="$2"
  [[ -n "$valor" ]] || return 0
  if grep -q "^$clave=" "$ENV_FILE" 2>/dev/null; then
    sed -i "s|^$clave=.*|$clave=$valor|" "$ENV_FILE"
  else
    echo "$clave=$valor" >> "$ENV_FILE"
  fi
}

if [[ -f "$ENV_FILE" ]]; then
  BACKUP="$ENV_FILE.bak.$(date +%Y%m%d-%H%M%S)"
  cp -p "$ENV_FILE" "$BACKUP"
  chmod 600 "$BACKUP"
  c_ok "Respaldo del .env anterior: $(basename "$BACKUP")"
  EXISTING=$(grep -c '^[A-Z]' "$ENV_FILE" || echo 0)
  c_info "Se conservan las $EXISTING variables que ya estaban"
else
  touch "$ENV_FILE"
  chmod 600 "$ENV_FILE"
  c_info "Creando un .env nuevo"
fi

# Lo que el instalador aporta: conexión y puerto.
env_set LM_DB_HOST localhost
env_set LM_DB_USER "$DB_USER"
env_set LM_DB_PASS "$DB_PASS"
env_set LM_DB_NAME "$DB_NAME"
env_set LM_PORT    "$PORT"
env_set_if_empty LM_RATE 0.06
env_set_if_empty LM_USER "$PANEL_USER"

# Contraseñas: SÓLO si no había ninguna. Nunca se resetea una existente.
env_set_if_empty LM_PASS "$PANEL_PASS"

# La clave de sesión se genera una vez y se conserva. Regenerarla en cada
# instalación desloguea a todo el mundo sin motivo.
env_set_if_empty LM_SECRET "$SECRET"

# El token de servicio se genera si falta: sin él, n8n no puede leer la
# configuración de rutas.
env_set_if_empty LM_ROUTES_API_TOKEN "$(tr -dc 'a-f0-9' </dev/urandom | head -c 64)"

# Credenciales que el instalador NO inventa. Se dejan vacías y se avisa:
# generarlas aquí sería crear accesos que el operador no eligió.
for REQ in LM_MASTER_PASS LM_SUPPORT_PASS; do
  if [[ -z "$(env_get $REQ)" ]]; then
    grep -q "^$REQ=" "$ENV_FILE" || echo "$REQ=" >> "$ENV_FILE"
    MISSING_CREDS+=("$REQ")
  fi
done

# Configuración de n8n: necesaria para el modo de operación. No se inventa.
for OPT in LM_N8N_BASE_URL LM_N8N_API_KEY; do
  grep -q "^$OPT=" "$ENV_FILE" || echo "$OPT=" >> "$ENV_FILE"
  [[ -z "$(env_get $OPT)" ]] && MISSING_N8N+=("$OPT")
done

chmod 600 "$ENV_FILE"
c_ok "Configuración en $ENV_FILE (sólo root puede leerlo)"

if [[ ${#MISSING_CREDS[@]} -gt 0 ]]; then
  c_warn "FALTAN credenciales obligatorias: ${MISSING_CREDS[*]}"
  c_warn "El panel NO arrancará hasta que las definas en $ENV_FILE"
  c_warn "Ninguna contraseña tiene valor por defecto en el código."
fi
if [[ ${#MISSING_N8N[@]} -gt 0 ]]; then
  c_warn "Sin ${MISSING_N8N[*]} el panel no podrá verificar el estado de"
  c_warn "los workflows legacy, y el cambio a V2_PRIMARY quedará bloqueado."
fi

c_head "6/6  Servicio del sistema"
cat > /etc/systemd/system/$SERVICE.service <<EOF
[Unit]
Description=Landmark Markets Control Panel
After=network.target mariadb.service mysql.service

[Service]
Type=simple
WorkingDirectory=$APP_DIR
EnvironmentFile=$APP_DIR/.env
ExecStart=$APP_DIR/venv/bin/gunicorn \\
    --workers 3 --threads 2 --timeout 120 \\
    --bind 0.0.0.0:$PORT \\
    --access-logfile - --error-logfile - \\
    server:app
Restart=always
RestartSec=5
StandardOutput=journal
StandardError=journal

# Endurecimiento básico
NoNewPrivileges=true
PrivateTmp=true
ProtectSystem=full

[Install]
WantedBy=multi-user.target
EOF

systemctl daemon-reload
systemctl enable -q $SERVICE
systemctl restart $SERVICE
sleep 3

if systemctl is-active --quiet $SERVICE; then
  c_ok "Servicio activo"
else
  c_warn "El servicio no arrancó. Mirá el detalle con:  journalctl -u $SERVICE -n 40"
  exit 1
fi

# ── Comprobación real de salud ──────────────────────────────────
HEALTH=$(curl -fsS "http://127.0.0.1:$PORT/health" 2>/dev/null || echo '')
IP=$(hostname -I | awk '{print $1}')

echo
echo "════════════════════════════════════════════════════════════"
if [[ -n "$HEALTH" ]]; then
  c_ok "El panel responde correctamente"
  echo "     $HEALTH"
else
  c_warn "El panel arrancó pero /health no respondió todavía"
fi
echo "════════════════════════════════════════════════════════════"
echo
echo "  Abrir en:   http://$IP:$PORT"
echo "  Usuario:    $PANEL_USER"
if [[ "${NEW_PASS:-0}" == "1" ]]; then
  echo -e "  Se generó una contraseña para el rol viewer."
  echo -e "  Está en \033[1m$ENV_FILE\033[0m (LM_PASS), legible sólo por root."
  echo -e "  No se imprime aquí: los logs del instalador se guardan."
else
  echo "  Contraseña: (la que ya tenías; está en $APP_DIR/.env)"
fi
echo
echo "  Comandos útiles:"
echo "     systemctl restart $SERVICE     reiniciar"
echo "     systemctl status  $SERVICE     ver estado"
echo "     journalctl -u $SERVICE -f      ver registros en vivo"
echo
echo "  Falta un paso: importá WF14_Sync_Panel.json en n8n y activalo"
echo "  para que el panel muestre cuentas abiertas y conversión."
echo
