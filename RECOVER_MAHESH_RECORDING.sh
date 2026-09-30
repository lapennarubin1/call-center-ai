#!/usr/bin/env bash
set -euo pipefail

BASE="https://lead-studio-9gnl.onrender.com"

LEAD_ID="5aa091c1-a023-4fa5-afc9-e15bf0d4af4c"
PHONE="+919880456637"
UNIQUEID="1790332448.33497"
WAV="/tmp/test-${UNIQUEID}.wav"

echo "============================================================"
echo "RECUPERAR GRABACION Y MAHESH KUMAR"
echo "SIN WF9 / SIN WF10 / SIN TELEGRAM"
echo "============================================================"

# Evitar cualquier procesamiento paralelo del mismo WAV.
systemctl stop wf10-recording-worker 2>/dev/null || true

echo
echo "1. WAV"

if [ ! -f "$WAV" ]; then
    echo "ERROR: no existe $WAV"
    exit 1
fi

SIZE=$(stat -c%s "$WAV")

echo "WAV : $WAV"
echo "SIZE: $SIZE bytes"

if [ "$SIZE" -le 44 ]; then
    echo "ERROR: WAV vacio."
    exit 1
fi


echo
echo "2. TOKEN LEADSTUDIO"

TOKEN=$(curl -sS -X POST \
  http://172.18.0.1:8092/token \
  | python3 -c '
import sys,json
d=json.load(sys.stdin)
print(d.get("accessToken",""))
')

if [ -z "$TOKEN" ]; then
    echo "ERROR: no pude obtener token."
    exit 1
fi

echo "TOKEN: OK"


echo
echo "3. BUSCAR EL FOLLOWUP YA EXISTENTE"

rm -f /tmp/mahesh-lead.json
rm -f /tmp/mahesh-followups.json

HTTP1=$(curl -sS \
  -o /tmp/mahesh-lead.json \
  -w '%{http_code}' \
  -H "Authorization: Bearer $TOKEN" \
  "$BASE/api/leads/$LEAD_ID" || true)

HTTP2=$(curl -sS \
  -o /tmp/mahesh-followups.json \
  -w '%{http_code}' \
  -H "Authorization: Bearer $TOKEN" \
  "$BASE/api/leads/$LEAD_ID/followups" || true)

echo "GET lead      : HTTP $HTTP1"
echo "GET followups : HTTP $HTTP2"


echo
echo "4. IDENTIFICAR LA LLAMADA CONNECTED DE 154s"

python3 <<'PY'
import json
import os
import re
from datetime import datetime, timezone

LEAD_ID="5aa091c1-a023-4fa5-afc9-e15bf0d4af4c"

files=[
    "/tmp/mahesh-lead.json",
    "/tmp/mahesh-followups.json"
]

objects=[]

def walk(x,path="root"):
    if isinstance(x,dict):
        objects.append((path,x))
        for k,v in x.items():
            walk(v,path+"."+str(k))

    elif isinstance(x,list):
        for i,v in enumerate(x):
            walk(v,f"{path}[{i}]")

for fn in files:
    if not os.path.isfile(fn):
        continue

    try:
        with open(fn) as f:
            data=json.load(f)
        walk(data,fn)
    except Exception:
        pass


uuid_re=re.compile(
    r"^[0-9a-fA-F]{8}-"
    r"[0-9a-fA-F]{4}-"
    r"[0-9a-fA-F]{4}-"
    r"[0-9a-fA-F]{4}-"
    r"[0-9a-fA-F]{12}$"
)

target=datetime(
    2026,9,25,
    10,34,8,
    tzinfo=timezone.utc
)

candidates=[]

for path,d in objects:

    fid=(
        d.get("id")
        or d.get("followUpId")
        or d.get("followupId")
    )

    if not isinstance(fid,str):
        continue

    if not uuid_re.match(fid):
        continue

    if fid==LEAD_ID:
        continue

    typ=str(
        d.get("type","")
    ).upper()

    outcome=str(
        d.get("outcome","")
    ).upper()

    status=str(
        d.get("callStatus")
        or d.get("call_status")
        or d.get("status")
        or ""
    ).upper()

    duration=(
        d.get("durationSeconds")
        or d.get("duration_secs")
        or d.get("duration")
    )

    score=0

    if typ=="CALL":
        score+=20

    if outcome=="CONNECTED":
        score+=30

    if status=="ANSWERED":
        score+=30

    try:
        dur=float(duration)

        # La llamada tuvo 154s de conversación.
        if 120 <= dur <= 190:
            score+=20

        if abs(dur-154)<=10:
            score+=20

    except Exception:
        dur=None

    # Buscar timestamp cercano si el objeto lo tiene.
    for k in (
        "createdAt",
        "created_at",
        "timestamp",
        "date",
        "created"
    ):

        raw=d.get(k)

        if not raw:
            continue

        try:
            s=str(raw).replace(
                "Z","+00:00"
            )

            dt=datetime.fromisoformat(s)

            if dt.tzinfo is None:
                dt=dt.replace(
                    tzinfo=timezone.utc
                )

            delta=abs(
                (
                    dt.astimezone(timezone.utc)
                    -target
                ).total_seconds()
            )

            if delta <= 900:
                score+=40

            elif delta <= 3600:
                score+=10

        except Exception:
            pass

    if (
        typ=="CALL"
        or outcome=="CONNECTED"
        or status=="ANSWERED"
    ):
        candidates.append(
            (
                score,
                fid,
                typ,
                outcome,
                status,
                dur,
                path,
                d
            )
        )


candidates.sort(
    key=lambda x:x[0],
    reverse=True
)

print("Candidatos encontrados:")

for c in candidates[:10]:
    print(
        c[0],
        c[1],
        "type="+str(c[2]),
        "outcome="+str(c[3]),
        "status="+str(c[4]),
        "duration="+str(c[5])
    )

if not candidates:
    raise SystemExit(
        "ERROR: LeadStudio no devolvio ningun followup CALL."
    )

best=candidates[0]

# Exigir evidencia fuerte de que es la llamada correcta.
if best[0] < 50:
    raise SystemExit(
        "ERROR: no puedo identificar con seguridad "
        "el followup correcto."
    )

# Si hay empate real con otro ID, no arriesgar una grabacion equivocada.
if (
    len(candidates)>1
    and candidates[1][0]==best[0]
    and candidates[1][1]!=best[1]
):
    raise SystemExit(
        "ERROR: hay dos followups igualmente probables. "
        "No subo el audio para evitar asociarlo mal."
    )

fid=best[1]

with open(
    "/tmp/mahesh-followup-id",
    "w"
) as f:
    f.write(fid)

print()
print("FOLLOWUP SELECCIONADO:",fid)

PY


FID=$(cat /tmp/mahesh-followup-id)

echo
echo "============================================================"
echo "5. SUBIR WAV DIRECTAMENTE A LEADSTUDIO"
echo "============================================================"

echo "FOLLOWUP: $FID"

HTTP=$(curl -sS \
  -o /tmp/mahesh-upload-response.json \
  -w '%{http_code}' \
  -X PUT \
  "$BASE/api/calls/recording/$FID" \
  -H "Authorization: Bearer $TOKEN" \
  -F "file=@${WAV};type=audio/wav")

echo "HTTP=$HTTP"

cat /tmp/mahesh-upload-response.json
echo

case "$HTTP" in
    200|201|204)
        ;;
    *)
        echo "ERROR: LeadStudio rechazo el upload."
        exit 1
        ;;
esac


echo
echo "6. CONFIRMAR hasRecording"

python3 <<'PY'
import json

fn="/tmp/mahesh-upload-response.json"

try:
    d=json.load(open(fn))
except Exception:
    raise SystemExit(
        "ERROR: respuesta LeadStudio no es JSON."
    )

if d.get("hasRecording") is not True:
    raise SystemExit(
        "ERROR: LeadStudio no confirmo hasRecording=true."
    )

print("LEADSTUDIO: hasRecording=true")
print(
    "followUpId:",
    d.get("followUpId")
    or d.get("followupId")
)
PY


echo
echo "============================================================"
echo "7. REPARAR MAPEO LOCAL"
echo "============================================================"

FID_ESC=$(printf "%s" "$FID" | sed "s/'/''/g")

mariadb asterisk -e "
INSERT INTO wf_call_followups
(
    phone,
    lead_id,
    followup_id,
    provider,
    outcome,
    call_status,
    recording_synced
)
SELECT
    '$PHONE',
    '$LEAD_ID',
    '$FID_ESC',
    'asterisk',
    'CONNECTED',
    'ANSWERED',
    1
WHERE NOT EXISTS
(
    SELECT 1
    FROM wf_call_followups
    WHERE followup_id='$FID_ESC'
);

UPDATE wf_call_followups
SET
    recording_synced=1,
    outcome='CONNECTED',
    call_status='ANSWERED',
    provider='asterisk'
WHERE followup_id='$FID_ESC';
"


echo
echo "8. SACAR ESTE UNIQUEID DE QUEUE SI EXISTE"

python3 <<'PY'
import glob
import json
import os
import shutil

UID="1790332448.33497"

Q="/var/spool/wf10/queue"
D="/var/spool/wf10/done"

os.makedirs(D,exist_ok=True)

moved=0

for path in glob.glob(Q+"/*.json"):

    try:
        j=json.load(open(path))
    except Exception:
        continue

    if str(
        j.get("uniqueid","")
    ) != UID:
        continue

    j["recovered_directly"]=True
    j["crm_done"]=True
    j["suppress_telegram"]=True

    tmp=path+".tmp"

    with open(tmp,"w") as f:
        json.dump(j,f)

    os.replace(tmp,path)

    dest=os.path.join(
        D,
        os.path.basename(path)
    )

    if os.path.exists(dest):
        os.remove(dest)

    shutil.move(path,dest)

    moved+=1

print("JOBS MOVIDOS A DONE:",moved)
PY


echo
echo "============================================================"
echo "9. RESULTADO FINAL"
echo "============================================================"

mariadb asterisk -e "
SELECT
    phone,
    lead_id,
    followup_id,
    provider,
    outcome,
    call_status,
    recording_synced
FROM wf_call_followups
WHERE followup_id='$FID_ESC';
"

echo
echo "GRABACION CARGADA DIRECTAMENTE AL CRM."
echo "NO SE LLAMO AL CLIENTE."
echo "NO SE ENVIO TELEGRAM."
echo "NO SE CREO OTRO FOLLOWUP."
