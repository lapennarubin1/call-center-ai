#!/bin/bash
BASE="https://lead-studio-9gnl.onrender.com"
AYER=$(date -u -d '1 day ago' '+%Y-%m-%dT%H:%M:%S.000Z')
CONFIRM="${CONFIRM_REAL_ACCOUNT:-no}"

TOKEN=$(curl -s -m 30 -X POST "$BASE/api/auth/login" -H "Content-Type: application/json" \
  -d '{"email":"admin@leadstudio.com","password":"#9L33dS1udi0"}' \
  | python3 -c 'import json,sys; print(json.load(sys.stdin).get("accessToken",""))')
[ -z "$TOKEN" ] && { echo "LOGIN FALLÓ"; exit 1; }
echo "token OK (admin)"

echo "── Creando lead 'Dani Dani Dani' (country=IN) ──"
curl -s -m 30 -X POST "$BASE/api/leads" \
  -H "Authorization: Bearer $TOKEN" -H "Content-Type: application/json" \
  -d "{
    \"name\": \"Dani Dani Dani\",
    \"phone\": \"+911234567890\",
    \"country\": \"IN\",
    \"source\": \"test-manual\",
    \"createdAt\": \"$AYER\"
  }" | tee /root/lead_dani_response.json | python3 -m json.tool

LEAD_ID=$(python3 -c 'import json
try: print(json.load(open("/root/lead_dani_response.json")).get("id",""))
except Exception: print("")')

if [ -z "$LEAD_ID" ]; then
  echo "No se pudo extraer un id — revisá /root/lead_dani_response.json (la API devolvió otro formato/error)."
  exit 1
fi
echo "── Lead creado: $LEAD_ID ──"

echo "── Marcando doNotCall=true (para que WF2 nunca lo marque) ──"
curl -s -m 30 -X PATCH "$BASE/api/leads/$LEAD_ID" \
  -H "Authorization: Bearer $TOKEN" -H "Content-Type: application/json" \
  -d '{"doNotCall":true}' | python3 -m json.tool

if [ "$CONFIRM" != "SI" ]; then
  echo
  echo "PARADO ACÁ A PROPÓSITO — lead creado pero SIN cuenta real todavía."
  echo "Para crear la cuenta REAL en CashStudio (igual que WF3 en producción), correr:"
  echo "  CONFIRM_REAL_ACCOUNT=SI bash /root/test_lead_dani.sh"
  echo "(usará el mismo lead_id ya creado? NO — este script crea un lead nuevo cada vez que corre."
  echo " Si ya corriste esta parte, decime el lead_id: $LEAD_ID y seguimos desde ahí sin duplicar.)"
  exit 0
fi

echo "── CONFIRM_REAL_ACCOUNT=SI — creando cuenta REAL en CashStudio (market IND) ──"
ACC=$(curl -s -m 20 -X POST "$BASE/api/leads/$LEAD_ID/cashstudio-account" \
  -H "Authorization: Bearer $TOKEN" -H "Content-Type: application/json" \
  -d '{"market":"IND"}')
echo "$ACC" | tee /root/lead_dani_account.json | python3 -m json.tool

echo "── PATCH stage=INTERESTED (Account Opened) ──"
curl -s -m 30 -X PATCH "$BASE/api/leads/$LEAD_ID" \
  -H "Authorization: Bearer $TOKEN" -H "Content-Type: application/json" \
  -d '{"stage":"INTERESTED","lastContactedAt":"'"$AYER"'"}' | python3 -m json.tool

USERNAME=$(python3 -c 'import json;
try: print(json.load(open("/root/lead_dani_account.json")).get("account",{}).get("userName",""))
except Exception: print("")')
if [ -n "$USERNAME" ]; then
  echo "── POST followup (nota de cuenta creada) ──"
  curl -s -m 30 -X POST "$BASE/api/leads/$LEAD_ID/followups" \
    -H "Authorization: Bearer $TOKEN" -H "Content-Type: application/json" \
    -d "{\"type\":\"NOTE\",\"notes\":\"Cuenta CashStudio creada (prueba) | user: $USERNAME\"}" \
    | python3 -m json.tool
fi

echo
echo "LISTO — abrí en LeadStudio: $BASE/leads/$LEAD_ID (o buscá 'Dani Dani Dani' en el CRM)"
