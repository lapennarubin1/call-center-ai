
ROOT="/root/wf10/dur_apply_20260930T113821Z"
AUDIT=$(ls -dt /root/wf10/republish_duration_* | head -1)

echo "============================================================"
echo " CONTINUAR REPUBLISH - SANITIZAR API PAYLOAD"
echo "============================================================"
echo "AUDIT=$AUDIT"

KEY="$(
  grep '^LM_N8N_API_KEY=' /opt/landmark-panel/.env \
  | head -1 \
  | cut -d= -f2- \
  | tr -d "\"'"
)"

[ -n "$KEY" ] || {
  echo "ERROR: no encuentro LM_N8N_API_KEY"
  exit 1
}

PG=$(
  docker ps --format '{{.ID}} {{.Names}}' |
  awk '$2 ~ /^landmarket_n8n-db/ {print $1; exit}'
)

[ -n "$PG" ] || {
  echo "ERROR: no encuentro PostgreSQL"
  exit 2
}


echo
echo "=== 1. SANITIZAR SOLO CAMPOS VISUALES NO ACEPTADOS ==="

for F in \
  WF9_old_payload \
  WF9_new_payload \
  WF2_old_payload \
  WF2_new_payload
do
  SRC="$AUDIT/${F}.json"
  DST="$AUDIT/${F}_api.json"

  [ -f "$SRC" ] || {
    echo "ERROR: falta $SRC"
    exit 3
  }

  jq '
    .nodes |= map(
      del(
        .height,
        .width
      )
    )
  ' "$SRC" > "$DST"

  jq empty "$DST"

  echo "$F:"
  echo "  nodes=$(jq '.nodes|length' "$DST")"
  echo "  top-level height/width=$(
    jq '[.nodes[] | select(has("height") or has("width"))] | length' "$DST"
  )"
done


api_put () {
  ID="$1"
  FILE="$2"
  LABEL="$3"

  HTTP=$(
    curl -sS \
      -o "$AUDIT/${LABEL}.response.json" \
      -w '%{http_code}' \
      -X PUT \
      "$BASE/api/v1/workflows/$ID" \
      -H "X-N8N-API-KEY: $KEY" \
      -H "Content-Type: application/json" \
      --data-binary "@$FILE"
  )

  echo "$LABEL HTTP=$HTTP"

  if [ "$HTTP" -lt 200 ] || [ "$HTTP" -ge 300 ]; then
    echo
    echo "ERROR API:"
    cat "$AUDIT/${LABEL}.response.json" \
      | jq . 2>/dev/null \
      || cat "$AUDIT/${LABEL}.response.json"
    return 1
  fi
}


echo
echo "=== 2. WF9: 65 -> 67 POR API ==="

api_put \
  "$WF9" \
  "$AUDIT/WF9_old_payload_api.json" \
  "WF9_old_retry"

api_put \
  "$WF9" \
  "$AUDIT/WF9_new_payload_api.json" \
  "WF9_new_retry"


echo
echo "=== 3. WF2: 86 -> 88 POR API ==="

api_put \
  "$WF2" \
  "$AUDIT/WF2_old_payload_api.json" \
  "WF2_old_retry"

api_put \
  "$WF2" \
  "$AUDIT/WF2_new_payload_api.json" \
  "WF2_new_retry"


echo
echo "=== 4. CONFIRMAR VIA API ==="

curl -fsS \
  -H "X-N8N-API-KEY: $KEY" \
  "$BASE/api/v1/workflows/$WF9" \
  > "$AUDIT/WF9_api_final.json"

curl -fsS \
  -H "X-N8N-API-KEY: $KEY" \
  "$BASE/api/v1/workflows/$WF2" \
  > "$AUDIT/WF2_api_final.json"

N9=$(jq '.nodes|length' "$AUDIT/WF9_api_final.json")
N2=$(jq '.nodes|length' "$AUDIT/WF2_api_final.json")

echo "WF9 API=$N9"
echo "WF2 API=$N2"

[ "$N9" = "67" ]
[ "$N2" = "88" ]

jq -e '
.nodes[]
| select(.name=="🧾 Build CDR SQL (Asterisk)")
' "$AUDIT/WF9_api_final.json" >/dev/null

jq -e '
.nodes[]
| select(.name=="🗄️ CDR Asterisk (billsec)")
' "$AUDIT/WF9_api_final.json" >/dev/null

jq -e '
.nodes[]
| select(.name=="🧾 Build Deferred Ledger (Asterisk)")
' "$AUDIT/WF2_api_final.json" >/dev/null

jq -e '
.nodes[]
| select(.name=="💾 Ledger Deferred (Asterisk)")
' "$AUDIT/WF2_api_final.json" >/dev/null

echo "OK: API tiene WF9=67 y WF2=88"


echo
echo "=== 5. PUBLICAR WF9 ==="

N8N=$(
  docker ps --format '{{.ID}} {{.Names}}' |
  awk '$2 ~ /^landmarket_n8n\./ {print $1; exit}'
)

[ -n "$N8N" ] || {
  echo "ERROR: no encuentro n8n"
  exit 4
}

docker exec "$N8N" \
  n8n publish:workflow \
  --id="$WF9"


echo
echo "=== 6. PUBLICAR WF2 ==="

docker exec "$N8N" \
  n8n publish:workflow \
  --id="$WF2"


echo
echo "=== 7. RESTART SOLO N8N ==="

MARK=$(date -u '+%Y-%m-%d %H:%M:%S+00')

docker service update \
  --force landmarket_n8n >/dev/null

for i in $(seq 1 90)
do
  R=$(
    docker service ls \
      --filter name=landmarket_n8n \
      --format '{{.Name}} {{.Replicas}}' \
    | awk '$1=="landmarket_n8n"{print $2}'
  )

  [ "$R" = "1/1" ] && break
  sleep 2
done

sleep 10

echo "n8n=$R"
[ "$R" = "1/1" ]


echo
echo "=== 8. ESPERAR EJECUCION WF2 REAL ==="

WF2_REAL=""

for i in $(seq 1 30)
do
  WF2_REAL=$(
    docker exec "$PG" psql \
      -U postgres \
      -d landmarket \
      -At \
      -F '|' \
      -c "
SELECT
 e.id,
 e.status,
 e.\"workflowVersionId\",
 jsonb_array_length(
   d.\"workflowData\"::jsonb->'nodes'
 ),
 CASE WHEN EXISTS (
   SELECT 1
   FROM jsonb_array_elements(
     d.\"workflowData\"::jsonb->'nodes'
   ) n
   WHERE n->>'name'=
     '🧾 Build Deferred Ledger (Asterisk)'
 ) THEN 'YES' ELSE 'NO' END,
 CASE WHEN EXISTS (
   SELECT 1
   FROM jsonb_array_elements(
     d.\"workflowData\"::jsonb->'nodes'
   ) n
   WHERE n->>'name'=
     '💾 Ledger Deferred (Asterisk)'
 ) THEN 'YES' ELSE 'NO' END
FROM execution_entity e
JOIN execution_data d
  ON d.\"executionId\"=e.id
WHERE e.\"workflowId\"='$WF2'
  AND e.\"startedAt\" >= '$MARK'
ORDER BY e.id DESC
LIMIT 1;
"
  )

  if [ -n "$WF2_REAL" ]; then
    C=$(echo "$WF2_REAL" | cut -d'|' -f4)
    B=$(echo "$WF2_REAL" | cut -d'|' -f5)
    L=$(echo "$WF2_REAL" | cut -d'|' -f6)

    if [ "$C" = "88" ] &&
       [ "$B" = "YES" ] &&
       [ "$L" = "YES" ]; then
      break
    fi
  fi

  echo "esperando WF2 nuevo... $i/30"
  sleep 10
done

echo
echo "WF2_REAL=$WF2_REAL"


echo
echo "=== 9. ESPERAR EJECUCION WF9 REAL ==="

WF9_REAL=""

for i in $(seq 1 30)
do
  WF9_REAL=$(
    docker exec "$PG" psql \
      -U postgres \
      -d landmarket \
      -At \
      -F '|' \
      -c "
SELECT
 e.id,
 e.status,
 e.\"workflowVersionId\",
 jsonb_array_length(
   d.\"workflowData\"::jsonb->'nodes'
 ),
 CASE WHEN EXISTS (
   SELECT 1
   FROM jsonb_array_elements(
     d.\"workflowData\"::jsonb->'nodes'
   ) n
   WHERE n->>'name'=
     '🧾 Build CDR SQL (Asterisk)'
 ) THEN 'YES' ELSE 'NO' END,
 CASE WHEN EXISTS (
   SELECT 1
   FROM jsonb_array_elements(
     d.\"workflowData\"::jsonb->'nodes'
   ) n
   WHERE n->>'name'=
     '🗄️ CDR Asterisk (billsec)'
 ) THEN 'YES' ELSE 'NO' END
FROM execution_entity e
JOIN execution_data d
  ON d.\"executionId\"=e.id
WHERE e.\"workflowId\"='$WF9'
  AND e.\"startedAt\" >= '$MARK'
ORDER BY e.id DESC
LIMIT 1;
"
  )

  if [ -n "$WF9_REAL" ]; then
    C=$(echo "$WF9_REAL" | cut -d'|' -f4)
    B=$(echo "$WF9_REAL" | cut -d'|' -f5)
    L=$(echo "$WF9_REAL" | cut -d'|' -f6)

    if [ "$C" = "67" ] &&
       [ "$B" = "YES" ] &&
       [ "$L" = "YES" ]; then
      break
    fi
  fi

  echo "esperando WF9 nuevo... $i/30"
  sleep 10
done

echo
echo "WF9_REAL=$WF9_REAL"


echo
echo "=== 10. ASSERT ==="

[ "$(echo "$WF2_REAL" | cut -d'|' -f4)" = "88" ] || {
  echo "FAIL: WF2 no ejecutó 88 nodos"
  exit 20
}

[ "$(echo "$WF2_REAL" | cut -d'|' -f5)" = "YES" ]
[ "$(echo "$WF2_REAL" | cut -d'|' -f6)" = "YES" ]

[ "$(echo "$WF9_REAL" | cut -d'|' -f4)" = "67" ] || {
  echo "FAIL: WF9 no ejecutó 67 nodos"
  exit 21
}

[ "$(echo "$WF9_REAL" | cut -d'|' -f5)" = "YES" ]
[ "$(echo "$WF9_REAL" | cut -d'|' -f6)" = "YES" ]


echo
echo "============================================================"
echo " REPUBLISH OK"
echo "============================================================"
echo "WF2 = 88 | deferred YES | ledger YES"
echo "WF9 = 67 | CDR build YES | CDR node YES"
echo "WF10 = NO TOCADO"
echo "Stringee = SIN CAMBIO DE LOGICA"
echo "============================================================"

BASH

bash <<'BASH'
set -u

WF2="Bem8pAJ4gxZWrhDU"
WF9="wNUh5ugIlHCyShyI"
WF10="TW1CHksqyf66MjQz"

EXPECTED_WF10_HASH="b0d6c1e44571388d9ddaac823de6e18b"

PASS=0
FAIL=0

ok() {
  echo "✅ $*"
  PASS=$((PASS+1))
}

bad() {
  echo "❌ $*"
  FAIL=$((FAIL+1))
}

echo "============================================================"
echo " VALIDACION FINAL COMPLETA - ASTERISK DURATION"
echo " SOLO LECTURA - NO MODIFICA PRODUCCION"
echo "============================================================"

PG=$(
  docker ps --format '{{.ID}} {{.Names}}' |
  awk '$2 ~ /^landmarket_n8n-db/ {print $1; exit}'
)

N8N=$(
  docker ps --format '{{.ID}} {{.Names}}' |
  awk '$2 ~ /^landmarket_n8n\./ {print $1; exit}'
)

if [ -z "${PG:-}" ]; then
  echo "❌ PostgreSQL n8n no encontrado"
  exit 1
fi

if [ -z "${N8N:-}" ]; then
  echo "❌ n8n no encontrado"
  exit 1
fi


echo
echo "============================================================"
echo " 1. WORKFLOWS PUBLICADOS"
echo "============================================================"

docker exec "$PG" psql \
  -U postgres \
  -d landmarket \
  -P pager=off \
  -c "
SELECT
 id,
 active,
 jsonb_array_length(nodes::jsonb) AS nodes
FROM workflow_entity
WHERE id IN (
 '$WF2',
 '$WF9',
 '$WF10'
)
ORDER BY id;
"

C2=$(
  docker exec "$PG" psql \
    -U postgres -d landmarket -At \
    -c "
SELECT jsonb_array_length(nodes::jsonb)
FROM workflow_entity
WHERE id='$WF2';
"
)

C9=$(
  docker exec "$PG" psql \
    -U postgres -d landmarket -At \
    -c "
SELECT jsonb_array_length(nodes::jsonb)
FROM workflow_entity
WHERE id='$WF9';
"
)

C10=$(
  docker exec "$PG" psql \
    -U postgres -d landmarket -At \
    -c "
SELECT jsonb_array_length(nodes::jsonb)
FROM workflow_entity
WHERE id='$WF10';
"
)

A2=$(
  docker exec "$PG" psql \
    -U postgres -d landmarket -At \
    -c "SELECT active FROM workflow_entity WHERE id='$WF2';"
)

A9=$(
  docker exec "$PG" psql \
    -U postgres -d landmarket -At \
    -c "SELECT active FROM workflow_entity WHERE id='$WF9';"
)

[ "$C2" = "88" ] && ok "WF2 publicado = 88 nodos" || bad "WF2=$C2, esperado 88"
[ "$C9" = "67" ] && ok "WF9 publicado = 67 nodos" || bad "WF9=$C9, esperado 67"
[ "$C10" = "35" ] && ok "WF10 sigue en 35 nodos" || bad "WF10=$C10"

[ "$A2" = "t" ] && ok "WF2 activo" || bad "WF2 no activo"
[ "$A9" = "t" ] && ok "WF9 activo" || bad "WF9 no activo"


echo
echo "============================================================"
echo " 2. VERSION REAL QUE N8N ESTA EJECUTANDO"
echo "============================================================"

REAL2=$(
docker exec "$PG" psql \
  -U postgres -d landmarket \
  -At -F '|' \
  -c "
SELECT
 e.id,
 e.status,
 e.\"workflowVersionId\",
 jsonb_array_length(d.\"workflowData\"::jsonb->'nodes'),
 CASE WHEN EXISTS (
   SELECT 1
   FROM jsonb_array_elements(
     d.\"workflowData\"::jsonb->'nodes'
   ) n
   WHERE n->>'name'='🧾 Build Deferred Ledger (Asterisk)'
 ) THEN 'YES' ELSE 'NO' END,
 CASE WHEN EXISTS (
   SELECT 1
   FROM jsonb_array_elements(
     d.\"workflowData\"::jsonb->'nodes'
   ) n
   WHERE n->>'name'='💾 Ledger Deferred (Asterisk)'
 ) THEN 'YES' ELSE 'NO' END
FROM execution_entity e
JOIN execution_data d
  ON d.\"executionId\"=e.id
WHERE e.\"workflowId\"='$WF2'
ORDER BY e.id DESC
LIMIT 1;
"
)

REAL9=$(
docker exec "$PG" psql \
  -U postgres -d landmarket \
  -At -F '|' \
  -c "
SELECT
 e.id,
 e.status,
 e.\"workflowVersionId\",
 jsonb_array_length(d.\"workflowData\"::jsonb->'nodes'),
 CASE WHEN EXISTS (
   SELECT 1
   FROM jsonb_array_elements(
     d.\"workflowData\"::jsonb->'nodes'
   ) n
   WHERE n->>'name'='🧾 Build CDR SQL (Asterisk)'
 ) THEN 'YES' ELSE 'NO' END,
 CASE WHEN EXISTS (
   SELECT 1
   FROM jsonb_array_elements(
     d.\"workflowData\"::jsonb->'nodes'
   ) n
   WHERE n->>'name'='🗄️ CDR Asterisk (billsec)'
 ) THEN 'YES' ELSE 'NO' END
FROM execution_entity e
JOIN execution_data d
  ON d.\"executionId\"=e.id
WHERE e.\"workflowId\"='$WF9'
ORDER BY e.id DESC
LIMIT 1;
"
)

echo "WF2_REAL=$REAL2"
echo "WF9_REAL=$REAL9"

R2N=$(echo "$REAL2" | cut -d'|' -f4)
R2B=$(echo "$REAL2" | cut -d'|' -f5)
R2L=$(echo "$REAL2" | cut -d'|' -f6)

R9N=$(echo "$REAL9" | cut -d'|' -f4)
R9B=$(echo "$REAL9" | cut -d'|' -f5)
R9C=$(echo "$REAL9" | cut -d'|' -f6)

[ "$R2N" = "88" ] && ok "WF2 REAL usa 88 nodos" || bad "WF2 REAL usa $R2N"
[ "$R2B" = "YES" ] && ok "Build Deferred realmente ejecutado" || bad "Build Deferred ausente"
[ "$R2L" = "YES" ] && ok "Ledger Deferred realmente ejecutado" || bad "Ledger Deferred ausente"

[ "$R9N" = "67" ] && ok "WF9 REAL usa 67 nodos" || bad "WF9 REAL usa $R9N"
[ "$R9B" = "YES" ] && ok "Build CDR realmente publicado" || bad "Build CDR ausente"
[ "$R9C" = "YES" ] && ok "Nodo CDR billsec realmente publicado" || bad "Nodo CDR ausente"


echo
echo "============================================================"
echo " 3. WF10 / WORKER"
echo "============================================================"

WF10_HASH=$(
docker exec "$PG" psql \
  -U postgres -d landmarket -At \
  -c "
SELECT md5(
 nodes::text ||
 connections::text ||
 settings::text
)
FROM workflow_entity
WHERE id='$WF10';
"
)

echo "WF10 hash=$WF10_HASH"

[ "$WF10_HASH" = "$EXPECTED_WF10_HASH" ] \
  && ok "WF10 no fue modificado" \
  || bad "WF10 cambió"

WORKER=$(systemctl is-active wf10-recording-worker.service 2>/dev/null || true)

[ "$WORKER" = "active" ] \
  && ok "Worker WF10 activo" \
  || bad "Worker WF10=$WORKER"

grep -F \
  'delays = [10, 20, 30, 60, 120, 300, 600, 900]' \
  /usr/local/bin/wf10-worker.py >/dev/null \
  && ok "Worker reintenta hasta 900 s" \
  || bad "Retry worker inesperado"

grep -F 'timeout=120' \
  /usr/local/bin/wf10-worker.py >/dev/null \
  && ok "Worker timeout=120 s" \
  || bad "Worker timeout incorrecto"


echo
echo "============================================================"
echo " 4. CDR PRODUCTIVO"
echo "============================================================"

REF=$(
mariadb asterisk -N -B -e "
SELECT CONCAT(duration,':',billsec)
FROM cdr
WHERE uniqueid='1790750598.32578'
LIMIT 1;
"
)

[ "$REF" = "126:88" ] \
  && ok "asterisk.cdr productivo confirmado" \
  || bad "CDR referencia=$REF"

RECENT=$(
mariadb asterisk -N -B -e "
SELECT COUNT(*)
FROM cdr
WHERE calldate >= UTC_TIMESTAMP() - INTERVAL 1 HOUR;
"
)

[ "${RECENT:-0}" -gt 0 ] \
  && ok "CDR recibe llamadas en vivo ($RECENT última hora)" \
  || bad "CDR sin llamadas recientes"


echo
echo "============================================================"
echo " 5. MARCA DEL REPUBLISH CORRECTO"
echo "============================================================"

AUDIT=$(
  ls -dt /root/wf10/republish_duration_* 2>/dev/null |
  head -1
)

if [ -z "${AUDIT:-}" ]; then
  bad "No encuentro directorio republish"
  exit 1
fi

TAG="${AUDIT##*_}"

MARK=$(
python3 - "$TAG" <<'PY'
from datetime import datetime
import sys
d=datetime.strptime(sys.argv[1],"%Y%m%dT%H%M%SZ")
print(d.strftime("%Y-%m-%d %H:%M:%S"))
PY
)

echo "Republish UTC: $MARK"
echo "Audit: $AUDIT"


echo
echo "============================================================"
echo " 6. ESPERAR UNA LLAMADA ASTERISK REAL"
echo "    PROCESADA POR EL NUEVO DEFER"
echo "============================================================"

ROW=""
END=$(( $(date +%s) + 1200 ))

while [ "$(date +%s)" -lt "$END" ]
do

ROW=$(
mariadb asterisk -N -B -e "
SELECT
 e.event_key,
 e.external_id,
 e.lead_id,
 e.phone,
 e.call_attempts,
 COALESCE(e.crm_attempt,''),
 e.state,
 COALESCE(e.resolution,''),
 e.followup_id,
 DATE_FORMAT(e.dispatched_at,'%Y-%m-%d %H:%i:%s'),
 DATE_FORMAT(e.event_at,'%Y-%m-%d %H:%i:%s'),
 DATE_FORMAT(e.first_seen_at,'%Y-%m-%d %H:%i:%s'),
 e.lifecycle_applied,
 f.outcome,
 f.call_status,
 COALESCE(f.recording_synced,0)
FROM wf_call_events e
JOIN wf_call_followups f
  ON f.followup_id=e.followup_id
WHERE e.provider='asterisk'
  AND e.dispatched_at IS NOT NULL
  AND e.first_seen_at >= '$MARK'
  AND e.followup_id IS NOT NULL
  AND e.state IN ('DONE','STALE')
  AND f.outcome='CONNECTED'
  AND f.call_status='ANSWERED'
ORDER BY e.id DESC
LIMIT 1;
" 2>/dev/null
)

if [ -n "$ROW" ]; then
  break
fi

echo "$(date -u '+%H:%M:%S') esperando Asterisk ANSWERED real..."
sleep 15
done

if [ -z "$ROW" ]; then
  bad "No apareció una Asterisk CONNECTED/ANSWERED nueva en 20 min"
  echo
  echo "No es un fallo del patch; simplemente no hubo muestra contestada."
  echo
  echo "PASS=$PASS FAIL=$FAIL"
  exit 2
fi


EVKEY=$(echo "$ROW" | cut -f1)
CONV=$(echo "$ROW" | cut -f2)
LEAD=$(echo "$ROW" | cut -f3)
PHONE=$(echo "$ROW" | cut -f4)
CALL_ATTEMPT=$(echo "$ROW" | cut -f5)
CRM_ATTEMPT=$(echo "$ROW" | cut -f6)
STATE=$(echo "$ROW" | cut -f7)
RESOLUTION=$(echo "$ROW" | cut -f8)
FID=$(echo "$ROW" | cut -f9)
DISPATCHED=$(echo "$ROW" | cut -f10)
EVENT_AT=$(echo "$ROW" | cut -f11)
FIRST_SEEN=$(echo "$ROW" | cut -f12)
LIFECYCLE=$(echo "$ROW" | cut -f13)
OUTCOME=$(echo "$ROW" | cut -f14)
CALLSTATUS=$(echo "$ROW" | cut -f15)
SYNCED=$(echo "$ROW" | cut -f16)

echo
echo "event_key    = $EVKEY"
echo "conversation = $CONV"
echo "lead         = $LEAD"
echo "phone        = ****${PHONE: -4}"
echo "attempt      = $CALL_ATTEMPT"
echo "crm_attempt  = $CRM_ATTEMPT"
echo "state        = $STATE"
echo "resolution   = $RESOLUTION"
echo "followup     = $FID"
echo "dispatched   = $DISPATCHED"
echo "event_at     = $EVENT_AT"

[ -n "$DISPATCHED" ] \
  && ok "Ledger diferido tiene dispatched_at" \
  || bad "dispatched_at vacío"

case "$RESOLUTION" in
  *ASTERISK_OWNED_BY_WF2*)
    bad "La llamada cayó al owner viejo WF2"
    ;;
  *)
    ok "La llamada NO cayó al fallback viejo WF2"
    ;;
esac

EC=$(
mariadb asterisk -N -B -e "
SELECT COUNT(*)
FROM wf_call_events
WHERE event_key='${EVKEY//\'/\'\'}';
"
)

[ "$EC" = "1" ] \
  && ok "Un solo evento físico en ledger" \
  || bad "event_key duplicado: $EC"


echo
echo "============================================================"
echo " 7. ESPERAR GRABACION"
echo "============================================================"

ENDREC=$(( $(date +%s) + 900 ))

while [ "$(date +%s)" -lt "$ENDREC" ]
do
  SYNCED=$(
    mariadb asterisk -N -B -e "
SELECT COALESCE(recording_synced,0)
FROM wf_call_followups
WHERE followup_id='$FID'
LIMIT 1;
"
  )

  [ "$SYNCED" = "1" ] && break

  echo "$(date -u '+%H:%M:%S') esperando recording_synced..."
  sleep 15
done

[ "$SYNCED" = "1" ] \
  && ok "Recording synced" \
  || bad "Recording no sincronizado después de 15 min"


echo
echo "============================================================"
echo " 8. ENCONTRAR UNIQUEID EXACTO DE WF10"
echo "============================================================"

JOB=$(
python3 - "$FID" <<'PY'
import glob,json,sys

fid=sys.argv[1]

for folder in (
    "/var/spool/wf10/done",
    "/var/spool/wf10/queue",
):
    for p in glob.glob(folder+"/*.json"):
        try:
            j=json.load(open(p))
        except Exception:
            continue

        if str(j.get("followup_id") or "") == fid:
            print(p)
            raise SystemExit

print("")
PY
)

echo "JOB=$JOB"

if [ -z "$JOB" ]; then
  bad "No encuentro job WF10 por followup_id"
  CALL_UID=""
else
  CALL_UID=$(jq -r '.uniqueid // empty' "$JOB")
  JOB_FID=$(jq -r '.followup_id // empty' "$JOB")

  [ "$JOB_FID" = "$FID" ] \
    && ok "WF10 apunta al mismo followup_id" \
    || bad "WF10 followup mismatch"

  [ -n "$CALL_UID" ] \
    && ok "WF10 conserva UNIQUEID exacto $CALL_UID" \
    || bad "WF10 sin uniqueid"
fi


echo
echo "============================================================"
echo " 9. CDR EXACTO DE ESA LLAMADA"
echo "============================================================"

if [ -n "${CALL_UID:-}" ]; then

  CDR=$(
    mariadb asterisk -N -B -e "
SELECT
 DATE_FORMAT(calldate,'%Y-%m-%d %H:%i:%s'),
 duration,
 billsec,
 disposition,
 dst
FROM cdr
WHERE uniqueid='$CALL_UID'
LIMIT 1;
"
  )

else
  CDR=""
fi

echo "CDR=$CDR"

if [ -z "$CDR" ]; then
  bad "No encuentro CDR exacto"
  CDR_DATE=""
  CDR_DURATION=""
  BILLSEC=""
else
  CDR_DATE=$(echo "$CDR" | cut -f1)
  CDR_DURATION=$(echo "$CDR" | cut -f2)
  BILLSEC=$(echo "$CDR" | cut -f3)
  DISP=$(echo "$CDR" | cut -f4)

  [ "$DISP" = "ANSWERED" ] \
    && ok "CDR disposition=ANSWERED" \
    || bad "CDR disposition=$DISP"

  [ "${BILLSEC:-0}" -gt 0 ] \
    && ok "CDR billsec=$BILLSEC" \
    || bad "CDR billsec=$BILLSEC"
fi


echo
echo "============================================================"
echo " 10. LEADSTUDIO - ACTIVITY EXACTA"
echo "============================================================"

TOKEN=$(
  curl -fsS \
    -X POST \
    http://172.18.0.1:8092/token \
  | jq -r '.accessToken // empty'
)

if [ -z "$TOKEN" ]; then
  bad "No pude obtener token LeadStudio"
  CRM_DUR=""
  CRM_CREATED=""
else

  curl -fsS \
    "https://lead-studio-9gnl.onrender.com/api/leads/$LEAD/followups" \
    -H "Authorization: Bearer $TOKEN" \
    > /tmp/final_asterisk_followups.json

  CRM_COUNT=$(
    jq --arg ID "$FID" '
      [
        ..
        | objects
        | select(.id? == $ID)
      ]
      | length
    ' /tmp/final_asterisk_followups.json
  )

  jq --arg ID "$FID" '
    [
      ..
      | objects
      | select(.id? == $ID)
    ][0] // {}
    |
    {
      id,
      outcome,
      callStatus,
      durationSeconds,
      notes,
      hasRecording,
      createdAt,
      completedAt
    }
  ' /tmp/final_asterisk_followups.json

  CRM_DUR=$(
    jq -r --arg ID "$FID" '
      [
        ..
        | objects
        | select(.id? == $ID)
      ][0].durationSeconds // -1
    ' /tmp/final_asterisk_followups.json
  )

  CRM_CREATED=$(
    jq -r --arg ID "$FID" '
      [
        ..
        | objects
        | select(.id? == $ID)
      ][0].createdAt // ""
    ' /tmp/final_asterisk_followups.json
  )

  CRM_OUTCOME=$(
    jq -r --arg ID "$FID" '
      [
        ..
        | objects
        | select(.id? == $ID)
      ][0].outcome // ""
    ' /tmp/final_asterisk_followups.json
  )

  CRM_STATUS=$(
    jq -r --arg ID "$FID" '
      [
        ..
        | objects
        | select(.id? == $ID)
      ][0].callStatus // ""
    ' /tmp/final_asterisk_followups.json
  )

  [ "$CRM_COUNT" = "1" ] \
    && ok "Exactamente una Activity CRM para followup_id" \
    || bad "CRM contiene $CRM_COUNT Activities con ese ID"

  [ "$CRM_OUTCOME" = "CONNECTED" ] \
    && ok "CRM outcome=CONNECTED" \
    || bad "CRM outcome=$CRM_OUTCOME"

  [ "$CRM_STATUS" = "ANSWERED" ] \
    && ok "CRM callStatus=ANSWERED" \
    || bad "CRM callStatus=$CRM_STATUS"
fi


echo
echo "============================================================"
echo " 11. PRUEBA PRINCIPAL: TALK TIME"
echo "============================================================"

echo "CDR duration        = ${CDR_DURATION:-N/A}"
echo "CDR billsec         = ${BILLSEC:-N/A}"
echo "CRM durationSeconds = ${CRM_DUR:-N/A}"

if [ -n "${BILLSEC:-}" ] &&
   [ -n "${CRM_DUR:-}" ] &&
   [ "$CRM_DUR" = "$BILLSEC" ]; then

  ok "CRM durationSeconds == CDR.billsec ($BILLSEC segundos)"

else

  bad "CRM durationSeconds NO coincide con CDR.billsec"

fi


echo
echo "============================================================"
echo " 12. ACTIVITY DESPUES DE COLGAR"
echo "============================================================"

if [ -n "${CDR_DATE:-}" ] &&
   [ -n "${CDR_DURATION:-}" ] &&
   [ -n "${CRM_CREATED:-}" ]; then

python3 - \
  "$CDR_DATE" \
  "$CDR_DURATION" \
  "$CRM_CREATED" <<'PY'

from datetime import datetime,timezone,timedelta
import sys

cdr=datetime.strptime(
    sys.argv[1],
    "%Y-%m-%d %H:%M:%S"
).replace(tzinfo=timezone.utc)

dur=int(sys.argv[2])

crm=datetime.fromisoformat(
    sys.argv[3].replace("Z","+00:00")
)

end=cdr+timedelta(seconds=dur)

print("CDR start :",cdr.isoformat())
print("CDR end   :",end.isoformat())
print("CRM create:",crm.isoformat())

if crm >= end-timedelta(seconds=2):
    print("TIME_OK=YES")
    raise SystemExit(0)

print("TIME_OK=NO")
raise SystemExit(1)
PY

  if [ "$?" = "0" ]; then
    ok "Activity fue creada al terminar la llamada, no al contestar"
  else
    bad "Activity parece creada antes de finalizar la llamada"
  fi

else
  bad "No pude validar tiempos de creación"
fi


echo
echo "============================================================"
echo " 13. IDEMPOTENCIA / INTENTOS"
echo "============================================================"

LFCOUNT=$(
mariadb asterisk -N -B -e "
SELECT COUNT(*)
FROM wf_call_followups
WHERE followup_id='$FID';
"
)

[ "$LFCOUNT" = "1" ] \
  && ok "Una sola fila wf_call_followups" \
  || bad "wf_call_followups tiene $LFCOUNT filas"

if [ -n "$CRM_ATTEMPT" ]; then

  [ "$CRM_ATTEMPT" = "$CALL_ATTEMPT" ] \
    && ok "Intento CRM=$CRM_ATTEMPT, incremento exacto" \
    || bad "call_attempt=$CALL_ATTEMPT crm_attempt=$CRM_ATTEMPT"

else

  bad "crm_attempt todavía vacío"

fi

if [ "${CALL_ATTEMPT:-99}" -le 9 ]; then
  ok "No existe intento >9"
else
  bad "Intento inválido $CALL_ATTEMPT"
fi


echo
echo "============================================================"
echo " 14. ESTADO DE SERVICIOS"
echo "============================================================"

REP=$(
docker service ls \
  --filter name=landmarket_n8n \
  --format '{{.Name}} {{.Replicas}}' |
awk '$1=="landmarket_n8n"{print $2}'
)

[ "$REP" = "1/1" ] \
  && ok "n8n sano 1/1" \
  || bad "n8n replicas=$REP"

systemctl is-active wf10-recording-worker.service >/dev/null \
  && ok "WF10 worker sigue activo" \
  || bad "WF10 worker caído"

echo
echo "Cron viejo:"
crontab -l 2>/dev/null |
grep send_recordings ||
true


echo
echo "============================================================"
echo " RESULTADO FINAL"
echo "============================================================"
echo "PASS=$PASS"
echo "FAIL=$FAIL"
echo

if [ "$FAIL" -eq 0 ]; then

  echo "✅✅✅ FINAL OK ✅✅✅"
  echo
  echo "ASTERISK DURATION FIX VALIDADO END-TO-END"
  echo
  echo "WF2 real      : 88 nodos"
  echo "WF9 real      : 67 nodos"
  echo "Deferred      : OK"
  echo "CDR billsec   : OK"
  echo "Activity unica: OK"
  echo "Talk time     : OK"
  echo "Recording     : OK"
  echo "Attempts      : OK"
  echo "WF10          : intacto"
  echo
  echo "NO HAY NADA MAS QUE APLICAR."

else

  echo "❌ FINAL CON $FAIL FALLO(S)"
  echo
  echo "No se modificó nada durante esta validación."

fi

echo "============================================================"

BASH

bash <<'BASH'
set -u

START="$(date -u '+%Y-%m-%d %H:%M:%S')"
DEADLINE=$(( $(date +%s) + 300 ))

echo "============================================================"
echo " MONITOR ASTERISK 5 MINUTOS"
echo "============================================================"
echo "Inicio UTC: $START"
echo
echo "Esperando una nueva Asterisk ANSWERED..."
echo

EVENT=""

while [ "$(date +%s)" -lt "$DEADLINE" ]; do

  EVENT=$(
    mariadb asterisk -N -B -e "
SELECT
  event_key,
  external_id,
  lead_id,
  phone,
  call_attempts,
  DATE_FORMAT(dispatched_at,'%Y-%m-%d %H:%i:%s')
FROM wf_call_events
WHERE provider='asterisk'
  AND dispatched_at IS NOT NULL
  AND first_seen_at >= '$START'
ORDER BY id ASC
LIMIT 1;
" 2>/dev/null
  )

  if [ -n "$EVENT" ]; then
    break
  fi

  echo "$(date -u '+%H:%M:%S') esperando..."
  sleep 10
done


if [ -z "$EVENT" ]; then
  echo
  echo "============================================================"
  echo " NO HUBO ASTERISK ANSWERED NUEVA EN 5 MINUTOS"
  echo "============================================================"
  exit 0
fi


EVKEY=$(echo "$EVENT" | cut -f1)
CONV=$(echo "$EVENT" | cut -f2)
LEAD=$(echo "$EVENT" | cut -f3)
PHONE=$(echo "$EVENT" | cut -f4)
ATTEMPT=$(echo "$EVENT" | cut -f5)
DISPATCH=$(echo "$EVENT" | cut -f6)

echo
echo "============================================================"
echo " LLAMADA NUEVA DETECTADA"
echo "============================================================"
echo "event       : $EVKEY"
echo "conversation: $CONV"
echo "lead        : $LEAD"
echo "phone       : ****${PHONE: -4}"
echo "attempt     : $ATTEMPT"
echo "dispatch    : $DISPATCH"


echo
echo "============================================================"
echo " ESPERANDO A WF9 / ACTIVITY"
echo "============================================================"

FID=""
WAIT=$(( $(date +%s) + 300 ))

while [ "$(date +%s)" -lt "$WAIT" ]; do

  DATA=$(
    mariadb asterisk -N -B -e "
SELECT
  COALESCE(followup_id,''),
  state,
  COALESCE(resolution,''),
  COALESCE(crm_attempt,''),
  lifecycle_applied
FROM wf_call_events
WHERE event_key='${EVKEY//\'/\'\'}'
LIMIT 1;
" 2>/dev/null
  )

  FID=$(echo "$DATA" | cut -f1)
  STATE=$(echo "$DATA" | cut -f2)
  RES=$(echo "$DATA" | cut -f3)
  CRM_ATTEMPT=$(echo "$DATA" | cut -f4)
  LIFE=$(echo "$DATA" | cut -f5)

  echo "$(date -u '+%H:%M:%S') state=$STATE resolution=$RES followup=${FID:-pendiente}"

  if [ -n "$FID" ]; then
    break
  fi

  sleep 10
done


if [ -z "$FID" ]; then
  echo
  echo "❌ Se detectó la llamada, pero WF9 todavía no creó followup."
  echo
  echo "Evento actual:"
  mariadb asterisk -t -e "
SELECT *
FROM wf_call_events
WHERE event_key='${EVKEY//\'/\'\'}';
"
  exit 1
fi


echo
echo "============================================================"
echo " FOLLOWUP CREADO"
echo "============================================================"
echo "followup    : $FID"
echo "state       : $STATE"
echo "resolution  : $RES"
echo "crm_attempt : $CRM_ATTEMPT"

case "$RES" in
  *ASTERISK_OWNED_BY_WF2*)
    echo "❌ FALLÓ DEFER: cayó al owner viejo WF2"
    ;;
  *)
    echo "✅ Defer nuevo usado"
    ;;
esac


echo
echo "============================================================"
echo " FOLLOWUP LOCAL"
echo "============================================================"

mariadb asterisk -t -e "
SELECT
 id,
 phone,
 lead_id,
 followup_id,
 provider,
 outcome,
 call_status,
 recording_synced,
 created_at
FROM wf_call_followups
WHERE followup_id='$FID';
"


echo
echo "============================================================"
echo " BUSCAR UNIQUEID WF10"
echo "============================================================"

JOB=""

for i in $(seq 1 30); do

  JOB=$(
    python3 - "$FID" <<'PY'
import glob,json,sys

fid=sys.argv[1]

for folder in [
    "/var/spool/wf10/done",
    "/var/spool/wf10/queue"
]:
    for f in glob.glob(folder+"/*.json"):
        try:
            j=json.load(open(f))
        except:
            continue

        if str(j.get("followup_id") or "") == fid:
            print(f)
            raise SystemExit
PY
  )

  [ -n "$JOB" ] && break

  echo "$(date -u '+%H:%M:%S') esperando job WF10..."
  sleep 10
done

echo "JOB=$JOB"

if [ -z "$JOB" ]; then
  echo "⚠️ Todavía no encontré job WF10."
  exit 1
fi

CALL_UID=$(jq -r '.uniqueid // empty' "$JOB")

echo "uniqueid=$CALL_UID"


echo
echo "============================================================"
echo " CDR EXACTO"
echo "============================================================"

CDR=$(
  mariadb asterisk -N -B -e "
SELECT
  DATE_FORMAT(calldate,'%Y-%m-%d %H:%i:%s'),
  duration,
  billsec,
  disposition
FROM cdr
WHERE uniqueid='$CALL_UID'
LIMIT 1;
"
)

echo "$CDR"

CDR_DURATION=$(echo "$CDR" | cut -f2)
BILLSEC=$(echo "$CDR" | cut -f3)
DISP=$(echo "$CDR" | cut -f4)


echo
echo "============================================================"
echo " CRM EXACTO"
echo "============================================================"

TOKEN=$(
  curl -fsS \
    -X POST \
    http://172.18.0.1:8092/token \
  | jq -r '.accessToken // empty'
)

CRM=$(
  curl -fsS \
    "https://lead-studio-9gnl.onrender.com/api/leads/$LEAD/followups" \
    -H "Authorization: Bearer $TOKEN" \
  | jq -c --arg ID "$FID" '
      [
        ..
        | objects
        | select(.id? == $ID)
      ][0] // {}
    '
)

echo "$CRM" | jq '{
  id,
  outcome,
  callStatus,
  durationSeconds,
  notes,
  createdAt,
  completedAt
}'

CRM_DUR=$(echo "$CRM" | jq -r '.durationSeconds // -1')
CRM_OUTCOME=$(echo "$CRM" | jq -r '.outcome // ""')
CRM_STATUS=$(echo "$CRM" | jq -r '.callStatus // ""')


echo
echo "============================================================"
echo " RESULTADO FINAL"
echo "============================================================"

echo "CDR duration        = $CDR_DURATION"
echo "CDR billsec         = $BILLSEC"
echo "CRM durationSeconds = $CRM_DUR"
echo

FAIL=0

if [ "$DISP" = "ANSWERED" ]; then
  echo "✅ CDR ANSWERED"
else
  echo "❌ CDR disposition=$DISP"
  FAIL=1
fi

if [ "$CRM_OUTCOME" = "CONNECTED" ]; then
  echo "✅ CRM CONNECTED"
else
  echo "❌ CRM outcome=$CRM_OUTCOME"
  FAIL=1
fi

if [ "$CRM_STATUS" = "ANSWERED" ]; then
  echo "✅ CRM ANSWERED"
else
  echo "❌ CRM callStatus=$CRM_STATUS"
  FAIL=1
fi

if [ "$CRM_DUR" = "$BILLSEC" ]; then
  echo "✅ durationSeconds == CDR.billsec ($BILLSEC segundos)"
else
  echo "❌ durationSeconds=$CRM_DUR / billsec=$BILLSEC"
  FAIL=1
fi

COUNT=$(
  mariadb asterisk -N -B -e "
SELECT COUNT(*)
FROM wf_call_followups
WHERE followup_id='$FID';
"
)

if [ "$COUNT" = "1" ]; then
  echo "✅ Una sola Activity/followup"
else
  echo "❌ followups=$COUNT"
  FAIL=1
fi

SYNC=$(
  mariadb asterisk -N -B -e "
SELECT COALESCE(recording_synced,0)
FROM wf_call_followups
WHERE followup_id='$FID'
LIMIT 1;
"
)

if [ "$SYNC" = "1" ]; then
  echo "✅ Recording synced"
else
  echo "⚠️ Recording todavía pendiente"
fi

echo
if [ "$FAIL" = "0" ]; then
  echo "✅✅✅ ASTERISK DURATION FIX CONFIRMADO EN VIVO ✅✅✅"
else
  echo "❌ HAY ALGO QUE REVISAR"
fi

echo "============================================================"

BASH

bash <<'BASH'
set -u

MARK="2026-09-30 13:21:32"

echo "============================================================"
echo " BUSCAR PRIMER ASTERISK ANSWERED DESPUES DEL FIX"
echo " Desde: $MARK UTC"
echo "============================================================"

while true; do

  ROW=$(
    mariadb asterisk -N -B -e "
SELECT
  event_key,
  lead_id,
  phone,
  state,
  COALESCE(resolution,''),
  COALESCE(followup_id,''),
  DATE_FORMAT(dispatched_at,'%Y-%m-%d %H:%i:%s'),
  DATE_FORMAT(first_seen_at,'%Y-%m-%d %H:%i:%s')
FROM wf_call_events
WHERE provider='asterisk'
  AND first_seen_at >= '$MARK'
  AND dispatched_at IS NOT NULL
ORDER BY id ASC
LIMIT 1;
" 2>/dev/null
  )

  if [ -n "$ROW" ]; then
    echo
    echo "✅ LLAMADA NUEVA DEL DEFER ENCONTRADA"
    echo "$ROW"
    echo
    break
  fi

  echo "$(date -u '+%H:%M:%S') todavía no hay Asterisk ANSWERED posterior al fix..."
  sleep 15
done

echo "============================================================"
echo " FIN"
echo "============================================================"

BASH

bash <<'BASH'
set -u

MARK="2026-09-30 13:21:32"

echo "============================================================"
echo " BUSCAR PRIMER ASTERISK ANSWERED DESPUES DEL FIX"
echo " Desde: $MARK UTC"
echo "============================================================"

while true; do

  ROW=$(
    mariadb asterisk -N -B -e "
SELECT
  event_key,
  lead_id,
  phone,
  state,
  COALESCE(resolution,''),
  COALESCE(followup_id,''),
  DATE_FORMAT(dispatched_at,'%Y-%m-%d %H:%i:%s'),
  DATE_FORMAT(first_seen_at,'%Y-%m-%d %H:%i:%s')
FROM wf_call_events
WHERE provider='asterisk'
  AND first_seen_at >= '$MARK'
  AND dispatched_at IS NOT NULL
ORDER BY id ASC
LIMIT 1;
" 2>/dev/null
  )

  if [ -n "$ROW" ]; then
    echo
    echo "✅ LLAMADA NUEVA DEL DEFER ENCONTRADA"
    echo "$ROW"
    echo
    break
  fi

  echo "$(date -u '+%H:%M:%S') todavía no hay Asterisk ANSWERED posterior al fix..."
  sleep 15
done

echo "============================================================"
echo " FIN"
echo "============================================================"

BASH

bash <<'BASH'
set -euo pipefail

MARK="2026-09-30 13:21:32"

echo "============================================================"
echo " AUDITORIA REAL POST-DEPLOY - ASTERISK"
echo " DESDE $MARK UTC"
echo " SOLO LECTURA"
echo "============================================================"

echo
echo "=== 1. CDR ANSWERED REALES DESPUES DEL FIX ==="

mariadb asterisk -t -e "
SELECT
  calldate,
  uniqueid,
  dst,
  dcontext,
  disposition,
  duration,
  billsec
FROM cdr
WHERE calldate >= '$MARK'
  AND disposition='ANSWERED'
  AND dcontext='from-client-elevenlabs'
ORDER BY calldate DESC
LIMIT 30;
"


echo
echo "=== 2. EVENTOS ASTERISK DESPUES DEL FIX ==="

mariadb asterisk -t -e "
SELECT
  id,
  event_key,
  lead_id,
  phone,
  call_attempts,
  crm_attempt,
  state,
  resolution,
  followup_id,
  event_at,
  dispatched_at,
  first_seen_at,
  processed_at,
  lifecycle_applied,
  error_count,
  last_error
FROM wf_call_events
WHERE provider='asterisk'
  AND first_seen_at >= '$MARK'
ORDER BY id DESC
LIMIT 50;
"


echo
echo "=== 3. RESUMEN DISPATCHED_AT ==="

mariadb asterisk -t -e "
SELECT
  COUNT(*) AS total,
  SUM(dispatched_at IS NOT NULL) AS con_dispatched_at,
  SUM(dispatched_at IS NULL) AS sin_dispatched_at
FROM wf_call_events
WHERE provider='asterisk'
  AND first_seen_at >= '$MARK';
"


echo
echo "=== 4. FOLLOWUPS ASTERISK CREADOS DESPUES DEL FIX ==="

mariadb asterisk -t -e "
SELECT
  id,
  phone,
  lead_id,
  followup_id,
  provider,
  outcome,
  call_status,
  recording_synced,
  created_at
FROM wf_call_followups
WHERE provider='asterisk'
  AND created_at >= '$MARK'
ORDER BY id DESC
LIMIT 30;
"


echo
echo "=== 5. VALIDAR CADA ANSWERED CONTRA WF10 + CDR + CRM ==="

TOKEN=$(
  curl -fsS \
    -X POST \
    http://172.18.0.1:8092/token \
  | jq -r '.accessToken // empty'
)

[ -n "$TOKEN" ] || {
  echo "ERROR: no pude obtener token LeadStudio"
  exit 1
}

TMP=$(mktemp)

mariadb asterisk -N -B -e "
SELECT
  lead_id,
  followup_id,
  phone,
  DATE_FORMAT(created_at,'%Y-%m-%d %H:%i:%s')
FROM wf_call_followups
WHERE provider='asterisk'
  AND call_status='ANSWERED'
  AND created_at >= '$MARK'
ORDER BY created_at DESC
LIMIT 20;
" > "$TMP"

COUNT=0
MATCH=0
ZERO=0
NOCALL=0

while IFS=$'\t' read -r LEAD FID PHONE CREATED
do
  [ -n "$FID" ] || continue

  COUNT=$((COUNT+1))

  echo
  echo "------------------------------------------------------------"
  echo "FOLLOWUP $FID"
  echo "lead    : $LEAD"
  echo "phone   : ****${PHONE: -4}"
  echo "created : $CREATED"

  JOB=$(
    python3 - "$FID" <<'PY'
import glob,json,sys

fid=sys.argv[1]

for folder in (
    "/var/spool/wf10/done",
    "/var/spool/wf10/queue",
):
    for p in glob.glob(folder+"/*.json"):
        try:
            j=json.load(open(p))
        except Exception:
            continue
        if str(j.get("followup_id") or "") == fid:
            print(p)
            raise SystemExit
PY
  )

  if [ -z "$JOB" ]; then
    echo "WF10    : NO JOB ENCONTRADO"
    NOCALL=$((NOCALL+1))
    continue
  fi

  CALL_UID=$(jq -r '.uniqueid // empty' "$JOB")

  echo "WF10 job: $JOB"
  echo "uniqueid: $CALL_UID"

  CDR=$(
    mariadb asterisk -N -B -e "
SELECT
  duration,
  billsec,
  disposition,
  DATE_FORMAT(calldate,'%Y-%m-%d %H:%i:%s')
FROM cdr
WHERE uniqueid='$CALL_UID'
LIMIT 1;
"
  )

  if [ -z "$CDR" ]; then
    echo "CDR     : NO ENCONTRADO"
    continue
  fi

  DUR=$(echo "$CDR" | cut -f1)
  BILL=$(echo "$CDR" | cut -f2)
  DISP=$(echo "$CDR" | cut -f3)
  CALLDATE=$(echo "$CDR" | cut -f4)

  CRM=$(
    curl -fsS \
      "https://lead-studio-9gnl.onrender.com/api/leads/$LEAD/followups" \
      -H "Authorization: Bearer $TOKEN" \
    | jq -c --arg ID "$FID" '
        [
          ..
          | objects
          | select(.id? == $ID)
        ][0] // {}
      '
  )

  CDUR=$(echo "$CRM" | jq -r '.durationSeconds // -1')
  OUTCOME=$(echo "$CRM" | jq -r '.outcome // ""')
  STATUS=$(echo "$CRM" | jq -r '.callStatus // ""')
  NOTES=$(echo "$CRM" | jq -r '.notes // ""')
  CRM_CREATED=$(echo "$CRM" | jq -r '.createdAt // ""')

  EVENT=$(
    mariadb asterisk -N -B -e "
SELECT
  COALESCE(state,''),
  COALESCE(resolution,''),
  COALESCE(dispatched_at,''),
  COALESCE(event_key,'')
FROM wf_call_events
WHERE followup_id='$FID'
   OR lead_id='$LEAD'
ORDER BY
  CASE WHEN followup_id='$FID' THEN 0 ELSE 1 END,
  id DESC
LIMIT 1;
"
  )

  ESTATE=$(echo "$EVENT" | cut -f1)
  ERES=$(echo "$EVENT" | cut -f2)
  EDISP=$(echo "$EVENT" | cut -f3)
  EKEY=$(echo "$EVENT" | cut -f4)

  echo "call date       : $CALLDATE"
  echo "CDR duration    : $DUR"
  echo "CDR billsec     : $BILL"
  echo "CDR disposition : $DISP"
  echo "CRM outcome     : $OUTCOME"
  echo "CRM callStatus  : $STATUS"
  echo "CRM durationSec : $CDUR"
  echo "CRM createdAt   : $CRM_CREATED"
  echo "event state     : $ESTATE"
  echo "event resolution: $ERES"
  echo "event dispatched: $EDISP"
  echo "event key       : $EKEY"
  echo "notes           : $NOTES"

  if [ "$CDUR" = "$BILL" ] && [ "$BILL" -gt 0 ]; then
    echo "✅ MATCH: CRM durationSeconds == CDR.billsec"
    MATCH=$((MATCH+1))
  else
    echo "❌ MISMATCH: CRM=$CDUR / billsec=$BILL"

    if [ "$CDUR" = "0" ]; then
      ZERO=$((ZERO+1))
    fi
  fi

done < "$TMP"

rm -f "$TMP"


echo
echo "============================================================"
echo " RESUMEN"
echo "============================================================"
echo "ANSWERED followups auditados : $COUNT"
echo "duration == billsec          : $MATCH"
echo "duration = 0                : $ZERO"
echo "sin job WF10                : $NOCALL"
echo "============================================================"

BASH

sudo apt update && sudo apt install git -y
git --version
mkdir -p /root/call-center-ai
cd /root/call-center-ai
git config --global user.name "lapennarubin1"
git config --global user.email "lapennarubin1@gmail.com"
ssh-keygen -t ed25519 -C "lapennarubin1@gmail.com"
cat ~/.ssh/id_ed25519.pub
git init
git branch -M main
printf "node_modules/\n.env\n*.log\nvenv/\n" > .gitignore
git remote add origin git@github.com:lapennarubin1/call-center-ai.git
nano ~/.bashrc
source ~/.bashrc
subir
nano ~/.bashrc
source ~/.bashrc
subir
cp -r /ruta/original/del/proyecto/* /root/call-center-ai/
[200~ls -la /root~
exit
