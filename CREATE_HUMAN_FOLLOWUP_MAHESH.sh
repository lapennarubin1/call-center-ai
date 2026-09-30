#!/usr/bin/env bash
set -euo pipefail

BASE="https://lead-studio-9gnl.onrender.com"

LEAD_ID="5aa091c1-a023-4fa5-afc9-e15bf0d4af4c"
PHONE="+919880456637"
NAME="Y MAHESH KUMAR"

MARKER="HUMAN_CALLBACK_MAHESH_20260925"

# Lo ponemos venciendo prácticamente ahora para que aparezca
# en la cola humana de Follow-ups.
NEXT_ACTION=$(date -u -d '+1 minute' '+%Y-%m-%dT%H:%M:%SZ')

ADMIN_EMAIL="admin@leadstudio.com"

echo "============================================================"
echo "CREAR FOLLOW-UP PARA AGENTE HUMANO"
echo "============================================================"
echo "Cliente : $NAME"
echo "Telefono: $PHONE"
echo "Lead ID : $LEAD_ID"
echo "Fecha   : $NEXT_ACTION"

echo
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
  -o /tmp/mahesh-admin-login.json \
  -X POST \
  "$BASE/api/auth/login" \
  -H 'Content-Type: application/json' \
  --data-binary "$LOGIN"

TOKEN=$(python3 <<'PY'
import json

try:
    d=json.load(open("/tmp/mahesh-admin-login.json"))
    print(d.get("accessToken",""))
except:
    print("")
PY
)

if [ -z "$TOKEN" ]; then
    echo "ERROR: login admin rechazado."
    cat /tmp/mahesh-admin-login.json
    exit 1
fi

echo "ADMIN LOGIN: OK"

echo
echo "============================================================"
echo "1. EVITAR FOLLOW-UP DUPLICADO"
echo "============================================================"

curl -sS \
  "$BASE/api/leads/$LEAD_ID/followups" \
  -H "Authorization: Bearer $TOKEN" \
  > /tmp/mahesh-existing-followups.json

EXISTING=$(python3 - "$MARKER" <<'PY'
import json,sys

marker=sys.argv[1]

try:
    data=json.load(
        open("/tmp/mahesh-existing-followups.json")
    )
except:
    print("")
    raise SystemExit

found=[]

def walk(x):
    if isinstance(x,dict):

        notes=str(x.get("notes") or "")

        if (
            marker in notes
            and not x.get("completedAt")
        ):
            fid=(
                x.get("id")
                or x.get("followUpId")
                or ""
            )

            if fid:
                found.append(fid)

        for v in x.values():
            walk(v)

    elif isinstance(x,list):
        for v in x:
            walk(v)

walk(data)

print(found[0] if found else "")
PY
)

if [ -n "$EXISTING" ]; then
    echo "YA EXISTE FOLLOW-UP HUMANO:"
    echo "$EXISTING"
    echo
    echo "No creo otro duplicado."
    exit 0
fi

echo "No existe duplicado."

echo
echo "============================================================"
echo "2. CREAR FOLLOW-UP HUMANO"
echo "============================================================"

BODY=$(python3 - "$NEXT_ACTION" "$MARKER" <<'PY'
import json,sys

next_action=sys.argv[1]
marker=sys.argv[2]

print(json.dumps({
    "type":"CALL",
    "nextActionAt":next_action,
    "notes":(
        marker +
        " | HUMAN CALLBACK REQUIRED. "
        "Previous AI call was CONNECTED/ANSWERED. "
        "Customer: Y MAHESH KUMAR, +919880456637. "
        "Previous connected call talk time was approximately 154 seconds. "
        "The call recording is already available in the CRM. "
        "Please review the recording and contact the customer manually."
    )
}))
PY
)

HTTP=$(curl -sS \
  -o /tmp/mahesh-human-followup.json \
  -w '%{http_code}' \
  -X POST \
  "$BASE/api/leads/$LEAD_ID/followups" \
  -H "Authorization: Bearer $TOKEN" \
  -H 'Content-Type: application/json' \
  --data-binary "$BODY")

echo "HTTP=$HTTP"

cat /tmp/mahesh-human-followup.json
echo

case "$HTTP" in
    200|201)
        ;;
    *)
        echo "ERROR: LeadStudio rechazo el follow-up."
        exit 1
        ;;
esac

echo
echo "============================================================"
echo "3. FOLLOW-UP CREADO"
echo "============================================================"

python3 <<'PY'
import json

d=json.load(
    open("/tmp/mahesh-human-followup.json")
)

f=d.get("followUp",d)

print(
    "ID            :",
    f.get("id")
)

print(
    "TYPE          :",
    f.get("type")
)

print(
    "NEXT ACTION   :",
    f.get("nextActionAt")
)

print(
    "COMPLETED     :",
    f.get("completedAt")
)

PY

echo
echo "============================================================"
echo "4. CONFIRMAR EN COLA FOLLOW-UPS"
echo "============================================================"

curl -sS \
  "$BASE/api/followups/due-today" \
  -H "Authorization: Bearer $TOKEN" \
  > /tmp/mahesh-due-today.json

python3 - "$LEAD_ID" "$MARKER" <<'PY'
import json,sys

lead_id=sys.argv[1]
marker=sys.argv[2]

try:
    d=json.load(
        open("/tmp/mahesh-due-today.json")
    )
except:
    print(
        "No pude interpretar la cola, "
        "pero el follow-up ya fue creado."
    )
    raise SystemExit

matches=[]

def walk(x):

    if isinstance(x,dict):

        txt=json.dumps(
            x,
            ensure_ascii=False
        )

        if (
            lead_id in txt
            or marker in txt
        ):
            matches.append(x)

        for v in x.values():
            walk(v)

    elif isinstance(x,list):
        for v in x:
            walk(v)

walk(d)

if matches:
    print("FOLLOW-UP VISIBLE EN COLA: SI")
else:
    print(
        "FOLLOW-UP CREADO; "
        "puede aparecer al llegar nextActionAt."
    )

PY

echo
echo "============================================================"
echo "LISTO"
echo "============================================================"
echo
echo "El cliente NO fue llamado."
echo "No se modifico la grabacion."
echo "No se creo otra llamada completada."
echo "Se creo solamente una tarea CALL para seguimiento humano."

