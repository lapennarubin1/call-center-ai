"""
Motor de analítica — Landmark Markets Control Panel
====================================================
Todas las métricas de telefonía y de embudo comercial.

SQL portable: funciona igual en MySQL (producción) y SQLite (tests).
Los rangos de fecha se pasan como parámetros Python, nunca con DATE_SUB,
para no depender de dialecto.

COSTO: se factura por minuto completo redondeado hacia arriba POR LLAMADA
       -> SUM(CEIL(billsec/60.0)), consistente con /root/menu.sh
"""
from datetime import datetime, timedelta, date
import calendar, time, threading, hashlib, json

DEFAULT_RATE = 0.06   # USD por minuto
TZ_SHIFT_MIN = 330    # UTC → IST (+5:30). El CDR guarda hora del servidor (UTC).


# ══════════════════════════════════════════════════════════════════
#  Caché TTL — los rangos largos (año) agregan cientos de miles de
#  filas; sin caché cada refresco del navegador re-ejecuta todo.
# ══════════════════════════════════════════════════════════════════
class TTLCache:
    def __init__(self, ttl=60):
        self.ttl = ttl
        self._d = {}
        self._lock = threading.Lock()

    def key(self, *parts):
        return hashlib.md5(json.dumps(parts, default=str).encode()).hexdigest()

    def get(self, k):
        with self._lock:
            v = self._d.get(k)
            if v and time.time() - v[0] < self.ttl:
                return v[1]
            if v:
                del self._d[k]
        return None

    def set(self, k, val):
        with self._lock:
            if len(self._d) > 200:            # poda simple
                oldest = sorted(self._d.items(), key=lambda x: x[1][0])[:100]
                for ok, _ in oldest:
                    del self._d[ok]
            self._d[k] = (time.time(), val)
        return val

    def clear(self):
        with self._lock:
            self._d.clear()


CACHE = TTLCache(ttl=60)


# ══════════════════════════════════════════════════════════════════
#  Capa de acceso a datos (abstrae MySQL vs SQLite)
# ══════════════════════════════════════════════════════════════════
class DB:
    def __init__(self, conn, driver):
        self.conn = conn
        self.driver = driver          # 'mysql' | 'sqlite'
        self.ph = '%s' if driver == 'mysql' else '?'

    def q(self, sql, params=()):
        """Ejecuta y devuelve lista de dicts."""
        sql = sql.replace('§', self.ph)
        cur = self.conn.cursor()
        cur.execute(sql, params)
        cols = [d[0] for d in cur.description]
        out = [dict(zip(cols, row)) for row in cur.fetchall()]
        cur.close()
        return out

    def one(self, sql, params=()):
        r = self.q(sql, params)
        return r[0] if r else {}

    def table_exists(self, name):
        try:
            self.q(f"SELECT 1 FROM {name} LIMIT 1")
            return True
        except Exception:
            return False


# ══════════════════════════════════════════════════════════════════
#  Helpers de rango de fechas
# ══════════════════════════════════════════════════════════════════
def resolve_range(period, ref=None, start=None, end=None):
    """
    Devuelve (start_dt, end_dt, label, prev_start, prev_end).
    El rango es [start, end) — end exclusivo, para no perder llamadas
    de las 23:59:59 por comparaciones de fecha.
    """
    ref = ref or datetime.now()
    today = ref.replace(hour=0, minute=0, second=0, microsecond=0)

    if period == 'custom' and start and end:
        s = datetime.strptime(start, '%Y-%m-%d')
        e = datetime.strptime(end, '%Y-%m-%d') + timedelta(days=1)
        label = f"{s:%d/%m/%Y} – {(e - timedelta(days=1)):%d/%m/%Y}"
    elif period == 'today':
        s, e = today, today + timedelta(days=1)
        label = f"Hoy · {s:%d/%m/%Y}"
    elif period == 'yesterday':
        s, e = today - timedelta(days=1), today
        label = f"Ayer · {s:%d/%m/%Y}"
    elif period == 'week':
        s = today - timedelta(days=today.weekday())        # lunes
        e = s + timedelta(days=7)
        label = f"Semana · {s:%d/%m} – {(e - timedelta(days=1)):%d/%m/%Y}"
    elif period == 'month':
        s = today.replace(day=1)
        last = calendar.monthrange(s.year, s.month)[1]
        e = s.replace(day=last) + timedelta(days=1)
        label = f"Mes · {s:%B %Y}"
    elif period == 'year':
        s = today.replace(month=1, day=1)
        e = s.replace(year=s.year + 1)
        label = f"Año · {s.year}"
    elif period == 'last7':
        s, e = today - timedelta(days=6), today + timedelta(days=1)
        label = "Últimos 7 días"
    elif period == 'last30':
        s, e = today - timedelta(days=29), today + timedelta(days=1)
        label = "Últimos 30 días"
    elif period == 'last90':
        s, e = today - timedelta(days=89), today + timedelta(days=1)
        label = "Últimos 90 días"
    else:
        s, e = today, today + timedelta(days=1)
        label = f"Hoy · {s:%d/%m/%Y}"

    span = e - s
    prev_s, prev_e = s - span, s
    return s, e, label, prev_s, prev_e


def _f(dt):
    return dt.strftime('%Y-%m-%d %H:%M:%S')


def pct(part, whole):
    return round(part / whole * 100, 2) if whole else 0.0


def delta(cur, prev):
    """Variación % contra el período anterior."""
    if not prev:
        return None
    return round((cur - prev) / prev * 100, 1)


# ══════════════════════════════════════════════════════════════════
#  MÉTRICAS DE TELEFONÍA (fuente: CDR de Asterisk)
# ══════════════════════════════════════════════════════════════════
TRAFFIC_SQL = """
SELECT
    COUNT(*)                                                        AS total_calls,
    SUM(CASE WHEN disposition='ANSWERED'   THEN 1 ELSE 0 END)       AS answered,
    SUM(CASE WHEN disposition='NO ANSWER'  THEN 1 ELSE 0 END)       AS no_answer,
    SUM(CASE WHEN disposition='BUSY'       THEN 1 ELSE 0 END)       AS busy,
    SUM(CASE WHEN disposition='FAILED'     THEN 1 ELSE 0 END)       AS failed,
    SUM(CASE WHEN disposition='CONGESTION' THEN 1 ELSE 0 END)       AS congestion,
    COALESCE(SUM(CEIL(billsec/60.0)), 0)                            AS billed_minutes,
    COALESCE(SUM(billsec), 0)                                       AS talk_seconds,
    COALESCE(MAX(billsec), 0)                                       AS longest_call
FROM cdr
WHERE calldate >= § AND calldate < §
"""


def traffic(db, s, e, rate=DEFAULT_RATE):
    r = db.one(TRAFFIC_SQL, (_f(s), _f(e)))
    total     = r.get('total_calls') or 0
    answered  = r.get('answered') or 0
    no_answer = r.get('no_answer') or 0
    busy      = r.get('busy') or 0
    failed    = r.get('failed') or 0
    cong      = r.get('congestion') or 0
    minutes   = int(r.get('billed_minutes') or 0)
    talk      = int(r.get('talk_seconds') or 0)

    cost = round(minutes * rate, 2)
    # ASR = Answer Seizure Ratio — % de intentos que terminan en conversación
    asr = pct(answered, total)
    # NER = Network Effectiveness Ratio — salud de la red: descarta solo fallos técnicos
    ner = pct(answered + no_answer + busy, total)
    # ACD = Average Call Duration (sobre contestadas)
    acd = round(talk / answered, 1) if answered else 0.0

    return {
        'total_calls'    : total,
        'answered'       : answered,
        'no_answer'      : no_answer,
        'busy'           : busy,
        'failed'         : failed,
        'congestion'     : cong,
        'tech_failures'  : failed + cong,
        'billed_minutes' : minutes,
        'talk_seconds'   : talk,
        'talk_minutes'   : round(talk / 60, 1),
        'longest_call'   : int(r.get('longest_call') or 0),
        'cost'           : cost,
        'asr'            : asr,
        'ner'            : ner,
        'acd'            : acd,
        'answer_rate'    : asr,
        'no_answer_rate' : pct(no_answer, total),
        'failure_rate'   : pct(failed + cong, total),
        'cost_per_answered': round(cost / answered, 4) if answered else 0.0,
        'cost_per_call'  : round(cost / total, 4) if total else 0.0,
        'avg_min_per_call': round(minutes / total, 2) if total else 0.0,
    }


TIMESERIES_SQL = """
SELECT
    {bucket}                                                  AS bucket,
    COUNT(*)                                                  AS total_calls,
    SUM(CASE WHEN disposition='ANSWERED' THEN 1 ELSE 0 END)   AS answered,
    SUM(CASE WHEN disposition='NO ANSWER' THEN 1 ELSE 0 END)  AS no_answer,
    SUM(CASE WHEN disposition IN ('FAILED','CONGESTION') THEN 1 ELSE 0 END) AS failed,
    COALESCE(SUM(CEIL(billsec/60.0)), 0)                      AS billed_minutes,
    COALESCE(SUM(billsec), 0)                                 AS talk_seconds
FROM cdr
WHERE calldate >= § AND calldate < §
GROUP BY bucket
ORDER BY bucket
"""


def timeseries(db, s, e, grain='day', rate=DEFAULT_RATE):
    """grain: hour | day | week | month"""
    if db.driver == 'mysql':
        bucket = {
            'hour' : "DATE_FORMAT(calldate,'%%Y-%%m-%%d %%H:00')",
            'day'  : "DATE_FORMAT(calldate,'%%Y-%%m-%%d')",
            'week' : "DATE_FORMAT(calldate,'%%x-W%%v')",
            'month': "DATE_FORMAT(calldate,'%%Y-%%m')",
        }[grain]
    else:
        bucket = {
            'hour' : "strftime('%Y-%m-%d %H:00', calldate)",
            'day'  : "strftime('%Y-%m-%d', calldate)",
            'week' : "strftime('%Y-W%W', calldate)",
            'month': "strftime('%Y-%m', calldate)",
        }[grain]

    rows = db.q(TIMESERIES_SQL.format(bucket=bucket), (_f(s), _f(e)))
    for r in rows:
        r['billed_minutes'] = int(r['billed_minutes'] or 0)
        r['cost']    = round(r['billed_minutes'] * rate, 2)
        r['asr']     = pct(r['answered'] or 0, r['total_calls'] or 0)
        r['acd']     = round((r['talk_seconds'] or 0) / r['answered'], 1) if r['answered'] else 0.0
    return rows


HOURLY_SQL = """
SELECT
    {hour_expr}                                               AS hh,
    COUNT(*)                                                  AS total_calls,
    SUM(CASE WHEN disposition='ANSWERED' THEN 1 ELSE 0 END)   AS answered,
    COALESCE(SUM(CEIL(billsec/60.0)), 0)                      AS billed_minutes
FROM cdr
WHERE calldate >= § AND calldate < §
GROUP BY hh
ORDER BY hh
"""


def hourly_profile(db, s, e, rate=DEFAULT_RATE, tz_shift_min=TZ_SHIFT_MIN):
    """
    Distribución por hora en horario LOCAL de la operación (IST).

    El CDR de Asterisk guarda la hora del servidor (UTC). India es UTC+5:30,
    por eso desplazamos el timestamp COMPLETO antes de extraer la hora —
    si extrajéramos la hora primero perderíamos los 30 minutos del offset
    y todo el perfil quedaría corrido una hora hacia atrás.
    """
    if db.driver == 'mysql':
        hour_expr = f"HOUR(DATE_ADD(calldate, INTERVAL {tz_shift_min} MINUTE))"
    else:
        hour_expr = f"CAST(strftime('%H', datetime(calldate, '+{tz_shift_min} minutes')) AS INTEGER)"

    rows = db.q(HOURLY_SQL.format(hour_expr=hour_expr), (_f(s), _f(e)))

    buckets = {h: {'hour': h, 'total_calls': 0, 'answered': 0,
                   'billed_minutes': 0, 'asr': 0.0, 'cost': 0.0} for h in range(24)}
    for r in rows:
        b = buckets[int(r['hh']) % 24]
        b['total_calls']   += r['total_calls'] or 0
        b['answered']      += r['answered'] or 0
        b['billed_minutes']+= int(r['billed_minutes'] or 0)
    for b in buckets.values():
        b['asr']  = pct(b['answered'], b['total_calls'])
        b['cost'] = round(b['billed_minutes'] * rate, 2)
    return [buckets[h] for h in range(24)]


DURATION_SQL = """
SELECT billsec FROM cdr
WHERE calldate >= § AND calldate < § AND disposition='ANSWERED'
"""


def duration_buckets(db, s, e):
    """
    Distribución de duración de llamadas contestadas.
    Clave para el negocio: separa 'atendió y colgó' de 'conversación real'.
    """
    rows = db.q(DURATION_SQL, (_f(s), _f(e)))
    edges = [(0, 10, '0–10s'), (10, 30, '10–30s'), (30, 60, '30–60s'),
             (60, 120, '1–2min'), (120, 300, '2–5min'), (300, 10**9, '+5min')]
    out = [{'label': lbl, 'count': 0, 'lo': lo, 'hi': hi} for lo, hi, lbl in edges]
    total = 0
    for r in rows:
        b = r['billsec'] or 0
        total += 1
        for i, (lo, hi, _) in enumerate(edges):
            if lo <= b < hi:
                out[i]['count'] += 1
                break
    for o in out:
        o['pct'] = pct(o['count'], total)
    # "Conversación real" = 30s o más (por debajo casi siempre es cuelgue inmediato)
    meaningful = sum(o['count'] for o in out if o['lo'] >= 30)
    return {'buckets': out, 'total': total, 'meaningful': meaningful,
            'meaningful_pct': pct(meaningful, total)}


TOP_NUMBERS_SQL = """
SELECT dst,
       COUNT(*)                                                AS attempts,
       SUM(CASE WHEN disposition='ANSWERED' THEN 1 ELSE 0 END) AS answered,
       COALESCE(SUM(billsec),0)                                AS talk_seconds,
       MAX(calldate)                                           AS last_attempt
FROM cdr
WHERE calldate >= § AND calldate < §
GROUP BY dst
HAVING answered > 0
ORDER BY talk_seconds DESC
LIMIT §
"""


def top_conversations(db, s, e, limit=15):
    rows = db.q(TOP_NUMBERS_SQL, (_f(s), _f(e), limit))
    for r in rows:
        r['talk_minutes'] = round((r['talk_seconds'] or 0) / 60, 1)
    return rows


REPEAT_SQL = """
SELECT attempts, COUNT(*) AS numbers FROM (
    SELECT dst, COUNT(*) AS attempts
    FROM cdr WHERE calldate >= § AND calldate < §
    GROUP BY dst
) t GROUP BY attempts ORDER BY attempts
"""


def retry_distribution(db, s, e):
    """Cuántos números recibieron 1, 2, 3+ intentos — mide eficiencia del dialer."""
    rows = db.q(REPEAT_SQL, (_f(s), _f(e)))
    total_numbers = sum(r['numbers'] for r in rows)
    out = []
    for r in rows:
        if r['attempts'] >= 4:
            continue
        out.append({'attempts': r['attempts'], 'numbers': r['numbers'],
                    'pct': pct(r['numbers'], total_numbers)})
    plus4 = sum(r['numbers'] for r in rows if r['attempts'] >= 4)
    if plus4:
        out.append({'attempts': '4+', 'numbers': plus4, 'pct': pct(plus4, total_numbers)})
    return {'rows': out, 'unique_numbers': total_numbers}


# ══════════════════════════════════════════════════════════════════
#  MÉTRICAS DE EMBUDO COMERCIAL (fuente: panel_leads / panel_conversions)
# ══════════════════════════════════════════════════════════════════
FINAL_STATES = ('SUCCESSFUL', 'INTERESTED', 'CONVERTED', 'VOICEMAIL',
                'FAILED', 'SCHEDULED', 'NO_ANSWER', 'DO_NOT_CALL',
                'SALES_HANDOFF', 'NOT_INTERESTED')


def funnel(db, s, e, traffic_stats=None, rate=DEFAULT_RATE):
    """
    Embudo comercial. Devuelve dict vacío si las tablas del panel
    todavía no fueron sincronizadas (WF14) — el panel no se rompe.
    """
    if not db.table_exists('panel_leads'):
        return {'available': False}

    by_status = db.q("""
        SELECT status, COUNT(*) AS n
        FROM panel_leads
        WHERE last_call_time >= § AND last_call_time < §
        GROUP BY status ORDER BY n DESC
    """, (_f(s), _f(e)))

    total_touched = sum(r['n'] for r in by_status)
    d = {r['status']: r['n'] for r in by_status}

    contacted   = d.get('SUCCESSFUL', 0) + d.get('INTERESTED', 0) + d.get('CONVERTED', 0)
    voicemail   = d.get('VOICEMAIL', 0)
    no_answer   = d.get('NO_ANSWER', 0)
    scheduled   = d.get('SCHEDULED', 0)
    interested  = d.get('INTERESTED', 0) + d.get('CONVERTED', 0)

    accounts = 0
    if db.table_exists('panel_conversions'):
        r = db.one("""SELECT COUNT(*) AS n FROM panel_conversions
                      WHERE created_at >= § AND created_at < §""", (_f(s), _f(e)))
        accounts = r.get('n') or 0

    cost = (traffic_stats or {}).get('cost', 0.0)
    answered = (traffic_stats or {}).get('answered', 0)

    return {
        'available'        : True,
        'by_status'        : by_status,
        'total_touched'    : total_touched,
        'contacted'        : contacted,
        'voicemail'        : voicemail,
        'no_answer'        : no_answer,
        'scheduled'        : scheduled,
        'interested'       : interested,
        'accounts_opened'  : accounts,
        'contact_rate'     : pct(contacted, total_touched),
        'conversion_rate'  : pct(accounts, contacted),
        'lead_to_account'  : pct(accounts, total_touched),
        'cpa'              : round(cost / accounts, 2) if accounts else 0.0,
        'cost_per_contact' : round(cost / contacted, 2) if contacted else 0.0,
        'callback_rate'    : pct(scheduled, contacted),
    }


def pipeline_snapshot(db):
    """Estado TOTAL de la base de leads (sin filtro de fecha) — foto del pipeline."""
    if not db.table_exists('panel_leads'):
        return {'available': False}
    rows = db.q("SELECT status, COUNT(*) AS n FROM panel_leads GROUP BY status ORDER BY n DESC")
    total = sum(r['n'] for r in rows)
    for r in rows:
        r['pct'] = pct(r['n'], total)
    d = {r['status']: r['n'] for r in rows}
    pending = d.get('READY_TO_CALL', 0)
    in_flight = d.get('CALL_IN_PROGRESS', 0) + d.get('DIALING', 0)
    return {
        'available'   : True,
        'rows'        : rows,
        'total'       : total,
        'pending'     : pending,
        'in_flight'   : in_flight,
        'exhausted'   : d.get('MAX_ATTEMPTS_REACHED', 0),
        'do_not_call' : d.get('DO_NOT_CALL', 0),
    }


def conversions_list(db, s, e, limit=100):
    if not db.table_exists('panel_conversions'):
        return []
    return db.q("""
        SELECT lead_id, full_name, phone, atlantis_user, account_id, created_at
        FROM panel_conversions
        WHERE created_at >= § AND created_at < §
        ORDER BY created_at DESC LIMIT §
    """, (_f(s), _f(e), limit))


# ══════════════════════════════════════════════════════════════════
#  ENSAMBLADO COMPLETO
# ══════════════════════════════════════════════════════════════════
def sync_status(db):
    """
    Frescura de los datos de leads. Si WF14 deja de correr, el panel
    seguiría mostrando cifras viejas como si fueran de hoy — esto lo
    hace visible en vez de silencioso.
    """
    if not db.table_exists('panel_sync_log'):
        # Sin bitácora, caemos al sello de la propia tabla de leads
        if not db.table_exists('panel_leads'):
            return {'available': False}
        r = db.one("SELECT MAX(synced_at) AS last FROM panel_leads")
    else:
        r = db.one("SELECT MAX(synced_at) AS last FROM panel_sync_log")

    last = r.get('last')
    if not last:
        return {'available': True, 'ever': False, 'stale': True, 'last': None, 'age_min': None}

    if isinstance(last, str):
        try:
            last = datetime.strptime(last[:19], '%Y-%m-%d %H:%M:%S')
        except ValueError:
            return {'available': True, 'ever': False, 'stale': True, 'last': None, 'age_min': None}

    age = (datetime.now() - last).total_seconds() / 60
    return {
        'available': True, 'ever': True,
        'last': last.strftime('%d/%m %H:%M'),
        'age_min': int(age),
        # WF14 corre cada 15 min; a partir de 45 hay algo mal
        'stale': age > 45,
    }


def build_report(db, period='today', start=None, end=None,
                 rate=DEFAULT_RATE, ref=None, use_cache=True):
    ck = CACHE.key('report', period, start, end, rate)
    if use_cache:
        hit = CACHE.get(ck)
        if hit is not None:
            return hit

    s, e, label, ps, pe = resolve_range(period, ref, start, end)

    cur  = traffic(db, s, e, rate)
    prev = traffic(db, ps, pe, rate)

    # Granularidad automática según el largo del rango
    span_days = (e - s).days
    grain = 'hour' if span_days <= 2 else 'day' if span_days <= 92 else 'month'

    fn = funnel(db, s, e, cur, rate)

    report = {
        'label'      : label,
        'period'     : period,
        'range'      : {'start': s.strftime('%Y-%m-%d'),
                        'end'  : (e - timedelta(days=1)).strftime('%Y-%m-%d')},
        'rate'       : rate,
        'traffic'    : cur,
        'deltas'     : {
            'total_calls'   : delta(cur['total_calls'],   prev['total_calls']),
            'answered'      : delta(cur['answered'],      prev['answered']),
            'cost'          : delta(cur['cost'],          prev['cost']),
            'billed_minutes': delta(cur['billed_minutes'],prev['billed_minutes']),
            'asr'           : delta(cur['asr'],           prev['asr']),
            'acd'           : delta(cur['acd'],           prev['acd']),
        },
        'previous'   : prev,
        'series'     : timeseries(db, s, e, grain, rate),
        'grain'      : grain,
        'hourly'     : hourly_profile(db, s, e, rate),
        'durations'  : duration_buckets(db, s, e),
        'top_calls'  : top_conversations(db, s, e),
        'retries'    : retry_distribution(db, s, e),
        'funnel'     : fn,
        'pipeline'   : pipeline_snapshot(db),
        'sync'       : sync_status(db),
        'generated'  : datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
    }
    return CACHE.set(ck, report) if use_cache else report
