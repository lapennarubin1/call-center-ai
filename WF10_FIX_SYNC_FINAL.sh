#!/usr/bin/env bash
set -euo pipefail

WFID="TW1CHksqyf66MjQz"
TEST_FID="262eb9a1-debf-4248-b42c-e066694c3038"

DB=$(docker ps -q --filter name=landmarket_n8n-db | head -1)
N8N=$(docker ps -q --filter name=landmarket_n8n | head -1)
STAMP=$(date +%Y%m%d-%H%M%S)

echo
echo "============================================================"
echo "1. PARAR WORKER"
echo "============================================================"

systemctl stop wf10-recording-worker || true

echo -n "WORKER: "
systemctl is-active wf10-recording-worker || true

echo
echo "============================================================"
echo "2. BACKUP REAL DE NODES + CONNECTIONS"
echo "============================================================"

docker exec "$DB" psql -U postgres -d landmarket -Atc \
"SELECT nodes::text FROM workflow_entity WHERE id='$WFID';" \
> "/root/wf10-sync-backup-nodes-$STAMP.json"

docker exec "$DB" psql -U postgres -d landmarket -Atc \
"SELECT connections::text FROM workflow_entity WHERE id='$WFID';" \
> "/root/wf10-sync-backup-connections-$STAMP.json"

WHNODE=$(docker exec "$DB" psql -U postgres -d landmarket -Atc "
SELECT node
FROM webhook_entity
WHERE \"workflowId\"='$WFID'
AND \"webhookPath\"='send-recording'
LIMIT 1;
")

echo "Webhook productivo actual: $WHNODE"

echo
echo "============================================================"
echo "3. CORREGIR WF10"
echo "============================================================"

python3 - \
"/root/wf10-sync-backup-nodes-$STAMP.json" \
"/root/wf10-sync-backup-connections-$STAMP.json" \
"/tmp/wf10-sync-fixed.json" \
"$WHNODE" <<'PY'

import json
import sys

nodes_path=sys.argv[1]
connections_path=sys.argv[2]
output_path=sys.argv[3]
webhook_node=sys.argv[4]

nodes=json.load(open(nodes_path,encoding="utf-8"))
connections=json.load(open(connections_path,encoding="utf-8"))

by_name={
    n.get("name"):n
    for n in nodes
}

# ------------------------------------------------------------
# A) HABILITAR LA RAMA REAL QUE SALE DEL WEBHOOK PRODUCTIVO
# ------------------------------------------------------------

reachable=set()
stack=[webhook_node]

while stack:

    name=stack.pop()

    if name in reachable:
        continue

    reachable.add(name)

    outputs=connections.get(name,{})

    for branches in outputs.values():

        if not isinstance(branches,list):
            continue

        for branch in branches:

            if not isinstance(branch,list):
                continue

            for edge in branch:

                nxt=edge.get("node")

                if nxt and nxt not in reachable:
                    stack.append(nxt)

for name in reachable:

    if name in by_name:
        by_name[name]["disabled"]=False

# Si existe otro Webhook duplicado, dejar solamente el registrado.
for n in nodes:

    name=n.get("name","")

    if (
        name.startswith("Webhook")
        and name != webhook_node
    ):
        n["disabled"]=True


# ------------------------------------------------------------
# B) LOGIN: TODOS POR TOKEN CACHE
# ------------------------------------------------------------

for n in nodes:

    name=n.get("name","")
    p=n.setdefault("parameters",{})

    if "Login LeadStudio" in name:

        p["method"]="POST"
        p["url"]="http://172.18.0.1:8092/token"
        p["sendBody"]=False

        p.pop("jsonBody",None)
        p.pop("bodyParameters",None)

        n["retryOnFail"]=False
        n["continueOnFail"]=False
        n.pop("onError",None)


# ------------------------------------------------------------
# C) PUT: ERROR HTTP DEBE PARAR
# ------------------------------------------------------------

for n in nodes:

    name=n.get("name","")

    if "PUT Recording" not in name:
        continue

    p=n.setdefault("parameters",{})

    opt=p.setdefault("options",{})
    r1=opt.setdefault("response",{})
    r2=r1.setdefault("response",{})

    r2["neverError"]=False
    r2["responseFormat"]="json"

    n["retryOnFail"]=False
    n["continueOnFail"]=False
    n.pop("onError",None)


# ------------------------------------------------------------
# D) FIX PRINCIPAL
#
# El PUT entrega:
#
# {
#   followUpId: "...",
#   hasRecording: true
# }
#
# Por tanto NO necesitamos buscar otro nodo.
# ------------------------------------------------------------

mark_nodes=[]

for n in nodes:

    name=n.get("name","")

    if "Mark Recording Synced" not in name:
        continue

    p=n.setdefault("parameters",{})

    p["query"] = """UPDATE wf_call_followups
SET recording_synced = 1
WHERE followup_id = '{{ $json.followUpId }}'
  AND '{{ $json.hasRecording }}' = 'true';"""

    n["retryOnFail"]=True
    n["maxTries"]=3
    n["continueOnFail"]=False
    n.pop("onError",None)

    mark_nodes.append(name)


# ------------------------------------------------------------
# E) STRINGEE: NUNCA MEZCLAR CON ASTERISK
#
# Esto evita nuevamente el caso Sharath.
# ------------------------------------------------------------

for n in nodes:

    name=n.get("name","")

    if "Lookup Followup ID (Stringee)" not in name:
        continue

    p=n.setdefault("parameters",{})

    p["query"] = """SELECT

(
 SELECT followup_id
 FROM wf_call_followups

 WHERE RIGHT(phone,10)=RIGHT('{{ $json.phone }}',10)

   AND provider='stringee'

   AND call_status='ANSWERED'

   AND COALESCE(recording_synced,0)=0

   AND created_at >= UTC_TIMESTAMP() - INTERVAL 1 DAY

 ORDER BY created_at DESC

 LIMIT 1
) AS followup_id,

(
 SELECT lead_id
 FROM wf_call_followups

 WHERE RIGHT(phone,10)=RIGHT('{{ $json.phone }}',10)

   AND provider='stringee'

   AND call_status='ANSWERED'

   AND COALESCE(recording_synced,0)=0

   AND created_at >= UTC_TIMESTAMP() - INTERVAL 1 DAY

 ORDER BY created_at DESC

 LIMIT 1
) AS lead_id;"""


# ------------------------------------------------------------
# F) ASTERISK LOOKUP:
# EXIGIR provider=asterisk
# ------------------------------------------------------------

for n in nodes:

    name=n.get("name","")

    if "Lookup Followup ID (Asterisk)" not in name:
        continue

    p=n.setdefault("parameters",{})

    p["query"] = """SELECT

(
 SELECT followup_id
 FROM wf_call_followups

 WHERE RIGHT(phone,10)=RIGHT('{{ $json.phone }}',10)

   AND provider='asterisk'

   AND call_status='ANSWERED'

   AND COALESCE(recording_synced,0)=0

 ORDER BY created_at DESC

 LIMIT 1
) AS followup_id,

(
 SELECT lead_id
 FROM wf_call_followups

 WHERE RIGHT(phone,10)=RIGHT('{{ $json.phone }}',10)

   AND provider='asterisk'

   AND call_status='ANSWERED'

   AND COALESCE(recording_synced,0)=0

 ORDER BY created_at DESC

 LIMIT 1
) AS lead_id;"""


# ------------------------------------------------------------
# G) BACKFILL ASTERISK NO REPETIR TELEGRAM
#
# Encontramos el Code conectado inmediatamente después
# de cada Mark Recording Synced Asterisk.
# ------------------------------------------------------------

for mark_name in mark_nodes:

    if "Asterisk" not in mark_name:
        continue

    outputs=connections.get(mark_name,{})

    suffix="2" if mark_name.endswith("2") else ""

    attach_name=f"🔗 Attach Audio + Token (Asterisk){suffix}"

    for branches in outputs.values():

        if not isinstance(branches,list):
            continue

        for branch in branches:

            if not isinstance(branch,list):
                continue

            for edge in branch:

                dest=edge.get("node")

                if dest not in by_name:
                    continue

                node=by_name[dest]

                if node.get("type")!="n8n-nodes-base.code":
                    continue

                node.setdefault(
                    "parameters",{}
                )["jsCode"]=f"""const items =
  $('{attach_name}').all();

return items
  .filter(it => {{

    if (
      it.json.suppress_telegram === true
    ) {{
      return false;
    }}

    const secs =
      Number(
        it.json.duration_secs || 0
      );

    return (
      it.json.duration_unknown !== true &&
      secs >= 60
    );

  }})
  .map(it => ({{
    json:it.json,
    binary:it.binary
  }}));"""


json.dump(
    nodes,
    open(output_path,"w",encoding="utf-8"),
    ensure_ascii=False,
    separators=(",",":")
)

print()
print("Rama webhook habilitada:")
for x in sorted(reachable):
    print(" +",x)

print()
print("Mark Recording Synced corregidos:")
for x in mark_nodes:
    print(" FIX",x)

PY

{
    echo "UPDATE workflow_entity SET nodes=\$WFJSON\$"
    cat /tmp/wf10-sync-fixed.json
    echo "\$WFJSON\$::json WHERE id='$WFID';"
} | docker exec -i "$DB" \
      psql -U postgres -d landmarket >/dev/null

echo
echo "============================================================"
echo "4. PUBLICAR LA VERSION REAL"
echo "============================================================"

docker exec "$N8N" \
 n8n publish:workflow \
 --id="$WFID" || true

echo
echo "Reiniciando n8n..."

docker service update \
 --force landmarket_n8n >/dev/null

for i in $(seq 1 60); do

    R=$(docker service ls \
       --filter name=landmarket_n8n \
       --format '{{.Name}} {{.Replicas}}' \
       | awk '$1=="landmarket_n8n"{print $2}')

    [ "$R" = "1/1" ] && break

    sleep 1

done

sleep 8

DB=$(docker ps -q \
 --filter name=landmarket_n8n-db \
 | head -1)

echo
echo "============================================================"
echo "5. CONFIRMAR WEBHOOK"
echo "============================================================"

docker exec "$DB" psql \
 -U postgres \
 -d landmarket \
 -c "
SELECT
 \"webhookPath\",
 method,
 node,
 \"workflowId\"
FROM webhook_entity
WHERE \"webhookPath\"='send-recording';
"

HTTP=$(curl -sS \
 -o /tmp/wf10-health.txt \
 -w '%{http_code}' \
 -X POST \
 'https://landmarket-n8n.dhsoig.easypanel.host/webhook/send-recording' \
 -H 'Content-Type: application/json' \
 -d '{}')

echo "WEBHOOK HTTP=$HTTP"
cat /tmp/wf10-health.txt
echo

echo
echo "============================================================"
echo "6. ARREGLAR LA PRUEBA QUE YA FUE SUBIDA"
echo "============================================================"

echo "LeadStudio ya respondio:"
echo "followUpId=$TEST_FID"
echo "hasRecording=true"
echo
echo "Por eso solo reconciliamos nuestra DB."

mariadb asterisk -e "
UPDATE wf_call_followups
SET recording_synced=1
WHERE followup_id='$TEST_FID';
"

mariadb asterisk -e "
SELECT
 phone,
 lead_id,
 followup_id,
 provider,
 call_status,
 recording_synced,
 created_at
FROM wf_call_followups
WHERE followup_id='$TEST_FID';
"

echo
echo "============================================================"
echo "7. SACAR ESA PRUEBA DE QUEUE PARA NO DUPLICARLA"
echo "============================================================"

python3 <<'PY'
import glob
import json
import os
import shutil

FID="262eb9a1-debf-4248-b42c-e066694c3038"

queue="/var/spool/wf10/queue"
done="/var/spool/wf10/done"

os.makedirs(done,exist_ok=True)

found=0

for path in glob.glob(
    queue+"/*.json"
):

    try:
        j=json.load(open(path))
    except:
        continue

    if j.get("followup_id") != FID:
        continue

    j["completed_manually"]=True
    j["reason"]="PUT LeadStudio confirmed hasRecording=true"

    tmp=path+".tmp"

    with open(tmp,"w") as f:
        json.dump(j,f)

    os.replace(tmp,path)

    dest=os.path.join(
        done,
        os.path.basename(path)
    )

    if os.path.exists(dest):
        os.remove(dest)

    shutil.move(path,dest)

    found+=1

print("JOB TEST MOVIDO A DONE:",found)
PY

echo
echo "============================================================"
echo "8. RESET DE LAS PENDIENTES"
echo "============================================================"

python3 <<'PY'
import glob
import json
import os

count=0

for path in glob.glob(
    "/var/spool/wf10/queue/*.json"
):

    with open(path) as f:
        j=json.load(f)

    if j.get("backfill"):
        j["suppress_telegram"]=True

    j["attempts"]=0
    j["next_attempt"]=0

    j.pop("last_error",None)

    tmp=path+".tmp"

    with open(tmp,"w") as f:
        json.dump(j,f)

    os.replace(tmp,path)

    count+=1

print("JOBS PREPARADOS:",count)
PY

echo
echo "============================================================"
echo "9. ARRANCAR WORKER"
echo "============================================================"

systemctl restart wf10-recording-worker

sleep 5

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
echo "10. ESPERAR 90 SEGUNDOS"
echo "============================================================"

sleep 90

echo
echo "============================================================"
echo "11. RESULTADO"
echo "============================================================"

echo -n "QUEUE : "
find /var/spool/wf10/queue \
 -maxdepth 1 -name '*.json' | wc -l

echo -n "DONE  : "
find /var/spool/wf10/done \
 -maxdepth 1 -name '*.json' | wc -l

echo
echo "PENDIENTES DB:"

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
echo "ULTIMOS LOGS:"
tail -60 \
 /var/log/asterisk/wf10-worker.log

echo
echo "============================================================"
echo "FIN"
echo "============================================================"

