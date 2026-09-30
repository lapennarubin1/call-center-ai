#!/usr/bin/env bash
set -euo pipefail

BASE="https://lead-studio-9gnl.onrender.com"
LEAD_ID="5aa091c1-a023-4fa5-afc9-e15bf0d4af4c"

# Follow-up en 5 minutos
NEXT_ACTION=$(date -u -d '+5 minutes' '+%Y-%m-%dT%H:%M:%SZ')

ADMIN_EMAIL="admin@leadstudio.com"

echo "============================================================"
echo "MAHESH -> SALES REP + FOLLOW-UP"
echo "ACTOR: LEADSTUDIO ADMIN"
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
echo "1. OBTENER REPRESENTANTES ASIGNABLES"
echo "============================================================"

HTTP=$(curl -sS \
  -o /tmp/mahesh-assignees-admin.json \
  -w '%{http_code}' \
  "$BASE/api/leads/assignees" \
  -H "Authorization: Bearer $TOKEN")

echo "HTTP=$HTTP"

if [ "$HTTP" != "200" ]; then
    cat /tmp/mahesh-assignees-admin.json
    exit 1
fi


echo
echo "Representantes disponibles:"

python3 <<'PY'
import json

d=json.load(open("/tmp/mahesh-assignees-admin.json"))

for u in d.get("users",[]):
    print(
        u.get("id"),
        "|",
        u.get("name"),
        "|",
        u.get("email")
    )
PY


echo
echo "============================================================"
echo "2. SELECCIONAR SALES REP ACTIVO"
echo "============================================================"

python3 <<'PY'
import json

d=json.load(open("/tmp/mahesh-assignees-admin.json"))
users=d.get("users",[])

# Sales Reps activos conocidos.
# Prioridad ELR One -> ELR Two -> abhishek
wanted=[
    "elr1@leadstudio.com",
    "elr2@leadstudio.com",
    "abhishek@leadstudio.com"
]

by_email={
    str(u.get("email","")).lower():u
    for u in users
}

chosen=None

for email in wanted:
    if email in by_email:
        chosen=by_email[email]
        break

if not chosen:
    raise SystemExit(
        "ERROR: no encontre ninguno de los Sales Rep activos."
    )

open("/tmp/mahesh-rep-id","w").write(str(chosen["id"]))
open("/tmp/mahesh-rep-name","w").write(str(chosen.get("name","")))
open("/tmp/mahesh-rep-email","w").write(str(chosen.get("email","")))

print("ID    :",chosen["id"])
print("NAME  :",chosen.get("name"))
print("EMAIL :",chosen.get("email"))
PY

REP_ID=$(cat /tmp/mahesh-rep-id)
REP_NAME=$(cat /tmp/mahesh-rep-name)
REP_EMAIL=$(cat /tmp/mahesh-rep-email)


echo
echo "============================================================"
echo "3. ASIGNAR MAHESH AL SALES REP"
echo "============================================================"

BODY=$(python3 - "$REP_ID" <<'PY'
import json,sys
print(json.dumps({
    "assignedToId":sys.argv[1]
}))
PY
)

HTTP=$(curl -sS \
  -o /tmp/mahesh-assign-admin.json \
  -w '%{http_code}' \
  -X POST \
  "$BASE/api/leads/$LEAD_ID/assign" \
  -H "Authorization: Bearer $TOKEN" \
  -H 'Content-Type: application/json' \
  --data-binary "$BODY")

echo "ASSIGN HTTP=$HTTP"
cat /tmp/mahesh-assign-admin.json
echo

case "$HTTP" in
  200|201) ;;
  *)
    echo "ERROR asignando lead."
    exit 1
    ;;
esac


echo
echo "============================================================"
echo "4. CREAR UN FOLLOW-UP LIMPIO"
echo "============================================================"

# Sin notas largas.
# Solo CALL + fecha.
FOLLOW_BODY=$(python3 - "$NEXT_ACTION" <<'PY'
import json,sys

print(json.dumps({
    "type":"CALL",
    "nextActionAt":sys.argv[1]
}))
PY
)

HTTP=$(curl -sS \
  -o /tmp/mahesh-final-followup.json \
  -w '%{http_code}' \
  -X POST \
  "$BASE/api/leads/$LEAD_ID/followups" \
  -H "Authorization: Bearer $TOKEN" \
  -H 'Content-Type: application/json' \
  --data-binary "$FOLLOW_BODY")

echo "FOLLOW-UP HTTP=$HTTP"
cat /tmp/mahesh-final-followup.json
echo

case "$HTTP" in
  200|201) ;;
  *)
    echo "ERROR creando follow-up."
    exit 1
    ;;
esac


echo
echo "============================================================"
echo "5. VERIFICACION FINAL"
echo "============================================================"

curl -sS \
  "$BASE/api/leads/$LEAD_ID" \
  -H "Authorization: Bearer $TOKEN" \
  > /tmp/mahesh-final-state.json

python3 - "$REP_ID" <<'PY'
import json,sys

expected=sys.argv[1]

d=json.load(open("/tmp/mahesh-final-state.json"))
lead=d.get("lead",d)

owner=lead.get("assignedTo") or {}

print("CLIENTE         :",lead.get("name"))
print("STAGE           :",lead.get("stage"))
print("STATUS          :",lead.get("status"))
print("OWNER ID        :",lead.get("assignedToId"))
print("OWNER           :",owner.get("name"))
print("OWNER EMAIL     :",owner.get("email"))
print("NEXT FOLLOW-UP  :",lead.get("nextFollowUpAt"))

if lead.get("assignedToId") != expected:
    raise SystemExit(
        "ERROR: el owner final no coincide con el Sales Rep."
    )

print()
print("OK: SALES REP ASIGNADO.")
PY


echo
echo "============================================================"
echo "6. FOLLOW-UP CREADO"
echo "============================================================"

python3 <<'PY'
import json

d=json.load(open("/tmp/mahesh-final-followup.json"))
x=d.get("followUp",d)

creator=x.get("createdBy") or {}

print("FOLLOWUP ID :",x.get("id"))
print("TYPE        :",x.get("type"))
print("NEXT ACTION :",x.get("nextActionAt"))
print("CREATED BY  :",creator.get("name"))
print("NOTES       :",x.get("notes"))
PY


echo
echo "============================================================"
echo "FINALIZADO"
echo "============================================================"
echo
echo "Sales Rep : $REP_NAME <$REP_EMAIL>"
echo "Follow-up : creado por Admin"
echo "Cliente   : no fue llamado"
echo "Recording : intacta"
