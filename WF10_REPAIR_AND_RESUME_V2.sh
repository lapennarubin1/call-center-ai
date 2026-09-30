#!/usr/bin/env bash
set -euo pipefail

WFID="TW1CHksqyf66MjQz"
DB=$(docker ps -q --filter name=landmarket_n8n-db | head -1)
STAMP=$(date +%Y%m%d-%H%M%S)

echo
echo "============================================================"
echo "1. PARAR WORKER"
echo "============================================================"

systemctl stop wf10-recording-worker || true

echo -n "WORKER: "
systemctl is-active wf10-recording-worker || true

echo -n "QUEUE : "
find /var/spool/wf10/queue -maxdepth 1 -name '*.json' | wc -l

echo
echo "============================================================"
echo "2. BACKUP WF10"
echo "============================================================"

docker exec "$DB" psql -U postgres -d landmarket -Atc \
"SELECT nodes::text FROM workflow_entity WHERE id='$WFID';" \
> "/root/wf10-before-v2-$STAMP.json"

echo "Backup OK"

echo
echo "============================================================"
echo "3. PATCH WF10 POR NOMBRE DE NODO"
echo "============================================================"

python3 - \
"/root/wf10-before-v2-$STAMP.json" \
"/tmp/wf10-v2.json" <<'PY'

import json
import sys

src=sys.argv[1]
dst=sys.argv[2]

nodes=json.load(open(src,encoding="utf-8"))

# ------------------------------------------------------------
# RAMA LEGACY: APAGAR
# ------------------------------------------------------------

legacy={
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
    "💾 Registrar Enviadas (ledger)"
}

# ------------------------------------------------------------
# RAMA ACTUAL: ENCENDER
# ------------------------------------------------------------

current={
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
    "🔎 Ya Enviadas (ledger)1",
    "🔄 Fetch & Convert Stringee Recordings2",
    "🔍 Lookup Followup ID (Stringee)2",
    "🔀 Followup Encontrado? (Stringee)2",
    "🔐 Login LeadStudio (WF10-Stringee)2",
    "🔗 Attach Audio + Token (Stringee)2",
    "📤 PUT Recording — LeadStudio (Stringee)2",
    "💾 Mark Recording Synced (Stringee)3",
    "Code in JavaScript (Stringee)3",
    "⚙️ SQL Registrar Enviadas1",
    "💾 Registrar Enviadas (ledger)1"
}

changed=[]

for n in nodes:

    name=n.get("name","")
    p=n.setdefault("parameters",{})

    if name in legacy:
        n["disabled"]=True

    if name in current:
        n["disabled"]=False

    # ========================================================
    # TODOS LOS LOGIN ACTIVOS -> TOKEN CACHE
    # ========================================================

    if name in {
        "🔐 Login LeadStudio (WF10-Asterisk)2",
        "🔐 Login LeadStudio (WF10-Stringee)2"
    }:

        p["method"]="POST"
        p["url"]="http://172.18.0.1:8092/token"
        p["sendBody"]=False

        p.pop("jsonBody",None)
        p.pop("bodyParameters",None)

        n["retryOnFail"]=False
        n["continueOnFail"]=False
        n.pop("onError",None)

    # ========================================================
    # ASTERISK:
    # followup exacto para backfill.
    # fallback telefono para LIVE.
    # ========================================================

    if name=="🔍 Lookup Followup ID (Asterisk)2":

        p["query"]=r"""SELECT

COALESCE(
 (
  SELECT followup_id
  FROM wf_call_followups
  WHERE followup_id='{{ $json.followup_id || "" }}'
    AND provider='asterisk'
    AND call_status='ANSWERED'
    AND COALESCE(recording_synced,0)=0
  LIMIT 1
 ),
 (
  SELECT followup_id
  FROM wf_call_followups
  WHERE RIGHT(phone,10)=RIGHT('{{ $json.phone }}',10)
    AND provider='asterisk'
    AND call_status='ANSWERED'
    AND COALESCE(recording_synced,0)=0
  ORDER BY created_at DESC
  LIMIT 1
 )
) AS followup_id,

COALESCE(
 (
  SELECT lead_id
  FROM wf_call_followups
  WHERE followup_id='{{ $json.followup_id || "" }}'
    AND provider='asterisk'
    AND call_status='ANSWERED'
    AND COALESCE(recording_synced,0)=0
  LIMIT 1
 ),
 (
  SELECT lead_id
  FROM wf_call_followups
  WHERE RIGHT(phone,10)=RIGHT('{{ $json.phone }}',10)
    AND provider='asterisk'
    AND call_status='ANSWERED'
    AND COALESCE(recording_synced,0)=0
  ORDER BY created_at DESC
  LIMIT 1
 )
) AS lead_id;"""

    # ========================================================
    # STRINGEE:
    #
    # filename ejemplo:
    # stringee-919964616975-1790328779273.wav
    #
    # Del filename sacamos timestamp epoch-ms.
    #
    # OBLIGATORIO:
    # provider=stringee
    # ANSWERED
    # mismo numero
    # +/- 5 minutos
    # ========================================================

    if name=="🔍 Lookup Followup ID (Stringee)2":

        p["query"]=r"""SELECT

(
 SELECT followup_id
 FROM wf_call_followups

 WHERE RIGHT(phone,10)=RIGHT('{{ $json.phone }}',10)

   AND provider='stringee'

   AND call_status='ANSWERED'

   AND COALESCE(recording_synced,0)=0

   AND ABS(
       TIMESTAMPDIFF(
           SECOND,
           created_at,
           FROM_UNIXTIME(
             {{
               Number(
                 String($json.filename || '')
                   .split('-')
                   .pop()
                   .replace('.wav','')
               ) / 1000
             }}
           )
       )
   ) <= 300

 ORDER BY ABS(
       TIMESTAMPDIFF(
           SECOND,
           created_at,
           FROM_UNIXTIME(
             {{
               Number(
                 String($json.filename || '')
                   .split('-')
                   .pop()
                   .replace('.wav','')
               ) / 1000
             }}
           )
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

   AND COALESCE(recording_synced,0)=0

   AND ABS(
       TIMESTAMPDIFF(
           SECOND,
           created_at,
           FROM_UNIXTIME(
             {{
               Number(
                 String($json.filename || '')
                   .split('-')
                   .pop()
                   .replace('.wav','')
               ) / 1000
             }}
           )
       )
   ) <= 300

 ORDER BY ABS(
       TIMESTAMPDIFF(
           SECOND,
           created_at,
           FROM_UNIXTIME(
             {{
               Number(
                 String($json.filename || '')
                   .split('-')
                   .pop()
                   .replace('.wav','')
               ) / 1000
             }}
           )
       )
   ) ASC

 LIMIT 1

) AS lead_id;"""

        n["continueOnFail"]=False

    # ========================================================
    # STRINGEE ATTACH - NO OCULTAR ERRORES
    # ========================================================

    if name=="🔗 Attach Audio + Token (Stringee)2":

        p["mode"]="runOnceForEachItem"

        p["jsCode"]=r"""const token=$input.item.json.accessToken;

const lookup=
  $('🔍 Lookup Followup ID (Stringee)2').item.json;

const original=
  $('🔄 Fetch & Convert Stringee Recordings2').item;

if (!token) {
  throw new Error(
    'WF10 Stringee: accessToken ausente'
  );
}

if (!lookup.followup_id) {
  throw new Error(
    'WF10 Stringee: followup_id ausente'
  );
}

if (!original.binary?.data) {
  throw new Error(
    'WF10 Stringee: audio binary ausente'
  );
}

return {
  json:{
    ...original.json,
    accessToken:token,
    followup_id:lookup.followup_id,
    lead_id:lookup.lead_id
  },
  binary:original.binary
};"""

        n["retryOnFail"]=False
        n["continueOnFail"]=False
        n.pop("onError",None)

    # ========================================================
    # PUTS: ERROR HTTP = ERROR REAL
    # ========================================================

    if name in {
        "📤 PUT Recording — LeadStudio (Asterisk)2",
        "📤 PUT Recording — LeadStudio (Stringee)2"
    }:

        opt=p.setdefault("options",{})
        r1=opt.setdefault("response",{})
        r2=r1.setdefault("response",{})

        r2["neverError"]=False
        r2["responseFormat"]="json"

        n["retryOnFail"]=False
        n["continueOnFail"]=False
        n.pop("onError",None)

    # ========================================================
    # BACKFILL ASTERISK:
    # nunca repetir Telegram.
    #
    # LIVE:
    # Telegram solamente >=60s.
    # ========================================================

    if name=="Code in JavaScript2":

        p["jsCode"]=r"""const items=
  $('🔗 Attach Audio + Token (Asterisk)2').all();

return items
  .filter(it => {

    if (it.json.suppress_telegram === true) {
      return false;
    }

    const secs=
      Number(it.json.duration_secs || 0);

    return (
      it.json.duration_unknown !== true &&
      secs >= 60
    );
  })
  .map(it => ({
    json:it.json,
    binary:it.binary
  }));"""

    # ========================================================
    # MARK SYNC EXACTO ASTERISK
    # ========================================================

    if name=="💾 Mark Recording Synced (Asterisk)2":

        p["query"]=r"""UPDATE wf_call_followups
SET recording_synced=1
WHERE followup_id='{{ $("🔗 Attach Audio + Token (Asterisk)2").first().json.followup_id }}';"""

        n["retryOnFail"]=True
        n["maxTries"]=3
        n["continueOnFail"]=False

    changed.append(name)

json.dump(
    nodes,
    open(dst,"w",encoding="utf-8"),
    ensure_ascii=False,
    separators=(",",":")
)

print("JSON WF10 generado correctamente")
print("Nodos:",len(nodes))

PY

{
    echo "UPDATE workflow_entity SET nodes=\$WFJSON\$"
    cat /tmp/wf10-v2.json
    echo "\$WFJSON\$::json WHERE id='$WFID';"
} | docker exec -i "$DB" \
    psql -U postgres -d landmarket >/dev/null

echo "WF10 actualizado."

echo
echo "============================================================"
echo "4. REINICIAR N8N"
echo "============================================================"

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

echo "n8n:"
docker service ls \
 --filter name=landmarket_n8n \
 --format '{{.Name}} {{.Replicas}}'

echo
echo "============================================================"
echo "5. VERIFICAR NODOS STRINGEE"
echo "============================================================"

docker exec "$DB" psql -U postgres -d landmarket -c "
SELECT
 n->>'name' AS node,
 COALESCE(n->>'disabled','false') AS disabled
FROM workflow_entity w,
LATERAL jsonb_array_elements(w.nodes::jsonb) n
WHERE w.id='$WFID'
AND (
 n->>'name' LIKE '%Schedule Stringee%'
 OR n->>'name' LIKE '%PUT Recording%Stringee%'
 OR n->>'name' LIKE '%Lookup Followup ID%Stringee%'
)
ORDER BY 1;
"

echo
echo "============================================================"
echo "6. WEBHOOK"
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

echo
echo "============================================================"
echo "7. TOKEN"
echo "============================================================"

TOKEN=$(curl -s \
 -X POST \
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

    echo "Token no disponible."
    echo "Worker permanece parado."

    exit 0

fi

echo
echo "============================================================"
echo "8. ASEGURAR QUE WORKER ENVIA FOLLOWUP EXACTO"
echo "============================================================"

grep -nE \
'followup_id|uniqueid|suppress_telegram' \
/usr/local/bin/wf10-worker.py \
| tail -20

if ! grep -q \
'"followup_id": job.get("followup_id")' \
/usr/local/bin/wf10-worker.py
then
    echo "ERROR: worker no contiene followup_id."
    exit 1
fi

echo "Worker payload OK."

echo
echo "============================================================"
echo "9. ACTUALIZAR BACKFILL CON NUEVAS ANSWERED DE HOY"
echo "============================================================"

python3 /root/wf10-backfill-today.py || true

echo
echo "============================================================"
echo "10. PROTEGER TODO BACKFILL DE TELEGRAM"
echo "============================================================"

python3 <<'PY'
import glob
import json
import os

n=0

for path in glob.glob(
    "/var/spool/wf10/queue/*.json"
):

    with open(path) as f:
        j=json.load(f)

    if not j.get("backfill"):
        continue

    j["suppress_telegram"]=True
    j["attempts"]=0
    j["next_attempt"]=0
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
echo "11. CREAR PRUEBA CON UNA SOLA GRABACION"
echo "============================================================"

python3 <<'PY'
import glob
import json
import base64
import sys

for path in sorted(
    glob.glob("/var/spool/wf10/queue/*.json")
):

    j=json.load(open(path))

    if not j.get("followup_id"):
        continue

    wav=j.get("wav")

    if not wav:
        continue

    with open(wav,"rb") as f:
        audio=base64.b64encode(
            f.read()
        ).decode()

    payload={
        "phone":j["phone"],
        "date":"2026-09-25",
        "filename":j["filename"],
        "audio_base64":audio,
        "followup_id":j["followup_id"],
        "uniqueid":j.get("uniqueid"),
        "suppress_telegram":True
    }

    json.dump(
        payload,
        open(
            "/tmp/wf10-v2-test.json",
            "w"
        )
    )

    open(
        "/tmp/wf10-v2-fid",
        "w"
    ).write(
        j["followup_id"]
    )

    print("PHONE   :",j["phone"])
    print("FOLLOWUP:",j["followup_id"])
    print("WAV     :",j["filename"])

    sys.exit(0)

raise SystemExit(
    "ERROR: no encuentro job para prueba"
)
PY

echo
echo "============================================================"
echo "12. ENVIAR UNA SOLA PRUEBA"
echo "============================================================"

HTTP=$(curl -sS \
 -o /tmp/wf10-v2-response.txt \
 -w '%{http_code}' \
 -X POST \
 'https://landmarket-n8n.dhsoig.easypanel.host/webhook/send-recording' \
 -H 'Content-Type: application/json' \
 --data-binary @/tmp/wf10-v2-test.json)

echo "HTTP=$HTTP"

cat /tmp/wf10-v2-response.txt
echo

if [ "$HTTP" != "200" ]; then

    echo "Webhook fallo."
    echo "Worker permanece parado."

    exit 1

fi

FID=$(cat /tmp/wf10-v2-fid)

echo
echo "Esperando CRM..."

SYNC=0

for i in $(seq 1 40); do

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
echo "============================================================"
echo "13. RESULTADO TEST"
echo "============================================================"

echo "FOLLOWUP=$FID"
echo "recording_synced=$SYNC"

if [ "$SYNC" != "1" ]; then

    echo
    echo "NO ARRANCO EL WORKER."
    echo "Hay que revisar esa ejecucion concreta en n8n."

    exit 0

fi

echo
echo "CRM CONFIRMADO."

echo
echo "============================================================"
echo "14. ARRANCAR PROCESAMIENTO MASIVO"
echo "============================================================"

systemctl start wf10-recording-worker

sleep 3

echo -n "WORKER: "
systemctl is-active wf10-recording-worker

echo -n "QUEUE : "
find /var/spool/wf10/queue \
 -maxdepth 1 -name '*.json' | wc -l

echo -n "DONE  : "
find /var/spool/wf10/done \
 -maxdepth 1 -name '*.json' | wc -l

echo
echo "============================================================"
echo "15. PENDIENTES ASTERISK"
echo "============================================================"

mariadb asterisk -e "
SELECT COUNT(*) AS pendientes
FROM wf_call_followups
WHERE provider='asterisk'
AND call_status='ANSWERED'
AND COALESCE(recording_synced,0)=0
AND created_at >= '2026-09-25 00:00:00'
AND created_at < '2026-09-26 00:00:00';
"

echo
echo "============================================================"
echo "LISTO"
echo "============================================================"

