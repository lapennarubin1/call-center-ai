#!/bin/bash
BASE="https://lead-studio-9gnl.onrender.com"
LEAD_ID="d36258f3-91d5-4cc3-844d-7653972a6efd"
# Fecha deseada — cambiá esto: "1 day ago", "7 days ago", o una fecha tipo "2026-09-01"
FECHA="${1:-1 day ago}"
TARGET=$(date -u -d "$FECHA" '+%Y-%m-%dT%H:%M:%S.000Z')
echo "Fecha objetivo: $TARGET"

TOKEN=$(curl -s -m 30 -X POST "$BASE/api/auth/login" -H "Content-Type: application/json" \
  -d '{"email":"admin@leadstudio.com","password":"#9L33dS1udi0"}' \
  | python3 -c 'import json,sys; print(json.load(sys.stdin).get("accessToken",""))')
[ -z "$TOKEN" ] && { echo "LOGIN FALLÓ"; exit 1; }

echo "── GET lead completo (para ver TODOS los campos reales, antes de tocar nada) ──"
curl -s "$BASE/api/leads/$LEAD_ID" -H "Authorization: Bearer $TOKEN" | tee /root/lead_dani_before.json | python3 -m json.tool

echo
echo "── Intento 1: PATCH lead con lastContactedAt ──"
curl -s -X PATCH "$BASE/api/leads/$LEAD_ID" -H "Authorization: Bearer $TOKEN" -H "Content-Type: application/json" \
  -d "{\"lastContactedAt\":\"$TARGET\"}" | python3 -m json.tool

echo
echo "── Intento 2: POST followup con completedAt explícito (fecha pasada) ──"
curl -s -X POST "$BASE/api/leads/$LEAD_ID/followups" -H "Authorization: Bearer $TOKEN" -H "Content-Type: application/json" \
  -d "{\"type\":\"NOTE\",\"notes\":\"Backdate test\",\"completedAt\":\"$TARGET\"}" | tee /root/followup_backdate.json | python3 -m json.tool

echo
echo "── GET lead de nuevo (para comparar qué cambió) ──"
curl -s "$BASE/api/leads/$LEAD_ID" -H "Authorization: Bearer $TOKEN" | tee /root/lead_dani_after.json | python3 -m json.tool

echo
echo "── Diff simple entre antes y después ──"
diff <(python3 -m json.tool /root/lead_dani_before.json) <(python3 -m json.tool /root/lead_dani_after.json) || true
