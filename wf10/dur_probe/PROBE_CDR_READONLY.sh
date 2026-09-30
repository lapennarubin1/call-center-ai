#!/bin/bash
# PROBE_CDR_READONLY.sh — SOLO LECTURA. No modifica MySQL, ni n8n, ni Asterisk, ni LeadStudio.
# Responde, con datos reales del VPS, lo que el diseño "Asterisk contestada -> Activity con CDR.billsec" necesita saber ANTES de desplegar:
#   1) ¿dónde está la tabla CDR (base.tabla) y qué columnas tiene?
#   2) ¿el usuario MySQL de la credencial de n8n puede leerla?  (si no: WF9 cae a la duración de ElevenLabs, sin romper nada)
#   3) el caso real (uniqueid 1790750598.32578): ¿billsec 88 / duration 126?
#   4) ¿el CDR se puede unir con nuestras llamadas por teléfono (últimos 10 dígitos) + epoch del uniqueid? (tasa de coincidencia real)
#   5) ¿hay CDR duplicados, formatos raros de uniqueid/dst, zona horaria de calldate, escritura por lotes (batch)?
#   6) Opción C (solo GET, sin token): ¿LeadStudio publica un OpenAPI/Swagger con algún endpoint que acepte durationSeconds?
#   7) el worker de grabaciones: ¿reintenta el 503 el tiempo suficiente?
# Uso:   cd /root/wf10 && bash PROBE_CDR_READONLY.sh
# Variables opcionales: MYSQL_CMD="mysql" DB=asterisk N8N_MYSQL_USER=usuario_de_la_credencial_de_n8n LS_BASE=https://lead-studio-9gnl.onrender.com
#                       CASE_UID=1790750598.32578 CASE_PHONE=919241014686
MYSQL_CMD="${MYSQL_CMD:-mysql}"; DB="${DB:-asterisk}"; LS_BASE="${LS_BASE:-https://lead-studio-9gnl.onrender.com}"
CASE_UID="${CASE_UID:-1790750598.32578}"; CASE_PHONE="${CASE_PHONE:-919241014686}"; N8N_MYSQL_USER="${N8N_MYSQL_USER:-}"
OUT="/root/probe_cdr_$(date +%Y%m%d_%H%M%S).txt"
read -r -d '' MASK_PY <<'PY'
import re, sys
def mask(s):
    s = re.sub(r"(?i)(password|passwd|pwd|secret|token|api[_-]?key|authorization)(\s*[=:]\s*)\S+", r"\1\2***MASKED***", s)
    s = re.sub(r"(MYSQL_PWD|PGPASSWORD)=\S+", r"\1=***MASKED***", s)
    s = re.sub(r"(://[^/:@\s]+:)[^@\s]+@", r"\1***MASKED***@", s)
    s = re.sub(r"(?<![0-9.])\+?(\d{6,})(\d{4})(?![0-9]|\.[0-9])", lambda m: "*" * len(m.group(1)) + m.group(2), s)   # teléfonos: solo últimos 4 dígitos
    return s
for line in sys.stdin: sys.stdout.write(mask(line))
PY
{
q() { $MYSQL_CMD -N -B -e "$1" 2>&1; }
qt() { $MYSQL_CMD -t -e "$1" 2>&1; }
echo "PROBE CDR (solo lectura) — $(date -u +%FT%TZ) — host $(hostname)"
echo "MySQL: $($MYSQL_CMD --version 2>&1 | head -1) | @@time_zone=$(q 'SELECT @@time_zone') @@system_time_zone=$(q 'SELECT @@system_time_zone') | ahora UTC=$(q 'SELECT UTC_TIMESTAMP()') NOW()=$(q 'SELECT NOW()')"
echo "Usuario de esta sonda: $(q 'SELECT CURRENT_USER()')"

echo; echo "===== 1. ¿Dónde está la tabla CDR? ====="
CANDS=$(q "SELECT CONCAT(TABLE_SCHEMA,'.',TABLE_NAME) FROM information_schema.TABLES WHERE LOWER(TABLE_NAME) IN ('cdr','cdr_table','asterisk_cdr') AND TABLE_SCHEMA NOT IN ('mysql','information_schema','performance_schema','sys')")
echo "${CANDS:-<ninguna encontrada por information_schema>}"
CDRT=""; for t in $CANDS asteriskcdrdb.cdr asterisk.cdr cdrdb.cdr $DB.cdr; do q "SELECT 1 FROM $t LIMIT 1" >/dev/null 2>&1 && { CDRT=$t; break; }; done
if [ -z "$CDRT" ]; then
  echo "❌ No se pudo leer ninguna tabla CDR con este usuario. Revisá: cat /etc/asterisk/cdr_mysql.conf /etc/asterisk/cdr_adaptive_odbc.conf /etc/odbc.ini (sin pegar contraseñas)."
  echo "   Sin CDR el diseño funciona igual pero usa la duración de ElevenLabs (respaldo). Mandame estas líneas y se ajusta CDR_TABLE."
else
  echo "✅ CDR = $CDRT   (en el nodo '🧾 Build CDR SQL (Asterisk)' de WF9 poné: const CDR_TABLE = '$CDRT';)"
  echo "--- columnas:"; qt "SHOW COLUMNS FROM $CDRT"
  echo "--- filas últimas 24 h / ANSWERED con billsec>0 / total: $(q "SELECT COUNT(*), SUM(disposition='ANSWERED' AND billsec>0) FROM $CDRT WHERE calldate >= NOW() - INTERVAL 1 DAY" | tr '\t' ' ') / $(q "SELECT COUNT(*) FROM $CDRT")"
  echo "--- índices:"; qt "SHOW INDEX FROM $CDRT" | cut -c1-150
fi

if [ -n "$CDRT" ]; then
echo; echo "===== 2. Caso real: uniqueid $CASE_UID (esperado: billsec 88, duration 126) ====="
qt "SELECT calldate, uniqueid, disposition, duration, billsec, RIGHT(dst,10) AS dst10, LENGTH(dst) AS dst_len, channel, dstchannel FROM $CDRT WHERE uniqueid LIKE '%${CASE_UID}' OR (RIGHT(REPLACE(dst,'+',''),10)=RIGHT('$CASE_PHONE',10) AND calldate >= NOW() - INTERVAL 10 DAY AND billsec BETWEEN 80 AND 95) ORDER BY calldate DESC LIMIT 10"

echo; echo "===== 3. Formato de uniqueid y dst (ANSWERED, últimas 24 h) ====="
qt "SELECT CASE WHEN uniqueid REGEXP '^[0-9]+\\\\.[0-9]+\$' THEN '<epoch>.<seq>' WHEN uniqueid REGEXP '^[A-Za-z0-9_.-]+-[0-9]+\\\\.[0-9]+\$' THEN '<systemname>-<epoch>.<seq>' ELSE 'OTRO' END AS formato_uniqueid, COUNT(*) n, MIN(uniqueid) ejemplo FROM $CDRT WHERE disposition='ANSWERED' AND billsec>0 AND calldate >= NOW() - INTERVAL 1 DAY GROUP BY 1"
qt "SELECT LENGTH(REPLACE(REPLACE(dst,'+',''),' ','')) AS largo_dst, LEFT(REPLACE(dst,'+',''),2) AS prefijo, COUNT(*) n FROM $CDRT WHERE disposition='ANSWERED' AND billsec>0 AND calldate >= NOW() - INTERVAL 1 DAY GROUP BY 1,2 ORDER BY n DESC LIMIT 12"
echo "--- zona horaria de calldate frente al epoch del uniqueid (segundos de diferencia; 0 = calldate está en la zona de la sesión MySQL):"
qt "SELECT ROUND(AVG(UNIX_TIMESTAMP(calldate) - CAST(SUBSTRING_INDEX(SUBSTRING_INDEX(SUBSTRING_INDEX(uniqueid,'.',-2),'.',1),'-',-1) AS UNSIGNED))) AS dif_media_s, MIN(UNIX_TIMESTAMP(calldate) - CAST(SUBSTRING_INDEX(SUBSTRING_INDEX(SUBSTRING_INDEX(uniqueid,'.',-2),'.',1),'-',-1) AS UNSIGNED)) AS dif_min_s, MAX(UNIX_TIMESTAMP(calldate) - CAST(SUBSTRING_INDEX(SUBSTRING_INDEX(SUBSTRING_INDEX(uniqueid,'.',-2),'.',1),'-',-1) AS UNSIGNED)) AS dif_max_s FROM $CDRT WHERE calldate >= NOW() - INTERVAL 1 DAY AND uniqueid REGEXP '[0-9]{9,11}\\\\.[0-9]+'"
echo "   (el diseño NO depende de la zona: une por el epoch del uniqueid; calldate solo se usa como cota gruesa de 2 días)"

echo; echo "===== 4. CDR duplicados (mismo uniqueid ANSWERED más de una vez, últimas 24 h) ====="
qt "SELECT COUNT(*) AS uniqueids_con_mas_de_1_fila FROM (SELECT uniqueid FROM $CDRT WHERE disposition='ANSWERED' AND billsec>0 AND calldate >= NOW() - INTERVAL 1 DAY GROUP BY uniqueid HAVING COUNT(*)>1) d"
qt "SELECT uniqueid, COUNT(*) filas, GROUP_CONCAT(DISTINCT billsec) billsecs, GROUP_CONCAT(DISTINCT duration) durations FROM $CDRT WHERE disposition='ANSWERED' AND billsec>0 AND calldate >= NOW() - INTERVAL 1 DAY GROUP BY uniqueid HAVING COUNT(*)>1 LIMIT 8"

echo; echo "===== 5. Unión real: últimas llamadas Asterisk CONNECTED de wf_call_followups <-> CDR (teléfono + epoch del uniqueid) ====="
echo "   Hoy la fila se crea cuando WF2 hace el POST (≈ 30-70 s después de marcar): el CDR debe empezar entre 900 s antes y 5 s después."
python3 - "$MYSQL_CMD" "$DB" "$CDRT" <<'PY'
import subprocess, sys
mysql, db, cdr = sys.argv[1], sys.argv[2], sys.argv[3]
def run(sql):
    r = subprocess.run(mysql.split() + ['-N', '-B', '-e', sql], capture_output=True, text=True)
    return [l.split('\t') for l in r.stdout.splitlines() if l.strip()]
EP = "CAST(SUBSTRING_INDEX(SUBSTRING_INDEX(SUBSTRING_INDEX(c.uniqueid,'.',-2),'.',1),'-',-1) AS UNSIGNED)"
rows = run("SELECT f.followup_id, RIGHT(f.phone,10), UNIX_TIMESTAMP(f.created_at) FROM %s.wf_call_followups f WHERE f.provider='asterisk' AND f.outcome='CONNECTED' AND f.created_at >= NOW() - INTERVAL 3 DAY ORDER BY f.id DESC LIMIT 40" % db)
tot = len(rows); one = multi = none = 0; deltas = []; ex = []
for fid, p10, ce in rows:
    ce = int(ce)
    m = run("SELECT c.uniqueid, c.duration, c.billsec, %s FROM %s c WHERE c.disposition='ANSWERED' AND c.billsec>0 AND c.calldate >= NOW() - INTERVAL 4 DAY AND RIGHT(REPLACE(REPLACE(c.dst,'+',''),' ',''),10)='%s' AND %s BETWEEN %d AND %d" % (EP, cdr, p10, EP, ce - 900, ce + 5))
    keys = {(x[0], x[1], x[2]) for x in m}
    if not keys: none += 1
    elif len(keys) == 1: one += 1; k = list(keys)[0]; deltas.append(ce - int(m[0][3])); ex.append((fid[:8], '*' * 6 + p10[-4:], k[0], k[1], k[2], ce - int(m[0][3])))
    else: multi += 1
print("Activities Asterisk CONNECTED (últimos 3 días, máx 40): %d | con 1 CDR único: %d | con varios CDR: %d | sin CDR: %d" % (tot, one, multi, none))
if tot: print("Tasa de unión exacta: %.0f %%  (esperado ≥ 90 %%; lo que falte usa la duración de ElevenLabs)" % (100.0 * one / tot))
if deltas: print("Retraso Activity(WF2) - inicio de llamada (s): min %d / mediana %d / max %d   <- por esto la Activity actual nace con duración 0" % (min(deltas), sorted(deltas)[len(deltas)//2], max(deltas)))
for e in ex[:8]: print("   followup %s… dst %s uid %s duration %s billsec %s (Activity %+ds tras el inicio)" % e)
PY
fi

echo; echo "===== 6. Permisos: ¿qué usuarios MySQL leen el CDR y las tablas wf_call_*? ====="
if [ -n "$CDRT" ]; then
  CDRDB=${CDRT%%.*}
  echo "--- SELECT sobre la base del CDR ($CDRDB) — grantees:"
  q "SELECT GRANTEE, PRIVILEGE_TYPE FROM information_schema.SCHEMA_PRIVILEGES WHERE TABLE_SCHEMA='$CDRDB' AND PRIVILEGE_TYPE IN ('SELECT','ALL PRIVILEGES') UNION SELECT GRANTEE, PRIVILEGE_TYPE FROM information_schema.USER_PRIVILEGES WHERE PRIVILEGE_TYPE IN ('SELECT','ALL PRIVILEGES')" | sort -u
  echo "--- SELECT/INSERT sobre '$DB' (donde vive wf_call_events) — grantees:"
  q "SELECT GRANTEE, GROUP_CONCAT(PRIVILEGE_TYPE) FROM information_schema.SCHEMA_PRIVILEGES WHERE TABLE_SCHEMA='$DB' GROUP BY GRANTEE" | sort -u
  if [ -n "$N8N_MYSQL_USER" ]; then echo "--- SHOW GRANTS del usuario indicado ($N8N_MYSQL_USER):"; for h in $(q "SELECT Host FROM mysql.user WHERE User='$N8N_MYSQL_USER'"); do q "SHOW GRANTS FOR '$N8N_MYSQL_USER'@'$h'"; done | sed -E "s/IDENTIFIED BY PASSWORD '[^']*'/IDENTIFIED BY PASSWORD '***'/"; fi
  if [ "$CDRDB" != "$DB" ]; then
    echo "⚠️ El CDR está en otra base ($CDRDB). La credencial 'MySQL account' de n8n usa la base '$DB': hace falta (a) CDR_TABLE = '$CDRT' en WF9 y (b) un GRANT SELECT para ese usuario."
    echo "   Ejecutalo vos (NO lo hace este script), cambiando USUARIO y HOST por los de la credencial de n8n:"
    echo "   GRANT SELECT ON ${CDRT%%.*}.cdr TO 'USUARIO'@'HOST';"
  fi
fi

echo; echo "===== 7. ¿Asterisk escribe el CDR al colgar o por lotes? (/etc/asterisk/cdr*.conf) ====="
for f in /etc/asterisk/cdr.conf /etc/asterisk/cdr_mysql.conf /etc/asterisk/cdr_adaptive_odbc.conf /etc/asterisk/cdr_odbc.conf; do
  [ -f "$f" ] && { echo "---- $f"; grep -nEi "^\s*(enable|batch|size|time|scheduleronly|safeshutdown|unanswered|endbeforehexten|initiatedseconds|table|dbname)\b" "$f" | grep -vi "password\|pass\b"; }
done
echo "   (batch=no o ausente = el CDR se escribe al colgar. batch=yes = puede tardar; WF9 espera hasta 150 s extra antes de usar la duración de ElevenLabs)"

echo; echo "===== 8. Opción C (solo GET, sin token): ¿LeadStudio publica un contrato que permita fijar durationSeconds después? ====="
for p in /api/openapi.json /openapi.json /api/docs /docs /api-docs /swagger.json /api/swagger.json /api/docs.json; do
  code=$(curl -s -o /tmp/ls_probe_body -m 15 -w '%{http_code} %{content_type}' "$LS_BASE$p")
  echo "GET $p -> $code"
  if echo "$code" | grep -q '^200.*json'; then
    cp /tmp/ls_probe_body "/root/probe_ls_openapi_$(echo $p | tr '/' '_').json"; echo "   guardado /root/probe_ls_openapi_$(echo $p | tr '/' '_').json"
    python3 - <<'PY'
import json
try:
    d = json.load(open('/tmp/ls_probe_body'))
    for path, ops in (d.get('paths') or {}).items():
        for m, spec in ops.items():
            if 'duration' in json.dumps(spec).lower(): print('   ⇒ %s %s menciona duration' % (m.upper(), path))
except Exception as e: print('   (no se pudo interpretar)', e)
PY
  fi
done
rm -f /tmp/ls_probe_body
echo "   Esta sonda NO ejecuta POST/PATCH/PUT contra LeadStudio. Un PATCH /api/followups/{id} con durationSeconds ya fue probado por vos: HTTP 400 'Nothing to update'."

echo; echo "===== 9. Worker de grabaciones (WF10): ¿reintenta el 503? ====="
W=/usr/local/bin/wf10-worker.py
if [ -f "$W" ]; then
  grep -nE "503|retry|reintent|sleep|timeout|backoff|MAX_|attempt|age|expire|drop|unlink|failed" "$W" | head -40
  echo "   Necesario: ante HTTP 503 debe dejar el archivo en cola y reintentar durante ≥ 15 min (la fila del followup ahora aparece 1-3 min después de colgar) y timeout HTTP ≥ 90 s."
else echo "   $W no existe (¿otro path?). Mandame el worker."; fi
echo; echo "FIN — sonda de solo lectura. Informe: $OUT"
} 2>&1 | python3 -c "$MASK_PY" | tee "$OUT"
