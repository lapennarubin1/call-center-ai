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
DB_PASS="${LM_DB_PASS:-PanelPass2026xK}"
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
    PANEL_PASS=$(tr -dc 'A-Za-z0-9' </dev/urandom | head -c 16 || true)
    NEW_PASS=1
  fi
fi
SECRET=$(tr -dc 'a-f0-9' </dev/urandom | head -c 64 || true)

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
cat > "$APP_DIR/.env" <<EOF
LM_DB_HOST=localhost
LM_DB_USER=$DB_USER
LM_DB_PASS=$DB_PASS
LM_DB_NAME=$DB_NAME
LM_PORT=$PORT
LM_RATE=0.06
LM_USER=$PANEL_USER
LM_PASS=$PANEL_PASS
LM_SECRET=$SECRET
EOF
chmod 600 "$APP_DIR/.env"
c_ok "Credenciales guardadas en $APP_DIR/.env (sólo root puede leerlo)"

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
  echo -e "  Contraseña: \033[1m$PANEL_PASS\033[0m   ← guardala, no se vuelve a mostrar"
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
