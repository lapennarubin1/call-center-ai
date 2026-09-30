#!/usr/bin/env bash
set -euo pipefail

QUEUE="/var/spool/wf10/queue"

echo
echo "============================================================"
echo "1. VERIFICAR TOKEN LEADSTUDIO"
echo "============================================================"

TOKEN_STATUS=$(curl -s -X POST http://172.18.0.1:8092/token | python3 -c '
import sys,json
try:
    d=json.load(sys.stdin)
    if d.get("accessToken"):
        print("OK")
    else:
        print("ERROR|" + str(d.get("message","sin token")))
except Exception as e:
    print("ERROR|" + str(e))
')

echo "$TOKEN_STATUS"

if [ "$TOKEN_STATUS" != "OK" ]; then
    echo
    echo "TOKEN TODAVIA NO ESTA DISPONIBLE."
    echo "NO se tocaran los 48 jobs."
    exit 1
fi

echo
echo "TOKEN OK - procedemos."

echo
echo "============================================================"
echo "2. PARAR WORKER 5 SEGUNDOS"
echo "============================================================"

systemctl stop wf10-recording-worker

echo "Worker detenido."

echo
echo "============================================================"
echo "3. DESPERTAR TODOS LOS JOBS"
echo "============================================================"

python3 - <<'PY'
import glob,json,os,time

files=glob.glob("/var/spool/wf10/queue/*.json")

changed=0

for path in files:
    try:
        with open(path) as f:
            j=json.load(f)

        # Reintento inmediato.
        j["next_attempt"]=0

        # Reiniciamos contador ahora que el problema del token está corregido.
        j["attempts"]=0

        j.pop("last_error",None)

        tmp=path+".tmp"

        with open(tmp,"w") as f:
            json.dump(j,f)

        os.replace(tmp,path)

        changed+=1

    except Exception as e:
        print("ERROR",path,e)

print("JOBS REACTIVADOS:",changed)
PY

echo
echo "============================================================"
echo "4. ARRANCAR WORKER"
echo "============================================================"

systemctl start wf10-recording-worker

sleep 3

systemctl status wf10-recording-worker --no-pager | head -8

echo
echo "============================================================"
echo "5. ESTADO INICIAL"
echo "============================================================"

echo -n "QUEUE: "
find /var/spool/wf10/queue -maxdepth 1 -name '*.json' | wc -l

echo -n "DONE : "
find /var/spool/wf10/done -maxdepth 1 -name '*.json' | wc -l

echo
echo "Esperando 90 segundos..."
sleep 90

echo
echo "============================================================"
echo "6. RESULTADO DESPUES DE 90 SEGUNDOS"
echo "============================================================"

echo -n "QUEUE: "
find /var/spool/wf10/queue -maxdepth 1 -name '*.json' | wc -l

echo -n "DONE : "
find /var/spool/wf10/done -maxdepth 1 -name '*.json' | wc -l

echo
echo "============================================================"
echo "7. PENDIENTES DB"
echo "============================================================"

mariadb asterisk -e "
SELECT
 COUNT(*) AS pendientes
FROM wf_call_followups
WHERE provider='asterisk'
  AND call_status='ANSWERED'
  AND COALESCE(recording_synced,0)=0
  AND created_at >= '2026-09-25 00:00:00'
  AND created_at <  '2026-09-26 00:00:00';
"

echo
echo "============================================================"
echo "8. ULTIMOS 80 LOGS"
echo "============================================================"

tail -80 /var/log/asterisk/wf10-worker.log

