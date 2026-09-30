#!/usr/bin/env bash
set -euo pipefail

WFID="TW1CHksqyf66MjQz"
DB=$(docker ps -q --filter name=landmarket_n8n-db | head -1)

docker exec "$DB" psql -U postgres -d landmarket -Atc "
SELECT json_build_object(
  'nodes',nodes::json,
  'connections',connections::json
)::text
FROM workflow_entity
WHERE id='$WFID';
" > /tmp/wf10-exact.json

python3 <<'PY'
import json

w=json.load(open("/tmp/wf10-exact.json"))

wanted=[
    "Convert Base64 to Binary",
    "🔍 Lookup Followup ID (Asterisk)",
    "🔀 Followup Encontrado? (Asterisk)",
    "🔐 Login LeadStudio (WF10-Asterisk)",
    "🔗 Attach Audio + Token (Asterisk)",
    "📤 PUT Recording — LeadStudio (Asterisk)",
    "💾 Mark Recording Synced (Asterisk)",
    "Code in JavaScript",
    "Send to Telegram",
    "Send to Telegram7",
]

for name in wanted:

    n=next(
        (x for x in w["nodes"] if x.get("name")==name),
        None
    )

    print()
    print("="*90)
    print(name)
    print("="*90)

    if not n:
        print("NO EXISTE")
        continue

    print("disabled =",n.get("disabled",False))

    p=n.get("parameters",{})

    for key in [
        "jsCode",
        "query",
        "url",
        "method",
        "jsonBody",
        "conditions",
        "chatId",
        "text",
    ]:
        if key in p:
            print()
            print(key.upper()+":")
            print(p[key])

PY

echo
echo "============================================================"
echo "WORKER ACTUAL"
echo "============================================================"

sed -n '1,260p' /usr/local/bin/wf10-worker.py

echo
echo "============================================================"
echo "ULTIMOS JOBS EN QUEUE"
echo "============================================================"

python3 <<'PY'
import glob,json,os

files=sorted(
    glob.glob("/var/spool/wf10/queue/*.json"),
    key=os.path.getmtime,
    reverse=True
)[:5]

for f in files:
    print()
    print("FILE:",f)

    try:
        d=json.load(open(f))

        for k,v in d.items():
            if k.lower() not in (
                "audio_base64",
                "base64",
                "audio"
            ):
                print(f"{k}: {v}")

    except Exception as e:
        print("ERROR:",e)
PY

echo
echo "============================================================"
echo "ENQUEUE LOG RECIENTE"
echo "============================================================"

tail -50 /var/log/asterisk/wf10-enqueue.log 2>/dev/null || true

echo
echo "============================================================"
echo "FIN - SOLO LECTURA"
echo "============================================================"
