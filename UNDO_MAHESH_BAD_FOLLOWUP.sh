#!/usr/bin/env bash
set -euo pipefail

BASE="https://lead-studio-9gnl.onrender.com"
LEAD_ID="5aa091c1-a023-4fa5-afc9-e15bf0d4af4c"
MARKER="HUMAN_CALLBACK_MAHESH_20260925"

echo "============================================================"
echo "REVERTIR FOLLOW-UP INCORRECTO DE MAHESH"
echo "ACTOR: AI"
echo "============================================================"

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

echo
echo "1. LOCALIZAR SOLO EL FOLLOW-UP QUE CREAMOS"

curl -sS \
  "$BASE/api/leads/$LEAD_ID/followups" \
  -H "Authorization: Bearer $TOKEN" \
  > /tmp/mahesh-followups-before.json

FID=$(python3 - "$MARKER" <<'PY'
import json,sys

marker=sys.argv[1]
data=json.load(open("/tmp/mahesh-followups-before.json"))

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

walk(data)

print(found[0] if found else "")
PY
)

if [ -z "$FID" ]; then
    echo "No encontre el follow-up incorrecto."
else
    echo "FOLLOWUP INCORRECTO: $FID"

    echo
    echo "2. INTENTAR ELIMINARLO"

    HTTP=$(curl -sS \
      -o /tmp/mahesh-delete.json \
      -w '%{http_code}' \
      -X DELETE \
      "$BASE/api/followups/$FID" \
      -H "Authorization: Bearer $TOKEN" || true)

    echo "DELETE HTTP=$HTTP"

    if [[ "$HTTP" =~ ^20[0-9]$ ]]; then
        echo "FOLLOW-UP ELIMINADO."
    else
        echo "DELETE no disponible. Lo cierro para sacarlo de la cola."

        HTTP2=$(curl -sS \
          -o /tmp/mahesh-complete.json \
          -w '%{http_code}' \
          -X PATCH \
          "$BASE/api/followups/$FID" \
          -H "Authorization: Bearer $TOKEN" \
          -H 'Content-Type: application/json' \
          -d '{"completed":true}' || true)

        echo "COMPLETE HTTP=$HTTP2"
        cat /tmp/mahesh-complete.json 2>/dev/null || true
        echo
    fi
fi

echo
echo "3. RESTAURAR LEAD AL ESTADO ANTERIOR"

HTTP=$(curl -sS \
  -o /tmp/mahesh-restore.json \
  -w '%{http_code}' \
  -X PATCH \
  "$BASE/api/leads/$LEAD_ID" \
  -H "Authorization: Bearer $TOKEN" \
  -H 'Content-Type: application/json' \
  -d '{
        "stage":"CONTACTED",
        "status":"CONTACTED",
        "attempts":1,
        "nextFollowUpAt":null
      }')

echo "RESTORE HTTP=$HTTP"
cat /tmp/mahesh-restore.json
echo

echo
echo "4. VERIFICACION"

curl -sS \
  "$BASE/api/leads/$LEAD_ID" \
  -H "Authorization: Bearer $TOKEN" \
  > /tmp/mahesh-after-cleanup.json

python3 <<'PY'
import json

d=json.load(open("/tmp/mahesh-after-cleanup.json"))
lead=d.get("lead",d)

print("name            :",lead.get("name"))
print("stage           :",lead.get("stage"))
print("status          :",lead.get("status"))
print("attempts        :",lead.get("attempts"))
print("nextFollowUpAt  :",lead.get("nextFollowUpAt"))
print("assignedToId    :",lead.get("assignedToId"))

a=lead.get("assignedTo") or {}
print("assignedTo      :",a.get("name"),a.get("email"))
PY

echo
echo "============================================================"
echo "5. BUSCAR SALES REP USANDO LA CUENTA AI"
echo "============================================================"

FOUND=0

for ENDPOINT in \
  "/api/users" \
  "/api/admin/users"
do
    FILE="/tmp/users$(echo "$ENDPOINT" | tr '/' '_').json"

    CODE=$(curl -sS \
      -o "$FILE" \
      -w '%{http_code}' \
      "$BASE$ENDPOINT" \
      -H "Authorization: Bearer $TOKEN" || true)

    echo "$CODE  $ENDPOINT"

    if [ "$CODE" = "200" ]; then

        python3 - "$FILE" <<'PY'
import json,sys

fn=sys.argv[1]
data=json.load(open(fn))

users=[]

def walk(x):
    if isinstance(x,dict):

        email=x.get("email")
        name=x.get("name")

        role=x.get("role")

        if isinstance(role,dict):
            role_name=role.get("name") or role.get("label") or ""
        else:
            role_name=str(role or "")

        if email and name and "sales rep" in role_name.lower():

            inactive=(
                bool(x.get("deactivatedAt"))
                or x.get("active") is False
                or x.get("isActive") is False
                or x.get("disabled") is True
            )

            if not inactive:
                users.append({
                    "id":x.get("id"),
                    "name":name,
                    "email":email,
                    "role":role_name
                })

        for v in x.values():
            walk(v)

    elif isinstance(x,list):
        for v in x:
            walk(v)

walk(data)

seen=set()

print()
print("SALES REP ACTIVOS:")

for u in users:
    key=u["id"] or u["email"]

    if key in seen:
        continue

    seen.add(key)

    print(
        f'{u["id"]}\t{u["name"]}\t{u["email"]}'
    )

PY

        FOUND=1
        break
    fi
done

echo
echo "============================================================"
echo "6. EXTRAER DEL FRONTEND COMO HACE LA ASIGNACION"
echo "============================================================"

python3 <<'PY'
import glob,re,os

files=glob.glob("/tmp/leadstudio-frontend/*.js")

patterns=[
    "assignedToId",
    "/api/users",
    "/api/admin/users",
]

shown=0

for fn in files:

    try:
        s=open(fn,errors="ignore").read()
    except:
        continue

    for p in patterns:

        start=0

        while True:
            pos=s.find(p,start)

            if pos < 0:
                break

            a=max(0,pos-350)
            b=min(len(s),pos+650)

            chunk=re.sub(
                r"\s+",
                " ",
                s[a:b]
            )

            print()
            print("---",p,"---")
            print(chunk)

            shown+=1

            if shown>=8:
                raise SystemExit

            start=pos+len(p)

PY

echo
echo "============================================================"
echo "FIN"
echo "============================================================"
