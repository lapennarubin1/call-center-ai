#!/usr/bin/env bash
set -u

WFID="TW1CHksqyf66MjQz"

echo "============================================================"
echo "AUDITORIA WF10 ACTUAL - NO MODIFICA NADA"
echo "============================================================"

DB=$(docker ps -q --filter name=landmarket_n8n-db | head -1)

if [ -z "$DB" ]; then
    echo "ERROR: no encuentro postgres de n8n"
    exit 1
fi

echo
echo "============================================================"
echo "1. WORKFLOW ACTIVO"
echo "============================================================"

docker exec "$DB" psql -U postgres -d landmarket -Atc "
SELECT
    id || ' | ' ||
    name || ' | active=' ||
    active::text
FROM workflow_entity
WHERE id='$WFID';
"

echo
echo "============================================================"
echo "2. WEBHOOK PRODUCTIVO REGISTRADO"
echo "============================================================"

docker exec "$DB" psql -U postgres -d landmarket -c "
SELECT *
FROM webhook_entity
WHERE \"workflowId\"='$WFID';
" 2>/dev/null || true


echo
echo "============================================================"
echo "3. EXPORTAR WF10 LIVE"
echo "============================================================"

docker exec "$DB" psql -U postgres -d landmarket -Atc "
SELECT json_build_object(
    'id',id,
    'name',name,
    'active',active,
    'nodes',nodes::json,
    'connections',connections::json
)::text
FROM workflow_entity
WHERE id='$WFID';
" > /tmp/wf10-live.json

python3 <<'PY'
import json,re

fn="/tmp/wf10-live.json"

try:
    w=json.load(open(fn))
except Exception as e:
    print("ERROR leyendo workflow:",e)
    raise SystemExit(1)

nodes=w.get("nodes",[])
conn=w.get("connections",{})

byname={n.get("name"):n for n in nodes}

print("Workflow:",w.get("name"))
print("Active:",w.get("active"))

print()
print("="*80)
print("NODOS IMPORTANTES Y ESTADO")
print("="*80)

terms=[
    "Webhook",
    "Convert Base64",
    "Lookup Followup",
    "Followup Encontrado",
    "Login LeadStudio",
    "Attach Audio",
    "PUT Recording",
    "Mark Recording",
    "Code in JavaScript",
    "Telegram",
]

for n in nodes:

    name=n.get("name","")

    if any(t.lower() in name.lower() for t in terms):

        disabled=bool(n.get("disabled",False))

        print(
            ("OFF " if disabled else "ON  "),
            "|",
            name,
            "|",
            n.get("type")
        )


print()
print("="*80)
print("RUTA REAL DESDE WEBHOOK PRODUCTIVO")
print("="*80)

start="Webhook"

if start not in byname:
    print("NO EXISTE nodo llamado exactamente 'Webhook'")
    print()
    print("Webhooks encontrados:")

    for n in nodes:
        if "webhook" in n.get("type","").lower():
            print(
                n.get("name"),
                "disabled=",
                n.get("disabled",False)
            )

    raise SystemExit


seen=set()
queue=[(start,0)]

while queue:

    name,depth=queue.pop(0)

    if name in seen:
        continue

    seen.add(name)

    n=byname.get(name,{})
    disabled=bool(n.get("disabled",False))

    print(
        "  "*depth +
        ("[OFF] " if disabled else "[ON ] ") +
        name
    )

    outputs=conn.get(name,{}).get("main",[])

    children=[]

    for branch in outputs or []:
        for x in branch or []:
            dest=x.get("node")
            if dest:
                children.append(dest)

    for dest in children:
        if dest not in seen:
            queue.append((dest,depth+1))


print()
print("="*80)
print("NODOS ASTERISK DUPLICADOS")
print("="*80)

for n in nodes:
    name=n.get("name","")

    if "asterisk" in name.lower():
        print(
            ("OFF " if n.get("disabled",False) else "ON  "),
            "|",
            name
        )

PY


echo
echo "============================================================"
echo "4. WORKER WF10"
echo "============================================================"

printf "worker="
systemctl is-active wf10-recording-worker 2>/dev/null || true

echo

printf "QUEUE="
find /var/spool/wf10/queue \
    -maxdepth 1 \
    -type f \
    -name '*.json' \
    2>/dev/null | wc -l

printf "DONE="
find /var/spool/wf10/done \
    -maxdepth 1 \
    -type f \
    -name '*.json' \
    2>/dev/null | wc -l


echo
echo "============================================================"
echo "5. FOLLOWUPS ANSWERED PENDIENTES"
echo "============================================================"

mariadb asterisk -e "
SELECT
    COUNT(*) AS answered_unsynced
FROM wf_call_followups
WHERE call_status='ANSWERED'
  AND COALESCE(recording_synced,0)=0;

SELECT
    id,
    phone,
    followup_id,
    provider,
    outcome,
    call_status,
    recording_synced,
    created_at
FROM wf_call_followups
WHERE call_status='ANSWERED'
  AND COALESCE(recording_synced,0)=0
ORDER BY created_at DESC
LIMIT 40;
"


echo
echo "============================================================"
echo "6. CDR ANSWERED DE HOY"
echo "============================================================"

mariadb asterisk -e "
SELECT
    COUNT(*) AS answered_today
FROM cdr
WHERE calldate >= CURDATE()
  AND disposition='ANSWERED';

SELECT
    calldate,
    dst,
    duration,
    billsec,
    uniqueid
FROM cdr
WHERE calldate >= CURDATE()
  AND disposition='ANSWERED'
ORDER BY calldate DESC
LIMIT 50;
"


echo
echo "============================================================"
echo "7. FOLLOWUPS CREADOS HOY"
echo "============================================================"

mariadb asterisk -e "
SELECT
    COUNT(*) AS followups_today
FROM wf_call_followups
WHERE created_at >= CURDATE();

SELECT
    COUNT(*) AS answered_followups_today
FROM wf_call_followups
WHERE created_at >= CURDATE()
  AND call_status='ANSWERED';
"


echo
echo "============================================================"
echo "8. ULTIMOS LOGS WORKER"
echo "============================================================"

tail -80 /var/log/asterisk/wf10-worker.log 2>/dev/null || true


echo
echo "============================================================"
echo "AUDITORIA TERMINADA - NO SE MODIFICO NADA"
echo "============================================================"
