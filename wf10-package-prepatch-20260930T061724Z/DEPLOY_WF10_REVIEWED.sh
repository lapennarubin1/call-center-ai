#!/usr/bin/env bash
set -euo pipefail
ACTION="${1:-apply}"
DIR="$(cd "$(dirname "$0")" && pwd)"
WFID='TW1CHksqyf66MjQz'
SERVICE='landmarket_n8n'

need(){ [ -f "$DIR/$1" ] || { echo "ERROR: falta $DIR/$1" >&2; exit 1; }; }
find_containers(){
  N8N=$(docker ps --format '{{.ID}} {{.Names}}' | awk '$2 ~ /^landmarket_n8n(\.1\.|$)/ {print $1; exit}')
  PG=$(docker ps --format '{{.ID}} {{.Names}}' | awk '$2 ~ /^landmarket_n8n-db(\.1\.|$)/ {print $1; exit}')
  [ -n "${N8N:-}" ] && [ -n "${PG:-}" ] || { echo 'ERROR: no encuentro n8n/Postgres de landmarket.' >&2; exit 1; }
}
restart_n8n(){
  if docker service ls --format '{{.Name}}' 2>/dev/null | grep -qx "$SERVICE"; then
    docker service update --force "$SERVICE" >/dev/null
    for _ in $(seq 1 90); do
      R=$(docker service ls --filter name="$SERVICE" --format '{{.Name}} {{.Replicas}}' | awk -v s="$SERVICE" '$1==s{print $2}')
      [ "$R" = '1/1' ] && break
      sleep 1
    done
  else
    docker restart "$N8N" >/dev/null
  fi
  sleep 8
  find_containers
}
workflow_backup(){
  docker exec "$PG" psql -U postgres -d landmarket -Atc "SELECT json_build_object('id',id,'name',name,'active',active,'nodes',nodes,'connections',connections,'settings',settings)::text FROM workflow_entity WHERE id='$WFID';"
}
workflow_apply_file(){
  local FILE="$1" OUTSQL="$2"
  python3 - "$FILE" "$WFID" "$OUTSQL" <<'PY'
import json,sys
p,wfid,out=sys.argv[1:]
d=json.load(open(p,encoding='utf-8'))
for key in ('nodes','connections','settings','name'):
    if key not in d: raise SystemExit('falta '+key)
def dq(tag,s):
    if f'${tag}$' in s: raise SystemExit('delimiter collision '+tag)
    return f'${tag}${s}${tag}$'
nodes=json.dumps(d['nodes'],ensure_ascii=False,separators=(',',':'))
conn=json.dumps(d['connections'],ensure_ascii=False,separators=(',',':'))
settings=json.dumps(d['settings'],ensure_ascii=False,separators=(',',':'))
name=d['name']
sql=f"""BEGIN;
UPDATE workflow_entity
SET nodes={dq('WF10N',nodes)}::json,
    connections={dq('WF10C',conn)}::json,
    settings={dq('WF10S',settings)}::json,
    name={dq('WF10M',name)}
WHERE id='{wfid}';
COMMIT;
"""
open(out,'w',encoding='utf-8').write(sql)
PY
  docker exec -i "$PG" psql -v ON_ERROR_STOP=1 -U postgres -d landmarket < "$OUTSQL" >/dev/null
  if docker exec "$PG" psql -U postgres -d landmarket -Atc "SELECT 1 FROM information_schema.columns WHERE table_name='workflow_entity' AND column_name='pinData';" | grep -qx 1; then
    docker exec "$PG" psql -v ON_ERROR_STOP=1 -U postgres -d landmarket -c "UPDATE workflow_entity SET \"pinData\"='{}'::json WHERE id='$WFID';" >/dev/null
  fi
}
workflow_restore_backup(){
  local FILE="$1" OUTSQL="$2"
  python3 - "$FILE" "$WFID" "$OUTSQL" <<'PY'
import json,sys
p,wfid,out=sys.argv[1:]
d=json.load(open(p,encoding='utf-8'))
def dq(tag,s): return f'${tag}${s}${tag}$'
nodes=json.dumps(d['nodes'],ensure_ascii=False,separators=(',',':'))
conn=json.dumps(d['connections'],ensure_ascii=False,separators=(',',':'))
settings=json.dumps(d['settings'],ensure_ascii=False,separators=(',',':'))
name=d['name']
active='true' if d.get('active') else 'false'
open(out,'w',encoding='utf-8').write(f"""BEGIN;
UPDATE workflow_entity SET nodes={dq('RB_N',nodes)}::json, connections={dq('RB_C',conn)}::json,
settings={dq('RB_S',settings)}::json, name={dq('RB_M',name)}, active={active}
WHERE id='{wfid}'; COMMIT;\n""")
PY
  docker exec -i "$PG" psql -v ON_ERROR_STOP=1 -U postgres -d landmarket < "$OUTSQL" >/dev/null
}

for f in WF10_FINAL_PRODUCTION.json WF10_ORIGINAL_para_rollback.json ASTERISK_WF10_FIX.sh PREFLIGHT_WF10_READONLY.sh VALIDATE_WF10_AFTER_DEPLOY.sh PATCH_WF10_WORKER_COMPAT.sh; do need "$f"; done
chmod 700 "$DIR"/*.sh

case "$ACTION" in
  check)
    cd "$DIR"
    bash PREFLIGHT_WF10_READONLY.sh 180
    ./ASTERISK_WF10_FIX.sh check
    ./PATCH_WF10_WORKER_COMPAT.sh check
    ;;

  apply)
    [ "$(id -u)" = 0 ] || { echo 'ERROR: ejecutar como root'; exit 1; }
    find_containers
    STAMP=$(date -u +%Y%m%dT%H%M%SZ)
    B="/root/wf10-reviewed-deploy/$STAMP"; mkdir -p "$B"
    echo "============================================================"
    echo "WF10 DEPLOY REVIEWED — $STAMP"
    echo "Backup: $B"
    echo "============================================================"

    echo; echo '===== 1. PREFLIGHT SOLO LECTURA ====='
    bash "$DIR/PREFLIGHT_WF10_READONLY.sh" 180 | tee "$B/preflight.txt"
    echo; echo '===== 2. CHECK ASTERISK ====='
    "$DIR/ASTERISK_WF10_FIX.sh" check | tee "$B/asterisk-check.txt"
    if grep -q '❌ CHECK con problemas' "$B/asterisk-check.txt"; then echo 'STOP: Asterisk check falló.'; exit 2; fi
    if grep -q '⚠️  fuera de la ventana' "$B/preflight.txt"; then echo 'STOP: hay llamadas fuera de ventana. No se publica.'; exit 3; fi

    echo; echo '===== 3. BACKUPS PRODUCCION ====='
    workflow_backup > "$B/wf10-db-before.json"
    cp -a /usr/local/bin/wf10-worker.py "$B/wf10-worker.py.before"
    crontab -l > "$B/root.crontab.before" 2>/dev/null || true
    echo "WF10 DB + worker + cron guardados en $B"

    echo; echo '===== 4. PARAR SOLO WORKER RECORDINGS ====='
    systemctl stop wf10-recording-worker
    echo "worker=$(systemctl is-active wf10-recording-worker 2>/dev/null || true)"

    AST_APPLIED=0
    WF_CHANGED=0
    rollback_all(){
      RC=$?
      echo; echo "===== ERROR: ROLLBACK AUTOMATICO (rc=$RC) ====="
      set +e
      if [ -f "$B/wf10-worker.py.before" ]; then cp -a "$B/wf10-worker.py.before" /usr/local/bin/wf10-worker.py; fi
      if [ "${AST_APPLIED:-0}" = '1' ]; then "$DIR/ASTERISK_WF10_FIX.sh" rollback >/dev/null 2>&1 || true; fi
      find_containers >/dev/null 2>&1 || true
      if [ "${WF_CHANGED:-0}" = '1' ] && [ -n "${PG:-}" ] && [ -f "$B/wf10-db-before.json" ]; then
        workflow_restore_backup "$B/wf10-db-before.json" "$B/rollback.sql" || true
        [ -n "${N8N:-}" ] && docker exec "$N8N" n8n publish:workflow --id="$WFID" >/dev/null 2>&1 || true
        restart_n8n >/dev/null 2>&1 || true
      fi
      systemctl restart wf10-recording-worker >/dev/null 2>&1 || true
      echo "Rollback intentado. Revisar $B"
      exit "$RC"
    }
    trap rollback_all ERR

    echo; echo '===== 5. COMPATIBILIDAD DEL WORKER ====='
    "$DIR/PATCH_WF10_WORKER_COMPAT.sh" apply

    echo; echo '===== 6. APLICAR PRODUCTOR ASTERISK ====='
    "$DIR/ASTERISK_WF10_FIX.sh" apply | tee "$B/asterisk-apply.txt"
    grep -q '✅ APLICADO' "$B/asterisk-apply.txt" && AST_APPLIED=1 || true
    "$DIR/ASTERISK_WF10_FIX.sh" status

    echo; echo '===== 7. INSTALAR WF10 FINAL EN EL MISMO ID ====='
    workflow_apply_file "$DIR/WF10_FINAL_PRODUCTION.json" "$B/apply.sql"
    WF_CHANGED=1
    docker exec "$N8N" n8n publish:workflow --id="$WFID"
    restart_n8n

    echo; echo '===== 8. VERIFICAR WF10 ACTIVO + WEBHOOK ====='
    docker exec "$PG" psql -U postgres -d landmarket -P pager=off -c "SELECT id,name,active,json_array_length(nodes) AS nodes,\"updatedAt\" FROM workflow_entity WHERE id='$WFID';"
    docker exec "$PG" psql -U postgres -d landmarket -P pager=off -c "SELECT \"webhookPath\",method,node,\"workflowId\" FROM webhook_entity WHERE \"webhookPath\"='send-recording';"
    CNT=$(docker exec "$PG" psql -U postgres -d landmarket -Atc "SELECT COUNT(*) FROM webhook_entity WHERE \"webhookPath\"='send-recording' AND \"workflowId\"='$WFID';")
    [ "$CNT" = '1' ] || { echo "ERROR: webhook send-recording count=$CNT"; false; }
    ACTIVE=$(docker exec "$PG" psql -U postgres -d landmarket -Atc "SELECT active::text FROM workflow_entity WHERE id='$WFID';")
    [ "$ACTIVE" = 'true' -o "$ACTIVE" = 't' ] || { echo "ERROR: WF10 quedó inactivo ($ACTIVE)"; false; }

    HTTP=$(curl -sS -o "$B/webhook-health.json" -w '%{http_code}' -X POST 'https://landmarket-n8n.dhsoig.easypanel.host/webhook/send-recording' -H 'Content-Type: application/json' -d '{}')
    echo "webhook test HTTP=$HTTP body=$(cat "$B/webhook-health.json")"
    [ "$HTTP" = '200' ] || false

    echo; echo '===== 9. ARRANCAR WORKER ====='
    systemctl restart wf10-recording-worker
    sleep 3
    systemctl --no-pager --full status wf10-recording-worker | head -20
    "$DIR/PATCH_WF10_WORKER_COMPAT.sh" status

    echo; echo '===== 10. ESTADO FINAL INMEDIATO ====='
    "$DIR/ASTERISK_WF10_FIX.sh" status
    mariadb asterisk -e "SELECT provider,COUNT(*) AS answered_unsynced FROM wf_call_followups WHERE call_status='ANSWERED' AND COALESCE(recording_synced,0)=0 GROUP BY provider;"
    echo "Backup deployment: $B"
    echo "NO se hizo backfill. NO se retiró todavía el cron viejo."
    echo "Dejá correr llamadas nuevas 10-15 min y ejecutá:"
    echo "  cd $DIR && bash VALIDATE_WF10_AFTER_DEPLOY.sh --minutes 30"
    trap - ERR
    ;;

  status)
    find_containers
    docker exec "$PG" psql -U postgres -d landmarket -P pager=off -c "SELECT id,name,active,json_array_length(nodes) AS nodes,\"updatedAt\" FROM workflow_entity WHERE id='$WFID';"
    "$DIR/ASTERISK_WF10_FIX.sh" status
    "$DIR/PATCH_WF10_WORKER_COMPAT.sh" status
    ;;

  validate)
    bash "$DIR/VALIDATE_WF10_AFTER_DEPLOY.sh" --minutes "${2:-30}"
    ;;

  finalize)
    bash "$DIR/VALIDATE_WF10_AFTER_DEPLOY.sh" --minutes "${2:-60}"
    "$DIR/ASTERISK_WF10_FIX.sh" finalize
    "$DIR/ASTERISK_WF10_FIX.sh" status
    ;;

  rollback)
    [ "$(id -u)" = 0 ] || exit 1
    B="${2:-$(ls -1d /root/wf10-reviewed-deploy/* 2>/dev/null | sort -r | head -1)}"
    [ -n "$B" ] && [ -f "$B/wf10-db-before.json" ] || { echo 'ERROR: backup deployment no encontrado'; exit 1; }
    find_containers
    systemctl stop wf10-recording-worker || true
    "$DIR/ASTERISK_WF10_FIX.sh" rollback || true
    cp -a "$B/wf10-worker.py.before" /usr/local/bin/wf10-worker.py
    python3 -m py_compile /usr/local/bin/wf10-worker.py
    workflow_restore_backup "$B/wf10-db-before.json" "$B/rollback-manual.sql"
    docker exec "$N8N" n8n publish:workflow --id="$WFID" || true
    restart_n8n
    systemctl restart wf10-recording-worker
    echo "ROLLBACK COMPLETO desde $B"
    ;;

  *) echo "Uso: $0 check|apply|status|validate [min]|finalize [min]|rollback [backup_dir]"; exit 2;;
esac
