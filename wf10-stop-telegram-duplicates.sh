#!/usr/bin/env bash
set -euo pipefail

WFID="TW1CHksqyf66MjQz"
DB=$(docker ps -q --filter name=landmarket_n8n-db | head -1)
STAMP=$(date +%Y%m%d-%H%M%S)

echo "============================================================"
echo "1. PARAR WORKER AHORA MISMO"
echo "============================================================"

systemctl stop wf10-recording-worker

echo "Worker parado. Las grabaciones nuevas seguiran entrando en QUEUE."

echo
echo "============================================================"
echo "2. BACKFILL: NO MAS TELEGRAM"
echo "============================================================"

python3 <<'PY'
import glob,json,os

files=glob.glob("/var/spool/wf10/queue/*.json")
n=0

for path in files:
    with open(path) as f:
        j=json.load(f)

    # Estas llamadas antiguas van SOLO al CRM.
    j["suppress_telegram"]=True
    j["attempts"]=0
    j["next_attempt"]=0
    j.pop("last_error",None)

    tmp=path+".tmp"

    with open(tmp,"w") as f:
        json.dump(j,f)

    os.replace(tmp,path)
    n+=1

print("JOBS PROTEGIDOS DE TELEGRAM:",n)
PY

echo
echo "============================================================"
echo "3. HACER QUE EL WORKER ENVIE suppress_telegram A WF10"
echo "============================================================"

cp -a /usr/local/bin/wf10-worker.py \
      "/root/wf10-worker-before-suppress-$STAMP.py"

python3 <<'PY'
from pathlib import Path

p=Path("/usr/local/bin/wf10-worker.py")
s=p.read_text()

if '"suppress_telegram": bool(job.get("suppress_telegram", False))' not in s:

    old='''        "filename": os.path.basename(wav),
        "audio_base64": audio
    }'''

    new='''        "filename": os.path.basename(wav),
        "audio_base64": audio,
        "suppress_telegram": bool(job.get("suppress_telegram", False))
    }'''

    if old not in s:
        raise SystemExit("ERROR: no encontre payload del worker. No modificado.")

    s=s.replace(old,new,1)
    p.write_text(s)

print("Worker payload OK")
PY

echo
echo "============================================================"
echo "4. PATCH WF10: NOMBRE REAL + SUPRIMIR TELEGRAM EN BACKFILL"
echo "============================================================"

docker exec "$DB" psql -U postgres -d landmarket -Atc \
"SELECT nodes::text FROM workflow_entity WHERE id='$WFID';" \
> "/root/wf10-before-telegram-fix-$STAMP.json"

python3 - \
"/root/wf10-before-telegram-fix-$STAMP.json" \
"/tmp/wf10-telegram-fix.json <<'PY'

import json,sys

nodes=json.load(open(sys.argv[1]))

found_convert=False
found_filter=False

for n in nodes:

    name=n.get("name","")
    p=n.setdefault("parameters",{})

    if name=="Convert Base64 to Binary2":

        p["jsCode"]=r"""const item = $input.first().json;
const body = item.body || item;

if (!body.audio_base64) {
  throw new Error('WF10 Asterisk: audio_base64 ausente');
}

const buffer = Buffer.from(body.audio_base64, 'base64');

const phoneRaw = String(body.phone || '');
const phoneDigits = phoneRaw.replace(/[^0-9]/g, '');

let country = 'unknown';

if (phoneDigits.startsWith('977')) country = 'nepal';
else if (phoneDigits.startsWith('91')) country = 'india';
else if (phoneDigits.startsWith('52')) country = 'mexico';
else if (phoneDigits.startsWith('58')) country = 'venezuela';
else if (phoneDigits.startsWith('57')) country = 'colombia';

let duration_secs = 0;
let duration_unknown = true;

try {
  if (
    buffer.length >= 44 &&
    buffer.toString('ascii',0,4) === 'RIFF'
  ) {
    const byteRate = buffer.readUInt32LE(28);

    if (byteRate > 0) {
      duration_secs = Math.round(
        Math.max(0,buffer.length-44) / byteRate
      );

      duration_unknown = false;
    }
  }
} catch (e) {}

const suppress_telegram =
  body.suppress_telegram === true ||
  String(body.suppress_telegram || '').toLowerCase() === 'true';

const date =
  String(body.date || new Date().toISOString().slice(0,10));

const display_filename =
  `CALL-${phoneDigits || 'UNKNOWN'}-${date}-${duration_secs}s.wav`;

return [{
  json: {
    phone: body.phone,
    date,
    filename: body.filename,
    display_filename,
    country,
    duration_secs,
    duration_unknown,
    suppress_telegram
  },
  binary: {
    data: {
      data: buffer.toString('base64'),
      mimeType: 'audio/wav',
      fileName: display_filename
    }
  }
}];"""

        found_convert=True

    if name=="Code in JavaScript2":

        p["jsCode"]=r"""// Telegram SOLAMENTE para llamadas LIVE >=60 s.
// Los backfills/reintentos con suppress_telegram=true NUNCA se envian.

const items =
  $('🔗 Attach Audio + Token (Asterisk)2').all();

return items
  .filter(it => {
    if (it.json.suppress_telegram === true) {
      return false;
    }

    const secs=Number(it.json.duration_secs || 0);

    return (
      it.json.duration_unknown !== true &&
      secs >= 60
    );
  })
  .map(it => ({
    json: it.json,
    binary: it.binary
  }));"""

        found_filter=True

if not found_convert:
    raise SystemExit("ERROR: Convert Base64 to Binary2 no encontrado")

if not found_filter:
    raise SystemExit("ERROR: Code in JavaScript2 no encontrado")

json.dump(
    nodes,
    open(sys.argv[2],"w"),
    ensure_ascii=False,
    separators=(",",":")
)

print("WF10 patched OK")
PY

{
    echo "UPDATE workflow_entity SET nodes=\$JSON\$"
    cat /tmp/wf10-telegram-fix.json
    echo "\$JSON\$::json WHERE id='$WFID';"
} | docker exec -i "$DB" \
      psql -U postgres -d landmarket >/dev/null

echo
echo "============================================================"
echo "5. RESTART N8N"
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

echo "n8n OK"

echo
echo "============================================================"
echo "6. COMPROBAR TOKEN"
echo "============================================================"

TOKEN_OK=$(curl -s -X POST \
  http://172.18.0.1:8092/token | \
python3 -c '
import sys,json
try:
 d=json.load(sys.stdin)
 print("YES" if d.get("accessToken") else "NO")
except:
 print("NO")
')

echo "TOKEN_OK=$TOKEN_OK"

if [ "$TOKEN_OK" != "YES" ]; then

    echo
    echo "TOKEN TODAVIA NO DISPONIBLE."
    echo "Worker queda PARADO."
    echo "Las grabaciones siguen seguras en QUEUE."

    exit 0
fi

echo
echo "============================================================"
echo "7. PROBAR SOLO UNA GRABACION"
echo "============================================================"

python3 <<'PY'
import glob,json,base64,sys

files=sorted(glob.glob("/var/spool/wf10/queue/*.json"))

if not files:
    print("NO_QUEUE")
    sys.exit(2)

path=files[0]

j=json.load(open(path))

wav=j["wav"]

with open(wav,"rb") as f:
    audio=base64.b64encode(f.read()).decode()

payload={
    "phone":j["phone"],
    "date":"2026-09-25",
    "filename":j["filename"],
    "audio_base64":audio,

    # CLAVE: prueba CRM sin Telegram
    "suppress_telegram":True
}

json.dump(payload,open("/tmp/wf10-one-test.json","w"))

open("/tmp/wf10-one-fid","w").write(j["followup_id"])
open("/tmp/wf10-one-phone","w").write(j["phone"])
open("/tmp/wf10-one-uid","w").write(j["uniqueid"])

print("PHONE:",j["phone"])
print("FOLLOWUP:",j["followup_id"])
print("UNIQUEID:",j["uniqueid"])
PY

HTTP=$(curl -sS \
  -o /tmp/wf10-one-response.txt \
  -w '%{http_code}' \
  -X POST \
  'https://landmarket-n8n.dhsoig.easypanel.host/webhook/send-recording' \
  -H 'Content-Type: application/json' \
  --data-binary @/tmp/wf10-one-test.json)

echo "Webhook HTTP: $HTTP"
cat /tmp/wf10-one-response.txt
echo

echo "Esperando 20 segundos..."
sleep 20

FID=$(cat /tmp/wf10-one-fid)

SYNC=$(mariadb asterisk -N -B -e "
SELECT COALESCE(recording_synced,0)
FROM wf_call_followups
WHERE followup_id='$FID'
LIMIT 1;
")

echo
echo "============================================================"
echo "8. RESULTADO PRUEBA"
echo "============================================================"

echo "FOLLOWUP: $FID"
echo "recording_synced: ${SYNC:-NULL}"

if [ "$SYNC" = "1" ]; then

    echo
    echo "CRM OK."
    echo "Telegram fue SUPRIMIDO para esta prueba."
    echo
    echo "ARRANCANDO WORKER PARA LAS DEMAS..."

    systemctl start wf10-recording-worker

else

    echo
    echo "CRM/SYNC TODAVIA FALLA."
    echo "NO ARRANCO EL WORKER."
    echo "NO SE ENVIARAN MAS DUPLICADOS A TELEGRAM."

fi

echo
echo "============================================================"
echo "9. ESTADO FINAL"
echo "============================================================"

echo -n "WORKER: "
systemctl is-active wf10-recording-worker || true

echo -n "QUEUE : "
find /var/spool/wf10/queue -maxdepth 1 -name '*.json' | wc -l

echo -n "DONE  : "
find /var/spool/wf10/done -maxdepth 1 -name '*.json' | wc -l

