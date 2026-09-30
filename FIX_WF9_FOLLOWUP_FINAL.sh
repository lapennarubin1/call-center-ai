#!/usr/bin/env bash
set -euo pipefail

DB=$(docker ps -q --filter name=landmarket_n8n-db | head -1)
N8N=$(docker ps -q --filter name=landmarket_n8n | head -1)
STAMP=$(date +%Y%m%d-%H%M%S)

echo "============================================================"
echo "FIX DEFINITIVO WF9 -> wf_call_followups"
echo "============================================================"

WFID=$(docker exec "$DB" psql -U postgres -d landmarket -Atc "
SELECT \"workflowId\"
FROM webhook_entity
WHERE \"webhookPath\"='elevenlabs-postcall-webhook'
AND method='POST'
LIMIT 1;
")

WHNODE=$(docker exec "$DB" psql -U postgres -d landmarket -Atc "
SELECT node
FROM webhook_entity
WHERE \"webhookPath\"='elevenlabs-postcall-webhook'
AND method='POST'
LIMIT 1;
")

if [ -z "$WFID" ] || [ -z "$WHNODE" ]; then
    echo "ERROR: no encuentro webhook activo de WF9."
    exit 1
fi

echo "WF9 ID   : $WFID"
echo "Webhook  : $WHNODE"

echo
echo "1. BACKUP"

docker exec "$DB" psql -U postgres -d landmarket -Atc \
"SELECT nodes::text FROM workflow_entity WHERE id='$WFID';" \
> "/root/wf9-nodes-before-fix-$STAMP.json"

docker exec "$DB" psql -U postgres -d landmarket -Atc \
"SELECT connections::text FROM workflow_entity WHERE id='$WFID';" \
> "/root/wf9-connections-before-fix-$STAMP.json"

echo "Backup OK"

echo
echo "2. LOCALIZAR RAMA PRODUCTIVA Y CORREGIRLA"

python3 - \
"/root/wf9-nodes-before-fix-$STAMP.json" \
"/root/wf9-connections-before-fix-$STAMP.json" \
"/tmp/wf9-fixed.json" \
"$WHNODE" <<'PY'

import json
import sys

nodes=json.load(open(sys.argv[1],encoding="utf-8"))
connections=json.load(open(sys.argv[2],encoding="utf-8"))
output=sys.argv[3]
webhook=sys.argv[4]

by_name={
    n.get("name"): n
    for n in nodes
}

# ---------------------------------------------------------
# Encontrar solamente los nodos conectados al webhook REAL.
# ---------------------------------------------------------

reachable=set()
stack=[webhook]

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


builds=[
    x for x in reachable
    if "Build Followup Payload (WF9)" in x
]

saves=[
    x for x in reachable
    if "Save Followup ID (WF9)" in x
]

posts=[
    x for x in reachable
    if "POST Followup" in x
]

logins=[
    x for x in reachable
    if "Login LeadStudio (WF9)" in x
]

if len(builds)!=1:
    raise SystemExit(
        f"ERROR: esperaba 1 Build activo, encontre {builds}"
    )

if len(saves)!=1:
    raise SystemExit(
        f"ERROR: esperaba 1 Save activo, encontre {saves}"
    )

BUILD=builds[0]
SAVE=saves[0]

print("Build productivo:",BUILD)
print("Save productivo :",SAVE)

# ---------------------------------------------------------
# FIX PRINCIPAL:
# Save debe leer DEL MISMO Build de esta rama.
# ---------------------------------------------------------

save=by_name[SAVE]

save.setdefault("parameters",{})["query"]=f"""INSERT INTO wf_call_followups
(
 phone,
 lead_id,
 followup_id,
 provider,
 outcome,
 call_status
)
VALUES
(
 '{{{{ $("{BUILD}").first().json.phone }}}}',
 '{{{{ $("{BUILD}").first().json.lead_id }}}}',
 '{{{{ $json.followUp.id }}}}',
 '{{{{ $("{BUILD}").first().json.is_stringee ? "stringee" : "asterisk" }}}}',
 '{{{{ $("{BUILD}").first().json.outcome }}}}',
 '{{{{ $("{BUILD}").first().json.callStatus }}}}'
);"""

save["retryOnFail"]=True
save["maxTries"]=3
save["continueOnFail"]=False
save.pop("onError",None)

# ---------------------------------------------------------
# Si LeadStudio rechaza el followup, NO fingir éxito.
# ---------------------------------------------------------

for name in posts:

    n=by_name[name]
    p=n.setdefault("parameters",{})

    options=p.setdefault("options",{})
    response=options.setdefault("response",{})
    response2=response.setdefault("response",{})

    response2["neverError"]=False
    response2["responseFormat"]="json"

    n["retryOnFail"]=True
    n["maxTries"]=3
    n["continueOnFail"]=False
    n.pop("onError",None)

# ---------------------------------------------------------
# Reusar el token broker que ya funciona.
# Evita logins repetidos y 429.
# ---------------------------------------------------------

for name in logins:

    n=by_name[name]
    p=n.setdefault("parameters",{})

    p["method"]="POST"
    p["url"]="http://172.18.0.1:8092/token"
    p["sendBody"]=False

    p.pop("jsonBody",None)
    p.pop("bodyParameters",None)

    n["retryOnFail"]=False
    n["continueOnFail"]=False
    n.pop("onError",None)

json.dump(
    nodes,
    open(output,"w",encoding="utf-8"),
    ensure_ascii=False,
    separators=(",",":")
)

print("WF9 corregido.")
PY

{
    echo "UPDATE workflow_entity SET nodes=\$WFJSON\$"
    cat /tmp/wf9-fixed.json
    echo "\$WFJSON\$::json WHERE id='$WFID';"
} | docker exec -i "$DB" \
    psql -U postgres -d landmarket >/dev/null

echo
echo "3. PUBLICAR WF9"

docker exec "$N8N" \
  n8n publish:workflow \
  --id="$WFID" >/dev/null 2>&1 || true

docker service update --force landmarket_n8n >/dev/null

for i in $(seq 1 60); do

    R=$(docker service ls \
      --filter name=landmarket_n8n \
      --format '{{.Name}} {{.Replicas}}' \
      | awk '$1=="landmarket_n8n"{print $2}')

    [ "$R" = "1/1" ] && break

    sleep 1
done

sleep 6

DB=$(docker ps -q --filter name=landmarket_n8n-db | head -1)

echo
echo "============================================================"
echo "WF9 CORREGIDO"
echo "============================================================"

docker exec "$DB" psql -U postgres -d landmarket -c "
SELECT
 id,
 name,
 active
FROM workflow_entity
WHERE id='$WFID';
"

echo
echo "No se hizo ninguna llamada."
echo "No se envio ningun audio."
echo "No se envio Telegram."
echo
echo "A partir de ahora:"
echo "LeadStudio followup -> wf_call_followups automaticamente."
