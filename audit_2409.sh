#!/bin/bash
# AUDITORÍA 24/09 — Cuentas abiertas por día/país/proveedor + México. SOLO LECTURA.
# Opcional: DIA=2026-09-23 (día IST a auditar, default = ayer IST)
export MYSQL_PWD="${MYSQL_PWD:-RwPass2026xK}"
DBU="${DB_USER:-panel_rw}"; DB="${DB_NAME:-asterisk}"
q()  { mysql -u "$DBU" "$DB" -t -e "$1" 2>&1; }
qn() { mysql -u "$DBU" "$DB" -N -B -e "$1" 2>/dev/null; }
h()  { echo; echo "══════════ $1 ══════════"; }
tbl() { [ "$(qn "SELECT COUNT(*) FROM information_schema.TABLES WHERE TABLE_SCHEMA='$DB' AND TABLE_NAME='$1'")" = "1" ]; }
col() { [ "$(qn "SELECT COUNT(*) FROM information_schema.COLUMNS WHERE TABLE_SCHEMA='$DB' AND TABLE_NAME='$1' AND COLUMN_NAME='$2'")" = "1" ]; }
dg()  { echo "REPLACE(REPLACE(REPLACE(COALESCE($1,''),'+',''),' ',''),'-','')"; }
ck()  { local d; d=$(dg "$1"); echo "(CASE WHEN $d LIKE '91%' AND LENGTH($d)=12 THEN 'india' WHEN $d LIKE '977%' AND LENGTH($d)=13 THEN 'nepal' WHEN $d LIKE '52%' AND LENGTH($d)=12 THEN 'mexico' WHEN $d LIKE '971%' AND LENGTH($d)=12 THEN 'dubai' ELSE NULL END)"; }
ckf() { local c; c=$(dg "$2"); echo "COALESCE($(ck "$1"), CASE WHEN $c='91' THEN 'india' WHEN $c='977' THEN 'nepal' WHEN $c='52' THEN 'mexico' END, CASE WHEN LOWER(TRIM(COALESCE($3,''))) IN ('india','in','ind') THEN 'india' WHEN LOWER(TRIM(COALESCE($3,''))) IN ('nepal','np','npl') THEN 'nepal' WHEN LOWER(TRIM(COALESCE($3,''))) IN ('mexico','méxico','mx','mex') THEN 'mexico' END)"; }
IST="INTERVAL 330 MINUTE"; CST="INTERVAL -360 MINUTE"
{
echo "AUDITORÍA CRM/MX — $(date -u '+%F %T') UTC"
h "0. RELOJ Y ESQUEMA"
q "SELECT NOW() db_now, UTC_TIMESTAMP() utc_now, @@global.time_zone tz_global, @@system_time_zone tz_sistema;"
for t in crm_conversions crm_leads cdr_panel stringee_calls support_actions wf_call_followups; do
  tbl $t && echo "$t [$(qn "SELECT TABLE_TYPE FROM information_schema.TABLES WHERE TABLE_SCHEMA='$DB' AND TABLE_NAME='$t'")]: $(qn "SELECT GROUP_CONCAT(COLUMN_NAME ORDER BY ORDINAL_POSITION) FROM information_schema.COLUMNS WHERE TABLE_SCHEMA='$DB' AND TABLE_NAME='$t'")" || echo "$t: NO EXISTE"
done
q "SELECT MAX(calldate) ultimo_cdr, MIN(calldate) primer_cdr FROM cdr_panel;"
DIA="${DIA:-$(qn "SELECT DATE(UTC_TIMESTAMP() + $IST) - INTERVAL 1 DAY")}"
BS=$(qn "SELECT '$DIA' - $IST"); BE=$(qn "SELECT '$DIA' + INTERVAL 1 DAY - $IST")
WK=$(qn "SELECT DATE(UTC_TIMESTAMP() + $IST) - INTERVAL WEEKDAY(UTC_TIMESTAMP() + $IST) DAY")
WS=$(qn "SELECT '$WK' - $IST")
MS=$(qn "SELECT DATE_FORMAT(UTC_TIMESTAMP() + $IST, '%Y-%m-01') - $IST")
echo "Día auditado (IST): $DIA  → UTC [$BS , $BE)   | semana desde $WK | mes desde UTC $MS"
if col crm_conversions account_opened_at; then AOA="c.account_opened_at"; else AOA="NULL"; fi
OPENED="COALESCE($AOA, l.last_contacted_at, c.created_at)"
SRC="CASE WHEN $AOA IS NOT NULL THEN 'account_opened_at' WHEN l.last_contacted_at IS NOT NULL THEN 'lead.last_contacted_at' ELSE 'conv.created_at' END"
ACC="(
  SELECT c.lead_id, COALESCE(l.phone,c.phone) phone,
         COALESCE(NULLIF(l.provider,''), NULLIF(c.provider,'')) prov,
         $OPENED opened_at, $SRC opened_src,
         COALESCE($(ckf l.phone l.country_code l.country), $(ckf c.phone NULL c.country)) ckey,
         IF(l.lead_id IS NULL,'NO','SI') en_crm_leads, l.call_attempts, l.status,
         l.last_contacted_at, c.created_at conv_created, $AOA aoa, 'crm_conversions' origen
  FROM crm_conversions c LEFT JOIN crm_leads l ON l.lead_id=c.lead_id
  UNION ALL
  SELECT l.lead_id, l.phone, NULLIF(l.provider,''), l.last_contacted_at, 'lead.last_contacted_at',
         $(ckf l.phone l.country_code l.country), 'SI', l.call_attempts, l.status,
         l.last_contacted_at, NULL, NULL, 'crm_leads INTERESTED'
  FROM crm_leads l LEFT JOIN crm_conversions c ON c.lead_id=l.lead_id
  WHERE c.lead_id IS NULL AND UPPER(COALESCE(l.stage,''))='INTERESTED'
) a"
BUCKET="IF(a.prov='stringee','Stringee','Provider1 (asterisk+NULL)')"
h "1. CUENTAS ABIERTAS — lo que muestra el panel, por día IST × país × proveedor (35 días)"
q "SELECT DATE(a.opened_at + $IST) dia_ist, a.ckey pais, $BUCKET proveedor_panel, COUNT(*) cuentas
   FROM $ACC WHERE a.opened_at >= UTC_TIMESTAMP() - INTERVAL 35 DAY
   GROUP BY dia_ist, pais, proveedor_panel ORDER BY dia_ist DESC, pais, proveedor_panel;"
echo "-- totales sin filtro de fecha:"
q "SELECT a.ckey pais, $BUCKET proveedor_panel, COUNT(*) cuentas, MIN(a.opened_at) primera, MAX(a.opened_at) ultima
   FROM $ACC GROUP BY pais, proveedor_panel;"
h "2. ¿DE DÓNDE SALE LA FECHA de las cuentas de $DIA? (el panel usa la primera no-nula)"
q "SELECT a.ckey pais, $BUCKET proveedor_panel, COALESCE(a.prov,'(NULL)') provider_real, a.opened_src fecha_tomada_de,
          a.origen, a.en_crm_leads, COUNT(*) n
   FROM $ACC WHERE a.opened_at >= '$BS' AND a.opened_at < '$BE'
   GROUP BY 1,2,3,4,5,6 ORDER BY n DESC;"
echo "-- estado/intentos del lead de esas cuentas (India):"
q "SELECT COALESCE(a.status,'(no está en crm_leads)') status, IF(COALESCE(a.call_attempts,0)=0,'0 intentos','>0 intentos') intentos, COUNT(*) n
   FROM $ACC WHERE a.opened_at >= '$BS' AND a.opened_at < '$BE' AND a.ckey='india'
   GROUP BY 1,2 ORDER BY n DESC;"
CDRAGG="(SELECT RIGHT(dst,10) p10, COUNT(*) llamadas, SUM(disposition='ANSWERED') contestadas,
               SUM(disposition='ANSWERED' AND billsec>=60) conv60, MAX(calldate) ultima_llamada
        FROM cdr_panel WHERE calldate >= UTC_TIMESTAMP() - INTERVAL 60 DAY AND dst REGEXP '^[+]?(91|977|52)[0-9]{10}\$'
        GROUP BY RIGHT(dst,10))"
STRAGG="(SELECT NULL p10, 0 llamadas, 0 contestadas, 0 conv60 FROM DUAL WHERE 1=0)"
tbl stringee_calls && col stringee_calls phone && STRAGG="(SELECT RIGHT($(dg phone),10) p10, COUNT(*) llamadas, SUM(answered=1) contestadas, SUM(answered=1 AND duration_secs>=60) conv60 FROM stringee_calls WHERE phone IS NOT NULL GROUP BY RIGHT($(dg phone),10))"
FUPAGG="(SELECT NULL p10, 0 n FROM DUAL WHERE 1=0)"
tbl wf_call_followups && FUPAGG="(SELECT RIGHT($(dg phone),10) p10, COUNT(*) n FROM wf_call_followups GROUP BY RIGHT($(dg phone),10))"
SUPAGG="(SELECT NULL p10, 0 n, NULL primera FROM DUAL WHERE 1=0)"
tbl support_actions && SUPAGG="(SELECT RIGHT($(dg phone),10) p10, COUNT(*) n, MIN(created_at) primera FROM support_actions WHERE action='account_open' GROUP BY RIGHT($(dg phone),10))"
JOINS="LEFT JOIN $CDRAGG cd ON cd.p10 = RIGHT($(dg a.phone),10)
       LEFT JOIN $STRAGG st ON st.p10 = RIGHT($(dg a.phone),10)
       LEFT JOIN $FUPAGG fu ON fu.p10 = RIGHT($(dg a.phone),10)
       LEFT JOIN $SUPAGG su ON su.p10 = RIGHT($(dg a.phone),10)"
real_check() {
  echo "-- [$1] ¿las llamamos nosotros? (CDR Asterisk 60d / Stringee / WF9 followups / Support)"
  q "SELECT a.ckey pais, $BUCKET proveedor_panel, COUNT(*) cuentas_panel,
            SUM(COALESCE(cd.llamadas,0)>0) con_cdr_asterisk, SUM(COALESCE(cd.contestadas,0)>0) asterisk_contesto, SUM(COALESCE(cd.conv60,0)>0) asterisk_conv_60s,
            SUM(COALESCE(st.llamadas,0)>0) con_stringee, SUM(COALESCE(st.contestadas,0)>0) stringee_contesto,
            SUM(COALESCE(fu.n,0)>0) con_followup_wf9, SUM(COALESCE(su.n,0)>0) abierta_desde_support,
            SUM(COALESCE(cd.llamadas,0)=0 AND COALESCE(st.llamadas,0)=0 AND COALESCE(fu.n,0)=0) SIN_NINGUNA_LLAMADA_NUESTRA
     FROM $ACC $JOINS
     WHERE a.opened_at >= '$2' AND a.opened_at < '$3'
     GROUP BY pais, proveedor_panel ORDER BY pais, proveedor_panel;"
}
h "3. ¿CUENTAS REALES NUESTRAS?"
real_check "día $DIA" "$BS" "$BE"
real_check "semana desde $WK" "$WS" "$(qn "SELECT UTC_TIMESTAMP()")"
real_check "mes" "$MS" "$(qn "SELECT UTC_TIMESTAMP()")"
echo "-- muestra 30 cuentas India de $DIA:"
q "SELECT a.lead_id, a.phone, COALESCE(a.prov,'(NULL)') prov, a.opened_src, a.opened_at, a.conv_created, a.status, a.call_attempts,
          COALESCE(cd.llamadas,0) cdr, COALESCE(cd.contestadas,0) cdr_ans, cd.ultima_llamada, COALESCE(st.llamadas,0) stringee,
          COALESCE(fu.n,0) wf9, COALESCE(su.n,0) support
   FROM $ACC $JOINS
   WHERE a.opened_at >= '$BS' AND a.opened_at < '$BE' AND a.ckey='india'
   ORDER BY a.opened_at LIMIT 30;"
h "4. crm_conversions CRUDO — ¿cuándo se cargó?"
q "SELECT COUNT(*) total, SUM($AOA IS NOT NULL) con_account_opened_at, MIN(c.created_at) min_created, MAX(c.created_at) max_created,
          MIN(c.synced_at) min_synced, MAX(c.synced_at) max_synced FROM crm_conversions c;"
q "SELECT DATE(c.created_at + $IST) dia_ist_created, $(ckf c.phone NULL c.country) pais, COALESCE(c.provider,'(NULL)') prov, COUNT(*) n
   FROM crm_conversions c GROUP BY 1,2,3 ORDER BY 1 DESC LIMIT 40;"
echo "-- leads con stage INTERESTED en crm_leads:"
q "SELECT $(ckf phone country_code country) pais, COALESCE(provider,'(NULL)') prov, status, IF(call_attempts=0,'0','>0') intentos, COUNT(*) n
   FROM crm_leads WHERE UPPER(COALESCE(stage,''))='INTERESTED' GROUP BY 1,2,3,4 ORDER BY 1, n DESC;"
q "SELECT UPPER(COALESCE(stage,'(NULL)')) stage, COUNT(*) n FROM crm_leads GROUP BY 1 ORDER BY n DESC;"
if tbl support_actions; then
  echo "-- aperturas REALES registradas desde Support/Telegram, por día:"
  q "SELECT DATE(created_at) dia, country, result, COUNT(*) n FROM support_actions
     WHERE action='account_open' AND created_at >= UTC_TIMESTAMP() - INTERVAL 35 DAY GROUP BY 1,2,3 ORDER BY 1 DESC;"
fi
h "5. API LEADSTUDIO — ¿qué fecha real de apertura trae el lead? (GET /api/leads, sin cupo)"
BASE="https://lead-studio-9gnl.onrender.com"
TOKEN=$(curl -s -m 40 -X POST "$BASE/api/auth/login" -H "Content-Type: application/json" \
  -d '{"email":"admin@leadstudio.com","password":"#9L33dS1udi0"}' | python3 -c 'import json,sys
try: print(json.load(sys.stdin).get("accessToken",""))
except Exception: print("")')
if [ -z "$TOKEN" ]; then echo "LOGIN FALLÓ"; else
for DC in 91 977 52; do
  echo "-- dialCode +$DC, stage=INTERESTED:"
  curl -s -m 90 "$BASE/api/leads?stage=INTERESTED&dialCode=%2B$DC&limit=200&offset=0" -H "Authorization: Bearer $TOKEN" | python3 -c '
import json,sys,collections
try: d=json.load(sys.stdin)
except Exception as e: print("  respuesta no JSON:",e); sys.exit()
L=d.get("leads",[]); print("  total API:",d.get("total"),"| devueltos:",len(L),"| counts:",json.dumps(d.get("counts"))[:300])
if not L: sys.exit()
st=collections.Counter(str(x.get("stage")) for x in L); print("  stage en respuesta:",dict(st))
PII={"name","phone","email","firstName","lastName","fullName","notes","address"}
print("  campos del lead:",sorted(k for k in L[0].keys() if k not in PII))
for k in sorted(L[0].keys()):
    if any(w in k.lower() for w in ("account","cash","opened","convert","interest")):
        print("   ",k,"→",collections.Counter(str(x.get(k))[:10] for x in L).most_common(8))
for f in ("accountOpenedAt","updatedAt","createdAt","lastContactedAt"):
    c=collections.Counter(str(x.get(f))[:10] for x in L)
    print("  ",f,"por día:",sorted(c.items(),reverse=True)[:12])
'
done; fi
h "6. MÉXICO — CDR Asterisk por día (hora CST)"
PMX=$(qn "SELECT price_per_minute FROM sip_provider_pricing WHERE country='mexico' ORDER BY provider_id LIMIT 1"); PMX=${PMX:-0.06}
echo "precio MX configurado en SIP Balance: $PMX USD/min (el dashboard usa 0.06 fijo)"
MXW="dst REGEXP '^[+]?52[0-9]{10}\$'"
DCTX="'?'"; col cdr_panel dcontext && DCTX="dcontext"
CHAN="'?'";  col cdr_panel channel  && CHAN="REGEXP_REPLACE(channel,'-[0-9a-fA-F]+$','')"
ACODE="'?'"; col cdr_panel accountcode && ACODE="COALESCE(NULLIF(accountcode,''),'(vacío)')"
DCH="'?'";   col cdr_panel dstchannel && DCH="REGEXP_REPLACE(dstchannel,'-[0-9a-fA-F]+$','')"
q "SELECT DATE(calldate + $CST) dia_cst, COUNT(*) intentos, SUM(disposition='ANSWERED') contestadas,
          SUM(billsec) seg, SUM(CEIL(billsec/60)) min_facturados, ROUND(SUM(CEIL(billsec/60))*0.06,2) costo_panel_usd,
          ROUND(SUM(CEIL(billsec/60))*$PMX,2) costo_real_usd, COUNT(DISTINCT dst) numeros, MIN(calldate) primera_utc, MAX(calldate) ultima_utc
   FROM cdr_panel WHERE $MXW AND calldate >= UTC_TIMESTAMP() - INTERVAL 45 DAY GROUP BY 1 ORDER BY 1 DESC;"
echo "-- por día × origen × trunk × accountcode:"
q "SELECT DATE(calldate + $CST) dia_cst, $DCTX contexto, $CHAN origen, $DCH trunk, $ACODE acode, COUNT(*) n,
          SUM(disposition='ANSWERED') ans, SUM(CEIL(billsec/60)) min_fact
   FROM cdr_panel WHERE $MXW AND calldate >= UTC_TIMESTAMP() - INTERVAL 45 DAY GROUP BY 1,2,3,4,5 ORDER BY 1 DESC, n DESC;"
echo "-- últimas 40 llamadas MX una por una:"
q "SELECT calldate utc, calldate + $CST cst, dst, $DCTX contexto, $CHAN origen, $DCH trunk, $ACODE acode, disposition, billsec
   FROM cdr_panel WHERE $MXW ORDER BY calldate DESC LIMIT 40;"
echo "-- números MX más marcados (45d) y si existen como lead en el CRM:"
q "SELECT RIGHT(c.dst,10) numero, COUNT(*) veces, COUNT(DISTINCT DATE(c.calldate)) dias, SUM(c.disposition='ANSWERED') ans,
          MIN(c.calldate) primera, MAX(c.calldate) ultima, IF(MAX(l.lead_id) IS NULL,'NO está en CRM','lead CRM') en_crm
   FROM cdr_panel c LEFT JOIN (SELECT RIGHT($(dg phone),10) p10, MAX(lead_id) lead_id FROM crm_leads GROUP BY 1) l ON l.p10 = RIGHT(c.dst,10)
   WHERE c.$MXW AND c.calldate >= UTC_TIMESTAMP() - INTERVAL 45 DAY
   GROUP BY RIGHT(c.dst,10) ORDER BY veces DESC LIMIT 25;"
if col cdr_panel dstchannel; then
  echo "-- *** CRUCES: trunk proveedor-mx con destino NO mexicano, o MX por otro trunk (45d) ***"
  q "SELECT DATE(calldate + $CST) dia, $DCH trunk, $(ck dst) pais_dst, LEFT(dst,6) dst_ini, COUNT(*) n, SUM(CEIL(billsec/60)) min_fact
     FROM cdr_panel WHERE calldate >= UTC_TIMESTAMP() - INTERVAL 45 DAY
       AND ((dstchannel LIKE 'PJSIP/proveedor-mx-%' AND NOT ($MXW)) OR (($MXW) AND dstchannel NOT LIKE 'PJSIP/proveedor-mx-%'))
     GROUP BY 1,2,3,4 ORDER BY 1 DESC, n DESC LIMIT 30;"
fi
if tbl cdr && [ "$(qn "SELECT TABLE_TYPE FROM information_schema.TABLES WHERE TABLE_SCHEMA='$DB' AND TABLE_NAME='cdr_panel'")" = "VIEW" ]; then
  echo "-- tabla cdr cruda, MX por día:"
  q "SELECT DATE(calldate + $CST) dia_cst, COUNT(*) n, SUM(CEIL(billsec/60)) min_fact FROM cdr
     WHERE $MXW AND calldate >= UTC_TIMESTAMP() - INTERVAL 45 DAY GROUP BY 1 ORDER BY 1 DESC;"
fi
h "7. MÉXICO — Stringee, CRM, WF9, switches"
if tbl stringee_calls; then
  q "SELECT DATE(FROM_UNIXTIME(IF(start_time>1e11,start_time/1000,start_time)) + $CST) dia_cst, country etiqueta, $(ck phone) pais_por_numero,
            COUNT(*) n, SUM(answered=1) ans
     FROM stringee_calls WHERE LOWER(COALESCE(country,''))='mexico' OR $(dg phone) LIKE '52%'
     GROUP BY 1,2,3 ORDER BY 1 DESC LIMIT 30;"
fi
q "SELECT status, COUNT(*) n, MAX(last_contacted_at) ultimo_contacto FROM crm_leads WHERE $(ckf phone country_code country)='mexico' GROUP BY status;"
q "SELECT DATE(last_contacted_at + $CST) dia_cst, COUNT(*) leads_mx_contactados FROM crm_leads
   WHERE $(ckf phone country_code country)='mexico' AND last_contacted_at >= UTC_TIMESTAMP() - INTERVAL 45 DAY GROUP BY 1 ORDER BY 1 DESC;"
tbl wf_call_followups && q "SELECT DATE(created_at) dia, provider, outcome, COUNT(*) n FROM wf_call_followups
   WHERE $(ck phone)='mexico' AND created_at >= NOW() - INTERVAL 45 DAY GROUP BY 1,2,3 ORDER BY 1 DESC;"
tbl wf2_provider_config && q "SELECT * FROM wf2_provider_config;"
tbl n8n_switches && q "SELECT * FROM n8n_switches;"
LOGS=$(ls /var/log/asterisk/full* /var/log/asterisk/messages* 2>/dev/null)
if [ -n "$LOGS" ]; then
  echo "-- log de Asterisk: veces que entró la rama 'Salida Mexico' por día:"
  zgrep -h "Salida Mexico" $LOGS 2>/dev/null | grep -oE '^\[[0-9]{4}-[0-9]{2}-[0-9]{2}' | tr -d '[' | sort | uniq -c | tail -20
fi
h "8. FRESCURA WF14-CRM"
q "SELECT source, rows_in, status, synced_at FROM panel_sync_log WHERE source='crm' ORDER BY id DESC LIMIT 5;"
q "SELECT MAX(synced_at) ult_sync_leads, COUNT(*) leads FROM crm_leads;"
echo; echo "FIN — pegá todo este output en el chat."
} 2>&1 | tee "/root/audit-2409-$(date +%H%M).txt"
