#!/usr/bin/env bash
set -euo pipefail

BASE="https://lead-studio-9gnl.onrender.com"
LEAD_ID="5aa091c1-a023-4fa5-afc9-e15bf0d4af4c"
FID="4ac761e3-2f5f-423f-9ed6-351c6ed5d2da"
DURATION=154

ADMIN_EMAIL="admin@leadstudio.com"

echo "============================================================"
echo "EDITAR DURACION EXISTENTE COMO ADMIN"
echo "FOLLOWUP: $FID"
echo "DURACION: ${DURATION}s"
echo "============================================================"

read -s -p "Password admin LeadStudio: " ADMIN_PASS
echo

LOGIN=$(python3 - "$ADMIN_EMAIL" "$ADMIN_PASS" <<'PY'
import json,sys
print(json.dumps({
    "email":sys.argv[1],
    "password":sys.argv[2]
}))
PY
)

unset ADMIN_PASS

curl -sS \
  -o /tmp/ls-admin-login.json \
  -X POST \
  "$BASE/api/auth/login" \
  -H 'Content-Type: application/json' \
  --data-binary "$LOGIN"

TOKEN=$(python3 - <<'PY'
import json
try:
    d=json.load(open("/tmp/ls-admin-login.json"))
    print(d.get("accessToken",""))
except:
    print("")
PY
)

if [ -z "$TOKEN" ]; then
    echo "ERROR: login admin rechazado."
    cat /tmp/ls-admin-login.json
    exit 1
fi

echo "ADMIN LOGIN: OK"

BODY='{
  "durationSeconds": 154,
  "outcome": "CONNECTED",
  "callStatus": "ANSWERED"
}'

SUCCESS=0
SUCCESS_URL=""

# No hay POST aquí.
# Solo intentamos editar ESTE follow-up ya existente.
URLS=(
  "$BASE/api/leads/$LEAD_ID/followups/$FID"
  "$BASE/api/followups/$FID"
  "$BASE/api/calls/$FID"
  "$BASE/api/admin/followups/$FID"
)

echo
echo "Buscando endpoint de edición..."

for URL in "${URLS[@]}"; do

    CODE=$(curl -sS \
      -o /tmp/ls-edit-response.json \
      -w '%{http_code}' \
      -X PATCH \
      "$URL" \
      -H "Authorization: Bearer $TOKEN" \
      -H 'Content-Type: application/json' \
      --data-binary "$BODY" || true)

    echo "$CODE  $URL"

    case "$CODE" in
      200|201|204)
        SUCCESS=1
        SUCCESS_URL="$URL"
        echo
        echo "EDICION ACEPTADA:"
        cat /tmp/ls-edit-response.json || true
        echo
        break
        ;;
    esac

done

if [ "$SUCCESS" != "1" ]; then
    echo
    echo "NINGUNA RUTA ADMIN ACEPTO PATCH."
    echo "No se creo ningun follow-up y no se modifico otra llamada."
    exit 2
fi

echo
echo "============================================================"
echo "VERIFICAR FOLLOWUP DESPUES DEL CAMBIO"
echo "============================================================"

curl -sS \
  "$BASE/api/leads/$LEAD_ID/followups" \
  -H "Authorization: Bearer $TOKEN" \
  > /tmp/mahesh-after-admin-edit.json

python3 - "$FID" <<'PY'
import json,sys

fid=sys.argv[1]
data=json.load(open("/tmp/mahesh-after-admin-edit.json"))

found=[]

def walk(x):
    if isinstance(x,dict):
        if (
            x.get("id")==fid
            or x.get("followUpId")==fid
            or x.get("followupId")==fid
        ):
            found.append(x)

        for v in x.values():
            walk(v)

    elif isinstance(x,list):
        for v in x:
            walk(v)

walk(data)

if not found:
    print("FOLLOWUP NO ENCONTRADO EN RESPUESTA GET")
    raise SystemExit(1)

x=found[0]

print("FOLLOWUP :",fid)
print("durationSeconds :",x.get("durationSeconds"))
print("outcome         :",x.get("outcome"))
print("callStatus      :",x.get("callStatus"))
print("hasRecording    :",x.get("hasRecording"))

if x.get("durationSeconds")==154:
    print()
    print("OK: TALK TIME CORREGIDO A 154 SEGUNDOS.")
else:
    print()
    print("La API acepto PATCH pero durationSeconds no refleja 154.")
PY

echo
echo "Ruta que acepto la edicion:"
echo "$SUCCESS_URL"

