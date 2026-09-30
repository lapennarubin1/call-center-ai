#!/usr/bin/env bash
# =============================================================================
# VALIDATE_WF10_AFTER_DEPLOY.sh — validación SOLO LECTURA después del deploy de WF10
#
# No modifica nada: no escribe en MySQL, no toca el dialplan, la cola ni el cron,
# no llama a LeadStudio y no reenvía nada al webhook. Sólo lee y cruza datos.
#
# Recorre cada llamada NUEVA (desde el deploy) y muestra en qué punto de la cadena está:
#   ASTERISK: WAV con audio -> post-proceso MixMonitor -> cola/done -> worker -> WF10
#             -> PUT LeadStudio -> wf_call_followups.recording_synced 0->1
#   STRINGEE: grabación en el worker -> fila dueña en wf_call_followups -> PUT
#             -> recording_synced 0->1 -> ledger wf10_sent_recordings (varias por ciclo)
#
# USO (como root en el VPS):
#   bash VALIDATE_WF10_AFTER_DEPLOY.sh                    # desde el último apply de ASTERISK_WF10_FIX.sh (o últimos 120 min)
#   bash VALIDATE_WF10_AFTER_DEPLOY.sh --minutes 90
#   bash VALIDATE_WF10_AFTER_DEPLOY.sh --since '2026-09-29 14:05'     (hora local del VPS)
#
# Opcional (recomendado): API pública de n8n para ver cada ejecución de WF10
#   N8N_URL=http://127.0.0.1:5678 N8N_API_KEY='...' bash VALIDATE_WF10_AFTER_DEPLOY.sh
#   (n8n: Settings -> n8n API -> Create API key. La clave no se imprime ni se guarda.)
#
# Variables: MYSQL_CMD="mysql" DB=asterisk WORKER_URL=http://172.18.0.1:8091
#            GRACE_MIN=10 (Asterisk) STRINGEE_GRACE_MIN=15 WF10_ID=TW1CHksqyf66MjQz
# Salida: también en /root/wf10_validate_<fecha>.txt. Código de salida: 0 = sin FAIL, 1 = hay FAIL.
# =============================================================================
set -uo pipefail
SINCE_EPOCH=""; SINCE_SRC=""
while [ $# -gt 0 ]; do
  case "$1" in
    --minutes) SINCE_EPOCH=$(( $(date +%s) - ${2:?falta N} * 60 )); SINCE_SRC="últimos $2 min"; shift 2;;
    --since)   SINCE_EPOCH=$(date -d "${2:?falta fecha}" +%s) || { echo "fecha inválida: $2"; exit 2; }; SINCE_SRC="--since $2"; shift 2;;
    -h|--help) sed -n 2,27p "$0"; exit 0;;
    *) echo "argumento desconocido: $1 (usar --minutes N o --since 'YYYY-MM-DD HH:MM')"; exit 2;;
  esac
done
if [ -z "$SINCE_EPOCH" ]; then
  for d in $(ls -1d /root/wf10-fix-backups/*/ 2>/dev/null | sort -r); do
    d="${d%/}"; if [ -f "$d/applied_at" ] && [ ! -f "$d/rolled_back" ]; then
      SINCE_EPOCH=$(date -d "$(cat "$d/applied_at")" +%s); SINCE_SRC="apply de ASTERISK_WF10_FIX ($d)"; break; fi
  done
fi
[ -z "$SINCE_EPOCH" ] && { SINCE_EPOCH=$(( $(date +%s) - 7200 )); SINCE_SRC="últimos 120 min (no hay apply registrado)"; }

OUT="/root/wf10_validate_$(date +%Y%m%d_%H%M%S).txt"
exec > >(tee "$OUT") 2>&1
export V_SINCE="$SINCE_EPOCH" V_SINCE_SRC="$SINCE_SRC"
python3 - <<'PY'
import os, sys, re, json, time, glob, subprocess, urllib.request, urllib.parse, datetime as dt

SINCE = float(os.environ['V_SINCE']); NOW = time.time()
MYSQL = os.environ.get('MYSQL_CMD', 'mysql').split(); DB = os.environ.get('DB', 'asterisk')
WORKER_URL = os.environ.get('WORKER_URL', 'http://172.18.0.1:8091').rstrip('/')
N8N_URL = os.environ.get('N8N_URL', 'http://127.0.0.1:5678').rstrip('/'); N8N_KEY = os.environ.get('N8N_API_KEY', '')
WF_ID = os.environ.get('WF10_ID', 'TW1CHksqyf66MjQz'); HOOK_PATH = os.environ.get('WEBHOOK_PATH', 'send-recording')
GRACE = int(os.environ.get('GRACE_MIN', '10')) * 60; SGRACE = int(os.environ.get('STRINGEE_GRACE_MIN', '15')) * 60
ENQ_LOG = os.environ.get('ENQ_LOG', '/var/log/asterisk/wf10-enqueue.log'); SPOOL = os.environ.get('SPOOL', '/var/spool/wf10')
WAV_GLOB = os.environ.get('WAV_GLOB', '/tmp/test-*.wav'); POST = '/usr/local/bin/wf10-mixmon-post.sh'
WORKER_UNIT = os.environ.get('WORKER_UNIT', 'wf10-recording-worker')
# MISMO criterio que WF10 (src/_sql.js FCF_OK): se ignoran las filas históricas 'undefined'
FCF_OK = ("CHAR_LENGTH(f.followup_id) = 36 AND f.followup_id REGEXP '^[0-9A-Fa-f]{8}-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{12}' "
          "AND f.phone <> 'undefined' AND f.lead_id <> 'undefined'")
AST_WIN, STR_WIN = 900, 5400   # ventanas del dueño usadas por WF10 (desde el inicio de la llamada)
N_EVAL_S, N_DEC_S, N_EVAL_A, N_DEC_A = '🧾 Evaluar PUT (Stringee)', '🧮 Decidir y Descargar (Stringee)', '🧾 Evaluar PUT (Asterisk)', '🧮 Decidir Match (Asterisk)'

RES = []
def res(area, name, status, detail=''):
    RES.append((area, name, status, detail))
    print(f"  [{status:4}] {name}" + (f" — {detail}" if detail else ''))
def hdr(t): print('\n' + '=' * 78 + '\n== ' + t + '\n' + '=' * 78)
def fmt(ts): return dt.datetime.fromtimestamp(ts).strftime('%m-%d %H:%M:%S') if ts else '-'
def mask(s):
    s = re.sub(r"(Bearer\s+)[A-Za-z0-9._~+/=-]+", r"\1***", s, flags=re.I)
    s = re.sub(r"((pass(word)?|passwd|pwd|token|secret|api[_-]?key|authorization|xi-api-key)[\"' ]*[:=]\s*[\"']?)(?!Bearer\b)[^\"' ,;)]+", r"\1***", s, flags=re.I)
    s = re.sub(r"(^|[\s\"'])-p[^\s\"']+", r"\1-p***", s); s = re.sub(r"(--password[= ])\S+", r"\1***", s, flags=re.I)
    s = re.sub(r"(MYSQL_PWD|PGPASSWORD)=\S+", r"\1=***", s); s = re.sub(r"(://[^/:@\s]+:)[^@\s]+@", r"\1***@", s)
    return re.sub(r"(sk_|wsec_)[A-Za-z0-9]{8,}", r"\1***", s)
class DBErr(Exception): pass
def q(sql):
    r = subprocess.run(MYSQL + ['-N', '-B', '-e', sql, DB], capture_output=True, text=True)
    if r.returncode != 0: raise DBErr(r.stderr.strip()[:300])
    return [l.split('\t') for l in r.stdout.split('\n') if l != '']
def digits(s): return re.sub(r'\D', '', str(s or ''))
def owner(provider, p10, t_s, win):
    r = q(f"SELECT f.followup_id, COALESCE(f.recording_synced,0), UNIX_TIMESTAMP(f.created_at), COALESCE(f.outcome,''), COALESCE(f.call_status,'') "
          f"FROM wf_call_followups f WHERE f.provider = '{provider}' AND {FCF_OK} AND RIGHT(f.phone,10) = '{p10}' "
          f"AND f.created_at >= FROM_UNIXTIME({int(t_s)} - 120) AND f.created_at <= FROM_UNIXTIME({int(t_s)} + {win}) ORDER BY f.created_at ASC, f.id ASC LIMIT 1")
    if not r: return None
    fid, syn, cr, oc, cs = r[0]
    return {'followup_id': fid, 'synced': int(syn), 'created': float(cr), 'outcome': oc, 'call_status': cs}
def nearest(p10, t_s):
    r = q(f"SELECT provider, LEFT(followup_id,8), CAST(UNIX_TIMESTAMP(created_at) - {int(t_s)} AS SIGNED), COALESCE(recording_synced,0), COALESCE(outcome,'') FROM wf_call_followups "
          f"WHERE RIGHT(phone,10) = '{p10}' AND created_at BETWEEN FROM_UNIXTIME({int(t_s)} - 86400) AND FROM_UNIXTIME({int(t_s)} + 86400) "
          f"ORDER BY ABS(UNIX_TIMESTAMP(created_at) - {int(t_s)}) LIMIT 3")
    return '; '.join(f"{p}:{f}… Δ={int(d):+d}s synced={s} {o}" for p, f, d, s, o in r) or 'ninguna fila ±24 h para ese teléfono'

print(f"VALIDATE WF10 (solo lectura) — {dt.datetime.now().isoformat(timespec='seconds')} — host {os.uname().nodename}")
print(f"Ventana: desde {dt.datetime.fromtimestamp(SINCE).strftime('%Y-%m-%d %H:%M:%S')} ({os.environ.get('V_SINCE_SRC')}) | gracia Asterisk {GRACE//60} min, Stringee {SGRACE//60} min")
try: q('SELECT 1')
except DBErr as e: print('❌ no se pudo consultar MySQL:', e); sys.exit(1)

# ---------------------------------------------------------------------------------
# n8n (opcional): ejecuciones reales de WF10 -> decisiones y PUT exactos por ejecución
# ---------------------------------------------------------------------------------
N8N = {'ok': False, 'str_execs': [], 'errors': [], 'deployed_at': None}
def api(path):
    req = urllib.request.Request(N8N_URL + '/api/v1' + path, headers={'X-N8N-API-KEY': N8N_KEY, 'Accept': 'application/json'})
    return json.load(urllib.request.urlopen(req, timeout=60))
def node_items(rd, name):
    out = []
    for run in rd.get(name) or []:
        for branch in ((run.get('data') or {}).get('main') or []):
            out += [i.get('json') or {} for i in (branch or [])]
    return out
def ts_iso(s):
    try: return dt.datetime.fromisoformat(str(s).replace('Z', '+00:00')).timestamp()
    except Exception: return None

hdr('0. n8n — WF10 publicado y ejecuciones (API pública, opcional)')
if not N8N_KEY:
    res('n8n', 'API de n8n', 'INFO', 'sin N8N_API_KEY: se omite (el resto de la validación no la necesita)')
else:
    try:
        wf = api('/workflows/' + WF_ID)
        names = {n.get('name') for n in wf.get('nodes', [])}
        N8N['deployed_at'] = ts_iso(wf.get('updatedAt'))
        res('n8n', f"WF10 {WF_ID} activo", 'PASS' if wf.get('active') else 'FAIL', f"'{wf.get('name')}' actualizado {wf.get('updatedAt')}")
        final = {N_EVAL_S, N_DEC_S, N_EVAL_A, N_DEC_A} <= names
        res('n8n', 'la versión publicada es WF10_FINAL (nodos Decidir/Evaluar presentes)', 'PASS' if final else 'FAIL',
            '' if final else 'faltan: ' + ', '.join(sorted({N_EVAL_S, N_DEC_S, N_EVAL_A, N_DEC_A} - names)))
        hooks, cursor = [], None
        for _ in range(20):
            page = api('/workflows?active=true&limit=100' + (f'&cursor={urllib.parse.quote(cursor)}' if cursor else ''))
            for w in page.get('data', []):
                for n in w.get('nodes', []):
                    if n.get('type') == 'n8n-nodes-base.webhook' and str((n.get('parameters') or {}).get('path', '')).strip('/') == HOOK_PATH:
                        hooks.append(f"{w['id']} '{w.get('name')}'")
                if w['id'] != WF_ID and any('Stringee Recordings' in (n.get('name') or '') for n in w.get('nodes', [])):
                    hooks.append(f"{w['id']} '{w.get('name')}' (otra copia con la rama Stringee activa)")
            cursor = page.get('nextCursor')
            if not cursor: break
        res('n8n', f"una sola copia activa con /webhook/{HOOK_PATH} y rama Stringee", 'PASS' if hooks == [f"{WF_ID} '{wf.get('name')}'"] else 'FAIL', ' | '.join(hooks))
        cursor, execs = None, []
        for _ in range(40):   # lista SIN datos (las ejecuciones del webhook traen el audio en base64: MB cada una)
            page = api(f'/executions?workflowId={WF_ID}&includeData=false&limit=100' + (f'&cursor={urllib.parse.quote(cursor)}' if cursor else ''))
            data = page.get('data', [])
            execs += [e for e in data if (ts_iso(e.get('startedAt')) or 0) >= SINCE]
            cursor = page.get('nextCursor')
            if not cursor or not data or (ts_iso(data[-1].get('startedAt')) or 0) < SINCE: break
        N8N['ok'] = True
        wh = [e for e in execs if e.get('mode') == 'webhook']; cyc = [e for e in execs if e.get('mode') != 'webhook']
        for e in execs:
            if e.get('status') in ('error', 'crashed'):
                N8N['errors'].append(f"#{e.get('id')} {fmt(ts_iso(e.get('startedAt')))} {e.get('mode')} {e.get('status')}")
        # detalle sólo de los ciclos Stringee (livianos: el audio va como binario aparte), máx. 60
        for e in sorted(cyc, key=lambda e: e.get('startedAt') or '')[-60:]:
            try: d = api(f"/executions/{e['id']}?includeData=true")
            except Exception: continue
            rd = (((d.get('data') or {}).get('resultData') or {}).get('runData')) or {}
            err = (((d.get('data') or {}).get('resultData') or {}).get('error')) or None
            if not any('Schedule' in k for k in rd): continue
            if err:
                nn = err.get('node', {}).get('name', '?') if isinstance(err.get('node'), dict) else '?'
                N8N['errors'] = [x if not x.startswith(f"#{e.get('id')} ") else x + f": {mask(str(err.get('message', '')))[:160]} (nodo: {nn})" for x in N8N['errors']]
            dec = node_items(rd, N_DEC_S); ev = node_items(rd, N_EVAL_S)
            N8N['str_execs'].append({'id': e.get('id'), 't': ts_iso(e.get('startedAt')), 'status': e.get('status'),
                'attach': sum(1 for x in dec if x.get('decision') == 'attach'),
                'ok': sum(1 for x in ev if x.get('uploaded') is True), 'fail': sum(1 for x in ev if x.get('uploaded') is False),
                'fids': [x.get('followup_id') for x in ev if x.get('uploaded') is True]})
        wst = {}
        for e in wh: wst[e.get('status')] = wst.get(e.get('status'), 0) + 1
        print(f"  ejecuciones de WF10 en la ventana: {len(execs)} | webhook Asterisk: {len(wh)} {wst} | ciclos Stringee: {len(N8N['str_execs'])}")
        res('n8n', 'ejecuciones de WF10 sin error', 'PASS' if not N8N['errors'] else 'FAIL', f"{len(N8N['errors'])} con error (abrirlas en n8n -> Executions)" if N8N['errors'] else '')
        for x in N8N['errors'][:10]: print('      ' + x)
    except Exception as ex:
        res('n8n', 'API de n8n', 'WARN', f'no se pudo consultar {N8N_URL}: {str(ex)[:160]}')

# ---------------------------------------------------------------------------------
# 1. ASTERISK: estado del mecanismo durable
# ---------------------------------------------------------------------------------
hdr('1. ASTERISK — mecanismo durable (post-proceso de MixMonitor) y productor viejo')
try:
    dp = subprocess.run(['asterisk', '-rx', 'dialplan show'], capture_output=True, text=True, timeout=30).stdout
    ctx, blocks = None, {}
    for ln in dp.split('\n'):
        m = re.match(r"^\[ Context '([^']+)'", ln)
        if m: ctx = m.group(1); continue
        if ctx: blocks.setdefault(ctx, []).append(ln)
    wf = {c: ls for c, ls in blocks.items() if any('wf10-enqueue' in l for l in ls)}
    fixed = [c for c, ls in wf.items() if any('MixMonitor(' in l and 'wf10-mixmon-post' in l for l in ls)]
    res('asterisk', 'contextos WF10 con MixMonitor + post-proceso', 'PASS' if wf and len(fixed) == len(wf) else 'FAIL',
        f"{len(fixed)}/{len(wf)}: " + ', '.join(f"{c}{'' if c in fixed else ' (SIN FIX)'}" for c in wf))
except Exception as ex:
    res('asterisk', 'dialplan', 'WARN', f"no se pudo leer 'dialplan show': {ex}")
res('asterisk', f'{POST} instalado y ejecutable', 'PASS' if os.access(POST, os.X_OK) else 'FAIL')
try: cron = subprocess.run(['crontab', '-l'], capture_output=True, text=True).stdout.split('\n')
except Exception: cron = []
act = [l for l in cron if 'send_recordings.sh' in l and not l.lstrip().startswith('#')]
res('asterisk', 'cron viejo send_recordings.sh', 'INFO', 'ACTIVO (normal hasta finalize; si sube una llamada antes que el worker, WF10 responde not_pending y no duplica)' if act else 'retirado (comentado)')

# log del post-proceso
calls, noaudio, rcerr = {}, 0, []
try: log = open(ENQ_LOG, encoding='utf-8', errors='replace').read().split('\n')
except Exception: log = []; res('asterisk', ENQ_LOG, 'WARN', 'no se pudo leer')
for ln in log:
    m = re.match(r'^(\d{4}-\d\d-\d\dT\d\d:\d\d:\d\dZ) wf10-mixmon-post (?:args=\[(.*?)\] size=(\d+) -> (.*)|enqueue rc=(\d+) args=\[(.*?)\])$', ln)
    if not m: continue
    t = dt.datetime.strptime(m.group(1), '%Y-%m-%dT%H:%M:%SZ').replace(tzinfo=dt.timezone.utc).timestamp()
    if t < SINCE: continue
    # args = los mismos del System(wf10-enqueue.py ...): teléfono, WAV, UNIQUEID, ANSWER (en el orden del dialplan)
    args = (m.group(2) if m.group(2) is not None else m.group(6)).split()
    wav = next((a for a in args if a.lower().endswith('.wav')), '')
    uids = [re.search(r'\d{9,11}\.\d+', a).group(0) for a in args + [wav] if re.search(r'\d{9,11}\.\d+', a)]
    uid = uids[0] if uids else None
    phone = next((a for a in args if a != wav and a != 'ANSWER' and not re.search(r'\d{9,11}\.\d+', a) and len(digits(a)) >= 6), '')
    if m.group(5):
        rcerr.append(f"{fmt(t)} rc={m.group(5)} uid={uid}"); calls.setdefault(uid, {}).update(rc=m.group(5)); continue
    if 'no se encola' in m.group(4): noaudio += 1; continue
    calls.setdefault(uid, {}).update(t=t, phone=phone, wav=wav, size=int(m.group(3)))
calls = {u: c for u, c in calls.items() if u and 't' in c}
print(f"  post-proceso desde la ventana: con audio -> enqueue = {len(calls)} | sin audio (no contestadas) = {noaudio} | enqueue con error = {len(rcerr)}")
res('asterisk', 'wf10-enqueue.py sin errores al ser llamado por el post-proceso', 'PASS' if not rcerr else 'FAIL', '; '.join(rcerr[:5]))

# WAV con audio de llamadas iniciadas en la ventana que NO pasaron por el post-proceso
miss = []
for f in glob.glob(WAV_GLOB):
    try: st = os.stat(f)
    except Exception: continue
    mm = re.search(r'(\d{9,11})\.(\d+)', os.path.basename(f))
    if not mm or st.st_size <= 44 or int(mm.group(1)) < SINCE or st.st_mtime > NOW - 120: continue
    if mm.group(0) not in calls: miss.append(f"{os.path.basename(f)} ({st.st_size} B, {fmt(st.st_mtime)})")
res('asterisk', 'toda contestada (WAV > 44 B) nueva pasó por el post-proceso', 'PASS' if not miss else 'FAIL',
    f"{len(miss)} sin línea en {ENQ_LOG}: " + ', '.join(miss[:6]) if miss else '')

# cola / done: índice por UNIQUEID (nombre o contenido), sin depender del formato del JSON
spool = {}
for sub in ('queue', 'done', 'failed', 'error'):
    for f in glob.glob(os.path.join(SPOOL, sub, '*')):
        try:
            st = os.stat(f)
            if st.st_mtime < SINCE - 3600: continue
            txt = os.path.basename(f) + ' ' + (open(f, errors='replace').read(4000) if st.st_size < 2_000_000 else '')
        except Exception: continue
        for u in set(re.findall(r'\d{9,11}\.\d+', txt)): spool[u] = (sub, st.st_mtime)
qfiles = [(f, os.stat(f).st_mtime) for f in glob.glob(os.path.join(SPOOL, 'queue', '*')) if os.path.isfile(f)]
old_q = [os.path.basename(f) for f, mt in qfiles if mt < NOW - GRACE]
res('asterisk', f'cola sin atascos (> {GRACE//60} min)', 'PASS' if not old_q else 'FAIL', f"{len(qfiles)} en cola, {len(old_q)} viejas: {', '.join(old_q[:5])}" if old_q else f"{len(qfiles)} en cola")
jr = []
try: jr = subprocess.run(['journalctl', '-u', WORKER_UNIT, '--since', '@%d' % int(SINCE - 60), '--no-pager', '-o', 'short-iso'], capture_output=True, text=True, timeout=60).stdout.split('\n')
except Exception: pass

hdr(f'2. ASTERISK — cada llamada contestada nueva (WAV -> cola -> worker -> WF10 -> PUT -> recording_synced)')
cnt = {'OK': 0, 'PEND': 0, 'FAIL': 0, 'WARN': 0}
print(f"  {'inicio':14} {'uniqueid':18} {'teléfono':14} {'KB':>5} {'cola':6} {'followup':10} {'Δfila':>6} {'synced':6} {'worker':8} estado")
for uid, c in sorted(calls.items(), key=lambda kv: kv[1]['t']):
    p10 = digits(c['phone'])[-10:]; t0 = int(uid.split('.')[0]); age = NOW - c['t']
    where = spool.get(uid, (None,))[0]
    o = owner('asterisk', p10, t0, AST_WIN) if p10 else None
    wl = [l for l in jr if uid in l or (c['wav'] and os.path.basename(c['wav']) in l)]
    hc = re.findall(r'\b(?:HTTP|status|code)[ =:]*([1-5]\d\d)\b', wl[-1]) if wl else []
    n8s = ('HTTP ' + hc[-1]) if hc else ('log' if wl else '-')
    if o and o['synced'] == 1 and o['outcome'].upper() == 'CONNECTED': st, why = 'OK', ''
    elif o and o['synced'] == 1: st, why = 'WARN', f"el dueño es {o['outcome'] or '?'} según WF2 (synced=1 de origen): hay audio pero no se sube"
    elif age < GRACE: st, why = 'PEND', f'hace {int(age)} s'
    elif not where: st, why = 'FAIL', 'no llegó a la cola (ver enqueue.log)'
    elif where == 'queue': st, why = 'FAIL', f'atascada en la cola > {GRACE//60} min'
    elif where in ('failed', 'error'): st, why = 'FAIL', f'el worker la movió a {where}/'
    elif not o: st, why = 'FAIL', 'sin fila asterisk dueña (teléfono + hora de inicio): ' + nearest(p10, t0)
    else: st, why = 'FAIL', 'en done/ pero recording_synced=0 (ver worker y ejecución n8n)'
    cnt[st] += 1
    print(f"  {fmt(t0):14} {uid:18} {c['phone'][-14:]:14} {c['size']//1024:5} {where or '-':6} {(o['followup_id'][:8] + '…') if o else '-':10} "
          f"{(str(int(o['created'] - t0)) + 's') if o else '-':>6} {str(o['synced']) if o else '-':6} {n8s:8} {st} {why}")
    if st == 'FAIL' and wl: print('        worker: ' + mask(wl[-1])[:200])
if not calls: print('  (todavía no hay contestadas nuevas desde la ventana: hacé llamadas reales y volvé a correr)')
res('asterisk', 'contestadas nuevas con grabación en el CRM (recording_synced=1)', 'FAIL' if cnt['FAIL'] else ('PEND' if not cnt['OK'] else 'PASS'),
    f"OK={cnt['OK']} pendientes<{GRACE//60}min={cnt['PEND']} FAIL={cnt['FAIL']} avisos={cnt['WARN']}")

# al revés: filas CONNECTED de WF2 sin grabación
rows = q(f"SELECT f.followup_id, RIGHT(f.phone,10), UNIX_TIMESTAMP(f.created_at), COALESCE(f.recording_synced,0) FROM wf_call_followups f WHERE f.provider='asterisk' AND {FCF_OK} "
         f"AND UPPER(COALESCE(f.outcome,''))='CONNECTED' AND f.created_at >= FROM_UNIXTIME({int(SINCE)}) ORDER BY f.created_at")
tot, syn = len(rows), sum(1 for r in rows if r[3] == '1')
orph = []  # (texto, creada_en)
for fid, p10, cr, s in rows:
    cr = float(cr)
    if s == '1' or cr > NOW - GRACE: continue
    has = [u for u, c in calls.items() if digits(c['phone'])[-10:] == p10 and cr - AST_WIN <= int(u.split('.')[0]) <= cr + 120]
    orph.append((f"{fid[:8]}… {p10} fila {fmt(cr)}" + (' (hay WAV encolado: ver tabla)' if has else ' (sin WAV encolado)'), cr))
print(f"  wf_call_followups asterisk CONNECTED desde la ventana: {tot} | recording_synced=1: {syn} | pendientes > {GRACE//60} min: {len(orph)}")
early = [o for o in orph if o[1] < SINCE + AST_WIN]   # llamadas que pudieron empezar ANTES del apply (no cubiertas por el post-proceso)
res('asterisk', 'filas CONNECTED de WF2 con su grabación', 'PASS' if not orph else ('WARN' if len(early) == len(orph) else 'FAIL'),
    ('; '.join(o[0] for o in orph[:6]) + (' — las creadas < 15 min después del apply pueden ser llamadas en curso durante el apply' if early else '')) if orph else f"{syn}/{tot}")

# ---------------------------------------------------------------------------------
# 3. STRINGEE
# ---------------------------------------------------------------------------------
hdr('3. STRINGEE — grabaciones nuevas del worker -> followup dueño -> PUT -> recording_synced -> ledger')
try:
    recs = json.load(urllib.request.urlopen(WORKER_URL + '/recordings', timeout=30)).get('recordings', [])
    ok_worker = True
except Exception as ex:
    recs, ok_worker = [], False; res('stringee', f'{WORKER_URL}/recordings', 'FAIL', str(ex)[:160])
SS = max(SINCE, N8N['deployed_at'] or 0)  # para "varias por ciclo": sólo ciclos del WF10 nuevo
cand = []
for r in recs:
    m = re.match(r'^stringee-(\d{8,15})-(\d{12,14})\.wav$', str(r.get('filename', '')))
    if not m: continue
    t = int(m.group(2)) / 1000
    if t < SINCE: continue
    cand.append({'fn': r['filename'], 'p10': m.group(1)[-10:], 't': t, 'size': int(r.get('size_bytes') or 0)})
led = {}
if cand:
    for i in range(0, len(cand), 200):
        names = ','.join("'" + c['fn'].replace("'", '') + "'" for c in cand[i:i + 200])
        for fn, sa, secs in q(f"SELECT filename, UNIX_TIMESTAMP(sent_at), duration_secs FROM wf10_sent_recordings WHERE filename IN ({names})"): led[fn] = float(sa)
scnt = {}
print(f"  {'grabación':44} {'KB':>5} {'followup':10} {'Δfila':>6} {'synced':6} {'ledger':14} estado")
for c in sorted(cand, key=lambda c: c['t']):
    age = NOW - c['t']; o = owner('stringee', c['p10'], c['t'], STR_WIN) if c['size'] > 44 else None
    c['owner'] = o; c['sent'] = led.get(c['fn']); c['ready'] = max(c['t'], o['created']) if o else None
    if c['size'] <= 44: st, why = 'IGN', 'WAV vacío (44 B): WF10 no lo sube'
    elif c['sent'] and o and o['synced'] == 1: st, why = 'OK', ''
    elif c['sent']: st, why = 'FAIL', 'en ledger SIN recording_synced=1 en su dueño (ledger escrito sin PUT confirmado)' if o else 'en ledger SIN followup dueño'
    elif o and o['synced'] == 0 and age > SGRACE and (NOW - o['created']) > SGRACE: st, why = 'FAIL', f'dueño pendiente hace > {SGRACE//60} min y no se subió'
    elif o and o['synced'] == 1: st, why = 'OMIT', 'dueño ya sincronizado (otro archivo de la misma llamada, o WF9 no esperaba grabación): sin PUT y sin ledger'
    elif not o and age > 7200: st, why = 'WARN', 'sin fila stringee dueña después de 2 h: ' + nearest(c['p10'], c['t'])
    else: st, why = 'PEND', 'esperando fila de WF9' if not o else 'esperando el próximo ciclo'
    scnt[st] = scnt.get(st, 0) + 1
    print(f"  {c['fn'][:44]:44} {c['size']//1024:5} {(o['followup_id'][:8] + '…') if o else '-':10} {(str(int(o['created'] - c['t'])) + 's') if o else '-':>6} "
          f"{str(o['synced']) if o else '-':6} {fmt(c['sent']):14} {st} {why}")
if ok_worker and not cand: print('  (no hay grabaciones Stringee nuevas desde la ventana)')
res('stringee', 'grabaciones contestadas nuevas subidas al followup correcto (synced=1 + ledger)', 'FAIL' if scnt.get('FAIL') else ('PASS' if scnt.get('OK') else 'PEND'),
    ' '.join(f"{k}={v}" for k, v in sorted(scnt.items())))
# dos grabaciones distintas en ledger para el mismo dueño (es normal sólo si son 2 archivos de la misma llamada)
by_owner = {}
for c in cand:
    if c.get('sent') and c.get('owner'): by_owner.setdefault(c['owner']['followup_id'], []).append(c['fn'])
multi = {k: v for k, v in by_owner.items() if len(v) > 1}
res('stringee', 'un followup recibe una sola grabación (ledger = PUT confirmados)', 'WARN' if multi else 'PASS',
    '; '.join(f"{k[:8]}…: {len(v)} archivos subidos: {', '.join(v)}" for k, v in list(multi.items())[:4]) if multi else '')
if scnt.get('OMIT'): print(f"  ℹ️  {scnt['OMIT']} omitida(s) a propósito (dueño ya sincronizado). Si son contestadas reales que WF9 marcó 'sin grabación', revisá la clasificación en WF9 (no se suben sin aprobación).")

# ledger por ciclo (agrupando escrituras a < 40 s entre sí)
lrows = q(f"SELECT UNIX_TIMESTAMP(sent_at), filename FROM wf10_sent_recordings WHERE sent_at >= FROM_UNIXTIME({int(SS)}) ORDER BY sent_at")
cycles = []
for sa, fn in lrows:
    sa = float(sa)
    if cycles and sa - cycles[-1]['last'] <= 40: cycles[-1]['last'] = sa; cycles[-1]['fns'].append(fn)
    else: cycles.append({'first': sa, 'last': sa, 'fns': [fn]})
print(f"  ledger desde {fmt(SS)}: {len(lrows)} fila(s) en {len(cycles)} ciclo(s) | por ciclo: " + ', '.join(f"{fmt(c['first'])[6:]}={len(c['fns'])}" for c in cycles[-15:]))
# prueba directa del bug viejo: grabación LISTA antes de un ciclo que procesó < 10 y que igual quedó para un ciclo posterior
fmap = {c['fn']: c for c in cand}
late = []
for cy in cycles:
    if len(cy['fns']) >= 10: continue
    owners_c = {fmap[f]['owner']['followup_id'] for f in cy['fns'] if f in fmap and fmap[f].get('owner')}
    for c in cand:
        o = c.get('owner')
        if not o or c['size'] <= 44 or c['fn'] in cy['fns'] or o['followup_id'] in owners_c: continue
        if c['ready'] < cy['first'] - 180 and ((c['sent'] and c['sent'] > cy['last'] + 30) or (not c['sent'] and o['synced'] == 0)):
            late.append(f"{c['fn']} (lista {fmt(c['ready'])}, ciclo {fmt(cy['first'])} procesó {len(cy['fns'])})")
mx = max([len(c['fns']) for c in cycles] or [0])
if late: res('stringee', 'todas las pendientes se procesan en el MISMO ciclo (no una por ejecución)', 'FAIL', f"{len(late)} quedaron para otro ciclo: " + '; '.join(late[:3]))
elif mx >= 2: res('stringee', 'varias grabaciones en el mismo ciclo', 'PASS', f'máximo {mx} filas de ledger en un ciclo')
else: res('stringee', 'varias grabaciones en el mismo ciclo', 'PEND', 'todavía no hubo 2+ grabaciones listas a la vez; sin atrasos detectados')
if N8N['ok']:
    se = N8N['str_execs']; m2 = [e for e in se if e['ok'] >= 2]; bad = [e for e in se if e['fail'] or e['status'] not in ('success', None)]
    print('  ciclos n8n con subidas: ' + ', '.join(f"#{e['id']} {fmt(e['t'])[6:]} subidas={e['ok']} fallidas={e['fail']}" for e in se if e['ok'] or e['fail']) if se else '  (sin ciclos Stringee en la ventana)')
    res('stringee', 'n8n: ciclos con 2+ PUT exitosos', 'PASS' if m2 else 'PEND', f"{len(m2)} ciclo(s); máximo {max([e['ok'] for e in se] or [0])} por ciclo")
    dup = [e for e in se if len(set(e['fids'])) != len(e['fids'])]
    res('stringee', 'n8n: nunca 2 PUT al mismo followup en un ciclo', 'PASS' if not dup else 'FAIL', ', '.join('#' + str(e['id']) for e in dup))
    res('stringee', 'n8n: ciclos sin PUT fallidos', 'PASS' if not bad else 'WARN', ', '.join(f"#{e['id']} fallidas={e['fail']} {e['status']}" for e in bad[:6]) + (' (quedan pendientes y se reintentan con espera: ni ledger ni synced)' if bad else ''))

# ---------------------------------------------------------------------------------
# resumen
# ---------------------------------------------------------------------------------
hdr('RESUMEN')
for st in ('FAIL', 'WARN', 'PEND', 'PASS', 'INFO'):
    xs = [r for r in RES if r[2] == st]
    if xs: print(f"  {st}: {len(xs)}"); [print(f"     - [{a}] {n}" + (f" — {d[:220]}" if d and st != 'PASS' else '')) for a, n, s, d in xs]
fails = sum(1 for r in RES if r[2] == 'FAIL')
print('\n' + ('❌ HAY FALLAS: revisar arriba antes de finalize.' if fails else
      ('✅ Sin fallas. ' + ('Listo para "ASTERISK_WF10_FIX.sh finalize".' if not any(r[2] == 'PEND' for r in RES) else 'Quedan checks PEND: esperar más llamadas reales y volver a correr.'))))
sys.exit(1 if fails else 0)
PY
RC=$?
echo "Informe guardado en $OUT"
exit $RC
