#!/bin/bash
BASE="https://lead-studio-9gnl.onrender.com"
LEAD_ID="d36258f3-91d5-4cc3-844d-7653972a6efd"
PHONE="+911234567890"
export MYSQL_PWD="RwPass2026xK"

TOKEN=$(curl -s -m 30 -X POST "$BASE/api/auth/login" -H "Content-Type: application/json" \
  -d '{"email":"admin@leadstudio.com","password":"#9L33dS1udi0"}' \
  | python3 -c 'import json,sys; print(json.load(sys.stdin).get("accessToken",""))')
[ -z "$TOKEN" ] && { echo "LOGIN FALLÓ"; exit 1; }

echo "── DELETE lead en LeadStudio ──"
curl -s -w "\nHTTP %{http_code}\n" -X DELETE "$BASE/api/leads/$LEAD_ID" -H "Authorization: Bearer $TOKEN"

echo
echo "── Limpiando lo que insertamos a mano en nuestra base ──"
mysql -u panel_rw asterisk -e "DELETE FROM crm_leads WHERE lead_id='$LEAD_ID';"
mysql -u panel_rw asterisk -e "DELETE FROM crm_conversions WHERE lead_id='$LEAD_ID';"
mysql -u panel_rw asterisk -e "DELETE FROM wf_call_followups WHERE lead_id='$LEAD_ID' OR phone LIKE '%1234567890';"
mysql -u panel_rw asterisk -e "DELETE FROM stringee_calls WHERE lead_id='$LEAD_ID' OR phone LIKE '%1234567890';"

echo
echo "── Verificación: no debería quedar nada ──"
mysql -u panel_rw asterisk -e "SELECT 'crm_leads' t, COUNT(*) n FROM crm_leads WHERE lead_id='$LEAD_ID'
UNION ALL SELECT 'crm_conversions', COUNT(*) FROM crm_conversions WHERE lead_id='$LEAD_ID'
UNION ALL SELECT 'wf_call_followups', COUNT(*) FROM wf_call_followups WHERE lead_id='$LEAD_ID'
UNION ALL SELECT 'stringee_calls', COUNT(*) FROM stringee_calls WHERE lead_id='$LEAD_ID';"
