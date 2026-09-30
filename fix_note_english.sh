#!/bin/bash
BASE="https://lead-studio-9gnl.onrender.com"
LEAD_ID="d36258f3-91d5-4cc3-844d-7653972a6efd"
BAD_FOLLOWUP="cebb4c9f-8619-4bca-9ec1-76b00fbbc527"

TOKEN=$(curl -s -m 30 -X POST "$BASE/api/auth/login" -H "Content-Type: application/json" \
  -d '{"email":"admin@leadstudio.com","password":"#9L33dS1udi0"}' \
  | python3 -c 'import json,sys; print(json.load(sys.stdin).get("accessToken",""))')
[ -z "$TOKEN" ] && { echo "LOGIN FALLÓ"; exit 1; }

echo "── Intento: DELETE del followup en español ──"
curl -s -o /dev/null -w "HTTP %{http_code}\n" -X DELETE "$BASE/api/leads/$LEAD_ID/followups/$BAD_FOLLOWUP" \
  -H "Authorization: Bearer $TOKEN"

echo "── Intento: PATCH del followup en español ──"
curl -s -X PATCH "$BASE/api/leads/$LEAD_ID/followups/$BAD_FOLLOWUP" \
  -H "Authorization: Bearer $TOKEN" -H "Content-Type: application/json" \
  -d '{"notes":"CashStudio account created (test) | user: dani5840"}' | python3 -m json.tool

echo
echo "── Agregando nota en inglés (por si no se puede editar/borrar la anterior) ──"
curl -s -X POST "$BASE/api/leads/$LEAD_ID/followups" -H "Authorization: Bearer $TOKEN" -H "Content-Type: application/json" \
  -d '{"type":"NOTE","notes":"CashStudio account created (test lead) | user: dani5840. Previous note in Spanish was a mistake — disregard it."}' \
  | python3 -m json.tool
