#!/usr/bin/env bash
# ══════════════════════════════════════════════════════════════
#  JARVIS — Instalación
#  Idempotente: se puede correr varias veces sin romper nada.
# ══════════════════════════════════════════════════════════════
set -euo pipefail

DEST="${JARVIS_HOME:-/opt/jarvis}"
SERVICE="jarvis"
PORT="${JARVIS_PORT:-8090}"

say(){ printf '\n\033[36m▸ %s\033[0m\n' "$*"; }
warn(){ printf '\033[33m  ! %s\033[0m\n' "$*"; }
ok(){ printf '\033[32m  ✓ %s\033[0m\n' "$*"; }

[ "$(id -u)" -eq 0 ] || { echo "Ejecutá como root (sudo)."; exit 1; }

say "Instalando dependencias del sistema"
if command -v apt-get >/dev/null; then
  apt-get update -qq
  apt-get install -y -qq python3 python3-venv python3-pip >/dev/null
fi
ok "python3 $(python3 --version 2>&1 | cut -d' ' -f2)"

say "Preparando $DEST"
mkdir -p "$DEST/data"
# Copia el código desde donde esté este script (soporta ejecutarlo
# desde el tar recién descomprimido).
SRC="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
if [ "$SRC" != "$DEST" ]; then
  cp -r "$SRC/app" "$DEST/"
  cp "$SRC/requirements.txt" "$DEST/"
  [ -f "$SRC/.env.example" ] && cp "$SRC/.env.example" "$DEST/"
fi
ok "código en $DEST"

say "Entorno virtual"
[ -d "$DEST/venv" ] || python3 -m venv "$DEST/venv"
"$DEST/venv/bin/pip" install -q --upgrade pip
"$DEST/venv/bin/pip" install -q -r "$DEST/requirements.txt"
ok "dependencias instaladas"

say "Configuración"
if [ ! -f "$DEST/.env" ]; then
  cp "$DEST/.env.example" "$DEST/.env"
  # Clave de sesión aleatoria de entrada: nadie debería quedarse con
  # la de ejemplo por olvido.
  SECRET=$(python3 -c 'import secrets;print(secrets.token_hex(32))')
  sed -i "s|^JARVIS_SECRET_KEY=.*|JARVIS_SECRET_KEY=$SECRET|" "$DEST/.env"
  warn "Creado $DEST/.env — HAY QUE COMPLETAR las claves antes de arrancar"
else
  ok ".env ya existe (no se toca)"
fi
chmod 600 "$DEST/.env"

say "Servicio systemd"
cat > "/etc/systemd/system/${SERVICE}.service" <<UNIT
[Unit]
Description=JARVIS — Cerebro del Call Center
After=network.target mysql.service mariadb.service

[Service]
Type=simple
WorkingDirectory=$DEST
EnvironmentFile=$DEST/.env
ExecStart=$DEST/venv/bin/gunicorn \\
    --workers 2 --threads 4 --timeout 180 \\
    --bind 0.0.0.0:$PORT \\
    --access-logfile - --error-logfile - \\
    app.server:app
Restart=always
RestartSec=5
StandardOutput=journal
StandardError=journal

# Endurecimiento — Jarvis solo necesita escribir en su carpeta de datos
NoNewPrivileges=true
PrivateTmp=true
ProtectSystem=full
ReadWritePaths=$DEST/data

[Install]
WantedBy=multi-user.target
UNIT

systemctl daemon-reload
systemctl enable "$SERVICE" >/dev/null 2>&1 || true
ok "servicio $SERVICE registrado"

say "Listo"
cat <<FIN

  Siguiente paso — completá las claves:

      nano $DEST/.env

  Mínimo indispensable:
      ANTHROPIC_API_KEY   (el cerebro)
      ELEVENLABS_API_KEY  (la voz)
      JARVIS_PASS         (tu contraseña de acceso)
      LM_DB_PASS          (base del panel)

  Después:

      systemctl restart $SERVICE
      systemctl status $SERVICE
      curl -s localhost:$PORT/health

  Y entrá a:  http://<IP-DEL-SERVIDOR>:$PORT

FIN
