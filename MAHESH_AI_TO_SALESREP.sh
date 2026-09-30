#!/usr/bin/env bash
set -euo pipefail

BASE="https://lead-studio-9gnl.onrender.com"
LEAD_ID="5aa091c1-a023-4fa5-afc9-e15bf0d4af4c"

MARKER="AI_SALES_HANDOFF_MAHESH_20260925"
NEXT_ACTION=$(date -u -d '+5 minutes' '+%Y-%m-%dT%H:%M:%SZ')

echo "============================================================"
echo "MAHESH -> FOLLOW-UP CREADO POR AI -> SALES REP"
echo "============================================================"

# ============================================================
# 1. TOKEN DE AI
# ============================================================

TOKEN=$(curl -sS -X POST \
  http://172.18.0.1:8092/token \
  | python3 -c '
import sys,json
d=json.load(sys.stdin)
print(d.get("accessToken",""))
')

if [ -z "$TOKEN" ]; then
    echo "ERROR: no pude obtener token AI."
    exit 1
fi

echo "TOKEN AI: OK"

# ============================================================
# 2. OBTENER AGENTES ASIGNABLES
# ============================================================

echo
echo "============================================================"
echo "AGENTES DISPONIBLES PARA AI"
echo "============================================================"

HTTP=$(curl -sS \
  -o /tmp/mahesh-assignees.json \
  -w '%{http_code}' \
  "$BASE/api/leads/assignees" \
  -H "Authorization: Bearer $TOKEN")

echo "HTTP=$HTTP"

if [ "$HTTP" != "200" ]; then
    cat /tmp/mahesh-assignees.json
    exit 1
fi

python3 <<'PY'
import json

d=json.load(open("/tmp/mahesh-assignees.json"))
users=d.get("users",[])

for u in users:
    print(
        u.get("id"),
        "|",
        u.get("name"),
        "|",
        u.get("email")
    )
PY

# ============================================================
# 3. ELEGIR SALES REP HUMANO
#    prioridad: ELR One -> ELR Two -> abhishek
# ============================================================

python3 <<'PY'
import json

d=json.load(open("/tmp/mahesh-assignees.json"))
users=d.get("users",[])

preferences=[
    "elr1@leadstudio.com",
    "elr2@leadstudio.com",
    "abhishek@leadstudio.com",
]

by_email={
    str(u.get("email","")).lower():u
    for u in users
}

chosen=None

for email in preferences:
    if email in by_email:
        chosen=by_email[email]
        break

if not chosen:
    raise SystemExit(
        "ERROR: ninguno de los Sales Rep activos esperados "
        "aparece como asignable para AI."
    )

open("/tmp/mahesh-salesrep-id","w").write(
    str(chosen["id"])
)

open("/tmp/mahesh-salesrep-name","w").write(
    str(chosen.get("name",""))
)

open("/tmp/mahesh-salesrep-email","w").write(
    str(chosen.get("email",""))
)

print()
print("SALES REP SELECCIONADO")
print("ID    :",chosen["id"])
print("NAME  :",chosen.get("name"))
print("EMAIL :",chosen.get("email"))
PY

REP_ID=$(cat /tmp/mahesh-salesrep-id)
REP_NAME=$(cat /tmp/mahesh-salesrep-name)
REP_EMAIL=$(cat /tmp/mahesh-salesrep-email)

# ============================================================
# 4. COMPROBAR QUE NO HAYAMOS CREADO ESTE HANDOFF ANTES
# ============================================================

curl -sS \
  "$BASE/api/leads/$LEAD_ID/followups" \
  -H "Authorization: Bearer $TOKEN" \
  > /tmp/mahesh-followups-ai.json

EXISTING=$(python3 - "$MARKER" <<'PY'
import json,sys

marker=sys.argv[1]
d=json.load(open("/tmp/mahesh-followups-ai.json"))

found=[]

def walk(x):
    if isinstance(x,dict):
        if marker in str(x.get("notes") or ""):
            fid=x.get("id") or x.get("followUpId")
            if fid:
                found.append(fid)

        for v in x.values():
            walk(v)

    elif isinstance(x,list):
        for v in x:
            walk(v)

walk(d)

print(found[0] if found else "")
PY
)

# ============================================================
# 5. CREAR FOLLOW-UP COMO AI
# ============================================================

if [ -z "$EXISTING" ]; then

    echo
    echo "============================================================"
    echo "CREANDO FOLLOW-UP COMO AI"
    echo "============================================================"

    BODY=$(python3 \
      - "$NEXT_ACTION" "$MARKER" "$REP_NAME" <<'PY'
import json,sys

dt=sys.argv[1]
marker=sys.argv[2]
rep=sys.argv[3]

print(json.dumps({
    "type":"CALL",
    "nextActionAt":dt,
    "notes":(
        marker +
        " | Sales follow-up. "
        "Review previous connected AI call recording before callback."
    )
}))
PY
)

    HTTP=$(curl -sS \
      -o /tmp/mahesh-ai-followup-response.json \
      -w '%{http_code}' \
      -X POST \
      "$BASE/api/leads/$LEAD_ID/followups" \
      -H "Authorization: Bearer $TOKEN" \
      -H 'Content-Type: application/json' \
      --data-binary "$BODY")

    echo "FOLLOWUP HTTP=$HTTP"
    cat /tmp/mahesh-ai-followup-response.json
    echo

    case "$HTTP" in
        200|201) ;;
        *)
            echo "ERROR creando follow-up AI."
            exit 1
            ;;
    esac

else
    echo
    echo "FOLLOW-UP AI YA EXISTE:"
    echo "$EXISTING"
    echo "No creo otro."
fi

# ============================================================
# 6. ASIGNAR EL LEAD AL SALES REP
# ============================================================

echo
echo "============================================================"
echo "ASIGNANDO A $REP_NAME"
echo "============================================================"

ASSIGN_BODY=$(python3 - "$REP_ID" <<'PY'
import json,sys

print(json.dumps({
    "assignedToId":sys.argv[1]
}))
PY
)

HTTP=$(curl -sS \
  -o /tmp/mahesh-assign-response.json \
  -w '%{http_code}' \
  -X POST \
  "$BASE/api/leads/$LEAD_ID/assign" \
  -H "Authorization: Bearer $TOKEN" \
  -H 'Content-Type: application/json' \
  --data-binary "$ASSIGN_BODY")

echo "ASSIGN HTTP=$HTTP"
cat /tmp/mahesh-assign-response.json
echo

case "$HTTP" in
    200|201) ;;
    *)
        echo "ERROR asignando Sales Rep."
        exit 1
        ;;
esac

# ============================================================
# 7. VERIFICAR PROPIETARIO REAL
# ============================================================

echo
echo "============================================================"
echo "VERIFICACION FINAL"
echo "============================================================"

curl -sS \
  "$BASE/api/leads/$LEAD_ID" \
  -H "Authorization: Bearer $TOKEN" \
  > /tmp/mahesh-final.json

python3 - "$REP_ID" <<'PY'
import json,sys

expected=sys.argv[1]

d=json.load(open("/tmp/mahesh-final.json"))
lead=d.get("lead",d)

a=lead.get("assignedTo") or {}

print("NAME            :",lead.get("name"))
print("STAGE           :",lead.get("stage"))
print("STATUS          :",lead.get("status"))
print("ASSIGNED ID     :",lead.get("assignedToId"))
print("ASSIGNED NAME   :",a.get("name"))
print("ASSIGNED EMAIL  :",a.get("email"))
print("NEXT FOLLOW-UP  :",lead.get("nextFollowUpAt"))

if lead.get("assignedToId") != expected:
    raise SystemExit(
        "ERROR: LeadStudio no dejo al Sales Rep como owner."
    )

print()
print("OK: EL LEAD YA PERTENECE AL SALES REP.")
PY

# ============================================================
# 8. CONFIRMAR QUE EL NUEVO FOLLOW-UP LO CREO AI
# ============================================================

curl -sS \
  "$BASE/api/leads/$LEAD_ID/followups" \
  -H "Authorization: Bearer $TOKEN" \
  > /tmp/mahesh-final-followups.json

python3 - "$MARKER" <<'PY'
import json,sys

marker=sys.argv[1]
d=json.load(open("/tmp/mahesh-final-followups.json"))

found=[]

def walk(x):
    if isinstance(x,dict):

        if marker in str(x.get("notes") or ""):
            found.append(x)

        for v in x.values():
            walk(v)

    elif isinstance(x,list):
        for v in x:
            walk(v)

walk(d)

if not found:
    raise SystemExit(
        "ERROR: no encuentro el follow-up AI."
    )

x=found[-1]
creator=x.get("createdBy") or {}

print()
print("FOLLOW-UP NUEVO")
print("ID         :",x.get("id"))
print("TYPE       :",x.get("type"))
print("NEXT       :",x.get("nextActionAt"))
print("CREATED BY :",creator.get("name"),creator.get("email"))
print("NOTES      :",x.get("notes"))

PY

echo
echo "============================================================"
echo "LISTO"
echo "============================================================"
echo "Owner nuevo : $REP_NAME <$REP_EMAIL>"
echo "Follow-up   : creado con TOKEN AI"
echo "Cliente     : NO fue llamado"
echo "Grabacion   : NO fue modificada"
