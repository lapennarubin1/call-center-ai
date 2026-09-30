#!/bin/bash
# Verifica la hipótesis: ¿el loop de México sale del Sheet legacy (panel_leads),
# no del CRM? Y si el panel/WF2 están cruzando datos de las dos fuentes en algún
# punto donde no deberían. SOLO LECTURA.
export MYSQL_PWD="${MYSQL_PWD:-RwPass2026xK}"
DBU="${DB_USER:-panel_rw}"; DB="${DB_NAME:-asterisk}"
q()  { mysql -u "$DBU" "$DB" -t -e "$1" 2>&1; }
qn() { mysql -u "$DBU" "$DB" -N -B -e "$1" 2>/dev/null; }
h()  { echo; echo "══════════ $1 ══════════"; }
tbl() { [ "$(qn "SELECT COUNT(*) FROM information_schema.TABLES WHERE TABLE_SCHEMA='$DB' AND TABLE_NAME='$1'")" = "1" ]; }
col() { [ "$(qn "SELECT COUNT(*) FROM information_schema.COLUMNS WHERE TABLE_SCHEMA='$DB' AND TABLE_NAME='$1' AND COLUMN_NAME='$2'")" = "1" ]; }
dg()  { echo "(REPLACE(REPLACE(REPLACE(COALESCE($1,''),'+',''),' ',''),'-','') COLLATE utf8mb4_general_ci)"; }

{
h "0. ¿EXISTEN panel_leads / panel_conversions (Sheet legacy)?"
for t in panel_leads panel_conversions; do
  tbl $t && echo "$t: $(qn "SELECT GROUP_CONCAT(COLUMN_NAME ORDER BY ORDINAL_POSITION) FROM information_schema.COLUMNS WHERE TABLE_SCHEMA='$DB' AND TABLE_NAME='$t'")" || echo "$t: NO EXISTE"
done

if tbl panel_leads; then
  PCOLS=$(qn "SELECT GROUP_CONCAT(COLUMN_NAME) FROM information_schema.COLUMNS WHERE TABLE_SCHEMA='$DB' AND TABLE_NAME='panel_leads'")
  PPHONE=""; for c in phone telefono numero phone_number; do [[ ",$PCOLS," == *",$c,"* ]] && PPHONE="$c" && break; done
  PCOUNTRY=""; for c in country pais; do [[ ",$PCOLS," == *",$c,"* ]] && PCOUNTRY="$c" && break; done
  PSTATUS=""; for c in status stage estado; do [[ ",$PCOLS," == *",$c,"* ]] && PSTATUS="$c" && break; done
  PLAST=""; for c in last_call_time last_contacted_at last_called; do [[ ",$PCOLS," == *",$c,"* ]] && PLAST="$c" && break; done
  echo "columna teléfono detectada: ${PPHONE:-'(ninguna reconocida)'} | status: ${PSTATUS:-?} | país: ${PCOUNTRY:-?} | último contacto: ${PLAST:-?}"

  h "1. ¿LOS 5-6 NÚMEROS EN LOOP DE MÉXICO ESTÁN EN panel_leads (Sheet)?"
  if [ -n "$PPHONE" ]; then
    q "SELECT * FROM panel_leads
       WHERE RIGHT($(dg "$PPHONE"),10) IN (
         SELECT RIGHT(dst,10) FROM cdr_panel WHERE dst REGEXP '^[+]?52[0-9]{10}\$'
           AND calldate >= UTC_TIMESTAMP() - INTERVAL 45 DAY
         GROUP BY RIGHT(dst,10) HAVING COUNT(*) > 20
       ) LIMIT 20;"
  else
    echo "No pude detectar la columna de teléfono — mostrando 5 filas de muestra para identificarla a mano:"
    q "SELECT * FROM panel_leads LIMIT 5;"
  fi

  h "2. TOTAL de leads México en panel_leads (Sheet) vs crm_leads (CRM)"
  if [ -n "$PPHONE" ] && [ -n "$PCOUNTRY" ]; then
    q "SELECT LOWER(TRIM(COALESCE($PCOUNTRY,''))) pais, ${PSTATUS:-'\"?\"'} status, COUNT(*) n
       FROM panel_leads WHERE LOWER(TRIM(COALESCE($PCOUNTRY,''))) IN ('mexico','méxico','mx')
          OR RIGHT($(dg "$PPHONE"),12) LIKE '52%'
       GROUP BY 1,2 ORDER BY n DESC LIMIT 20;"
  fi

  h "3. FRESCURA de WF14-Sheets (para saber si Sheet sigue vivo y corriendo hoy)"
  q "SELECT source, rows_in, status, synced_at FROM panel_sync_log WHERE source NOT LIKE '%crm%' ORDER BY id DESC LIMIT 8;"
else
  echo "panel_leads no existe en esta base — el Sheet legacy no está en esta instancia, o vive en otra BD/host. Avisar si Google Sheets corre en otro lado."
fi

h "4. wf2_provider_config Y switches — ¿hay algo específico de 'sheet' u 'origen' por el que México se dispare?"
tbl wf2_provider_config && q "SELECT * FROM wf2_provider_config;"
tbl n8n_switches && q "SELECT id,label,workflow_ids FROM n8n_switches WHERE label LIKE '%MEXICO%' OR label LIKE '%SHEET%';"

h "5. ¿Esos 5-6 números están en crm_leads TAMBIÉN? (confirmar que el cruce da vacío)"
q "SELECT COUNT(*) n FROM crm_leads WHERE RIGHT($(dg phone),10) IN (
     SELECT RIGHT(dst,10) FROM cdr_panel WHERE dst REGEXP '^[+]?52[0-9]{10}\$'
       AND calldate >= UTC_TIMESTAMP() - INTERVAL 45 DAY GROUP BY RIGHT(dst,10) HAVING COUNT(*) > 20
   );"

h "6. n8n — ¿qué workflows están ACTIVOS ahora mismo? (activo=true, desde la propia BD de n8n)"
docker exec landmarket_n8n-db psql -U n8n -d n8n -c \
  "SELECT id, name, active FROM workflow_entity WHERE name ILIKE '%mexico%' OR name ILIKE '%sheet%' ORDER BY name;" \
  2>&1 || echo "(no se pudo leer la BD de n8n directo — revisar a mano en https://landmarket-n8n.dhsoig.easypanel.host cuáles workflows de México/Sheet están ACTIVE)"

echo; echo "FIN."
} 2>&1 | tee "/root/audit-sheet-vs-crm-$(date +%H%M).txt"
