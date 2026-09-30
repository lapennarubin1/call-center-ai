#!/usr/bin/env bash
# =============================================================================
# PREFLIGHT_WF10_READONLY.sh — auditoría SOLO LECTURA de grabaciones (Asterisk + Stringee)
# No modifica nada: no escribe en MySQL, no toca el dialplan, no mueve archivos,
# no llama a LeadStudio. Sólo lee y resume. Enmascara contraseñas/tokens.
# Uso:  bash PREFLIGHT_WF10_READONLY.sh [minutos=180]   (salida también en /root/wf10_preflight_<fecha>.txt)
# Variables opcionales: MYSQL_CMD="mysql" DB=asterisk WORKER_URL=http://172.18.0.1:8091
# =============================================================================
set -uo pipefail
MIN="${1:-180}"
MYSQL_CMD="${MYSQL_CMD:-mysql}"
DB="${DB:-asterisk}"
WORKER_URL="${WORKER_URL:-http://172.18.0.1:8091}"
OUT="/root/wf10_preflight_$(date +%Y%m%d_%H%M%S).txt"
exec > >(tee "$OUT") 2>&1
MASK_PY=$(cat <<'PYEOF'
import re, sys
def mask(s):
    s = re.sub(r"(Bearer\s+)[A-Za-z0-9._~+/=-]+", r"\1***MASKED***", s, flags=re.I)
    s = re.sub(r"((pass(word)?|passwd|pwd|token|secret|api[_-]?key|authorization|xi-api-key)[\"' ]*[:=]\s*[\"']?)(?!Bearer\b)[^\"' ,;)\n]+", r"\1***MASKED***", s, flags=re.I)
    s = re.sub(r"(^|[\s\"'])-p[^\s\"']+", r"\1-p***MASKED***", s); s = re.sub(r"(--password[= ])\S+", r"\1***MASKED***", s, flags=re.I)
    s = re.sub(r"(MYSQL_PWD|PGPASSWORD)=\S+", r"\1=***MASKED***", s); s = re.sub(r"(://[^/:@\s]+:)[^@\s]+@", r"\1***MASKED***@", s)
    s = re.sub(r"(IDENTIFIED BY\s+)'[^']*'", r"\1'***MASKED***'", s, flags=re.I)
    return re.sub(r"(sk_|wsec_)[A-Za-z0-9]{8,}", r"\1***MASKED***", s)
for line in sys.stdin: sys.stdout.write(mask(line))
PYEOF
)
mask() { python3 -c "$MASK_PY"; }
sec() { echo; echo "================================================================"; echo "== $*"; echo "================================================================"; }
q() { $MYSQL_CMD -N -B -e "$1" "$DB" 2>&1; }
echo "PREFLIGHT WF10 (solo lectura) — $(date -Is) — host $(hostname) — ventana ${MIN} min"

sec "0. n8n (versión: WF10 se probó en n8n 1.123.82)"
if command -v docker >/dev/null; then
  for c in $(docker ps --format '{{.Names}}' 2>/dev/null | grep -i n8n); do echo "contenedor $c: imagen $(docker inspect -f '{{.Config.Image}}' "$c" 2>/dev/null) | n8n $(docker exec "$c" n8n --version 2>/dev/null | tail -1) | TZ=$(docker exec "$c" printenv GENERIC_TIMEZONE 2>/dev/null)"; done
fi
command -v n8n >/dev/null && echo "n8n local: $(n8n --version 2>/dev/null | tail -1)"
true

sec "1. ASTERISK"
if command -v asterisk >/dev/null && asterisk -rx "core show version" >/dev/null 2>&1; then
  asterisk -rx "core show version" | head -1
  asterisk -rx "core show uptime"
  asterisk -rx "core show channels count"
  echo "usuario del proceso: $(ps -o user= -C asterisk | head -1)"
  grep -E "^\s*systemname" /etc/asterisk/asterisk.conf 2>/dev/null || echo "systemname: (no definido; UNIQUEID = <epoch>.<n>)"
  systemctl show asterisk -p PrivateTmp 2>/dev/null || true
  command -v fwconsole >/dev/null && echo "⚠️  FreePBX detectado (fwconsole)" || echo "FreePBX: no"
  echo "--- MixMonitor: el 3.er argumento (command) se ejecuta al terminar la grabación:"
  asterisk -rx "core show application MixMonitor" | sed 's/\x1b\[[0-9;]*m//g' | grep -A1 "\[Syntax\]" | tail -1
  echo "--- contextos que llaman a wf10-enqueue (dialplan cargado, con archivo:línea):"
  asterisk -rx "dialplan show" > /tmp/.wf10_dp.$$ 2>/dev/null
  python3 - /tmp/.wf10_dp.$$ <<'PY'
import re, sys
txt = open(sys.argv[1], errors='replace').read().split('\n'); ctx = None; blocks = {}
for ln in txt:
    m = re.match(r"^\[ Context '([^']+)'", ln)
    if m: ctx = m.group(1); continue
    if ctx and re.match(r"^\s+('.+' =>\s+)?\d+\.", ln): blocks.setdefault(ctx, []).append(ln)
for c, lines in blocks.items():
    if any('wf10-enqueue' in l for l in lines):
        print(f"[{c}]"); [print('   ' + l.strip()) for l in lines if re.search(r'MixMonitor|Dial\(|System\(|StopMixMonitor|RECORDING|Hangup', l)]
        dials = [l for l in lines if 'Dial(' in l]
        for d in dials:
            m = re.search(r'Dial\(([^)]*)\)', d); opts = m.group(1).split(',')[2] if m and len(m.group(1).split(',')) > 2 else ''
            print(f"   -> opciones de Dial: '{opts}'" + ("  (incluye 'g': System corre si cuelga el cliente)" if 'g' in opts else "  (sin 'g': si contestan, System() después de Dial NO se ejecuta)"))
PY
  rm -f /tmp/.wf10_dp.$$
  grep -rn "wf10-mixmon-post" /etc/asterisk/*.conf 2>/dev/null | head -5 || true
else
  echo "Asterisk no responde a 'asterisk -rx' en este host"
fi

sec "2. SCRIPTS WF10 (contenido con secretos enmascarados)"
for f in /usr/local/bin/wf10-enqueue.py /usr/local/bin/wf10-worker.py /usr/local/bin/wf10-mixmon-post.sh /root/scripts/send_recordings.sh; do
  if [ -f "$f" ]; then echo "---- $f  ($(stat -c '%U:%G %a %y' "$f"))  sha256=$(sha256sum "$f" | cut -c1-16)"; mask < "$f"; else echo "---- $f: no existe"; fi
done
echo "--- ¿cómo trata el worker la respuesta del webhook? (líneas relevantes)"
grep -nE "post\(|urlopen|status_code|\.status|raise_for|retry|sleep|done|queue|failed|webhook|timeout" /usr/local/bin/wf10-worker.py 2>/dev/null | mask | head -40
echo "--- ¿qué manda el worker en el POST? (WF10 necesita el UNIQUEID o el nombre test-<UNIQUEID>.wav para el match exacto)"
grep -nE "filename|uniqueid|unique_id|audio_base64|'phone'|\"phone\"|json=|data=" /usr/local/bin/wf10-worker.py 2>/dev/null | mask | head -25

sec "3. WORKER systemd"
systemctl is-active wf10-recording-worker 2>&1; systemctl show wf10-recording-worker -p ExecStart -p User -p ActiveEnterTimestamp 2>/dev/null
journalctl -u wf10-recording-worker -n 40 --no-pager 2>/dev/null | mask

sec "4. COLA /var/spool/wf10 y log del enqueue"
for d in /var/spool/wf10/*/; do echo "$d: $(ls -1 "$d" 2>/dev/null | wc -l) archivos | más nuevo: $(ls -t "$d" 2>/dev/null | head -1) $(stat -c %y "$d$(ls -t "$d" 2>/dev/null | head -1)" 2>/dev/null)"; done
ls -la /var/spool/wf10 2>/dev/null
echo "--- ejemplo de JSON en done (enmascarado):"; f=$(ls -t /var/spool/wf10/done/* 2>/dev/null | head -1); [ -n "$f" ] && mask < "$f" | head -c 600; echo
echo "--- /var/log/asterisk/wf10-enqueue.log: $(stat -c '%s bytes, modificado %y' /var/log/asterisk/wf10-enqueue.log 2>/dev/null)"; tail -25 /var/log/asterisk/wf10-enqueue.log 2>/dev/null | mask
AU="$(ps -o user= -C asterisk | head -1 | tr -d ' ')"; AU="${AU:-asterisk}"
su -s /bin/sh "$AU" -c "test -w /var/spool/wf10/queue" 2>/dev/null && echo "$AU puede escribir la cola" || echo "⚠️  $AU NO puede escribir /var/spool/wf10/queue"
su -s /bin/sh "$AU" -c "test -x /usr/local/bin/wf10-enqueue.py" 2>/dev/null && echo "$AU puede ejecutar wf10-enqueue.py" || echo "⚠️  $AU NO puede ejecutar wf10-enqueue.py"

sec "5. WAV de Asterisk en /tmp (últimos ${MIN} min)"
find /tmp -maxdepth 1 -name 'test-*.wav' -mmin -"$MIN" -printf '%s\n' 2>/dev/null | awk '{t++; if ($1<=44) v++; else a++} END {printf "total=%d | con audio (>44 B)=%d | vacíos (44 B)=%d\n", t, a, v}'
echo "últimos con audio:"; find /tmp -maxdepth 1 -name 'test-*.wav' -mmin -"$MIN" -size +1k -printf '%TY-%Tm-%Td %TH:%TM  %8s  %f\n' 2>/dev/null | sort | tail -8

sec "6. CRON (productor viejo)"
crontab -l 2>/dev/null | grep -n "send_recordings" || echo "no hay línea con send_recordings"
[ -f /root/sent_recordings.txt ] && echo "/root/sent_recordings.txt: $(wc -l < /root/sent_recordings.txt) líneas" || echo "/root/sent_recordings.txt: no existe"
tail -5 /var/log/send_recordings.log 2>/dev/null | mask

sec "7. CDR (Asterisk) — contestadas vs encoladas"
CDRT=""; for t in asteriskcdrdb.cdr asterisk.cdr cdrdb.cdr; do q "SELECT 1 FROM $t LIMIT 1" >/dev/null 2>&1 && { CDRT=$t; break; }; done
if [ -n "$CDRT" ]; then
  echo "tabla CDR: $CDRT"
  q "SELECT disposition, COUNT(*) n, SUM(billsec>60) mas_60s FROM $CDRT WHERE calldate >= NOW() - INTERVAL $MIN MINUTE GROUP BY disposition"
else echo "no se encontró tabla CDR en MySQL (se omite)"; fi

sec "8. wf_call_followups (últimos ${MIN} min)"
q "SELECT provider, call_status, recording_synced, COUNT(*) n FROM wf_call_followups WHERE created_at >= NOW() - INTERVAL $MIN MINUTE GROUP BY provider, call_status, recording_synced ORDER BY provider, call_status"
echo "--- pendientes de grabación (recording_synced=0) con más de 15 min:"
q "SELECT provider, call_status, COUNT(*) FROM wf_call_followups WHERE COALESCE(recording_synced,0)=0 AND created_at <= NOW() - INTERVAL 15 MINUTE AND created_at >= NOW() - INTERVAL $MIN MINUTE AND followup_id <> 'undefined' GROUP BY provider, call_status"
echo "--- filas históricas 'undefined' (NO se reparan): $(q "SELECT COUNT(*) FROM wf_call_followups WHERE phone='undefined' OR lead_id='undefined' OR followup_id='undefined'")"

sec "9. STRINGEE: grabaciones del worker vs ledger vs followups"
python3 - "$WORKER_URL" "$MIN" "$MYSQL_CMD" "$DB" <<'PY'
import json, sys, time, subprocess, urllib.request, re
url, mins, mysql, db = sys.argv[1], int(sys.argv[2]), sys.argv[3], sys.argv[4]
def q(sql):
    r = subprocess.run(mysql.split() + ['-N', '-B', '-e', sql, db], capture_output=True, text=True)
    return [l.split('\t') for l in r.stdout.strip().split('\n') if l]
try: recs = json.load(urllib.request.urlopen(url + '/recordings', timeout=20)).get('recordings', [])
except Exception as e: print('no se pudo leer ' + url + '/recordings:', e); sys.exit(0)
ledger = {r[0] for r in q("SELECT filename FROM wf10_sent_recordings WHERE sent_at >= NOW() - INTERVAL 30 DAY")}
since = time.time() - mins * 60
win = [r for r in recs if (r.get('timestamp_ms') or 0) / 1000 >= since]
print(f"grabaciones en el worker: {len(recs)} | en la ventana: {len(win)} | de la ventana en ledger: {sum(1 for r in win if r['filename'] in ledger)}")
pend = [r for r in recs if r['filename'] not in ledger and (r.get('size_bytes') or 0) > 44 and (r.get('timestamp_ms') or 0) / 1000 >= time.time() - 10 * 86400]
own = {}
for r in pend[:600]:
    m = re.match(r'^stringee-(\d+)-(\d+)\.wav$', r['filename'])
    if not m: continue
    p10, t = m.group(1)[-10:], int(m.group(2)) // 1000
    age = '<=24h' if time.time() - t <= 86400 else '1-10d'
    row = q(f"SELECT COALESCE(recording_synced,0) FROM wf_call_followups WHERE provider='stringee' AND followup_id <> 'undefined' AND phone <> 'undefined' AND RIGHT(phone,10)='{p10}' AND created_at >= FROM_UNIXTIME({t}-120) AND created_at <= FROM_UNIXTIME({t}+5400) ORDER BY created_at, id LIMIT 1")
    k = (age, 'sin followup todavía' if not row else ('followup ya sincronizado (se omiten)' if row[0][0] == '1' else 'followup PENDIENTE'))
    own[k] = own.get(k, 0) + 1
print(f"fuera del ledger con audio (10 días): {len(pend)}")
for age in ('<=24h', '1-10d'):
    print(f"   {age}: " + ' | '.join(f"{k[1]}={v}" for k, v in sorted(own.items()) if k[0] == age))
print("   -> al publicar, WF10 NO sube estas pendientes anteriores (sin backfill): sólo grabaciones desde 30 min antes de su primer ciclo.")
print("   -> para subir el backlog (con tu aprobación): BACKLOG_FROM = '<fecha ISO>' en '🔄 Fetch & Convert Stringee Recordings' (máx. 10 días).")
# historia (sólo informativo, NO se reprocesa): grabaciones que el WF10 viejo puso en el ledger sin que su dueño quedara synced=1
lost = q("SELECT COUNT(*) FROM wf10_sent_recordings l WHERE l.sent_at >= NOW() - INTERVAL 10 DAY AND l.filename REGEXP '^stringee-[0-9]+-[0-9]+[.]wav$' AND EXISTS (SELECT 1 FROM wf_call_followups f WHERE f.provider='stringee' AND f.followup_id <> 'undefined' AND RIGHT(f.phone,10) = RIGHT(SUBSTRING_INDEX(SUBSTRING_INDEX(l.filename,'-',2),'-',-1),10) AND f.created_at BETWEEN FROM_UNIXTIME(SUBSTRING_INDEX(SUBSTRING_INDEX(l.filename,'-',-1),'.',1)/1000 - 120) AND FROM_UNIXTIME(SUBSTRING_INDEX(SUBSTRING_INDEX(l.filename,'-',-1),'.',1)/1000 + 5400) AND COALESCE(f.recording_synced,0)=0)")
print('ledger (10 días) con dueño todavía synced=0 = registradas por el WF10 viejo SIN PUT confirmado (NO se reprocesan sin aprobación):', lost[0][0] if lost else '?')
print('--- ledger por ciclo (últimas 6 h):')
for r in q("SELECT DATE_FORMAT(sent_at,'%Y-%m-%d %H:%i') m, COUNT(*) FROM wf10_sent_recordings WHERE sent_at >= NOW() - INTERVAL 6 HOUR GROUP BY m ORDER BY m DESC LIMIT 20"): print('  ', r[0], r[1])
PY

sec "10. wf_call_events (diagnóstico, sólo lectura): huecos Asterisk registrados por WF9 (7 días)"
q "SELECT resolution, COUNT(*) FROM wf_call_events WHERE resolution LIKE 'ASTERISK_GAP%' AND created_at >= NOW() - INTERVAL 7 DAY GROUP BY resolution" || true

sec "11. TIEMPOS: inicio de la llamada vs fila en wf_call_followups (valida las ventanas de WF10)"
python3 - "$MYSQL_CMD" "$DB" "$WORKER_URL" <<'PY'
# WF10 asigna la grabación a la PRIMERA fila del mismo proveedor+teléfono creada en [inicio-120 s, inicio+ventana]
# (Asterisk 900 s desde el UNIQUEID; Stringee 5400 s desde el timestamp del archivo). Acá se mide con datos reales.
import json, sys, glob, os, re, subprocess, urllib.request, statistics
mysql, db, wurl = sys.argv[1], sys.argv[2], sys.argv[3]
def q(sql):
    r = subprocess.run(mysql.split() + ['-N', '-B', '-e', sql, db], capture_output=True, text=True)
    return [l.split('\t') for l in r.stdout.strip().split('\n') if l]
def report(name, deltas, lo, hi, none=0):
    if not deltas: print(f"{name}: sin pares para medir (llamadas sin fila en ±1 h: {none})"); return
    inside = sum(1 for d in deltas if lo <= d <= hi)
    print(f"{name}: n={len(deltas)} | Δ(fila - inicio) min={min(deltas)}s mediana={int(statistics.median(deltas))}s max={max(deltas)}s | dentro de [{lo},{hi}] = {inside}/{len(deltas)} | sin fila en ±1 h: {none}")
    out = sorted(d for d in deltas if not lo <= d <= hi)[:10]
    if out: print(f"   ⚠️  fuera de la ventana de WF10 (primeros): {out}")
import csv, time
pairs, src = [], ''
# 1) CDR de Asterisk (contestadas de las últimas 24 h: uniqueid = inicio real de la llamada, dst = número)
for t in ('asteriskcdrdb.cdr', 'asterisk.cdr', 'cdrdb.cdr'):
    r = q(f"SELECT dst, uniqueid FROM {t} WHERE disposition='ANSWERED' AND calldate >= NOW() - INTERVAL 1 DAY ORDER BY calldate DESC LIMIT 200")
    if r: pairs = [(re.sub(r'\D', '', a)[-10:], int(float(u.split('-')[-1]))) for a, u in r if re.search(r'\d{9,11}\.\d+', u)]; src = t; break
if not pairs and os.path.exists('/var/log/asterisk/cdr-csv/Master.csv'):
    rows = list(csv.reader(open('/var/log/asterisk/cdr-csv/Master.csv', errors='replace')))[-3000:]
    for r in rows:
        if len(r) > 16 and r[14] == 'ANSWERED' and re.search(r'\d{9,11}\.\d+', r[16]):
            u = int(re.search(r'(\d{9,11})\.\d+', r[16]).group(1))
            if u >= time.time() - 86400: pairs.append((re.sub(r'\D', '', r[2])[-10:], u))
    src = 'Master.csv' if pairs else ''
# 2) respaldo: cola/done del worker
for f in ([] if pairs else sorted(glob.glob('/var/spool/wf10/done/*') + glob.glob('/var/spool/wf10/queue/*'), key=os.path.getmtime)[-60:]):
    try: j = json.load(open(f))
    except Exception: continue
    s = json.dumps(j); m = re.search(r'(\d{9,11})\.\d+', s); ph = re.sub(r'\D', '', str(j.get('phone') or ''))[-10:]
    if m and len(ph) == 10: pairs.append((ph, int(m.group(1))))
d, none = [], 0
for ph, t in pairs:   # con el ciclo 3x3 (>= 2 h entre llamadas al mismo número) la fila en [-10 min, +1 h] es la de ESA llamada
    r = q(f"SELECT CAST(UNIX_TIMESTAMP(created_at) - {t} AS SIGNED) FROM wf_call_followups WHERE provider='asterisk' AND followup_id <> 'undefined' AND RIGHT(phone,10)='{ph}' AND created_at BETWEEN FROM_UNIXTIME({t} - 600) AND FROM_UNIXTIME({t} + 3600) ORDER BY created_at LIMIT 1")
    if r: d.append(int(r[0][0]))
    else: none += 1
report(f"ASTERISK ({src or 'cola/done'}: UNIQUEID vs fila de WF2)", d, -120, 900, none)
try: recs = json.load(urllib.request.urlopen(wurl + '/recordings', timeout=20)).get('recordings', [])
except Exception as e: recs = []; print('worker Stringee no disponible:', e)
d, none = [], 0
for r in sorted(recs, key=lambda r: r.get('timestamp_ms') or 0)[-80:]:
    m = re.match(r'^stringee-(\d+)-(\d+)\.wav$', r.get('filename', ''))
    if not m: continue
    ph, t = m.group(1)[-10:], int(m.group(2)) // 1000
    x = q(f"SELECT CAST(UNIX_TIMESTAMP(created_at) - {t} AS SIGNED) FROM wf_call_followups WHERE provider='stringee' AND followup_id <> 'undefined' AND RIGHT(phone,10)='{ph}' AND created_at BETWEEN FROM_UNIXTIME({t} - 600) AND FROM_UNIXTIME({t} + 7200) ORDER BY created_at LIMIT 1")
    if x: d.append(int(x[0][0]))
    else: none += 1
report('STRINGEE (timestamp del archivo vs fila de WF9)', d, -120, 5400, none)
print("Si algún Δ cae fuera de la ventana, avisame ANTES de publicar WF10: la ventana se ajusta en 2 constantes.")
PY

echo; echo "Informe guardado en $OUT (no contiene contraseñas: se enmascaran)."
