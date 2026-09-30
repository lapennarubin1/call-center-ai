#!/bin/bash
# deploy.sh — despliegue del panel Landmark Markets.
# Uso: bash deploy.sh landmark-panel-release.tar.gz
#
# Qué hace, siempre en el mismo orden, sin pasos manuales sueltos:
#   1. Verifica que el .tar.gz exista
#   2. Extrae a una carpeta temporal (nunca pisa nada a medias)
#   3. Reemplaza /opt/landmark-panel/app/ completo (atómico: mv, no copy archivo por archivo)
#   4. Borra __pycache__ (bytecode viejo nunca sobrevive a un deploy)
#   5. Mata gunicorn a la fuerza y lo levanta de nuevo (un "restart" normal
#      a veces no basta si un worker quedó colgado)
#   6. Health-check automático al final — si falla, te avisa en rojo

set -e  # cualquier error corta el script, no sigue "a medias"

PKG="$1"
APP_DIR="/opt/landmark-panel/app"
TMP_DIR="/tmp/landmark-deploy-$$"

if [ -z "$PKG" ]; then
  echo "❌ Uso: bash deploy.sh archivo.tar.gz"
  exit 1
fi
if [ ! -f "$PKG" ]; then
  echo "❌ No existe el archivo: $PKG"
  exit 1
fi

echo "── 1/6 Extrayendo paquete ──"
mkdir -p "$TMP_DIR"
tar -xzf "$PKG" -C "$TMP_DIR"

if [ ! -d "$TMP_DIR/app" ]; then
  echo "❌ El .tar.gz no tiene una carpeta app/ adentro. Abortando."
  rm -rf "$TMP_DIR"
  exit 1
fi

echo "── 2/6 Preservando estado runtime (session key) ──"
if [ -f "$APP_DIR/.session_key" ]; then
  cp "$APP_DIR/.session_key" "$TMP_DIR/app/.session_key"
fi

echo "── 3/6 Reemplazando código (atómico) ──"
rm -rf "${APP_DIR}.old"
mv "$APP_DIR" "${APP_DIR}.old" 2>/dev/null || true
mv "$TMP_DIR/app" "$APP_DIR"
rm -rf "${APP_DIR}.old" "$TMP_DIR"

echo "── 4/6 Limpiando bytecode viejo ──"
find /opt/landmark-panel -name "__pycache__" -exec rm -rf {} + 2>/dev/null || true

echo "── 5/6 Reiniciando servicio (kill duro, no soft-restart) ──"
systemctl stop landmark-panel 2>/dev/null || true
pkill -9 -f "gunicorn.*landmark" 2>/dev/null || true
sleep 2
systemctl start landmark-panel
sleep 2

echo "── 6/6 Health check ──"
HEALTH=$(curl -s -w "\n%{http_code}" localhost:8080/health)
CODE=$(echo "$HEALTH" | tail -1)
BODY=$(echo "$HEALTH" | head -1)

if [ "$CODE" = "200" ]; then
  echo "✅ Deploy OK — $BODY"
else
  echo "🔴 Deploy con problemas — HTTP $CODE"
  echo "$BODY"
  echo "Revisá: journalctl -u landmark-panel -n 30 --no-pager"
  exit 1
fi
