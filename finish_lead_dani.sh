#!/bin/bash
BASE="https://lead-studio-9gnl.onrender.com"
LEAD_ID="d36258f3-91d5-4cc3-844d-7653972a6efd"
AYER=$(date -u -d '1 day ago' '+%Y-%m-%dT%H:%M:%S.000Z')

TOKEN=$(curl -s -m 30 -X POST "$BASE/api/auth/login" -H "Content-Type: application/json" \
  -d '{"email":"admin@leadstudio.com","password":"#9L33dS1udi0"}' \
  | python3 -c 'import json,sys; print(json.load(sys.stdin).get("accessToken",""))')
[ -z "$TOKEN" ] && { echo "LOGIN FALLÓ"; exit 1; }
echo "token OK (admin) | lead: $LEAD_ID"

echo "── PATCH doNotCall=true (para que WF2 nunca lo marque) ──"
curl -s -m 30 -X PATCH "$BASE/api/leads/$LEAD_ID" \
  -H "Authorization: Bearer $TOKEN" -H "Content-Type: application/json" \
  -d '{"doNotCall":true}' | python3 -m json.tool

echo
echo "── Creando cuenta REAL en CashStudio (market IND) — usa cupo real ──"
curl -s -m 20 -X POST "$BASE/api/leads/$LEAD_ID/cashstudio-account" \
  -H "Authorization: Bearer $TOKEN" -H "Content-Type: application/json" \
  -d '{"market":"IND"}' | tee /root/lead_dani_account.json | python3 -m json.tool

echo
echo "── PATCH stage=INTERESTED + lastContactedAt=ayer (esto es lo que el panel lee como fecha de apertura) ──"
curl -s -m 30 -X PATCH "$BASE/api/leads/$LEAD_ID" \
  -H "Authorization: Bearer $TOKEN" -H "Content-Type: application/json" \
  -d "{\"stage\":\"INTERESTED\",\"lastContactedAt\":\"$AYER\"}" | python3 -m json.tool

USERNAME=$(python3 -c 'import json
try: print(json.load(open("/root/lead_dani_account.json")).get("account",{}).get("userName",""))
except Exception: print("")')
if [ -n "$USERNAME" ]; then
  echo
  echo "── POST followup (nota de cuenta creada) ──"
  curl -s -m 30 -X POST "$BASE/api/leads/$LEAD_ID/followups" \
    -H "Authorization: Bearer $TOKEN" -H "Content-Type: application/json" \
    -d "{\"type\":\"NOTE\",\"notes\":\"Cuenta CashStudio creada (prueba) | user: $USERNAME\"}" \
    | python3 -m json.tool
fi

echo
echo "LISTO — abrí: $BASE/leads/$LEAD_ID (o buscá 'Dani Dani Dani' en LeadStudio)"
