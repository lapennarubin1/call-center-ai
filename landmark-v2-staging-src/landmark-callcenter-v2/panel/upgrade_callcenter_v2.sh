#!/usr/bin/env bash
# ═══════════════════════════════════════════════════════════════════════
#  Landmark Panel — actualización a Call Center V2
#
#  NO SE EJECUTA SOLO. Hay que invocarlo a mano, con sudo.
#
#  Qué hace:
#    · respalda el panel entero, con fecha
#    · copia los módulos, plantillas y estáticos de V2
#    · CONSERVA .env, el virtualenv, los logs y los datos
#    · valida que todo importa y que las plantillas compilan
#    · sólo entonces reinicia el servicio
#
#  Qué NO hace, nunca:
#    · ejecutar SQL            (la migración es una decisión aparte)
#    · activar workflows
#    · tocar Asterisk o el worker de Stringee
#    · cambiar el modo de operación
#    · resetear contraseñas
#
#  Uso:
#      sudo DRY_RUN=1 bash panel/upgrade_callcenter_v2.sh   # sólo enseña
#      sudo bash panel/upgrade_callcenter_v2.sh             # lo aplica
#
#  Si la validación falla, el servicio NO se reinicia y el panel sigue
#  corriendo con el código anterior. La vuelta atrás es restaurar el
#  respaldo: la ruta exacta se imprime al final.
# ═══════════════════════════════════════════════════════════════════════
set -uo pipefail

APP_DIR="${LM_APP_DIR:-/opt/landmark-panel}"
SERVICE="${LM_SERVICE:-landmark-panel}"
DRY_RUN="${DRY_RUN:-0}"
SRC="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
STAMP="$(date +%Y%m%d-%H%M%S)"
BACKUP_DIR="${LM_BACKUP_DIR:-/opt/landmark-panel-backups}/$STAMP"

c_ok()   { echo -e "  \033[32m✔\033[0m $*"; }
c_info() { echo -e "  \033[34m→\033[0m $*"; }
c_warn() { echo -e "  \033[33m!\033[0m $*"; }
c_err()  { echo -e "  \033[31m✘\033[0m $*" >&2; }
c_head() { echo -e "\n\033[1m$*\033[0m"; }
die()    { c_err "$*"; echo; c_err "ABORTADO. No se tocó nada del panel."; exit 1; }

# Lo que se copia. NUEVOS + MODIFICADOS del PANEL_PRODUCTION_PATCH_MANIFEST.
MODULOS=(
  routes_config.py call_jobs.py followup_engine.py ops_events.py
  analytics_v2.py v2_suite.py billing.py legacy_mode.py payments.py
  wf_settings.py crm_notes.py tool_requests.py recording_ledger.py
  server.py
)
PLANTILLAS=(
  routes.html country.html analytics.html issues.html settings.html
  billing.html legacy.html base.html
)
# Lo que NUNCA se toca, exista o no en el paquete.
PRESERVAR=( .env .session_key venv logs run instance )

echo "═══════════════════════════════════════════════════════════════════"
echo "  Landmark Panel → Call Center V2"
if [[ "$DRY_RUN" == "1" ]]; then
  echo "  MODO PRUEBA — no se escribe ni un byte"
fi
echo "═══════════════════════════════════════════════════════════════════"

# ── 1 · comprobaciones previas ────────────────────────────────────────
c_head "1/7  Comprobaciones previas"

[[ $EUID -eq 0 ]] || die "Ejecutá con sudo."
[[ -d "$APP_DIR" ]] || die "No existe $APP_DIR. Si el panel está en otro sitio: LM_APP_DIR=/ruta sudo bash $0"
[[ -f "$APP_DIR/app/server.py" ]] || die "$APP_DIR no parece un panel de Landmark: falta app/server.py"
[[ -d "$SRC/app" ]] || die "No encuentro el paquete de origen junto a este script."
command -v python3 >/dev/null || die "Falta python3."
c_ok "Panel encontrado en $APP_DIR"

# El .env es lo primero que hay que tener claro: sin él no se arranca.
if [[ -f "$APP_DIR/.env" ]]; then
  c_ok ".env presente — se conserva intacto ($(grep -c '^[A-Z]' "$APP_DIR/.env") variables)"
else
  c_warn "No hay .env en $APP_DIR."
  c_warn "El panel V2 NO arranca sin credenciales: ninguna tiene valor por"
  c_warn "defecto en el código. Plantilla: panel/.env.example"
fi

# Credenciales que V2 exige y que quizá no estén todavía.
if [[ -f "$APP_DIR/.env" ]]; then
  FALTAN=()
  for V in LM_DB_PASS LM_PASS LM_MASTER_PASS LM_SUPPORT_PASS; do
    grep -q "^$V=." "$APP_DIR/.env" || FALTAN+=("$V")
  done
  if [[ ${#FALTAN[@]} -gt 0 ]]; then
    c_warn "Faltan en el .env: ${FALTAN[*]}"
    c_warn "Antes traían un valor por defecto en el código; ya no."
    c_warn "El panel no arrancará hasta definirlas."
  fi
  for V in LM_N8N_BASE_URL LM_N8N_API_KEY; do
    grep -q "^$V=." "$APP_DIR/.env" || \
      c_warn "Sin $V el panel no podrá verificar el estado de los workflows legacy"
  done
fi

# ── 2 · qué va a cambiar ──────────────────────────────────────────────
c_head "2/7  Qué va a cambiar"

NUEVOS=0; CAMBIAN=0; IGUALES=0
comparar() {
  local origen="$1" destino="$2"
  if [[ ! -f "$destino" ]]; then
    echo "     NUEVO       $(basename "$destino")"; ((NUEVOS++))
  elif ! cmp -s "$origen" "$destino"; then
    echo "     MODIFICADO  $(basename "$destino")"; ((CAMBIAN++))
  else
    ((IGUALES++))
  fi
}
for m in "${MODULOS[@]}";    do comparar "$SRC/app/$m" "$APP_DIR/app/$m"; done
for t in "${PLANTILLAS[@]}"; do comparar "$SRC/app/templates/$t" "$APP_DIR/app/templates/$t"; done
if [[ -f "$SRC/app/static/panel.css" ]]; then
  comparar "$SRC/app/static/panel.css" "$APP_DIR/app/static/panel.css"
fi
echo
c_info "$NUEVOS nuevos · $CAMBIAN modificados · $IGUALES ya iguales"
c_info "Inventario completo: PANEL_PRODUCTION_PATCH_MANIFEST.md"

if [[ "$DRY_RUN" == "1" ]]; then
  c_head "MODO PRUEBA — hasta aquí"
  echo
  c_info "No se escribió nada. Para aplicarlo:"
  echo "     sudo bash $0"
  exit 0
fi

# ── 3 · respaldo ──────────────────────────────────────────────────────
c_head "3/7  Respaldo"

mkdir -p "$BACKUP_DIR" || die "No pude crear $BACKUP_DIR"
cp -a "$APP_DIR/." "$BACKUP_DIR/" || die "El respaldo falló. No se sigue."
chmod 700 "$BACKUP_DIR"
c_ok "Panel completo respaldado en $BACKUP_DIR"
c_info "Vuelta atrás:  systemctl stop $SERVICE && rm -rf $APP_DIR && cp -a $BACKUP_DIR $APP_DIR && systemctl start $SERVICE"

# ── 4 · copia ─────────────────────────────────────────────────────────
c_head "4/7  Copiando V2"

STAGE="$(mktemp -d)"
trap 'rm -rf "$STAGE"' EXIT
cp -a "$APP_DIR/." "$STAGE/" || die "No pude preparar el área de trabajo"

copiar() {
  local origen="$1" destino="$2"
  [[ -f "$origen" ]] || die "Falta en el paquete: $origen"
  mkdir -p "$(dirname "$destino")"
  cp -p "$origen" "$destino" || die "No pude copiar $(basename "$origen")"
}
for m in "${MODULOS[@]}";    do copiar "$SRC/app/$m" "$STAGE/app/$m"; done
for t in "${PLANTILLAS[@]}"; do copiar "$SRC/app/templates/$t" "$STAGE/app/templates/$t"; done
[[ -f "$SRC/app/static/panel.css" ]] && copiar "$SRC/app/static/panel.css" "$STAGE/app/static/panel.css"
c_ok "${#MODULOS[@]} módulos y ${#PLANTILLAS[@]} plantillas preparados"

# Lo que se preserva: se restaura desde el panel vivo, por si la copia lo pisó.
for keep in "${PRESERVAR[@]}"; do
  if [[ -e "$APP_DIR/$keep" ]]; then
    rm -rf "$STAGE/$keep" 2>/dev/null
    cp -a "$APP_DIR/$keep" "$STAGE/$keep"
    c_ok "Conservado: $keep"
  fi
  if [[ -e "$APP_DIR/app/$keep" ]]; then
    rm -rf "$STAGE/app/$keep" 2>/dev/null
    cp -a "$APP_DIR/app/$keep" "$STAGE/app/$keep"
    c_ok "Conservado: app/$keep"
  fi
done

# ── 5 · validación ANTES de tocar el panel vivo ───────────────────────
c_head "5/7  Validación (antes de reiniciar nada)"

PY="$APP_DIR/venv/bin/python3"
[[ -x "$PY" ]] || PY="$(command -v python3)"
c_info "Intérprete: $PY"

# 5a · compila todo
if ! "$PY" -m compileall -q "$STAGE/app" >/dev/null 2>&1; then
  "$PY" -m compileall "$STAGE/app" 2>&1 | tail -20
  die "Hay Python que no compila. El panel NO se ha tocado."
fi
c_ok "Todo el Python compila"

# 5b · importa de verdad, con credenciales de mentira para no exigir el .env
IMPORT_OUT=$(cd "$STAGE/app" && env \
  LM_SCHEDULER_DISABLED=1 LM_TELEGRAM_DISABLED=1 \
  LM_DB_PASS=validacion LM_PASS=validacion \
  LM_MASTER_PASS=validacion LM_SUPPORT_PASS=validacion \
  LM_SECRET=validacion \
  "$PY" -c "
import sys
sys.dont_write_bytecode = True
import server
import legacy_mode, analytics
assert getattr(analytics.n8n_switch_set_state, '_lm_guarded', False), \
    'el cerrojo del modo de operacion NO quedo instalado'
print('OK', len([r for r in server.app.url_map.iter_rules()]), 'rutas')
" 2>&1)
if [[ $? -ne 0 ]] || [[ "$IMPORT_OUT" != OK* ]]; then
  echo "$IMPORT_OUT" | tail -20
  die "El panel no importa. NO se ha tocado nada."
fi
c_ok "Importa correctamente · $IMPORT_OUT"

# 5c · las plantillas compilan
TPL_OUT=$(cd "$STAGE/app" && "$PY" -c "
import sys, os
sys.dont_write_bytecode = True
try:
    from jinja2 import Environment, FileSystemLoader
except ImportError:
    print('SKIP jinja2 no disponible'); raise SystemExit(0)
e = Environment(loader=FileSystemLoader('templates'))
malas = []
for f in sorted(os.listdir('templates')):
    if not f.endswith('.html'):
        continue
    try:
        e.parse(open(os.path.join('templates', f), encoding='utf-8').read())
    except Exception as ex:
        malas.append(f'{f}: {ex}')
print('FAIL ' + '; '.join(malas) if malas else 'OK plantillas')
" 2>&1)
case "$TPL_OUT" in
  OK*)   c_ok "$TPL_OUT" ;;
  SKIP*) c_warn "$TPL_OUT" ;;
  *)     echo "$TPL_OUT"; die "Hay plantillas rotas. NO se ha tocado nada." ;;
esac

# ── 6 · aplicar ───────────────────────────────────────────────────────
c_head "6/7  Aplicando"

rsync -a --delete-after \
      --exclude='.env' --exclude='.session_key' --exclude='venv' \
      --exclude='logs' --exclude='run' --exclude='instance' \
      "$STAGE/" "$APP_DIR/" 2>/dev/null || {
  cp -a "$STAGE/app/." "$APP_DIR/app/" || die "La copia falló. Restaurá: $BACKUP_DIR"
}
c_ok "Ficheros aplicados"

# ── 7 · reinicio ──────────────────────────────────────────────────────
c_head "7/7  Servicio"

if systemctl list-unit-files 2>/dev/null | grep -q "^$SERVICE.service"; then
  systemctl restart "$SERVICE" && sleep 3
  if systemctl is-active --quiet "$SERVICE"; then
    c_ok "$SERVICE activo"
  else
    c_err "$SERVICE NO arrancó. Últimas líneas del log:"
    journalctl -u "$SERVICE" -n 25 --no-pager | sed 's/^/     /'
    echo
    c_err "Para volver atrás:"
    echo "     systemctl stop $SERVICE"
    echo "     rm -rf $APP_DIR && cp -a $BACKUP_DIR $APP_DIR"
    echo "     systemctl start $SERVICE"
    exit 1
  fi
else
  c_warn "No encontré el servicio $SERVICE. Reiniciá el panel a mano."
fi

echo
echo "═══════════════════════════════════════════════════════════════════"
c_ok "Panel actualizado a Call Center V2"
echo "═══════════════════════════════════════════════════════════════════"
echo
c_info "Respaldo: $BACKUP_DIR"
echo
c_warn "LO QUE ESTE SCRIPT NO HIZO, a propósito:"
echo "     · la migración SQL           → sql/migration.sql, decisión aparte"
echo "     · activar workflows          → se importan con active:false"
echo "     · cambiar el modo            → arranca en LEGACY_BACKUP"
echo "     · tocar Asterisk o Stringee"
echo
c_info "El modo de operación arranca en LEGACY_BACKUP: V2 no llama a nadie"
c_info "hasta que alguien haga el cutover desde Call Center → Legacy Backup,"
c_info "que verifica contra n8n que el despachador viejo está apagado."
