#!/usr/bin/env bash
set -euo pipefail

WFID="TW1CHksqyf66MjQz"
DB=$(docker ps -q --filter name=landmarket_n8n-db | head -1)
STAMP=$(date +%Y%m%d-%H%M%S)

echo
echo "============================================================"
echo "1. PARAR WORKER MIENTRAS CORREGIMOS WF10"
echo "============================================================"

systemctl stop wf10-recording-worker || true

echo "Worker parado."

echo
echo "============================================================"
echo "2. BACKUP COMPLETO WF10"
echo "============================================================"

docker exec "$DB" psql -U postgres -d landmarket -Atc \
"SELECT nodes::text FROM workflow_entity WHERE id='$WFID';" \
> "/root/wf10-nodes-finalfix-$STAMP.json"

docker exec "$DB" psql -U postgres -d landmarket -Atc \
"SELECT connections::text FROM workflow_entity WHERE id='$WFID';" \
> "/root/wf10-connections-finalfix-$STAMP.json"

echo "Backup OK"

echo
echo "============================================================"
echo "3. DESACTIVAR RAMAS LEGACY + CORREGIR STRINGEE NUEVO"
echo "============================================================"

python3 - \
"/root/wf10-nodes-finalfix-$STAMP.json" \
"/tmp/wf10-finalfixed.json" <<'PY'

import json,sys

nodes=json.load(open(sys.argv[1],encoding="utf-8"))

# -------------------------------------------------------------
# RAMAS ANTIGUAS.
# Estas NO deben ejecutar llamadas ni subir grabaciones.
# -------------------------------------------------------------
legacy = {
    "⏰ Schedule Stringee Recordings",
    "🔄 Fetch & Convert Stringee Recordings",
    "🔍 Lookup Followup ID (Stringee)",
    "🔀 Followup Encontrado? (Stringee)",
    "🔐 Login LeadStudio (WF10-Stringee)",
    "🔗 Attach Audio + Token (Stringee)",
    "📤 PUT Recording — LeadStudio (Stringee)",
    "💾 Mark Recording Synced (Stringee)",
    "Code in JavaScript (Stringee)",
    "Send to Telegram (Stringee)",
    "Send to Telegram1 (Stringee)",

    "🔍 Lookup Followup ID (Asterisk)",
    "🔀 Followup Encontrado? (Asterisk)",
    "🔐 Login LeadStudio (WF10-Asterisk)",
    "🔗 Attach Audio + Token (Asterisk)",
    "📤 PUT Recording — LeadStudio (Asterisk)",
    "💾 Mark Recording Synced (Asterisk)",

    "🔎 Ya Enviadas (ledger)",
    "⚙️ SQL Registrar Enviadas",
    "💾 Registrar Enviadas (ledger)",
}

# -------------------------------------------------------------
# RAMAS NUEVAS QUE SI QUEREMOS
# -------------------------------------------------------------
active = {
    "Webhook2",
    "Convert Base64 to Binary2",

    "🔍 Lookup Followup ID (Asterisk)2",
    "🔀 Followup Encontrado? (Asterisk)2",
    "🔐 Login LeadStudio (WF10-Asterisk)2",
    "🔗 Attach Audio + Token (Asterisk)2",
    "📤 PUT Recording — LeadStudio (Asterisk)2",
    "💾 Mark Recording Synced (Asterisk)2",
    "Code in JavaScript2",

    "⏰ Schedule Stringee Recordings3",
    "🔄 Fetch & Convert Stringee Recordings2",
    "🔍 Lookup Followup ID (Stringee)2",
    "🔀 Followup Encontrado? (Stringee)2",
    "🔐 Login LeadStudio (WF10-Stringee)2",
    "🔗 Attach Audio + Token (Stringee)2",
    "📤 PUT Recording — LeadStudio (Stringee)2",
    "💾 Mark Recording Synced (Stringee)3",
    "Code in JavaScript (Stringee)3",

    "🔎 Ya Enviadas (ledger)1",
    "⚙️ SQL Registrar Enviadas1",
    "💾 Registrar Enviadas (ledger)1",
}

disabled=[]
enabled=[]

for n in nodes:

    name=n.get("name","")

    if name in legacy:
        n["disabled"]=True
        disabled.append(name)

    if name in active:
        n["disabled"]=False
        enabled.append(name)

    # =========================================================
    # STRINGEE FETCH:
    # conservar timestamp exacto de la grabacion
    # =========================================================
    if name=="🔄 Fetch & Convert Stringee Recordings2":

        code=n["parameters"]["jsCode"]

        old="""        phone: rec.phone,
        date: fecha,
        filename: rec.filename,
        country,"""

        new="""        phone: rec.phone,
        date: fecha,
        timestamp_ms: rec.timestamp_ms,
        filename: rec.filename,
        country,"""

        if "timestamp_ms: rec.timestamp_ms" not in code:

            if old not in code:
                raise SystemExit(
                    "ERROR: no pude localizar JSON output Stringee"
                )

            code=code.replace(old,new,1)

        n["parameters"]["jsCode"]=code

    # =========================================================
    # STRINGEE LOOKUP:
    # SOLO provider=stringee
    # SOLO ANSWERED
    # MISMO TELEFONO
    # CERCA EN EL TIEMPO DE LA GRABACION
    # =========================================================
    if name=="🔍 Lookup Followup ID (Stringee)2":

        n["parameters"]["query"]=r"""SELECT
(
 SELECT followup_id
 FROM wf_call_followups
 WHERE RIGHT(phone,10)=RIGHT('{{ $json.phone }}',10)
   AND provider='stringee'
   AND call_status='ANSWERED'
   AND (recording_synced IS NULL OR recording_synced=0)
   AND ABS(
       TIMESTAMPDIFF(
           SECOND,
           created_at,
           FROM_UNIXTIME({{ Number($json.timestamp_ms || 0) / 1000 }})
       )
   ) <= 300
 ORDER BY ABS(
       TIMESTAMPDIFF(
           SECOND,
           created_at,
           FROM_UNIXTIME({{ Number($json.timestamp_ms || 0) / 1000 }})
       )
   ) ASC
 LIMIT 1
) AS followup_id,

(
 SELECT lead_id
 FROM wf_call_followups
 WHERE RIGHT(phone,10)=RIGHT('{{ $json.phone }}',10)
   AND provider='stringee'
   AND call_status='ANSWERED'
   AND (recording_synced IS NULL OR recording_synced=0)
   AND ABS(
       TIMESTAMPDIFF(
           SECOND,
           created_at,
           FROM_UNIXTIME({{ Number($json.timestamp_ms || 0) / 1000 }})
       )
   ) <= 300
 ORDER BY ABS(
       TIMESTAMPDIFF(
           SECOND,
           created_at,
           FROM_UNIXTIME({{ Number($json.timestamp_ms || 0) / 1000 }})
       )
   ) ASC
 LIMIT 1
) AS lead_id;"""

        n["continueOnFail"]=False
        n["retryOnFail"]=True
        n["maxTries"]=2
        n.pop("onError",None)

    # =========================================================
    # STRINGEE ATTACH:
    # error real, no esconder errores.
    # =========================================================
    if name=="🔗 Attach Audio + Token (Stringee)2":

        n["parameters"]["mode"]="runOnceForEachItem"

        n["parameters"]["jsCode"]=r"""const token=$input.item.json.accessToken;
const lookup=$('🔍 Lookup Followup ID (Stringee)2').item.json;
const original=$('🔄 Fetch & Convert Stringee Recordings2').item;

if (!token) {
  throw new Error('WF10 Stringee: accessToken ausente');
}

if (!lookup.followup_id) {
  throw new Error('WF10 Stringee: followup_id ausente');
}

if (!original.binary || !original.binary.data) {
  throw new Error('WF10 Stringee: audio ausente');
}

return {
  json: {
    ...original.json,
    accessToken: token,
    followup_id: lookup.followup_id,
    lead_id: lookup.lead_id
  },
  binary: original.binary
};"""

        n["continueOnFail"]=False
        n["retryOnFail"]=False
        n.pop("onError",None)

    # =========================================================
    # STRINGEE PUT:
    # si LeadStudio responde 4xx/429/5xx NO marcar synced.
    # =========================================================
    if name=="📤 PUT Recording — LeadStudio (Stringee)2":

        opt=n["parameters"].setdefault("options",{})
        r1=opt.setdefault("response",{})
        r2=r1.setdefault("response",{})

        r2["neverError"]=False
        r2["responseFormat"]="json"

        n["retryOnFail"]=False
        n["continueOnFail"]=False
        n.pop("onError",None)

    # =========================================================
    # Asterisk PUT tambien estricto
    # =========================================================
    if name=="📤 PUT Recording — LeadStudio (Asterisk)2":

        opt=n["parameters"].setdefault("options",{})
        r1=opt.setdefault("response",{})
        r2=r1.setdefault("response",{})

        r2["neverError"]=False
        r2["responseFormat"]="json"

        n["retryOnFail"]=False
        n["continueOnFail"]=False
        n.pop("onError",None)

json.dump(
    nodes,
    open(sys.argv[2],"w",encoding="utf-8"),
    ensure_ascii=False,
    separators=(",",":")
)

print("LEGACY DESACTIVADOS:",len(disabled))
for x in disabled:
    print(" OFF:",x)

print()
print("NUEVOS ACTIVADOS:",len(enabled))

PY

{
  echo "UPDATE workflow_entity SET nodes=\$WFJSON\$"
  cat /tmp/wf10-finalfixed.json
  echo "\$WFJSON\$::json WHERE id='$WFID';"
} | docker exec -i "$DB" \
      psql -U postgres -d landmarket >/dev/null

echo
echo "============================================================"
echo "4. REPUBLICAR Y REINICIAR WF10"
echo "============================================================"

N8N=$(docker ps -q --filter name=landmarket_n8n | head -1)

docker exec "$N8N" \
  n8n publish:workflow --id="$WFID" >/dev/null 2>&1 || true

docker service update --force landmarket_n8n >/dev/null

for i in $(seq 1 60); do

    R=$(docker service ls \
      --filter name=landmarket_n8n \
      --format '{{.Name}} {{.Replicas}}' \
      | awk '$1=="landmarket_n8n"{print $2}')

    [ "$R" = "1/1" ] && break

    sleep 1
done

sleep 8

DB=$(docker ps -q --filter name=landmarket_n8n-db | head -1)

echo
echo "============================================================"
echo "5. CONFIRMAR WEBHOOK"
echo "============================================================"

docker exec "$DB" psql -U postgres -d landmarket -c "
SELECT
 \"webhookPath\",
 method,
 node,
 \"workflowId\"
FROM webhook_entity
WHERE \"webhookPath\"='send-recording';
"

HTTP=$(curl -sS \
   -o /tmp/wf10-check.txt \
   -w '%{http_code}' \
   -X POST \
   'https://landmarket-n8n.dhsoig.easypanel.host/webhook/send-recording' \
   -H 'Content-Type: application/json' \
   -d '{}')

echo "HTTP webhook=$HTTP"

if [ "$HTTP" != "200" ]; then
    echo "ERROR: webhook no esta operativo."
    exit 1
fi

echo
echo "============================================================"
echo "6. VERIFICAR SOLO UN SCHEDULE STRINGEE ACTIVO"
echo "============================================================"

docker exec "$DB" psql -U postgres -d landmarket -c "
SELECT
 n->>'name' AS node,
 COALESCE(n->>'disabled','false') AS disabled
FROM workflow_entity w,
LATERAL jsonb_array_elements(w.nodes::jsonb) n
WHERE w.id='$WFID'
AND n->>'name' LIKE '%Schedule Stringee%'
ORDER BY 1;
"

echo
echo "============================================================"
echo "7. TOKEN"
echo "============================================================"

TOKEN=$(curl -s -X POST \
    http://172.18.0.1:8092/token \
    | python3 -c '
import sys,json
try:
 d=json.load(sys.stdin)
 print("YES" if d.get("accessToken") else "NO")
except:
 print("NO")
')

echo "TOKEN_OK=$TOKEN"

if [ "$TOKEN" != "YES" ]; then
    echo "Token no disponible. Worker queda parado."
    exit 0
fi

echo
echo "============================================================"
echo "8. AGREGAR A QUEUE LAS NUEVAS ANSWERED QUE FALTAN"
echo "============================================================"

# El script es idempotente:
# no duplica uniqueids que ya estan en queue/done.
python3 /root/wf10-backfill-today.py || true

echo
echo "QUEUE AHORA:"
find /var/spool/wf10/queue \
  -maxdepth 1 -name '*.json' | wc -l

echo
echo "============================================================"
echo "9. TODAS LAS BACKFILL SOLO CRM, NO TELEGRAM REPETIDO"
echo "============================================================"

python3 <<'PY'
import glob,json,os

n=0

for path in glob.glob("/var/spool/wf10/queue/*.json"):

    with open(path) as f:
        j=json.load(f)

    if j.get("backfill") is True:

        j["suppress_telegram"]=True
        j["next_attempt"]=0
        j["attempts"]=0
        j.pop("last_error",None)

        tmp=path+".tmp"

        with open(tmp,"w") as f:
            json.dump(j,f)

        os.replace(tmp,path)
        n+=1

print("BACKFILL CRM-ONLY:",n)
PY

echo
echo "============================================================"
echo "10. PROBAR UNA SOLA GRABACION ASTERISK"
echo "============================================================"

python3 <<'PY'
import glob,json,base64,sys

for path in sorted(
    glob.glob("/var/spool/wf10/queue/*.json")
):

    j=json.load(open(path))

    if not j.get("followup_id"):
        continue

    with open(j["wav"],"rb") as f:
        audio=base64.b64encode(f.read()).decode()

    payload={
        "phone":j["phone"],
        "date":"2026-09-25",
        "filename":j["filename"],
        "audio_base64":audio,
        "followup_id":j["followup_id"],
        "uniqueid":j["uniqueid"],
        "suppress_telegram":True
    }

    json.dump(
        payload,
        open("/tmp/wf10-final-test.json","w")
    )

    open("/tmp/wf10-final-fid","w").write(
        j["followup_id"]
    )

    print("PHONE:",j["phone"])
    print("FOLLOWUP:",j["followup_id"])
    print("WAV:",j["filename"])

    sys.exit(0)

raise SystemExit("ERROR: no hay job para probar")
PY

HTTP=$(curl -sS \
   -o /tmp/wf10-final-response.txt \
   -w '%{http_code}' \
   -X POST \
   'https://landmarket-n8n.dhsoig.easypanel.host/webhook/send-recording' \
   -H 'Content-Type: application/json' \
   --data-binary @/tmp/wf10-final-test.json)

echo "HTTP=$HTTP"
cat /tmp/wf10-final-response.txt
echo

FID=$(cat /tmp/wf10-final-fid)

echo "Esperando hasta 60 segundos..."

SYNC=0

for i in $(seq 1 30); do

    SYNC=$(mariadb asterisk -N -B -e "
    SELECT COALESCE(recording_synced,0)
    FROM wf_call_followups
    WHERE followup_id='$FID'
    LIMIT 1;
    " | tr -d '\r')

    [ "$SYNC" = "1" ] && break

    sleep 2
done

echo
echo "TEST FOLLOWUP=$FID"
echo "recording_synced=$SYNC"

if [ "$SYNC" = "1" ]; then

    echo
    echo "============================================================"
    echo "CRM TEST OK - ARRANCANDO TODO"
    echo "============================================================"

    systemctl start wf10-recording-worker

else

    echo
    echo "============================================================"
    echo "TEST TODAVIA FALLA"
    echo "WORKER QUEDA PARADO"
    echo "============================================================"

fi

echo
echo "WORKER:"
systemctl is-active wf10-recording-worker || true

echo
echo "QUEUE:"
find /var/spool/wf10/queue \
  -maxdepth 1 -name '*.json' | wc -l

echo
echo "DONE:"
find /var/spool/wf10/done \
  -maxdepth 1 -name '*.json' | wc -l

echo
echo "PENDIENTES ASTERISK:"
mariadb asterisk -e "
SELECT COUNT(*) AS pendientes
FROM wf_call_followups
WHERE provider='asterisk'
AND call_status='ANSWERED'
AND COALESCE(recording_synced,0)=0
AND created_at >= '2026-09-25 00:00:00'
AND created_at < '2026-09-26 00:00:00';
"

