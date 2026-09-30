#!/usr/bin/env bash
set -euo pipefail

WFID="TW1CHksqyf66MjQz"
DB=$(docker ps -q --filter name=landmarket_n8n-db | head -1)
STAMP=$(date +%Y%m%d-%H%M%S)

echo "============================================================"
echo "1. BACKUP NODES + CONNECTIONS"
echo "============================================================"

docker exec "$DB" psql -U postgres -d landmarket -Atc \
"SELECT nodes::text FROM workflow_entity WHERE id='$WFID';" \
> "/root/wf10-nodes-$STAMP.json"

docker exec "$DB" psql -U postgres -d landmarket -Atc \
"SELECT connections::text FROM workflow_entity WHERE id='$WFID';" \
> "/root/wf10-connections-$STAMP.json"

echo "Backup OK"

echo
echo "============================================================"
echo "2. MOSTRAR CADENA ASTERISK ACTUAL"
echo "============================================================"

python3 - "/root/wf10-nodes-$STAMP.json" "/root/wf10-connections-$STAMP.json" <<'PY'
import json,sys

nodes=json.load(open(sys.argv[1]))
conn=json.load(open(sys.argv[2]))

nd={n["name"]:n for n in nodes}

start="Webhook2"
seen=set()
stack=[start]

while stack:
    x=stack.pop()
    if x in seen:
        continue
    seen.add(x)

    for outputs in conn.get(x,{}).values():
        for branch in outputs:
            for edge in branch:
                y=edge.get("node")
                if y and y not in seen:
                    stack.append(y)

print("NODOS ALCANZABLES DESDE Webhook2:")
for x in sorted(seen):
    n=nd.get(x,{})
    print(
        ("DISABLED" if n.get("disabled") is True else "ENABLED "),
        "|",
        x
    )
PY

echo
echo "============================================================"
echo "3. HABILITAR NODOS REALMENTE CONECTADOS"
echo "============================================================"

python3 - \
"/root/wf10-nodes-$STAMP.json" \
"/root/wf10-connections-$STAMP.json" \
"/tmp/wf10-nodes-enabled.json" <<'PY'

import json,sys

nodes=json.load(open(sys.argv[1]))
conn=json.load(open(sys.argv[2]))

starts=[
    "Webhook2",
    "⏰ Schedule Stringee Recordings3"
]

reachable=set()
stack=list(starts)

while stack:
    x=stack.pop()

    if x in reachable:
        continue

    reachable.add(x)

    for outputs in conn.get(x,{}).values():
        for branch in outputs:
            for edge in branch:
                y=edge.get("node")
                if y and y not in reachable:
                    stack.append(y)

changed=[]

for n in nodes:
    if n.get("name") in reachable and n.get("disabled") is True:
        n["disabled"]=False
        changed.append(n["name"])

json.dump(
    nodes,
    open(sys.argv[3],"w"),
    ensure_ascii=False,
    separators=(",",":")
)

print("HABILITADOS:")
if changed:
    for x in changed:
        print(" -",x)
else:
    print(" ninguno; ya estaban habilitados")
PY

{
    echo "UPDATE workflow_entity SET nodes=\$JSON\$"
    cat /tmp/wf10-nodes-enabled.json
    echo "\$JSON\$::json WHERE id='$WFID';"
} | docker exec -i "$DB" \
      psql -U postgres -d landmarket >/dev/null

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

sleep 5

echo
echo "============================================================"
echo "5. ESTADO FINAL DE NODOS IMPORTANTES"
echo "============================================================"

docker exec "$DB" psql -U postgres -d landmarket -c "
SELECT
 n->>'name' AS node,
 COALESCE(n->>'disabled','false') AS disabled,
 COALESCE(n->'parameters'->>'url','') AS url
FROM workflow_entity w,
LATERAL jsonb_array_elements(w.nodes::jsonb) n
WHERE w.id='$WFID'
AND (
 n->>'name' LIKE '%Asterisk%'
 OR n->>'name' LIKE '%Stringee%'
 OR n->>'name'='Webhook2'
)
ORDER BY 1;
"

echo
echo "============================================================"
echo "6. SERVICIOS Y COLA"
echo "============================================================"

systemctl is-active wf10-token-cache
systemctl is-active wf10-recording-worker

echo -n "QUEUE: "
find /var/spool/wf10/queue -maxdepth 1 -name '*.json' | wc -l

echo -n "DONE:  "
find /var/spool/wf10/done -maxdepth 1 -name '*.json' | wc -l

echo
echo "FIX COMPLETO"
