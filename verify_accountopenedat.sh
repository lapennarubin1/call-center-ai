#!/bin/bash
BASE="https://lead-studio-9gnl.onrender.com"
LEAD_ID="d0a8c5b3-8618-4cb3-a011-d4bb1ec949de"

TOKEN=$(curl -s -m 30 -X POST "$BASE/api/auth/login" -H "Content-Type: application/json" \
  -d '{"email":"admin@leadstudio.com","password":"#9L33dS1udi0"}' \
  | python3 -c 'import json,sys; print(json.load(sys.stdin).get("accessToken",""))')
[ -z "$TOKEN" ] && { echo "LOGIN FALLÓ"; exit 1; }

echo "── 1) GET /api/leads/{id} — detalle individual (lo que vimos con Dani) ──"
curl -s "$BASE/api/leads/$LEAD_ID" -H "Authorization: Bearer $TOKEN" | tee /root/avit_detail.json | python3 -c '
import json,sys
d=json.load(sys.stdin)
lead=d.get("lead",{})
acc=d.get("cashStudioAccount",{})
print("lead.accountOpenedAt (top-level, si existiera):", lead.get("accountOpenedAt","<no existe este campo en el lead>"))
print("lead.updatedAt:", lead.get("updatedAt"))
print("cashStudioAccount.accountOpenedAt:", acc.get("accountOpenedAt"))
print("cashStudioAccount.traderCreatedAt:", acc.get("traderCreatedAt"))
print("cashStudioAccount.createdAt (linked):", acc.get("createdAt"))
'

echo
echo "── 2) GET /api/leads (listado masivo, lo que usa WF14-CRM) — ¿aparece accountOpenedAt ahí? ──"
curl -s "$BASE/api/leads?status=CONTACTED&dialCode=%2B91&limit=200&offset=0" -H "Authorization: Bearer $TOKEN" \
  | python3 -c "
import json,sys
d=json.load(sys.stdin)
leads=d.get('leads',[])
match=[l for l in leads if l.get('id')=='$LEAD_ID']
if not match:
    print('No se encontró en este status/página — probando con otros status...')
else:
    l=match[0]
    print('Campos del lead en el LISTADO masivo:', sorted(l.keys()))
    print('¿trae accountOpenedAt?:', 'accountOpenedAt' in l)
    print('¿trae cashStudioAccount?:', 'cashStudioAccount' in l)
"
