"""
Analytics engine — Landmark Markets Control Panel
====================================================
All telephony and commercial funnel metrics.

SQL portable: funciona igual en MySQL (producción) y SQLite (tests).
Los rangos de fecha se pasan como parámetros Python, nunca con DATE_SUB,
para no depender de dialecto.

COSTO: se factura por minuto completo redondeado hacia arriba POR LLAMADA
       -> SUM(CEIL(billsec/60.0)), consistente con /root/menu.sh
"""
from datetime import datetime, timedelta, date
import calendar, time, threading, hashlib, json, os, re, subprocess
import urllib.request, urllib.error
from zoneinfo import ZoneInfo, available_timezones

DEFAULT_RATE = 0.06   # USD por minuto

# ══════════════════════════════════════════════════════════════════
#  CONFIGURACIÓN POR PAÍS — fuente única de verdad
#  ────────────────────────────────────────────────────────────────
#  El VPS guarda todo en UTC (CDR de Asterisk, y los timestamps que
#  WF14 normaliza desde Google Sheets). Cada país opera en su propia
#  zona horaria y con su propio prefijo E.164 — mezclar cualquiera de
#  los dos entre países hace que "hoy" de un país incluya horas que
#  todavía no pasaron ahí, o que el CDR de un país se contamine con
#  números de otro.
#
#  Agregar un país nuevo = agregar una entrada acá. Nada más del
#  módulo debería tocarse: todas las funciones de abajo leen
#  tz_shift_min/tz_label/dst_regex a través de los helpers
#  country_tz_shift(), country_tz_label() y cdr_valid_dst().
#
#  tz_shift_min: offset fijo en minutos vs UTC (sin DST). India no
#  observa DST. México abolió el horario de verano a nivel nacional
#  en 2022, así que un offset fijo (-360, UTC-6, hora del centro) es
#  correcto para prácticamente todo el país durante todo el año —
#  igual que el fijo de India ya asumía sin DST.
#
#  Regla aplicada en TODO el módulo, sin excepciones:
#  - Los límites de período (resolve_range) se calculan en hora LOCAL
#    del país seleccionado.
#  - Toda columna de fecha se compara/agrupa con esa MISMA columna
#    desplazada vía local_expr(db, col, country), nunca cruda.
#  - now_local(country) reemplaza a datetime.now() en cualquier lugar
#    donde el panel muestre o calcule "ahora" para ese país.
# ══════════════════════════════════════════════════════════════════
COUNTRIES_CFG = {
    'india': {
        'label'       : 'India',
        'dst_regex'   : "dst REGEXP '^[+]?91[0-9]{10}$'",
        'tz_shift_min': 330,     # UTC+5:30
        'tz_label'    : 'IST',
        'dial_prefix' : '91',    # prefijo E.164 sin '+' — usado para generar dialplan de extensiones
    },
    'mexico': {
        'label'       : 'Mexico',
        'dst_regex'   : "dst REGEXP '^[+]?52[0-9]{10}$'",
        'tz_shift_min': -360,    # UTC-6:00 (hora del centro, sin DST desde 2022)
        'tz_label'    : 'CST',
        'dial_prefix' : '52',
    },
    'nepal': {
        'label'       : 'Nepal',
        'dst_regex'   : "dst REGEXP '^[+]?977[0-9]{10}$'",
        'tz_shift_min': 345,     # UTC+5:45 (NPT, sin DST)
        'tz_label'    : 'NPT',
        'dial_prefix' : '977',
    },
    'dubai': {
        'label'       : 'Dubai',
        'dst_regex'   : "dst REGEXP '^[+]?971[0-9]{9}$'",
        'tz_shift_min': 240,     # UTC+4:00 (GST, sin DST)
        'tz_label'    : 'GST',
        'dial_prefix' : '971',
        # Solo pruebas internas, sin call center real todavía — no debe
        # aparecer como país operativo en el dashboard ni en Support,
        # para que empleados no lo confundan con un país activo. Sigue
        # existiendo en COUNTRIES_CFG para que el CDR de esas pruebas se
        # siga clasificando bien y el consumo quede visible en SIP
        # Balance (master), que es donde corresponde auditarlo.
        'dashboard_visible': False,
    },
}
DEFAULT_COUNTRY = 'india'

# Etiquetas legibles para el selector del dashboard y Support — solo
# países operativos (dashboard_visible != False). Para el listado
# COMPLETO (incluye países de solo-prueba como Dubai, usado en SIP
# Balance donde master gestiona pricing/consumo de cualquier trunk
# activo aunque no tenga call center), usar COUNTRIES_ALL.
COUNTRIES_ALL = [(k, v['label']) for k, v in COUNTRIES_CFG.items()]
COUNTRIES = [(k, v['label']) for k, v in COUNTRIES_CFG.items()
             if v.get('dashboard_visible', True)]


def cdr_valid_dst(country):
    """Regex CDR_VALID_DST del país pedido; si no se reconoce, cae a India."""
    return COUNTRIES_CFG.get(country, COUNTRIES_CFG[DEFAULT_COUNTRY])['dst_regex']


def country_tz_shift(country):
    """Offset en minutos vs UTC del país pedido; si no se reconoce, cae a India."""
    return COUNTRIES_CFG.get(country, COUNTRIES_CFG[DEFAULT_COUNTRY])['tz_shift_min']


def country_tz_label(country):
    """Etiqueta de zona horaria del país pedido (IST, CST, ...)."""
    return COUNTRIES_CFG.get(country, COUNTRIES_CFG[DEFAULT_COUNTRY])['tz_label']


def normalize_country(country):
    """Nunca dejar pasar un valor de country que no exista — evita
    queries silenciosamente vacías por un typo en la URL."""
    return country if country in COUNTRIES_CFG else DEFAULT_COUNTRY


def normalize_dashboard_country(country):
    """Como normalize_country(), pero además rechaza países que existen
    en COUNTRIES_CFG pero están ocultos del dashboard (dashboard_visible:
    False, ej. Dubai, solo pruebas internas). Evita que alguien vea esos
    datos en el dashboard/exports tecleando ?country=<pais> a mano,
    aunque la pestaña esté oculta. Usar solo para el dashboard operativo
    (server.py:params()) — SIP Balance sigue usando COUNTRIES_CFG
    completo vía validate_sip_country(), a propósito."""
    cfg = COUNTRIES_CFG.get(country)
    if cfg is None or not cfg.get('dashboard_visible', True):
        return DEFAULT_COUNTRY
    return country


def now_local(country=DEFAULT_COUNTRY):
    """'Ahora' en hora local del país, sin depender de la zona horaria del VPS."""
    return datetime.utcnow() + timedelta(minutes=country_tz_shift(country))


def to_local(dt_utc, country=DEFAULT_COUNTRY):
    """Convierte un datetime naive-UTC (ej. de MySQL) a hora local del país para mostrar."""
    if dt_utc is None:
        return None
    if isinstance(dt_utc, str):
        try:
            dt_utc = datetime.strptime(dt_utc[:19], '%Y-%m-%d %H:%M:%S')
        except ValueError:
            return None
    return dt_utc + timedelta(minutes=country_tz_shift(country))


def local_expr(db, col, country):
    """
    Expresión SQL que desplaza una columna UTC a hora local del país —
    SOLO para GROUP BY o extracción de hora, donde no hay forma de usar
    el índice de todos modos. Para WHERE, usar utc_bounds() en su lugar:
    envolver la columna en DATE_ADD/datetime() en el WHERE impide que el
    motor use el índice de calldate y fuerza un escaneo completo de la tabla.
    """
    shift = country_tz_shift(country)
    if db.driver == 'mysql':
        # MySQL acepta INTERVAL negativo tal cual (INTERVAL -360 MINUTE).
        return f"DATE_ADD({col}, INTERVAL {shift} MINUTE)"
    # SQLite necesita el signo explícito para offsets positivos; uno
    # negativo ya trae el '-' al formatear el número, así que no se
    # antepone nada (anteponer '+' siempre rompía offsets negativos:
    # "+-360 minutes" no es un modificador válido de datetime()).
    sign = '+' if shift >= 0 else ''
    return f"datetime({col}, '{sign}{shift} minutes')"


def local_hour_expr(db, col, country):
    """Expresión SQL que devuelve solo la HORA (0-23) de una columna UTC
    ya desplazada a hora local del país — usada por el perfil horario."""
    expr = local_expr(db, col, country)
    if db.driver == 'mysql':
        return f"HOUR({expr})"
    return f"CAST(strftime('%H', {expr}) AS INTEGER)"


def utc_bounds(s, e, country=DEFAULT_COUNTRY):
    """
    Convierte un rango en hora local del país (el que devuelve
    resolve_range) a UTC para comparar directo contra la columna cruda
    — así el motor SÍ puede usar el índice de calldate/last_call_time/
    created_at. Es el mismo desplazamiento que local_expr(), aplicado
    al límite en vez de a cada fila de la tabla: mismo resultado, sin
    escaneo completo.
    """
    delta = timedelta(minutes=country_tz_shift(country))
    return _f(s - delta), _f(e - delta)


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
#  FILTRO DE RUIDO — llamadas que NO son leads reales
#  ────────────────────────────────────────────────────────────────
#  ElevenLabs manda conversaciones basura ("Untitled conversation",
#  0s, Evaluation: Error) al endpoint SIP. El dialplan (_X. sin
#  validar formato) las reenvía igual al trunk correspondiente, y
#  quedan como CDR real con dst = ID corto (ej. "67001") en vez de
#  un teléfono real (código de país + 10 dígitos).
#  Se corta en origen en el dialplan (ver nota aparte); esto es la
#  segunda capa, para que datos viejos ya insertados no infecten
#  las métricas históricas.
#
#  MULTIPAÍS: cada país tiene su propio prefijo E.164 y por lo tanto
#  su propio regex — el panel filtra el CDR completo (que mezcla
#  todos los trunks) por el país seleccionado en el selector de arriba.
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

    def execute(self, sql, params=()):
        """
        DDL/INSERT/UPDATE/DELETE — sin resultado de filas. Devuelve
        (lastrowid, rowcount). El resto del panel hasta ahora solo leía
        (q/one); esto lo agrega SIP Balance, que necesita escribir.

        commit() explícito porque SQLite (a diferencia de la conexión
        MySQL, que ya abre con autocommit=True en server.py) no confirma
        solo — sin esto un INSERT quedaría solo visible dentro de la
        misma conexión y se perdería al cerrarla al final del request.
        """
        sql = sql.replace('§', self.ph)
        cur = self.conn.cursor()
        cur.execute(sql, params)
        lastrowid, rowcount = cur.lastrowid, cur.rowcount
        cur.close()
        self.conn.commit()
        return lastrowid, rowcount

    def insert(self, sql, params=()):
        """INSERT — devuelve el id autogenerado."""
        lastrowid, _ = self.execute(sql, params)
        return lastrowid


# ══════════════════════════════════════════════════════════════════
#  Helpers de rango de fechas
# ══════════════════════════════════════════════════════════════════
def resolve_range(period, ref=None, start=None, end=None, country=DEFAULT_COUNTRY):
    """
    Devuelve (start_dt, end_dt, label, prev_start, prev_end).
    El rango es [start, end) — end exclusivo.

    IMPORTANTE: s/e quedan expresados como hora LOCAL del país "naive"
    (sin convertir a UTC acá). Cada consulta SQL es la que se encarga
    de desplazar su propia columna con local_expr()/utc_bounds() antes
    de comparar — así el límite se calcula una sola vez y nunca se
    duplica el desplazamiento por accidente.

    "Hoy"/"ayer" se calculan sobre la medianoche LOCAL del país
    seleccionado, no sobre una zona horaria fija — India y México
    tienen jornadas operativas distintas, y comparar contra la
    medianoche de un solo país haría que "hoy" de otro país incluyera
    horas que ahí todavía no pasaron (o le faltaran horas ya pasadas).
    """
    ref = ref or now_local(country)
    today = ref.replace(hour=0, minute=0, second=0, microsecond=0)

    if period == 'custom' and start and end:
        s = datetime.strptime(start, '%Y-%m-%d')
        e = datetime.strptime(end, '%Y-%m-%d') + timedelta(days=1)
        label = f"{s:%d/%m/%Y} – {(e - timedelta(days=1)):%d/%m/%Y}"
    elif period == 'today':
        s, e = today, today + timedelta(days=1)
        label = f"Today · {s:%d/%m/%Y}"
    elif period == 'yesterday':
        s, e = today - timedelta(days=1), today
        label = f"Yesterday · {s:%d/%m/%Y}"
    elif period == 'week':
        s = today - timedelta(days=today.weekday())        # lunes
        e = s + timedelta(days=7)
        label = f"Week · {s:%d/%m} – {(e - timedelta(days=1)):%d/%m/%Y}"
    elif period == 'month':
        s = today.replace(day=1)
        last = calendar.monthrange(s.year, s.month)[1]
        e = s.replace(day=last) + timedelta(days=1)
        label = f"Month · {s:%B %Y}"
    elif period == 'year':
        s = today.replace(month=1, day=1)
        e = s.replace(year=s.year + 1)
        label = f"Year · {s.year}"
    elif period == 'last7':
        s, e = today - timedelta(days=6), today + timedelta(days=1)
        label = "Last 7 days"
    elif period == 'last30':
        s, e = today - timedelta(days=29), today + timedelta(days=1)
        label = "Last 30 days"
    elif period == 'last90':
        s, e = today - timedelta(days=89), today + timedelta(days=1)
        label = "Last 90 days"
    else:
        s, e = today, today + timedelta(days=1)
        label = f"Today · {s:%d/%m/%Y}"

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
def _traffic_sql(db, country):
    # WHERE sin envolver la columna: usa el índice de calldate.
    # Los límites ya vienen convertidos a UTC por utc_bounds().
    return """
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
WHERE calldate >= § AND calldate < § AND """ + cdr_valid_dst(country) + """
"""


def _int(v):
    """Convierte int, float o Decimal (MariaDB) a int de Python."""
    try:    return int(v) if v is not None else 0
    except Exception: return 0

def _float(v):
    """Convierte int, float o Decimal (MariaDB) a float de Python."""
    try:    return float(v) if v is not None else 0.0
    except Exception: return 0.0

def traffic(db, s, e, rate=DEFAULT_RATE, country=DEFAULT_COUNTRY):
    r = db.one(_traffic_sql(db, country), utc_bounds(s, e, country))
    total     = _int(r.get('total_calls'))
    answered  = _int(r.get('answered'))
    no_answer = _int(r.get('no_answer'))
    busy      = _int(r.get('busy'))
    failed    = _int(r.get('failed'))
    cong      = _int(r.get('congestion'))
    minutes   = _int(r.get('billed_minutes'))
    talk      = _int(r.get('talk_seconds'))

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


def timeseries(db, s, e, grain='day', rate=DEFAULT_RATE, country=DEFAULT_COUNTRY):
    """
    grain: hour | day | week | month — todo agrupado en calendario local
    del país. Antes esto agrupaba por la columna cruda (UTC): una llamada
    de las 23:50 hora local podía aparecer bajo el día equivocado o la
    hora corrida. Ahora se agrupa sobre la MISMA columna ya desplazada.
    """
    # GROUP BY sí necesita la columna desplazada (no hay índice que
    # ayude a agrupar de todos modos). El WHERE usa la columna cruda
    # con límites ya convertidos a UTC — así sí aprovecha el índice.
    local_col = local_expr(db, 'calldate', country)
    if db.driver == 'mysql':
        bucket = {
            'hour' : f"DATE_FORMAT({local_col},'%%Y-%%m-%%d %%H:00')",
            'day'  : f"DATE_FORMAT({local_col},'%%Y-%%m-%%d')",
            'week' : f"DATE_FORMAT({local_col},'%%x-W%%v')",
            'month': f"DATE_FORMAT({local_col},'%%Y-%%m')",
        }[grain]
    else:
        bucket = {
            'hour' : f"strftime('%Y-%m-%d %H:00', {local_col})",
            'day'  : f"strftime('%Y-%m-%d', {local_col})",
            'week' : f"strftime('%Y-W%W', {local_col})",
            'month': f"strftime('%Y-%m', {local_col})",
        }[grain]

    sql = f"""
SELECT
    {bucket}                                                  AS bucket,
    COUNT(*)                                                  AS total_calls,
    SUM(CASE WHEN disposition='ANSWERED' THEN 1 ELSE 0 END)   AS answered,
    SUM(CASE WHEN disposition='NO ANSWER' THEN 1 ELSE 0 END)  AS no_answer,
    SUM(CASE WHEN disposition IN ('FAILED','CONGESTION') THEN 1 ELSE 0 END) AS failed,
    COALESCE(SUM(CEIL(billsec/60.0)), 0)                      AS billed_minutes,
    COALESCE(SUM(billsec), 0)                                 AS talk_seconds
FROM cdr
WHERE calldate >= § AND calldate < § AND {cdr_valid_dst(country)}
GROUP BY bucket
ORDER BY bucket
"""
    rows = db.q(sql, utc_bounds(s, e, country))
    for r in rows:
        r['total_calls']    = _int(r.get('total_calls'))
        r['answered']       = _int(r.get('answered'))
        r['no_answer']      = _int(r.get('no_answer'))
        r['failed']         = _int(r.get('failed'))
        r['billed_minutes'] = _int(r.get('billed_minutes'))
        r['talk_seconds']   = _int(r.get('talk_seconds'))
        r['cost']    = round(r['billed_minutes'] * rate, 2)
        r['asr']     = pct(r['answered'], r['total_calls'])
        r['acd']     = round(r['talk_seconds'] / r['answered'], 1) if r['answered'] else 0.0
    return rows


def funnel_timeseries(db, s, e, grain='day', country=DEFAULT_COUNTRY):
    """
    Evolución del funnel en el tiempo: para cada bucket (hora en Today/
    Yesterday, día en Week/Month, etc.) trae attempts, answered, real
    conversation (60s+) y cuentas abiertas — todo normalizado a % sobre
    attempts DE ESE MISMO bucket, para poder graficar las 3 líneas juntas
    en una sola escala 0-100% sin que "Account opened" quede invisible
    contra "Call attempts".
    """
    local_col = local_expr(db, 'calldate', country)
    if db.driver == 'mysql':
        bucket = {
            '30min': f"DATE_FORMAT(DATE_SUB({local_col}, INTERVAL (MINUTE({local_col}) MOD 30) MINUTE),'%%Y-%%m-%%d %%H:%%i')",
            'hour' : f"DATE_FORMAT({local_col},'%%Y-%%m-%%d %%H:00')",
            'day'  : f"DATE_FORMAT({local_col},'%%Y-%%m-%%d')",
            'week' : f"DATE_FORMAT({local_col},'%%x-W%%v')",
            'month': f"DATE_FORMAT({local_col},'%%Y-%%m')",
        }[grain]
    else:
        bucket = {
            '30min': f"strftime('%Y-%m-%d %H:', {local_col}) || printf('%02d', (CAST(strftime('%M', {local_col}) AS INTEGER) / 30) * 30)",
            'hour' : f"strftime('%Y-%m-%d %H:00', {local_col})",
            'day'  : f"strftime('%Y-%m-%d', {local_col})",
            'week' : f"strftime('%Y-W%W', {local_col})",
            'month': f"strftime('%Y-%m', {local_col})",
        }[grain]

    sql = f"""
SELECT
    {bucket}                                                          AS bucket,
    COUNT(*)                                                          AS attempts,
    SUM(CASE WHEN disposition='ANSWERED' THEN 1 ELSE 0 END)           AS answered,
    SUM(CASE WHEN disposition='ANSWERED' AND billsec >= 60 THEN 1 ELSE 0 END) AS real_conv
FROM cdr
WHERE calldate >= § AND calldate < § AND {cdr_valid_dst(country)}
GROUP BY bucket
ORDER BY bucket
"""
    rows = db.q(sql, utc_bounds(s, e, country))
    by_bucket = {}
    for r in rows:
        attempts = _int(r.get('attempts'))
        answered = _int(r.get('answered'))
        real_conv = _int(r.get('real_conv'))
        by_bucket[r['bucket']] = {
            'bucket': r['bucket'], 'attempts': attempts,
            'answered': answered, 'real_conv': real_conv,
            'accounts': 0,  # se completa abajo si hay panel_conversions
            'pct_answered': pct(answered, attempts),
            'pct_real_conv': pct(real_conv, attempts),
            'pct_accounts': 0.0,
        }

    if db.table_exists('panel_conversions'):
        local_col_pc = local_expr(db, 'created_at', country)
        if db.driver == 'mysql':
            bucket_pc = {
                '30min': f"DATE_FORMAT(DATE_SUB({local_col_pc}, INTERVAL (MINUTE({local_col_pc}) MOD 30) MINUTE),'%%Y-%%m-%%d %%H:%%i')",
                'hour' : f"DATE_FORMAT({local_col_pc},'%%Y-%%m-%%d %%H:00')",
                'day'  : f"DATE_FORMAT({local_col_pc},'%%Y-%%m-%%d')",
                'week' : f"DATE_FORMAT({local_col_pc},'%%x-W%%v')",
                'month': f"DATE_FORMAT({local_col_pc},'%%Y-%%m')",
            }[grain]
        else:
            bucket_pc = {
                '30min': f"strftime('%Y-%m-%d %H:', {local_col_pc}) || printf('%02d', (CAST(strftime('%M', {local_col_pc}) AS INTEGER) / 30) * 30)",
                'hour' : f"strftime('%Y-%m-%d %H:00', {local_col_pc})",
                'day'  : f"strftime('%Y-%m-%d', {local_col_pc})",
                'week' : f"strftime('%Y-W%W', {local_col_pc})",
                'month': f"strftime('%Y-%m', {local_col_pc})",
            }[grain]
        sql_pc = f"""
SELECT {bucket_pc} AS bucket, COUNT(*) AS n
FROM panel_conversions
WHERE created_at >= § AND created_at < § AND LOWER(country) = §
GROUP BY bucket
"""
        # LOWER() porque el país llega a la tabla con distinta capitalización
        # según la fuente (WF1 escribe 'Mexico', el phone-lookup de WF14
        # escribe 'Mexico' también, pero mejor no asumir consistencia).
        bs_pc, be_pc = utc_bounds(s, e, country)
        pc_rows = db.q(sql_pc, (bs_pc, be_pc, country.lower()))
        for r in pc_rows:
            n = _int(r.get('n'))
            if r['bucket'] in by_bucket:
                b = by_bucket[r['bucket']]
                b['accounts'] = n
                b['pct_accounts'] = pct(n, b['attempts'])
            else:
                # cuenta abierta en un bucket sin intentos de llamada propios
                # (poco común, pero no se debe perder del gráfico)
                by_bucket[r['bucket']] = {
                    'bucket': r['bucket'], 'attempts': 0, 'answered': 0,
                    'real_conv': 0, 'accounts': n,
                    'pct_answered': 0.0, 'pct_real_conv': 0.0, 'pct_accounts': 0.0,
                }

    return [by_bucket[k] for k in sorted(by_bucket.keys())]


def _hourly_sql(db, hour_expr, country):
    # WHERE con la columna cruda (usa índice); solo hour_expr desplaza.
    return f"""
SELECT
    {hour_expr}                                               AS hh,
    COUNT(*)                                                  AS total_calls,
    SUM(CASE WHEN disposition='ANSWERED' THEN 1 ELSE 0 END)   AS answered,
    COALESCE(SUM(CEIL(billsec/60.0)), 0)                      AS billed_minutes
FROM cdr
WHERE calldate >= § AND calldate < § AND {cdr_valid_dst(country)}
GROUP BY hh
ORDER BY hh
"""


def hourly_profile(db, s, e, rate=DEFAULT_RATE, country=DEFAULT_COUNTRY):
    """
    Distribución por hora en horario LOCAL del país (ver COUNTRIES_CFG),
    solo para las horas con actividad real.

    El CDR de Asterisk guarda la hora del servidor (UTC); cada país
    desplaza el timestamp COMPLETO antes de extraer la hora — si se
    extrajera la hora primero se perdería el offset y el perfil
    quedaría corrido. El WHERE externo usa la MISMA columna desplazada
    para no desalinearse contra s/e (que ya vienen en hora local).

    Dinámico por diseño: en vez de devolver siempre las 24 horas del
    día, devuelve solo el tramo [primera hora con actividad, última
    hora con actividad] del período consultado — un día que llamó de
    10am a 5pm se ve como 10-17, uno que llamó de 11am a 7pm se ve
    como 11-19, sin las horas vacías aplastando la escala del resto.
    "Actividad" incluye tanto intentos de llamada como cuentas
    abiertas, para no recortar una cuenta que se abrió fuera del
    horario de discado. Devuelve [] si no hubo ninguna actividad.
    """
    hour_expr = local_hour_expr(db, 'calldate', country)
    rows = db.q(_hourly_sql(db, hour_expr, country), utc_bounds(s, e, country))

    buckets = {h: {'hour': h, 'total_calls': 0, 'answered': 0,
                   'billed_minutes': 0, 'accounts': 0, 'asr': 0.0} for h in range(24)}
    for r in rows:
        b = buckets[int(r['hh']) % 24]
        b['total_calls']   += _int(r.get('total_calls'))
        b['answered']      += _int(r.get('answered'))
        b['billed_minutes']+= _int(r.get('billed_minutes'))

    # Cuentas abiertas por hora — mismo patrón de país + LOWER() que el
    # resto del módulo usa contra panel_conversions (ver funnel_timeseries).
    if db.table_exists('panel_conversions'):
        acc_hour_expr = local_hour_expr(db, 'created_at', country)
        sql_acc = f"""
SELECT {acc_hour_expr} AS hh, COUNT(*) AS n
FROM panel_conversions
WHERE created_at >= § AND created_at < § AND LOWER(country) = §
GROUP BY hh
"""
        bs, be = utc_bounds(s, e, country)
        for r in db.q(sql_acc, (bs, be, country.lower())):
            buckets[int(r['hh']) % 24]['accounts'] += _int(r.get('n'))

    for b in buckets.values():
        b['asr'] = pct(b['answered'], b['total_calls'])

    active = [h for h in range(24)
              if buckets[h]['total_calls'] > 0 or buckets[h]['accounts'] > 0]
    if not active:
        return []
    lo, hi = min(active), max(active)
    return [buckets[h] for h in range(lo, hi + 1)]


def _duration_sql(db, country):
    return "SELECT billsec FROM cdr WHERE calldate >= § AND calldate < § AND disposition='ANSWERED' AND " + cdr_valid_dst(country)


def duration_buckets(db, s, e, country=DEFAULT_COUNTRY):
    """
    Duration distribution of answered calls.
    Clave para el negocio: separa 'atendió y colgó' de 'real conversation'.
    """
    rows = db.q(_duration_sql(db, country), utc_bounds(s, e, country))
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
    # "Real conversation" = 60s or more (below that is almost always an immediate hang-up)
    meaningful = sum(o['count'] for o in out if o['lo'] >= 60)
    return {'buckets': out, 'total': total, 'meaningful': meaningful,
            'meaningful_pct': pct(meaningful, total)}


def _top_numbers_sql(db, country):
    # Antes sin filtro de país — con un solo país en el CDR no se notaba,
    # pero ahora el CDR mezcla varios trunks y sin esto el top de un país
    # se contamina con números de otro.
    return """
SELECT dst,
       COUNT(*)                                                AS attempts,
       SUM(CASE WHEN disposition='ANSWERED' THEN 1 ELSE 0 END) AS answered,
       COALESCE(SUM(billsec),0)                                AS talk_seconds,
       MAX(calldate)                                           AS last_attempt
FROM cdr
WHERE calldate >= § AND calldate < § AND """ + cdr_valid_dst(country) + """
GROUP BY dst
HAVING answered > 0
ORDER BY talk_seconds DESC
LIMIT §
"""


def top_conversations(db, s, e, limit=15, country=DEFAULT_COUNTRY):
    bs, be = utc_bounds(s, e, country)
    rows = db.q(_top_numbers_sql(db, country), (bs, be, limit))
    for r in rows:
        r['talk_minutes'] = round((r['talk_seconds'] or 0) / 60, 1)
        r['last_attempt_local'] = to_local(r.get('last_attempt'), country)
    return rows


def _repeat_sql(db, country):
    return """
SELECT attempts, COUNT(*) AS numbers FROM (
    SELECT dst, COUNT(*) AS attempts
    FROM cdr WHERE calldate >= § AND calldate < § AND """ + cdr_valid_dst(country) + """
    GROUP BY dst
) t GROUP BY attempts ORDER BY attempts
"""


def retry_distribution(db, s, e, country=DEFAULT_COUNTRY):
    """Cuántos números recibieron 1, 2, 3+ intentos — measures dialer efficiency."""
    rows = db.q(_repeat_sql(db, country), utc_bounds(s, e, country))
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

# Explica qué significa cada status y qué evalúa el sistema para
# ponerlo — se muestra como tooltip al pasar el mouse en el panel.
STATUS_DESCRIPTIONS = {
    'READY_TO_CALL'       : 'Waiting in queue. WF2 has not dialed this lead yet.',
    'DIALING'              : 'WF2 just sent this call to ElevenLabs — resolves in seconds.',
    'CALL_IN_PROGRESS'     : 'ElevenLabs accepted the call and it is ringing or in conversation right now.',
    'NO_ANSWER'            : 'The phone rang but nobody picked up.',
    'VOICEMAIL'            : 'An answering machine picked up instead of a real person.',
    'FAILED'               : 'The call could not connect — carrier or network issue, not the phone number.',
    'DIAL_FAILED'          : 'The dial attempt itself failed before it could ring.',
    'SUCCESSFUL'           : 'A person answered and had a real conversation with the agent.',
    'SCHEDULED'            : 'The lead asked to be called back at a specific time. WF4 handles the callback.',
    'INTERESTED'           : 'WF3 confirmed this only when a real Atlantis account was actually opened — not guessed from the conversation.',
    'CONVERTED'            : 'The account is open and the client is active.',
    'SALES_HANDOFF'        : 'Escalated to a human sales agent instead of continuing automated calls.',
    'NOT_INTERESTED'       : 'The lead explicitly said they are not interested.',
    'DO_NOT_CALL'          : 'The lead asked to never be called again — permanently excluded from WF2.',
    'MAX_ATTEMPTS_REACHED' : 'WF2 tried 3 times with no result and stopped — needs manual review.',
    'INVALID_LEAD'         : 'The phone number or lead data was invalid and could not be dialed.',
}


def funnel(db, s, e, traffic_stats=None, rate=DEFAULT_RATE, country=DEFAULT_COUNTRY):
    """
    Embudo comercial. Devuelve dict vacío si las tablas del panel
    todavía no fueron sincronizadas (WF14) — el panel no se rompe.
    """
    if not db.table_exists('panel_leads'):
        return {'available': False}

    bs, be = utc_bounds(s, e, country)
    by_status = db.q("""
        SELECT status, COUNT(*) AS n
        FROM panel_leads
        WHERE last_call_time >= § AND last_call_time < § AND LOWER(country) = §
        GROUP BY status ORDER BY n DESC
    """, (bs, be, country.lower()))

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
                      WHERE created_at >= § AND created_at < § AND LOWER(country) = §""",
                   (bs, be, country.lower()))
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


def pipeline_snapshot(db, s=None, e=None, country=DEFAULT_COUNTRY):
    """
    Estado del pipeline de leads.

    Si se pasan s/e, filtra por last_call_time dentro de ese período
    (mismo criterio que funnel(), sincronizado con el selector de
    fechas). "In progress now" y "Pending calls" en cambio muestran
    SIEMPRE el estado actual real — son conceptos de "ahora mismo",
    no de "qué pasó en tal período": no tendría sentido que la cola
    de pendientes cambie según qué rango de fechas estés mirando.
    """
    if not db.table_exists('panel_leads'):
        return {'available': False}

    if s is not None and e is not None:
        bs, be = utc_bounds(s, e, country)
        rows = db.q("""
            SELECT status, COUNT(*) AS n FROM panel_leads
            WHERE last_call_time >= § AND last_call_time < § AND LOWER(country) = §
            GROUP BY status ORDER BY n DESC
        """, (bs, be, country.lower()))
        scoped = True
    else:
        rows = db.q("SELECT status, COUNT(*) AS n FROM panel_leads WHERE LOWER(country) = § GROUP BY status ORDER BY n DESC",
                    (country.lower(),))
        scoped = False

    total = sum(r['n'] for r in rows)
    for r in rows:
        r['pct'] = pct(r['n'], total)
        r['description'] = STATUS_DESCRIPTIONS.get((r['status'] or '').upper(), '')

    # Siempre el total actual, sin filtrar por fecha — son colas operativas
    # "ahora mismo" — pero SÍ filtradas por país, o sumarían las colas de
    # todos los países juntas.
    cur = db.q("SELECT status, COUNT(*) AS n FROM panel_leads WHERE LOWER(country) = § GROUP BY status",
               (country.lower(),))
    cd = {r['status']: r['n'] for r in cur}

    # "In progress now" solo cuenta CALL_IN_PROGRESS/DIALING con actividad
    # reciente (últimas 2h) — una llamada real dura minutos, no días. Sin
    # este filtro, leads que quedaron colgados por un bug viejo de WF9
    # (nunca resolvieron su estado) se acumulan para siempre e inflan este
    # número de forma permanente, sin relación con la actividad real.
    fresh = db.q("""
        SELECT COUNT(*) AS n FROM panel_leads
        WHERE status IN ('CALL_IN_PROGRESS', 'DIALING')
          AND last_call_time >= § AND LOWER(country) = §
    """, (_f(datetime.utcnow() - timedelta(hours=2)), country.lower()))
    in_flight = _int(fresh[0]['n']) if fresh else 0

    return {
        'available'   : True,
        'scoped'      : scoped,
        'rows'        : rows,
        'total'       : total,
        'pending'     : cd.get('READY_TO_CALL', 0),
        'in_flight'   : in_flight,
        'exhausted'   : cd.get('MAX_ATTEMPTS_REACHED', 0),
        'do_not_call' : cd.get('DO_NOT_CALL', 0),
    }


def conversions_list(db, s, e, limit=100, country=DEFAULT_COUNTRY):
    if not db.table_exists('panel_conversions'):
        return []
    bs, be = utc_bounds(s, e, country)
    rows = db.q("""
        SELECT lead_id, full_name, phone, atlantis_user, account_id, created_at
        FROM panel_conversions
        WHERE created_at >= § AND created_at < § AND LOWER(country) = §
        ORDER BY created_at DESC LIMIT §
    """, (bs, be, country.lower(), limit))
    for r in rows:
        local_dt = to_local(r.get('created_at'), country)
        r['created_at_local'] = local_dt.strftime('%Y-%m-%d %H:%M:%S') if local_dt else ''
    return rows


# ══════════════════════════════════════════════════════════════════
#  ENSAMBLADO COMPLETO
# ══════════════════════════════════════════════════════════════════
def sync_status(db, country=DEFAULT_COUNTRY):
    """
    Frescura de los datos de leads. Si WF14 deja de correr, el panel
    seguiría mostrando cifras viejas como si fueran de hoy — esto lo
    hace visible en vez de silencioso.

    WF14 es un único proceso global (no corre por país), pero el
    horario que se muestra se expresa en la hora LOCAL del país que
    se está viendo — mismo criterio que el resto de la pestaña, para
    no mezclar "actualizado a las 20:15 IST" en un dashboard que por
    lo demás está todo en hora de México.
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

    age = (now_local(country) - to_local(last, country)).total_seconds() / 60
    return {
        'available': True, 'ever': True,
        'last': to_local(last, country).strftime('%d/%m %H:%M') + ' ' + country_tz_label(country),
        'age_min': int(age),
        # WF14 corre cada 15 min; a partir de 45 hay algo mal
        'stale': age > 45,
    }


def build_report(db, period='today', start=None, end=None,
                 rate=DEFAULT_RATE, ref=None, use_cache=True, country=DEFAULT_COUNTRY):
    country = normalize_country(country)
    ck = CACHE.key('report', period, start, end, rate, country)
    if use_cache:
        hit = CACHE.get(ck)
        if hit is not None:
            return hit

    s, e, label, ps, pe = resolve_range(period, ref, start, end, country)

    cur  = traffic(db, s, e, rate, country)
    prev = traffic(db, ps, pe, rate, country)

    # Granularidad automática según el largo del rango
    span_days = (e - s).days
    grain = 'hour' if span_days <= 2 else 'day' if span_days <= 92 else 'month'

    fn = funnel(db, s, e, cur, rate, country)

    report = {
        'label'      : label,
        'period'     : period,
        'country'    : country,
        'tz_label'   : country_tz_label(country),
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
        'series'     : timeseries(db, s, e, grain, rate, country),
        # Funnel over time usa bloques de 30 min en vez de 1h cuando el
        # rango es Today/Yesterday (grain='hour') — más resolución para
        # ver el movimiento del día. Call Traffic (series, arriba) sigue
        # en 'hour' sin cambios, a propósito.
        'funnel_series': funnel_timeseries(db, s, e, '30min' if grain == 'hour' else grain, country),
        'grain'      : grain,
        'hourly'     : hourly_profile(db, s, e, rate, country=country),
        'durations'  : duration_buckets(db, s, e, country),
        'top_calls'  : top_conversations(db, s, e, country=country),
        'retries'    : retry_distribution(db, s, e, country),
        'funnel'     : fn,
        'pipeline'   : pipeline_snapshot(db, s, e, country),
        'sync'       : sync_status(db, country),
        'stringee'   : build_stringee_report(db, s, e, country),
        'generated'  : now_local(country).strftime('%Y-%m-%d %H:%M:%S'),
    }
    return CACHE.set(ck, report) if use_cache else report


# ══════════════════════════════════════════════════════════════════
#  SIP BALANCE — cuenta con el proveedor (depósitos vs consumo real)
#  ────────────────────────────────────────────────────────────────
#  Esto es independiente del resto del panel: no es una métrica del
#  negocio (leads, cuentas, ASR), es la cuenta corriente con el
#  proveedor SIP — cuánto le depositamos vs cuánto consumimos según
#  el CDR real. Es un saldo ACUMULADO histórico, no acotado a un
#  período — a diferencia de todo lo demás en build_report(), que sí
#  se recalcula por Today/Week/Month/etc.
#
#  Cada fila de precio de un proveedor lleva su propio 'trunk_name':
#  el nombre EXACTO del endpoint pjsip en Asterisk para ese país bajo
#  ese proveedor (ej. 'proveedor1' para India, 'proveedor-mx' para
#  México — ver pjsip.conf). Un mismo proveedor puede tener varios
#  trunks (uno por país) con precios distintos; un proveedor nuevo a
#  futuro con otro trunk se agrega como fila nueva, sin tocar nada
#  de lo existente.
# ══════════════════════════════════════════════════════════════════
def ensure_sip_tables(db):
    """
    Crea las tablas de SIP Balance si no existen — todo lo maneja el
    panel mismo, sin depender de n8n ni de una migración externa.
    Idempotente: seguro de llamar en cada request.
    """
    if db.driver == 'mysql':
        db.execute("""CREATE TABLE IF NOT EXISTS sip_providers (
            id INT AUTO_INCREMENT PRIMARY KEY,
            name VARCHAR(150) NOT NULL,
            active TINYINT(1) NOT NULL DEFAULT 1,
            billing_start_date DATE NULL,
            created_at DATETIME DEFAULT CURRENT_TIMESTAMP
        )""")
        db.execute("""CREATE TABLE IF NOT EXISTS sip_provider_pricing (
            id INT AUTO_INCREMENT PRIMARY KEY,
            provider_id INT NOT NULL,
            country VARCHAR(50) NOT NULL,
            trunk_name VARCHAR(100) NOT NULL,
            price_per_minute DECIMAL(10,4) NOT NULL,
            UNIQUE KEY uniq_provider_country (provider_id, country)
        )""")
        db.execute("""CREATE TABLE IF NOT EXISTS sip_deposits (
            id INT AUTO_INCREMENT PRIMARY KEY,
            provider_id INT NOT NULL,
            amount_usd DECIMAL(10,2) NOT NULL,
            reference VARCHAR(255),
            deposit_date DATE NOT NULL,
            created_at DATETIME DEFAULT CURRENT_TIMESTAMP
        )""")
        # Migración: si sip_providers ya existía (deploy anterior a
        # esta versión, sin la columna), agregarla sin tocar filas.
        col = db.q("""SELECT COLUMN_NAME FROM INFORMATION_SCHEMA.COLUMNS
                      WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = 'sip_providers'
                      AND COLUMN_NAME = 'billing_start_date'""")
        if not col:
            db.execute("ALTER TABLE sip_providers ADD COLUMN billing_start_date DATE NULL")
    else:
        db.execute("""CREATE TABLE IF NOT EXISTS sip_providers (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL,
            active INTEGER NOT NULL DEFAULT 1,
            billing_start_date DATE,
            created_at DATETIME DEFAULT CURRENT_TIMESTAMP
        )""")
        db.execute("""CREATE TABLE IF NOT EXISTS sip_provider_pricing (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            provider_id INTEGER NOT NULL,
            country TEXT NOT NULL,
            trunk_name TEXT NOT NULL,
            price_per_minute REAL NOT NULL,
            UNIQUE (provider_id, country)
        )""")
        db.execute("""CREATE TABLE IF NOT EXISTS sip_deposits (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            provider_id INTEGER NOT NULL,
            amount_usd REAL NOT NULL,
            reference TEXT,
            deposit_date DATE NOT NULL,
            created_at DATETIME DEFAULT CURRENT_TIMESTAMP
        )""")
        cols = db.q("PRAGMA table_info(sip_providers)")
        if not any(c['name'] == 'billing_start_date' for c in cols):
            db.execute("ALTER TABLE sip_providers ADD COLUMN billing_start_date DATE")

    # Seed inicial — SOLO si no hay ningún proveedor todavía. Usa los
    # nombres reales ya conectados en el VPS (pjsip.conf): un único
    # proveedor (cuenta 650098 @ 2.28.59.175) sirve tanto a India
    # (trunk 'proveedor1') como a México (trunk 'proveedor-mx'), al
    # mismo precio hoy — exactamente lo que ya se ve en el resto del
    # panel vía DEFAULT_RATE. Sin billing_start_date: en una instalación
    # nueva no hay saldo anterior que excluir, así que cuenta desde
    # siempre por defecto (configurable después desde el panel).
    existing = db.one("SELECT COUNT(*) AS n FROM sip_providers")
    if _int(existing.get('n')) == 0:
        provider_id = db.insert(
            "INSERT INTO sip_providers (name, active) VALUES (§, 1)",
            ('650098 @ 2.28.59.175',))
        db.execute("""INSERT INTO sip_provider_pricing
                      (provider_id, country, trunk_name, price_per_minute)
                      VALUES (§,§,§,§)""",
                   (provider_id, 'india', 'proveedor1', DEFAULT_RATE))
        db.execute("""INSERT INTO sip_provider_pricing
                      (provider_id, country, trunk_name, price_per_minute)
                      VALUES (§,§,§,§)""",
                   (provider_id, 'mexico', 'proveedor-mx', DEFAULT_RATE))
        db.execute("""INSERT INTO sip_provider_pricing
                      (provider_id, country, trunk_name, price_per_minute)
                      VALUES (§,§,§,§)""",
                   (provider_id, 'nepal', 'proveedor-nepal', 0.36))


def validate_sip_country(country):
    """
    Valida un país para SIP Balance contra COUNTRIES_CFG — sin caer
    silenciosamente a India como hace normalize_country() en el resto
    del panel. Acá un typo no puede convertirse en plata mal asignada:
    si el país no existe, es un error explícito, no un fallback.
    """
    country = (country or '').strip().lower()
    if country not in COUNTRIES_CFG:
        valid = ', '.join(sorted(COUNTRIES_CFG.keys()))
        raise ValueError(f"país desconocido '{country}' — válidos: {valid}")
    return country


def sip_providers_list(db):
    ensure_sip_tables(db)
    return db.q("SELECT id, name, active, billing_start_date FROM sip_providers ORDER BY name")


def sip_provider_balance(db, provider_id):
    """
    Balance completo de un proveedor: consumo real por país (CDR *
    precio configurado para ESE proveedor, no el DEFAULT_RATE del
    negocio) vs. depósitos registrados — histórico completo por
    defecto, o desde 'billing_start_date' si el proveedor tiene una
    fecha de corte configurada (para no mezclar un saldo/consumo
    anterior a que el sistema empezara a usar este trunk/proveedor).
    El corte aplica igual a consumo y a depósitos: ambos cuentan
    "desde esa fecha en adelante", nunca antes.
    """
    ensure_sip_tables(db)
    provider = db.one("""SELECT id, name, active, billing_start_date
                         FROM sip_providers WHERE id = §""", (provider_id,))
    if not provider:
        return None
    cutoff = provider.get('billing_start_date')  # None, o 'YYYY-MM-DD'

    pricing = db.q("""SELECT country, trunk_name, price_per_minute
                      FROM sip_provider_pricing WHERE provider_id = §
                      ORDER BY country""", (provider_id,))

    consumption, total_consumption = [], 0.0
    for row in pricing:
        country = row['country']
        price = _float(row['price_per_minute'])
        sql = f"""SELECT COALESCE(SUM(CEIL(billsec/60.0)), 0) AS billed_minutes
                 FROM cdr WHERE {cdr_valid_dst(country)}"""
        params = ()
        if cutoff:
            sql += " AND calldate >= §"
            params = (str(cutoff),)
        r = db.one(sql, params)
        minutes = _int(r.get('billed_minutes'))
        cost = round(minutes * price, 2)
        total_consumption += cost
        consumption.append({
            'country'         : country,
            'country_label'   : COUNTRIES_CFG.get(country, {}).get('label', country.title()),
            'trunk_name'      : row['trunk_name'],
            'price_per_minute': price,
            'billed_minutes'  : minutes,
            'total'           : cost,
        })

    dep_sql = """SELECT id, amount_usd, reference, deposit_date
                FROM sip_deposits WHERE provider_id = §"""
    dep_params = [provider_id]
    if cutoff:
        dep_sql += " AND deposit_date >= §"
        dep_params.append(str(cutoff))
    dep_sql += " ORDER BY deposit_date DESC, id DESC"
    deposits = db.q(dep_sql, tuple(dep_params))
    for d in deposits:
        d['amount_usd'] = _float(d['amount_usd'])
    total_deposits = round(sum(d['amount_usd'] for d in deposits), 2)
    total_consumption = round(total_consumption, 2)

    return {
        'provider'          : provider,
        'billing_start_date': cutoff,
        'consumption'       : consumption,
        'total_consumption' : total_consumption,
        'deposits'          : deposits,
        'total_deposits'    : total_deposits,
        'balance'           : round(total_deposits - total_consumption, 2),
    }


def sip_provider_set_billing_start(db, provider_id, date_str):
    """
    Fija (o borra, si date_str viene vacío) la fecha de corte de un
    proveedor: todo antes de esa fecha se excluye del cálculo de
    consumo Y de depósitos (saldo anterior al uso de este proveedor,
    que no se lleva por acá).
    """
    ensure_sip_tables(db)
    provider = db.one("SELECT id FROM sip_providers WHERE id = §", (provider_id,))
    if not provider:
        raise ValueError('proveedor no encontrado')

    date_str = (date_str or '').strip()
    if not date_str:
        db.execute("UPDATE sip_providers SET billing_start_date = NULL WHERE id = §", (provider_id,))
        return

    try:
        datetime.strptime(date_str, '%Y-%m-%d')
    except ValueError:
        raise ValueError('fecha inválida — usar formato YYYY-MM-DD')
    db.execute("UPDATE sip_providers SET billing_start_date = § WHERE id = §", (date_str, provider_id))


def sip_provider_create(db, name, pricing):
    """
    Crea un proveedor nuevo con su pricing inicial por país.
    pricing: lista de dicts {country, trunk_name, price}.
    Lanza ValueError si el nombre está vacío, no hay pricing, o algún
    país no existe en COUNTRIES_CFG (ver validate_sip_country) — nunca
    crea una fila a medias.
    """
    ensure_sip_tables(db)
    name = (name or '').strip()
    if not name:
        raise ValueError('el nombre del proveedor es obligatorio')
    if not pricing:
        raise ValueError('hace falta al menos un país con precio')

    clean = []
    for p in pricing:
        country = validate_sip_country(p.get('country'))
        trunk_name = (p.get('trunk_name') or '').strip()
        if not trunk_name:
            raise ValueError(f"falta el nombre del trunk pjsip para '{country}'")
        try:
            price = float(p.get('price'))
        except (TypeError, ValueError):
            raise ValueError(f"precio inválido para '{country}'")
        if price <= 0:
            raise ValueError(f"el precio de '{country}' debe ser mayor a 0")
        clean.append((country, trunk_name, price))

    provider_id = db.insert("INSERT INTO sip_providers (name, active) VALUES (§, 1)", (name,))
    for country, trunk_name, price in clean:
        db.execute("""INSERT INTO sip_provider_pricing
                      (provider_id, country, trunk_name, price_per_minute)
                      VALUES (§,§,§,§)""", (provider_id, country, trunk_name, price))
    return provider_id


def sip_provider_pricing_upsert(db, provider_id, country, trunk_name, price):
    """Agrega o actualiza el precio de un país para un proveedor ya
    existente — ej. agregar Dubai a un proveedor que ya tiene India."""
    ensure_sip_tables(db)
    country = validate_sip_country(country)
    trunk_name = (trunk_name or '').strip()
    if not trunk_name:
        raise ValueError('falta el nombre del trunk pjsip')
    try:
        price = float(price)
    except (TypeError, ValueError):
        raise ValueError('precio inválido')
    if price <= 0:
        raise ValueError('el precio debe ser mayor a 0')

    existing = db.one("""SELECT id FROM sip_provider_pricing
                         WHERE provider_id = § AND country = §""", (provider_id, country))
    if existing:
        db.execute("""UPDATE sip_provider_pricing SET trunk_name = §, price_per_minute = §
                      WHERE id = §""", (trunk_name, price, existing['id']))
    else:
        db.execute("""INSERT INTO sip_provider_pricing
                      (provider_id, country, trunk_name, price_per_minute)
                      VALUES (§,§,§,§)""", (provider_id, country, trunk_name, price))


def sip_deposit_add(db, provider_id, amount, reference, deposit_date):
    ensure_sip_tables(db)
    try:
        amount = float(amount)
    except (TypeError, ValueError):
        raise ValueError('monto inválido')
    if amount <= 0:
        raise ValueError('el monto debe ser mayor a 0')
    if not deposit_date:
        raise ValueError('falta la fecha del depósito')
    provider = db.one("SELECT id FROM sip_providers WHERE id = §", (provider_id,))
    if not provider:
        raise ValueError('proveedor no encontrado')

    return db.insert("""INSERT INTO sip_deposits (provider_id, amount_usd, reference, deposit_date)
                        VALUES (§,§,§,§)""",
                     (provider_id, amount, (reference or '').strip(), deposit_date))


def sip_deposit_delete(db, deposit_id):
    ensure_sip_tables(db)
    db.execute("DELETE FROM sip_deposits WHERE id = §", (deposit_id,))


# ══════════════════════════════════════════════════════════════════
#  SIP EXTENSIONS — llamadas manuales desde softphone (celular/PC)
#  ────────────────────────────────────────────────────────────────
#  Cada extensión es un endpoint PJSIP interno (no un trunk) al que
#  te conectás con un softphone (Zoiper, Linphone, MicroSIP...) y
#  desde ahí marcás manualmente. La llamada sale por el trunk del
#  proveedor asociado, exactamente igual que las automáticas de WF2
#  — mismo trunk, mismo costo por minuto, mismo CDR. Por eso el
#  consumo de una extensión NO se suma aparte en SIP Balance: ya
#  está incluido ahí (es el mismo CDR). Esta sección es un desglose
#  — qué parte de ese consumo salió de cada extensión — filtrando
#  por 'channel' (Asterisk registra el endpoint origen ahí).
#
#  Los datos viven en MySQL (igual que providers/pricing/deposits),
#  pero para que la extensión funcione DE VERDAD en Asterisk hace
#  falta además escribir su configuración PJSIP + dialplan a disco
#  y recargar Asterisk — eso es lo que hace sip_extensions_apply().
# ══════════════════════════════════════════════════════════════════

# Rutas de los archivos que el panel genera e incluye desde la
# config real de Asterisk (ver instrucciones de instalación — hay
# que agregar un '#include' una única vez a mano en pjsip.conf y
# en extensions.conf; después de eso el panel reescribe SOLO estos
# dos archivos, nunca los originales).
AST_PJSIP_INCLUDE    = os.getenv('LM_AST_PJSIP_INCLUDE', '/etc/asterisk/landmark-extensions-pjsip.conf')
AST_DIALPLAN_INCLUDE = os.getenv('LM_AST_DIALPLAN_INCLUDE', '/etc/asterisk/landmark-extensions-dialplan.conf')
AST_HOST             = os.getenv('LM_AST_HOST', '200.141.5.142')   # para mostrar en la ficha del softphone
AST_PORT             = os.getenv('LM_AST_PORT', '5060')

# Validación estricta — esto se inyecta LITERAL en archivos de config
# de Asterisk. Un usuario/password con '[' ']' o un salto de línea
# podría cerrar una sección e inyectar directivas arbitrarias. Nunca
# se relaja esto, incluso para valores que "total, total, es admin
# el único que carga esto" — el whitelist es la única defensa real.
_EXT_NUMBER_RE = re.compile(r'^[0-9]{2,6}$')
_EXT_CRED_RE   = re.compile(r'^[A-Za-z0-9_\-\.]{4,64}$')
_EXT_LABEL_RE  = re.compile(r'^[A-Za-z0-9À-ÿ_\-\.\s]{0,80}$')


def _validate_extension_number(n):
    n = (n or '').strip()
    if not _EXT_NUMBER_RE.match(n):
        raise ValueError('extension number must be 2–6 digits (e.g. 100)')
    return n


def _validate_sip_credential(v, field):
    v = (v or '').strip()
    if not _EXT_CRED_RE.match(v):
        raise ValueError(f'{field} must be 4–64 chars, letters/numbers/._- only (no spaces)')
    return v


def _validate_label(v):
    v = (v or '').strip()
    if not _EXT_LABEL_RE.match(v):
        raise ValueError('label must be plain text, max 80 chars')
    return v


def ensure_sip_extensions_table(db):
    """Idempotente, igual criterio que ensure_sip_tables()."""
    if db.driver == 'mysql':
        db.execute("""CREATE TABLE IF NOT EXISTS sip_extensions (
            id INT AUTO_INCREMENT PRIMARY KEY,
            provider_id INT NOT NULL,
            extension_number VARCHAR(10) NOT NULL UNIQUE,
            sip_username VARCHAR(64) NOT NULL,
            sip_password VARCHAR(64) NOT NULL,
            label VARCHAR(80),
            active TINYINT(1) NOT NULL DEFAULT 1,
            created_at DATETIME DEFAULT CURRENT_TIMESTAMP
        )""")
    else:
        db.execute("""CREATE TABLE IF NOT EXISTS sip_extensions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            provider_id INTEGER NOT NULL,
            extension_number TEXT NOT NULL UNIQUE,
            sip_username TEXT NOT NULL,
            sip_password TEXT NOT NULL,
            label TEXT,
            active INTEGER NOT NULL DEFAULT 1,
            created_at DATETIME DEFAULT CURRENT_TIMESTAMP
        )""")


def sip_extensions_list(db, provider_id=None):
    ensure_sip_tables(db)
    ensure_sip_extensions_table(db)
    if provider_id:
        return db.q("""SELECT id, provider_id, extension_number, sip_username, sip_password,
                       label, active FROM sip_extensions WHERE provider_id = §
                       ORDER BY extension_number""", (provider_id,))
    return db.q("""SELECT id, provider_id, extension_number, sip_username, sip_password,
                   label, active FROM sip_extensions ORDER BY extension_number""")


def sip_extension_create(db, provider_id, extension_number, sip_username, sip_password, label):
    ensure_sip_tables(db)
    ensure_sip_extensions_table(db)
    provider = db.one("SELECT id FROM sip_providers WHERE id = §", (provider_id,))
    if not provider:
        raise ValueError('provider not found')

    extension_number = _validate_extension_number(extension_number)
    sip_username = _validate_sip_credential(sip_username or extension_number, 'username')
    sip_password = _validate_sip_credential(sip_password, 'password')
    label = _validate_label(label)

    existing = db.one("SELECT id FROM sip_extensions WHERE extension_number = §", (extension_number,))
    if existing:
        raise ValueError(f'extension {extension_number} already exists')

    return db.insert("""INSERT INTO sip_extensions
                        (provider_id, extension_number, sip_username, sip_password, label, active)
                        VALUES (§,§,§,§,§,1)""",
                     (provider_id, extension_number, sip_username, sip_password, label))


def sip_extension_toggle(db, extension_id, active):
    ensure_sip_extensions_table(db)
    db.execute("UPDATE sip_extensions SET active = § WHERE id = §", (1 if active else 0, extension_id))


def sip_extension_delete(db, extension_id):
    ensure_sip_extensions_table(db)
    db.execute("DELETE FROM sip_extensions WHERE id = §", (extension_id,))


def sip_extension_consumption(db, extension):
    """
    Desglose de consumo de UNA extensión, por país — mismo precio y
    mismo corte de fecha (billing_start_date) que ya tiene configurado
    su proveedor en SIP Balance, filtrando el CDR por el canal que
    generó la llamada (PJSIP/ext<numero>-...). Es un subconjunto del
    consumo del proveedor, no algo aparte — nunca se suma dos veces.
    """
    ensure_sip_tables(db)
    provider = db.one("""SELECT billing_start_date FROM sip_providers WHERE id = §""",
                      (extension['provider_id'],))
    cutoff = provider.get('billing_start_date') if provider else None

    pricing = db.q("""SELECT country, trunk_name, price_per_minute
                      FROM sip_provider_pricing WHERE provider_id = §
                      ORDER BY country""", (extension['provider_id'],))

    endpoint = 'ext' + extension['extension_number']
    breakdown, total = [], 0.0
    for row in pricing:
        country = row['country']
        price = _float(row['price_per_minute'])
        sql = f"""SELECT COALESCE(SUM(CEIL(billsec/60.0)), 0) AS billed_minutes
                 FROM cdr WHERE {cdr_valid_dst(country)}
                 AND channel LIKE §"""
        params = [f'PJSIP/{endpoint}-%']
        if cutoff:
            sql += " AND calldate >= §"
            params.append(str(cutoff))
        r = db.one(sql, tuple(params))
        minutes = _int(r.get('billed_minutes'))
        cost = round(minutes * price, 2)
        total += cost
        if minutes > 0:
            breakdown.append({
                'country'       : country,
                'country_label' : COUNTRIES_CFG.get(country, {}).get('label', country.title()),
                'billed_minutes': minutes,
                'total'         : cost,
            })
    return {'breakdown': breakdown, 'total': round(total, 2)}


def _build_pjsip_extensions_conf(extensions, host):
    """
    Genera el contenido completo de AST_PJSIP_INCLUDE: un
    endpoint+aor+auth por cada extensión ACTIVA. Extensiones inactivas
    simplemente no se incluyen (Asterisk deja de aceptar registros de
    ellas en el próximo reload, sin borrar nada de la base).
    """
    lines = [
        '; ══════════════════════════════════════════════════════════',
        '; Generado por Landmark Panel — SIP Extensions.',
        '; NO EDITAR A MANO: se sobreescribe en cada cambio desde el panel.',
        '; ══════════════════════════════════════════════════════════',
        '',
    ]
    for ext in extensions:
        if not ext['active']:
            continue
        endpoint = 'ext' + ext['extension_number']
        lines += [
            f'[{endpoint}]',
            'type=endpoint',
            'context=landmark-extensions-out',
            'disallow=all',
            'allow=ulaw,alaw,g722',
            f'auth={endpoint}-auth',
            f'aors={endpoint}-aor',
            'direct_media=no',
            '',
            f'[{endpoint}-auth]',
            'type=auth',
            'auth_type=userpass',
            f'username={ext["sip_username"]}',
            f'password={ext["sip_password"]}',
            '',
            f'[{endpoint}-aor]',
            'type=aor',
            'max_contacts=2',
            'remove_existing=yes',
            '',
        ]
    return '\n'.join(lines) + '\n'


def _build_dialplan_extensions_conf(db, extensions):
    """
    Genera el dialplan de salida para todas las extensiones activas:
    un patrón _+<prefijo>. por cada país que tenga precio configurado
    en el proveedor de esa extensión, marcando directo por el trunk
    (trunk_name) de ese país — mismo trunk que usan las llamadas
    automáticas de WF2, mismo costo.
    """
    lines = [
        '; ══════════════════════════════════════════════════════════',
        '; Generado por Landmark Panel — SIP Extensions.',
        '; NO EDITAR A MANO: se sobreescribe en cada cambio desde el panel.',
        '; ══════════════════════════════════════════════════════════',
        '',
        '[landmark-extensions-out]',
    ]
    # Un mismo contexto sirve para todas las extensiones activas — el
    # trunk se resuelve por el prefijo marcado, no por quién marca.
    seen_patterns = set()
    provider_ids = {ext['provider_id'] for ext in extensions if ext['active']}
    for provider_id in provider_ids:
        pricing = db.q("""SELECT country, trunk_name FROM sip_provider_pricing
                          WHERE provider_id = §""", (provider_id,))
        for row in pricing:
            country = row['country']
            prefix = COUNTRIES_CFG.get(country, {}).get('dial_prefix')
            if not prefix or prefix in seen_patterns:
                continue
            seen_patterns.add(prefix)
            trunk = row['trunk_name']
            lines += [
                f'exten => _+{prefix}.,1,NoOp(Landmark ext call -> {trunk})',
                ' same => n,Dial(PJSIP/${EXTEN}@' + trunk + ',60)',
                ' same => n,Hangup()',
                '',
            ]
    lines += ['exten => _X.,1,NoOp(Landmark ext — no route configured for this prefix)',
              ' same => n,Congestion()', '']
    return '\n'.join(lines) + '\n'


def sip_extensions_apply(db):
    """
    Escribe la config generada a los 2 archivos include y recarga
    Asterisk (pjsip + dialplan) — sin esto, crear/editar/borrar una
    extensión en el panel NO tiene efecto real hasta que se aplica.

    Requiere que el usuario del sistema que corre el panel (panel_rw
    a nivel de proceso, no de MySQL) pueda escribir esos 2 archivos y
    ejecutar 'asterisk -rx' — ver instrucciones de instalación. Si
    falta el permiso, esto lanza ValueError con el mensaje exacto de
    qué falló, igual que hicimos con el GRANT de MySQL — nunca falla
    en silencio.
    """
    extensions = sip_extensions_list(db)

    pjsip_conf = _build_pjsip_extensions_conf(extensions, AST_HOST)
    dialplan_conf = _build_dialplan_extensions_conf(db, extensions)

    try:
        with open(AST_PJSIP_INCLUDE, 'w') as fh:
            fh.write(pjsip_conf)
        with open(AST_DIALPLAN_INCLUDE, 'w') as fh:
            fh.write(dialplan_conf)
    except OSError as ex:
        raise ValueError(
            f"could not write Asterisk config files ({ex}). "
            f"The panel process needs write access to {AST_PJSIP_INCLUDE} and "
            f"{AST_DIALPLAN_INCLUDE} — see install instructions (chown/chmod).")

    for cmd_desc, cmd in [('pjsip reload', ['sudo', 'asterisk', '-rx', 'pjsip reload']),
                          ('dialplan reload', ['sudo', 'asterisk', '-rx', 'dialplan reload'])]:
        try:
            result = subprocess.run(cmd, capture_output=True, text=True, timeout=15)
        except (OSError, subprocess.SubprocessError) as ex:
            raise ValueError(f"could not run '{cmd_desc}' ({ex}). "
                             f"Check that the panel's sudoers rule for 'asterisk -rx' is set up.")
        if result.returncode != 0:
            raise ValueError(f"'{cmd_desc}' failed: {result.stderr.strip() or result.stdout.strip()}")

    return {'applied': True, 'active_extensions': sum(1 for e in extensions if e['active'])}


# ══════════════════════════════════════════════════════════════════
#  CALL CENTER SWITCHES — encender/apagar grupos de workflows n8n
#  ────────────────────────────────────────────────────────────────
#  Un "switch" es simplemente: una etiqueta (ej. "India", "México",
#  "Compartido WF9-14") + una lista de IDs de workflow de n8n. El
#  panel no sabe nada de qué hace cada workflow — solo prende/apaga
#  los IDs que el usuario metió en ese switch, vía la API pública de
#  n8n. Todo editable desde el panel: crear switch, agregar/sacar
#  workflows de un switch existente, borrar switch — sin tocar código
#  ni volver a desplegar una versión nueva del panel para cada cambio
#  de qué workflows van en cada botón.
# ══════════════════════════════════════════════════════════════════

N8N_BASE_URL = os.getenv('LM_N8N_BASE_URL', 'https://landmarket-n8n.dhsoig.easypanel.host')
N8N_API_KEY  = os.getenv('LM_N8N_API_KEY', '')
N8N_TIMEOUT  = 15

_WORKFLOW_ID_RE = re.compile(r'^[A-Za-z0-9_-]{1,64}$')
# Label de switch: más permisivo que el de extensiones SIP (no se
# escribe en config de Asterisk, solo vive en la DB del panel y se
# muestra vía Jinja, que ya escapa HTML) — pero igual sin saltos de
# línea ni caracteres de control, para que no rompa el layout de la UI.
_SWITCH_LABEL_RE = re.compile(r'^[^\r\n\t]{1,80}$')


def _validate_switch_label(v):
    v = (v or '').strip()
    if not v or not _SWITCH_LABEL_RE.match(v):
        raise ValueError('label must be 1–80 chars, no line breaks')
    return v


def _n8n_request(method, path, body=None):
    if not N8N_API_KEY:
        raise ValueError("n8n API key not configured — set LM_N8N_API_KEY in the panel's .env "
                         "and restart the service.")
    url = N8N_BASE_URL.rstrip('/') + path
    data = json.dumps(body).encode('utf-8') if body is not None else None
    req = urllib.request.Request(url, data=data, method=method)
    req.add_header('X-N8N-API-KEY', N8N_API_KEY)
    req.add_header('Accept', 'application/json')
    if data is not None:
        req.add_header('Content-Type', 'application/json')
    try:
        with urllib.request.urlopen(req, timeout=N8N_TIMEOUT) as resp:
            raw = resp.read()
            return json.loads(raw) if raw else {}
    except urllib.error.HTTPError as ex:
        detail = ex.read().decode('utf-8', errors='replace')[:300]
        raise ValueError(f"n8n API error {ex.code} on {method} {path}: {detail}")
    except urllib.error.URLError as ex:
        raise ValueError(f"could not reach n8n at {N8N_BASE_URL} ({ex.reason})")
    except TimeoutError:
        raise ValueError(f"n8n at {N8N_BASE_URL} did not respond in {N8N_TIMEOUT}s")


def n8n_fetch_workflows():
    """
    Lista TODOS los workflows que existen en la instancia de n8n
    (id, name, active) — se usa para el picker del panel (así el
    usuario elige por nombre, sin tener que ir a copiar el ID a mano
    desde la URL de cada workflow) y para pintar el estado real de
    cada switch. Pagina automáticamente si hay más de 250.
    """
    items, cursor = [], None
    while True:
        path = '/api/v1/workflows?limit=250'
        if cursor:
            path += f'&cursor={cursor}'
        result = _n8n_request('GET', path)
        page = result.get('data', result if isinstance(result, list) else [])
        items.extend(page)
        cursor = result.get('nextCursor') if isinstance(result, dict) else None
        if not cursor:
            break
    return [{'id': str(w['id']), 'name': w.get('name', '(sin nombre)'), 'active': bool(w.get('active'))}
            for w in items]


def n8n_workflow_activate(workflow_id):
    _n8n_request('POST', f'/api/v1/workflows/{workflow_id}/activate')


def n8n_workflow_deactivate(workflow_id):
    _n8n_request('POST', f'/api/v1/workflows/{workflow_id}/deactivate')


def ensure_n8n_switches_table(db):
    if db.driver == 'mysql':
        db.execute("""CREATE TABLE IF NOT EXISTS n8n_switches (
            id INT AUTO_INCREMENT PRIMARY KEY,
            label VARCHAR(80) NOT NULL,
            workflow_ids TEXT NOT NULL,
            created_at DATETIME DEFAULT CURRENT_TIMESTAMP
        )""")
    else:
        db.execute("""CREATE TABLE IF NOT EXISTS n8n_switches (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            label TEXT NOT NULL,
            workflow_ids TEXT NOT NULL,
            created_at DATETIME DEFAULT CURRENT_TIMESTAMP
        )""")


def _validate_workflow_ids(workflow_ids):
    ids = [w.strip() for w in workflow_ids if w and w.strip()]
    if not ids:
        raise ValueError('select at least one workflow')
    seen = set()
    clean = []
    for wid in ids:
        if not _WORKFLOW_ID_RE.match(wid):
            raise ValueError(f'invalid workflow id: {wid!r}')
        if wid not in seen:
            seen.add(wid)
            clean.append(wid)
    return clean


def n8n_switches_list(db):
    ensure_n8n_switches_table(db)
    rows = db.q("SELECT id, label, workflow_ids FROM n8n_switches ORDER BY id")
    for r in rows:
        r['workflow_ids'] = [w for w in r['workflow_ids'].split(',') if w]
    return rows


def n8n_switch_create(db, label, workflow_ids):
    ensure_n8n_switches_table(db)
    label = _validate_switch_label(label)
    ids = _validate_workflow_ids(workflow_ids)
    return db.insert("INSERT INTO n8n_switches (label, workflow_ids) VALUES (§,§)",
                     (label, ','.join(ids)))


def n8n_switch_update(db, switch_id, label, workflow_ids):
    ensure_n8n_switches_table(db)
    existing = db.one("SELECT id FROM n8n_switches WHERE id = §", (switch_id,))
    if not existing:
        raise ValueError('switch not found')
    label = _validate_switch_label(label)
    ids = _validate_workflow_ids(workflow_ids)
    db.execute("UPDATE n8n_switches SET label = §, workflow_ids = § WHERE id = §",
              (label, ','.join(ids), switch_id))


def n8n_switch_delete(db, switch_id):
    ensure_n8n_switches_table(db)
    db.execute("DELETE FROM n8n_switches WHERE id = §", (switch_id,))


def n8n_switches_with_status(db):
    """
    Trae todos los switches guardados y les pega encima el estado real
    (nombre + activo/inactivo) de cada workflow, con UNA sola llamada
    a n8n (no una por workflow). Si n8n no responde, cada switch queda
    con status_error seteado y sin datos de estado — el CRUD de
    switches sigue funcionando igual (viven en la DB del panel, no en
    n8n), solo no se puede saber/cambiar su estado en ese momento.
    """
    switches = n8n_switches_list(db)
    try:
        workflows = n8n_fetch_workflows()
        by_id = {w['id']: w for w in workflows}
        status_error = None
    except ValueError as ex:
        by_id, status_error = {}, str(ex)

    for sw in switches:
        sw['workflows'] = []
        for wid in sw['workflow_ids']:
            w = by_id.get(wid)
            if w:
                sw['workflows'].append(w)
            else:
                sw['workflows'].append({'id': wid, 'name': '(not found in n8n)', 'active': None})
        active_count = sum(1 for w in sw['workflows'] if w['active'] is True)
        sw['active_count'] = active_count
        sw['total_count'] = len(sw['workflows'])
        sw['all_on'] = status_error is None and active_count == len(sw['workflows']) and active_count > 0
        sw['all_off'] = status_error is None and active_count == 0
    return switches, status_error


def n8n_switch_set_state(db, switch_id, turn_on):
    """
    Prende o apaga TODOS los workflows de un switch. Best-effort: si
    uno falla (ej. n8n devuelve error para un ID puntual), sigue con
    los demás y junta los errores — así un solo workflow con problema
    no te deja el resto sin poder apagarse.
    """
    ensure_n8n_switches_table(db)
    row = db.one("SELECT workflow_ids FROM n8n_switches WHERE id = §", (switch_id,))
    if not row:
        raise ValueError('switch not found')
    ids = [w for w in row['workflow_ids'].split(',') if w]

    action = n8n_workflow_activate if turn_on else n8n_workflow_deactivate
    ok, errors = [], []
    for wid in ids:
        try:
            action(wid)
            ok.append(wid)
        except ValueError as ex:
            errors.append(f'{wid}: {ex}')
    if errors:
        raise ValueError('some workflows failed to ' + ('activate' if turn_on else 'deactivate') +
                         ': ' + '; '.join(errors))
    return {'changed': ok}


# ══════════════════════════════════════════════════════════════════
#  CALL CENTER SCHEDULES — prender/apagar switches automáticamente
#  ────────────────────────────────────────────────────────────────
#  Cada switch (ver sección de arriba) puede tener UN horario
#  configurado: hora de encendido, hora de apagado, zona horaria
#  propia (así "17:30 hora Dubai" se calcula bien incluso si el VPS
#  está en otro huso) y qué días de la semana aplica. Un hilo en
#  background (uno por proceso de gunicorn) revisa cada ~20s si algún
#  horario "cae" ahora mismo y dispara la acción — con una reclamación
#  atómica en la DB para que, aunque haya varios workers de gunicorn
#  corriendo el mismo chequeo al mismo tiempo, la acción se dispare
#  UNA sola vez por día por horario (no 3 veces, una por worker).
# ══════════════════════════════════════════════════════════════════

_WEEKDAY_CODES = ['mon', 'tue', 'wed', 'thu', 'fri', 'sat', 'sun']
_HHMM_RE = re.compile(r'^([01]\d|2[0-3]):[0-5]\d$')

# Zonas más probables para este proyecto — el selector del panel las
# ofrece primero, pero se acepta cualquier zona IANA válida además de
# estas (se valida contra la lista real de Python, no contra este
# subset).
COMMON_TIMEZONES = [
    'Asia/Dubai', 'Asia/Kolkata', 'America/Mexico_City',
    'America/Bogota', 'America/Caracas', 'UTC',
]


def _validate_timezone(tz):
    tz = (tz or '').strip()
    if tz not in available_timezones():
        raise ValueError(f'unknown timezone: {tz!r}')
    return tz


def _validate_hhmm(v, field):
    v = (v or '').strip()
    if not _HHMM_RE.match(v):
        raise ValueError(f'{field} must be HH:MM (24h), e.g. 17:30')
    return v


def _validate_days(days):
    days = [d.strip().lower()[:3] for d in days if d and d.strip()]
    if not days:
        raise ValueError('select at least one day')
    seen, clean = set(), []
    for d in days:
        if d not in _WEEKDAY_CODES:
            raise ValueError(f'invalid day: {d!r}')
        if d not in seen:
            seen.add(d)
            clean.append(d)
    # Mantener siempre el orden lun..dom, sin importar en qué orden
    # vinieron los checkboxes del form — hace que el resumen mostrado
    # en la UI sea consistente.
    return [d for d in _WEEKDAY_CODES if d in clean]


def ensure_n8n_schedules_table(db):
    if db.driver == 'mysql':
        db.execute("""CREATE TABLE IF NOT EXISTS n8n_switch_schedules (
            id INT AUTO_INCREMENT PRIMARY KEY,
            switch_id INT NOT NULL UNIQUE,
            timezone VARCHAR(64) NOT NULL,
            on_time VARCHAR(5) NOT NULL,
            off_time VARCHAR(5) NOT NULL,
            days VARCHAR(24) NOT NULL,
            enabled TINYINT NOT NULL DEFAULT 1,
            last_on_date VARCHAR(10),
            last_off_date VARCHAR(10),
            last_error TEXT,
            last_checked_at DATETIME,
            created_at DATETIME DEFAULT CURRENT_TIMESTAMP
        )""")
    else:
        db.execute("""CREATE TABLE IF NOT EXISTS n8n_switch_schedules (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            switch_id INTEGER NOT NULL UNIQUE,
            timezone TEXT NOT NULL,
            on_time TEXT NOT NULL,
            off_time TEXT NOT NULL,
            days TEXT NOT NULL,
            enabled INTEGER NOT NULL DEFAULT 1,
            last_on_date TEXT,
            last_off_date TEXT,
            last_error TEXT,
            last_checked_at DATETIME,
            created_at DATETIME DEFAULT CURRENT_TIMESTAMP
        )""")


def schedule_get(db, switch_id):
    ensure_n8n_schedules_table(db)
    row = db.one("SELECT * FROM n8n_switch_schedules WHERE switch_id = §", (switch_id,))
    if not row:
        return None
    row['days'] = [d for d in row['days'].split(',') if d]
    return row


def schedules_all(db):
    ensure_n8n_schedules_table(db)
    rows = db.q("SELECT * FROM n8n_switch_schedules")
    for r in rows:
        r['days'] = [d for d in r['days'].split(',') if d]
    return rows


def schedule_upsert(db, switch_id, timezone, on_time, off_time, days, enabled=True):
    ensure_n8n_schedules_table(db)
    switch = db.one("SELECT id FROM n8n_switches WHERE id = §", (switch_id,))
    if not switch:
        raise ValueError('switch not found')

    tz = _validate_timezone(timezone)
    on_t = _validate_hhmm(on_time, 'on time')
    off_t = _validate_hhmm(off_time, 'off time')
    day_list = _validate_days(days)

    existing = db.one("SELECT id FROM n8n_switch_schedules WHERE switch_id = §", (switch_id,))
    if existing:
        db.execute("""UPDATE n8n_switch_schedules
                      SET timezone=§, on_time=§, off_time=§, days=§, enabled=§
                      WHERE switch_id = §""",
                  (tz, on_t, off_t, ','.join(day_list), 1 if enabled else 0, switch_id))
        return existing['id']
    return db.insert("""INSERT INTO n8n_switch_schedules
                        (switch_id, timezone, on_time, off_time, days, enabled)
                        VALUES (§,§,§,§,§,§)""",
                     (switch_id, tz, on_t, off_t, ','.join(day_list), 1 if enabled else 0))


def schedule_set_enabled(db, switch_id, enabled):
    ensure_n8n_schedules_table(db)
    db.execute("UPDATE n8n_switch_schedules SET enabled = § WHERE switch_id = §",
              (1 if enabled else 0, switch_id))


def schedule_delete(db, switch_id):
    ensure_n8n_schedules_table(db)
    db.execute("DELETE FROM n8n_switch_schedules WHERE switch_id = §", (switch_id,))


def _claim(db, schedule_id, column, date_str):
    """
    Reclamo atómico: intenta marcar `column` (last_on_date u
    last_off_date) con la fecha de hoy, PERO solo si todavía no
    estaba marcada con esa fecha. El UPDATE con esa condición en el
    WHERE es atómico a nivel de fila — si dos workers de gunicorn lo
    corren al mismo tiempo, la base de datos solo deja pasar a UNO
    (rowcount=1 para el que gana, 0 para el resto). Así, aunque el
    scheduler corra en paralelo en varios procesos, la acción se
    dispara una sola vez por día.
    """
    _, rowcount = db.execute(
        f"UPDATE n8n_switch_schedules SET {column} = § "
        f"WHERE id = § AND ({column} IS NULL OR {column} != §)",
        (date_str, schedule_id, date_str))
    return rowcount > 0


def run_due_schedules(db):
    """
    Un solo barrido: revisa todos los horarios habilitados y dispara
    ON/OFF los que correspondan ahora mismo. Pensado para llamarse
    cada ~20-30s desde un loop en background. Nunca lanza — cualquier
    error de un horario puntual (zona horaria rota, n8n caído) se
    guarda en su propia fila (last_error) y se sigue con los demás.
    """
    now_utc_marker = datetime.now(ZoneInfo('UTC')).strftime('%Y-%m-%d %H:%M:%S')
    for sched in schedules_all(db):
        if not sched['enabled']:
            continue
        try:
            tz = ZoneInfo(sched['timezone'])
        except Exception as ex:
            db.execute("UPDATE n8n_switch_schedules SET last_error=§, last_checked_at=§ WHERE id=§",
                      (f'invalid timezone: {ex}', now_utc_marker, sched['id']))
            continue

        now_local = datetime.now(tz)
        today_str = now_local.strftime('%Y-%m-%d')
        current_hm = now_local.strftime('%H:%M')
        weekday = _WEEKDAY_CODES[now_local.weekday()]
        db.execute("UPDATE n8n_switch_schedules SET last_checked_at=§ WHERE id=§",
                  (now_utc_marker, sched['id']))

        if weekday not in sched['days']:
            continue

        if current_hm == sched['on_time'] and sched['last_on_date'] != today_str:
            if _claim(db, sched['id'], 'last_on_date', today_str):
                try:
                    n8n_switch_set_state(db, sched['switch_id'], turn_on=True)
                    db.execute("UPDATE n8n_switch_schedules SET last_error=NULL WHERE id=§", (sched['id'],))
                except ValueError as ex:
                    db.execute("UPDATE n8n_switch_schedules SET last_error=§ WHERE id=§",
                              (f'scheduled ON at {sched["on_time"]} failed: {ex}', sched['id']))

        if current_hm == sched['off_time'] and sched['last_off_date'] != today_str:
            if _claim(db, sched['id'], 'last_off_date', today_str):
                try:
                    n8n_switch_set_state(db, sched['switch_id'], turn_on=False)
                    db.execute("UPDATE n8n_switch_schedules SET last_error=NULL WHERE id=§", (sched['id'],))
                except ValueError as ex:
                    db.execute("UPDATE n8n_switch_schedules SET last_error=§ WHERE id=§",
                              (f'scheduled OFF at {sched["off_time"]} failed: {ex}', sched['id']))


def scheduler_loop(get_db_fn, interval=20, stop_event=None):
    """
    Loop de background — un proceso gunicorn = un hilo de estos. No
    hace falta coordinarlos entre sí más allá de la reclamación
    atómica de run_due_schedules(): si 3 workers corren este loop a
    la vez, cada barrido de los 3 intenta reclamar, gana uno solo.
    """
    while not (stop_event and stop_event.is_set()):
        try:
            db = get_db_fn()
            try:
                run_due_schedules(db)
            finally:
                try:
                    db.conn.close()
                except Exception:
                    pass
        except Exception:
            pass  # nunca tirar el hilo por un error puntual (DB caída, etc.)
        (stop_event.wait(interval) if stop_event else time.sleep(interval))


# ══════════════════════════════════════════════════════════════════
#  Support panel — apertura de cuenta y payment links manuales
#  (misma lógica que ya usa el bot: dispara los webhooks existentes
#  de WF3 y WF7, sin duplicar workflows en n8n)
# ══════════════════════════════════════════════════════════════════

# Webhook paths ya activos en n8n (WF3 / WF7). México queda en None
# hasta que el usuario pase esos workflows — el panel avisa con un
# mensaje claro en vez de fallar silenciosamente.
SUPPORT_WEBHOOKS = {
    'india': {
        'account_open': '/webhook/elevenlabs-call-webhook',   # WF3
        'payment_link': '/webhook/okpay-payment-link',        # WF7
        'currency': 'INR',
        'min_amount': 2000,   # regla propia de WF7 (Guard + Build Order)
    },
    'mexico': {
        'account_open': None,   # pendiente — WF3-MX
        'payment_link': None,   # pendiente — WF7-MX
        'currency': 'USD',
        'min_amount': 1,
    },
    'nepal': {
        'account_open': '/webhook/elevenlabs-call-webhook',   # WF3 (mismo webhook compartido con India)
        'payment_link': None,   # pendiente — WF7 rama Nepal/Monetix deshabilitada hasta tener credenciales
        'currency': 'NPR',
        'min_amount': 1,   # placeholder — ajustar cuando Monetix quede configurado
    },
}


def ensure_support_tables(db):
    """Config persistida (telegram token, etc.) + audit log de acciones
    de soporte. Idempotente, igual que ensure_sip_tables."""
    if db.driver == 'mysql':
        db.execute("""CREATE TABLE IF NOT EXISTS app_settings (
            setting_key VARCHAR(100) PRIMARY KEY,
            setting_value TEXT
        )""")
        db.execute("""CREATE TABLE IF NOT EXISTS support_actions (
            id INT AUTO_INCREMENT PRIMARY KEY,
            created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
            actor VARCHAR(50),
            country VARCHAR(50),
            action VARCHAR(30),
            phone VARCHAR(30),
            full_name VARCHAR(150),
            amount VARCHAR(30),
            currency VARCHAR(10),
            result VARCHAR(10),
            detail TEXT
        )""")
    else:
        db.execute("""CREATE TABLE IF NOT EXISTS app_settings (
            setting_key TEXT PRIMARY KEY,
            setting_value TEXT
        )""")
        db.execute("""CREATE TABLE IF NOT EXISTS support_actions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
            actor TEXT, country TEXT, action TEXT, phone TEXT,
            full_name TEXT, amount TEXT, currency TEXT,
            result TEXT, detail TEXT
        )""")


def get_setting(db, key, default=None):
    ensure_support_tables(db)
    r = db.one("SELECT setting_value AS v FROM app_settings WHERE setting_key=§", (key,))
    return r.get('v') if r else default


def set_setting(db, key, value):
    ensure_support_tables(db)
    if db.driver == 'mysql':
        db.execute("""INSERT INTO app_settings (setting_key, setting_value) VALUES (§,§)
                      ON DUPLICATE KEY UPDATE setting_value=VALUES(setting_value)""", (key, value))
    else:
        db.execute("""INSERT INTO app_settings (setting_key, setting_value) VALUES (§,§)
                      ON CONFLICT(setting_key) DO UPDATE SET setting_value=excluded.setting_value""", (key, value))


def log_support_action(db, actor, country, action, phone, full_name, amount, currency, result, detail):
    ensure_support_tables(db)
    db.insert("""INSERT INTO support_actions
                 (actor, country, action, phone, full_name, amount, currency, result, detail)
                 VALUES (§,§,§,§,§,§,§,§,§)""",
              (actor, country, action, phone, full_name, str(amount or ''), currency or '',
               result, (detail or '')[:500]))


def support_actions_recent(db, limit=30):
    ensure_support_tables(db)
    return db.q("SELECT * FROM support_actions ORDER BY created_at DESC LIMIT §", (limit,))


def _n8n_webhook_post(path, payload, timeout=20):
    url = N8N_BASE_URL.rstrip('/') + path
    data = json.dumps(payload).encode('utf-8')
    req = urllib.request.Request(url, data=data, method='POST')
    req.add_header('Content-Type', 'application/json')
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read()
            try:
                return True, (json.loads(raw) if raw else {})
            except Exception:
                return True, {'raw': raw.decode('utf-8', errors='replace')[:300]}
    except urllib.error.HTTPError as ex:
        detail = ex.read().decode('utf-8', errors='replace')[:300]
        return False, {'error': f'HTTP {ex.code}: {detail}'}
    except urllib.error.URLError as ex:
        return False, {'error': f'no se pudo conectar a n8n ({ex.reason})'}
    except TimeoutError:
        return False, {'error': f'n8n no respondió en {timeout}s'}


def trigger_account_creation(db, actor, country, full_name, phone):
    """Dispara el webhook de WF3 (elevenlabs-call-webhook) con el
    payload 'tool call' que WF3 ya sabe interpretar sin necesitar la
    estructura completa de ElevenLabs. WF3 hace el dedupe: si el
    teléfono ya tiene cuenta, reenvía las credenciales existentes."""
    country = normalize_country(country)
    cfg = SUPPORT_WEBHOOKS.get(country, {})
    path = cfg.get('account_open')
    full_name = (full_name or '').strip()
    phone_clean = re.sub(r'[^0-9]', '', phone or '')
    label = COUNTRIES_CFG.get(country, {}).get('label', country.capitalize())

    if not full_name or len(full_name) < 2:
        raise ValueError('Full name required (min 2 characters)')
    if not phone_clean or len(phone_clean) < 8:
        raise ValueError('Valid phone required, with country code (e.g. 91XXXXXXXXXX)')
    if not path:
        raise ValueError(f'Account creation is not wired up yet for {label} — pending WF3-{label}')

    payload = {
        'event': 'account_requested',
        'full_name': full_name,
        'phone': phone_clean,
        'country': label,
        'lead_id': f'SUPPORT-{phone_clean}-{int(time.time())}',
    }
    ok, resp = _n8n_webhook_post(path, payload)
    log_support_action(db, actor, country, 'account_open', phone_clean, full_name, '', '',
                       'ok' if ok else 'error', json.dumps(resp)[:500])
    if not ok:
        raise ValueError(resp.get('error', 'unknown error'))
    return resp


def trigger_payment_link(db, actor, country, full_name, phone, amount):
    """Dispara el webhook de WF7 (okpay-payment-link) — mismo formato
    que usa el bot vía tool call durante la llamada."""
    country = normalize_country(country)
    cfg = SUPPORT_WEBHOOKS.get(country, {})
    path = cfg.get('payment_link')
    currency = cfg.get('currency', 'USD')
    min_amount = cfg.get('min_amount', 1)
    full_name = (full_name or '').strip() or 'Client'
    phone_clean = re.sub(r'[^0-9]', '', phone or '')
    label = COUNTRIES_CFG.get(country, {}).get('label', country.capitalize())

    try:
        amount_num = float(amount)
    except (TypeError, ValueError):
        amount_num = 0
    if not phone_clean or len(phone_clean) < 8:
        raise ValueError('Valid phone required, with country code')
    if amount_num < min_amount:
        raise ValueError(f'Amount must be at least {min_amount} {currency}')
    if not path:
        raise ValueError(f'Payment links are not wired up yet for {label} — pending WF7-{label}')

    payload = {
        'lead_id': phone_clean,
        'full_name': full_name,
        'phone': phone_clean,
        'currency': currency,
        'amount': int(amount_num) if currency == 'INR' else amount_num,
        'risk_disclosure_confirmed': True,
    }
    ok, resp = _n8n_webhook_post(path, payload)
    log_support_action(db, actor, country, 'payment_link', phone_clean, full_name, amount_num, currency,
                       'ok' if ok else 'error', json.dumps(resp)[:500])
    if not ok:
        raise ValueError(resp.get('error', 'unknown error'))
    if isinstance(resp, dict) and resp.get('refused'):
        raise ValueError(resp.get('user_message') or resp.get('reason') or 'Request refused: phone not registered yet')
    return resp


# ── Telegram — polling (sin necesitar HTTPS pública) ────────────────
# Dos formas de usarlo: menú con botones (recomendado) o comandos de
# texto directos (siguen funcionando para quien los prefiera):
#   /newaccount India Juan Perez 919812345678
#   /paymentlink India 919812345678 2500
_TG_LAST_UPDATE_KEY = 'telegram_last_update_id'

_TG_MAIN_MENU = {
    'keyboard': [[{'text': '🆕 New account'}, {'text': '💳 Payment link'}]],
    'resize_keyboard': True,
}


def _tg_country_kb(action):
    return {'inline_keyboard': [[
        {'text': 'India', 'callback_data': f'country:{action}:india'},
        {'text': 'Mexico', 'callback_data': f'country:{action}:mexico'},
        {'text': 'Nepal', 'callback_data': f'country:{action}:nepal'},
    ]]}


def ensure_telegram_sessions_table(db):
    """Guarda en qué paso de un flujo guiado (botones) está cada chat.
    En DB (no en memoria del proceso) para que sobreviva un restart del
    panel a mitad de una conversación."""
    if db.driver == 'mysql':
        db.execute("""CREATE TABLE IF NOT EXISTS telegram_sessions (
            chat_id VARCHAR(50) PRIMARY KEY,
            action VARCHAR(30), country VARCHAR(50),
            full_name VARCHAR(150), phone VARCHAR(30), step VARCHAR(30),
            updated_at DATETIME DEFAULT CURRENT_TIMESTAMP
        )""")
    else:
        db.execute("""CREATE TABLE IF NOT EXISTS telegram_sessions (
            chat_id TEXT PRIMARY KEY,
            action TEXT, country TEXT, full_name TEXT, phone TEXT, step TEXT,
            updated_at DATETIME DEFAULT CURRENT_TIMESTAMP
        )""")


def _tg_session_get(db, chat_id):
    ensure_telegram_sessions_table(db)
    r = db.one("SELECT * FROM telegram_sessions WHERE chat_id=§", (chat_id,))
    return r if r else None


def _tg_session_set(db, chat_id, **fields):
    ensure_telegram_sessions_table(db)
    cur = _tg_session_get(db, chat_id) or {}
    merged = {'action': cur.get('action'), 'country': cur.get('country'),
             'full_name': cur.get('full_name'), 'phone': cur.get('phone'), 'step': cur.get('step')}
    merged.update(fields)
    if db.driver == 'mysql':
        db.execute("""INSERT INTO telegram_sessions (chat_id, action, country, full_name, phone, step)
                     VALUES (§,§,§,§,§,§)
                     ON DUPLICATE KEY UPDATE action=VALUES(action), country=VALUES(country),
                     full_name=VALUES(full_name), phone=VALUES(phone), step=VALUES(step),
                     updated_at=CURRENT_TIMESTAMP""",
                  (chat_id, merged['action'], merged['country'], merged['full_name'], merged['phone'], merged['step']))
    else:
        db.execute("""INSERT INTO telegram_sessions (chat_id, action, country, full_name, phone, step)
                     VALUES (§,§,§,§,§,§)
                     ON CONFLICT(chat_id) DO UPDATE SET action=excluded.action, country=excluded.country,
                     full_name=excluded.full_name, phone=excluded.phone, step=excluded.step,
                     updated_at=CURRENT_TIMESTAMP""",
                  (chat_id, merged['action'], merged['country'], merged['full_name'], merged['phone'], merged['step']))


def _tg_session_clear(db, chat_id):
    ensure_telegram_sessions_table(db)
    db.execute("DELETE FROM telegram_sessions WHERE chat_id=§", (chat_id,))


def _tg_api(token, method, params=None, timeout=15):
    import urllib.parse
    url = f'https://api.telegram.org/bot{token}/{method}'
    data = urllib.parse.urlencode(params or {}).encode('utf-8')
    req = urllib.request.Request(url, data=data, method='POST')
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read())


def _tg_send(token, chat_id, text, reply_markup=None):
    params = {'chat_id': chat_id, 'text': text}
    if reply_markup is not None:
        params['reply_markup'] = json.dumps(reply_markup)
    try:
        _tg_api(token, 'sendMessage', params)
    except Exception:
        pass


def _tg_answer_callback(token, callback_query_id, text=None):
    try:
        params = {'callback_query_id': callback_query_id}
        if text:
            params['text'] = text
        _tg_api(token, 'answerCallbackQuery', params)
    except Exception:
        pass


def _tg_run_command(db, token, chat_id, actor, cmd, args_text):
    """Comandos de texto directos (/newaccount, /paymentlink) — siguen
    funcionando para quien no quiere usar los botones."""
    parts = args_text.split()
    try:
        if cmd == '/newaccount':
            if len(parts) < 3:
                _tg_send(token, chat_id, 'Usage: /newaccount <India|Mexico> <Full Name> <phone>')
                return
            country, phone, full_name = parts[0], parts[-1], ' '.join(parts[1:-1])
            trigger_account_creation(db, actor, country, full_name, phone)
            _tg_send(token, chat_id, f'✅ Account request sent for {full_name} ({phone}). SMS with credentials incoming.', _TG_MAIN_MENU)
        elif cmd == '/paymentlink':
            if len(parts) < 3:
                _tg_send(token, chat_id, 'Usage: /paymentlink <India|Mexico> <phone> <amount>')
                return
            country, phone, amount = parts[0], parts[1], parts[2]
            trigger_payment_link(db, actor, country, '', phone, amount)
            _tg_send(token, chat_id, f'✅ Payment link requested for {phone} ({amount}). SMS incoming.', _TG_MAIN_MENU)
    except ValueError as ex:
        _tg_send(token, chat_id, f'❌ {ex}', _TG_MAIN_MENU)
    except Exception as ex:
        _tg_send(token, chat_id, f'❌ Unexpected error: {ex}', _TG_MAIN_MENU)


def _tg_handle_callback(db, token, cb):
    """Botón de país tocado (inline keyboard) — continúa el flujo guiado."""
    chat_id = str(cb.get('message', {}).get('chat', {}).get('id', ''))
    data = cb.get('data', '')
    _tg_answer_callback(token, cb.get('id', ''))
    if not chat_id or not data.startswith('country:'):
        return
    _, action, country = data.split(':', 2)
    label = COUNTRIES_CFG.get(country, {}).get('label', country.capitalize())
    if action == 'account_open':
        _tg_session_set(db, chat_id, action=action, country=country, step='await_name')
        _tg_send(token, chat_id, f'{label} selected. Send the client\'s full name:')
    else:
        _tg_session_set(db, chat_id, action=action, country=country, step='await_phone')
        _tg_send(token, chat_id, f'{label} selected. Send the client\'s phone (with country code):')


def _tg_handle_message(db, token, chat_id, actor, text):
    text = (text or '').strip()
    if not text:
        return

    if text in ('🆕 New account',):
        _tg_session_set(db, chat_id, action='account_open', country=None, full_name=None, phone=None, step='await_country')
        _tg_send(token, chat_id, 'Which country?', _tg_country_kb('account_open'))
        return
    if text in ('💳 Payment link',):
        _tg_session_set(db, chat_id, action='payment_link', country=None, full_name=None, phone=None, step='await_country')
        _tg_send(token, chat_id, 'Which country?', _tg_country_kb('payment_link'))
        return
    if text.lower() in ('/start', '/menu', '/help'):
        _tg_session_clear(db, chat_id)
        _tg_send(token, chat_id, 'What do you need?', _TG_MAIN_MENU)
        return
    if text.startswith('/newaccount'):
        _tg_session_clear(db, chat_id)
        _tg_run_command(db, token, chat_id, actor, '/newaccount', text[len('/newaccount'):].strip())
        return
    if text.startswith('/paymentlink'):
        _tg_session_clear(db, chat_id)
        _tg_run_command(db, token, chat_id, actor, '/paymentlink', text[len('/paymentlink'):].strip())
        return

    # ── continuar un flujo guiado ya en curso ──
    sess = _tg_session_get(db, chat_id)
    step = sess.get('step') if sess else None
    if step == 'await_name':
        _tg_session_set(db, chat_id, full_name=text, step='await_phone')
        _tg_send(token, chat_id, 'Now send the phone (with country code, digits only):')
        return
    if step == 'await_phone' and sess.get('action') == 'account_open':
        try:
            trigger_account_creation(db, actor, sess['country'], sess['full_name'], text)
            _tg_send(token, chat_id, f"✅ Account request sent for {sess['full_name']} ({text}). SMS with credentials incoming.", _TG_MAIN_MENU)
        except ValueError as ex:
            _tg_send(token, chat_id, f'❌ {ex}', _TG_MAIN_MENU)
        _tg_session_clear(db, chat_id)
        return
    if step == 'await_phone' and sess.get('action') == 'payment_link':
        _tg_session_set(db, chat_id, phone=text, step='await_amount')
        _tg_send(token, chat_id, 'Now send the amount:')
        return
    if step == 'await_amount':
        try:
            trigger_payment_link(db, actor, sess['country'], '', sess['phone'], text)
            _tg_send(token, chat_id, f"✅ Payment link requested for {sess['phone']} ({text}). SMS incoming.", _TG_MAIN_MENU)
        except ValueError as ex:
            _tg_send(token, chat_id, f'❌ {ex}', _TG_MAIN_MENU)
        _tg_session_clear(db, chat_id)
        return

    # sin sesión activa y no reconocido — mostrar el menú
    _tg_send(token, chat_id, "Not sure what you mean — here's the menu:", _TG_MAIN_MENU)


def _tg_cas_advance(db, old_last_id, new_last_id):
    """Con --workers 3, los 3 procesos de gunicorn corren este polling
    en paralelo — sin esto, cada uno procesaría el mismo lote de updates
    y se duplicarían SMS/payment links. CAS optimista sobre app_settings:
    solo el worker que gana la carrera (rowcount=1) procesa; los demás
    descartan su lote sin responder nada."""
    ensure_support_tables(db)
    if db.driver == 'mysql':
        _, rc = db.execute("""UPDATE app_settings SET setting_value=§
                              WHERE setting_key=§ AND setting_value=§""",
                          (str(new_last_id), _TG_LAST_UPDATE_KEY, str(old_last_id)))
    else:
        _, rc = db.execute("""UPDATE app_settings SET setting_value=§
                              WHERE setting_key=§ AND setting_value=§""",
                          (str(new_last_id), _TG_LAST_UPDATE_KEY, str(old_last_id)))
    return rc > 0


def telegram_poll_loop(get_db_fn, interval=3, stop_event=None):
    """Long-polling — mismo patrón que scheduler_loop. No necesita
    HTTPS pública. CAS-gated para no duplicar acciones entre los N
    workers de gunicorn que corren este mismo hilo en paralelo."""
    while not (stop_event and stop_event.is_set()):
        try:
            db = get_db_fn()
            try:
                token = get_setting(db, 'telegram_bot_token', '')
                whitelist_raw = get_setting(db, 'telegram_chat_whitelist', '')
                whitelist = {x.strip() for x in whitelist_raw.split(',') if x.strip()}
                if token:
                    existing = get_setting(db, _TG_LAST_UPDATE_KEY, None)
                    if existing is None:
                        set_setting(db, _TG_LAST_UPDATE_KEY, '0')
                        existing = '0'
                    last_id = int(existing or '0')
                    result = _tg_api(token, 'getUpdates',
                                     {'offset': last_id + 1, 'timeout': 0, 'limit': 20})
                    updates = result.get('result', [])
                    if updates:
                        new_last_id = max(last_id, max(u['update_id'] for u in updates))
                        # Solo el worker que gana el CAS procesa y responde —
                        # evita 2-3 respuestas duplicadas al mismo mensaje.
                        if _tg_cas_advance(db, last_id, new_last_id):
                            for upd in updates:
                                cb = upd.get('callback_query')
                                if cb:
                                    _tg_handle_callback(db, token, cb)
                                    continue
                                msg = upd.get('message') or {}
                                chat_id = str(msg.get('chat', {}).get('id', ''))
                                text = msg.get('text', '')
                                username = msg.get('from', {}).get('username', '')
                                if not chat_id or not text:
                                    continue
                                if whitelist and chat_id not in whitelist and f'@{username}' not in whitelist:
                                    _tg_send(token, chat_id, '⛔ Not authorized. Ask master to whitelist your chat_id.')
                                    continue
                                actor = f'telegram:{username or chat_id}'
                                _tg_handle_message(db, token, chat_id, actor, text)
            finally:
                try:
                    db.conn.close()
                except Exception:
                    pass
        except Exception:
            pass
        (stop_event.wait(interval) if stop_event else time.sleep(interval))



# ══════════════════════════════════════════════════════════════════
#  STRINGEE — segundo proveedor (cuota fija, sin costo/min)
# ══════════════════════════════════════════════════════════════════
def _stringee_ms_bounds(s, e, country):
    # s, e ya vienen en hora local del país de resolve_range()
    # stringee_calls.start_time está en ms UTC directo de Stringee
    # así que solo convierte s/e a UTC sin desplazar por tz
    return int(s.timestamp() * 1000), int(e.timestamp() * 1000)

def stringee_traffic(db, s, e, country=DEFAULT_COUNTRY):
    """
    Mismo shape de salida que traffic() (Asterisk) — reutiliza los KPI
    cards y el donut de Call outcome del template sin tocarlos. busy/
    failed/congestion/tech_failures quedan en 0: stringee_calls solo
    distingue answered/no answered, no el motivo técnico del rechazo.
    cost/cost_per_* quedan en 0 siempre: Stringee es cuota fija, no
    factura por minuto.
    """
    if not db.table_exists('stringee_calls'):
        return None
    bs_ms, be_ms = _stringee_ms_bounds(s, e, country)
    sql = """
SELECT
    COUNT(*)                                                    AS total_calls,
    SUM(CASE WHEN answered=1 THEN 1 ELSE 0 END)                 AS answered,
    COALESCE(SUM(CASE WHEN answered=1 THEN duration_secs ELSE 0 END), 0) AS talk_seconds,
    COALESCE(MAX(duration_secs), 0)                             AS longest_call
FROM stringee_calls
WHERE country = § AND start_time >= § AND start_time < §
"""
    r = db.one(sql, (country, bs_ms, be_ms))
    total     = _int(r.get('total_calls'))
    answered  = _int(r.get('answered'))
    talk      = _int(r.get('talk_seconds'))
    no_answer = total - answered
    asr       = pct(answered, total)
    minutes   = round(talk / 60)
    return {
        'total_calls'      : total,
        'answered'         : answered,
        'no_answer'        : no_answer,
        'busy'             : 0,
        'failed'           : 0,
        'congestion'       : 0,
        'tech_failures'    : 0,
        'billed_minutes'   : minutes,
        'talk_seconds'     : talk,
        'talk_minutes'     : round(talk / 60, 1),
        'longest_call'     : _int(r.get('longest_call')),
        'cost'             : 0,
        'asr'              : asr,
        'ner'              : pct(answered + no_answer, total),
        'acd'              : round(talk / answered, 1) if answered else 0.0,
        'answer_rate'      : asr,
        'no_answer_rate'   : pct(no_answer, total),
        'failure_rate'     : 0.0,
        'cost_per_answered': 0.0,
        'cost_per_call'    : 0.0,
        'avg_min_per_call' : round(minutes / total, 2) if total else 0.0,
    }


def stringee_timeseries(db, s, e, grain='day', country=DEFAULT_COUNTRY):
    """Mismo shape que timeseries() (Asterisk) — alimenta charts.trace()
    sin tocarlo. cost siempre 0 (cuota fija)."""
    if not db.table_exists('stringee_calls'):
        return []
    bs_ms, be_ms = _stringee_ms_bounds(s, e, country)
    shift_sec = country_tz_shift(country) * 60
    if db.driver == 'mysql':
        dt_expr = f"FROM_UNIXTIME((start_time/1000) + {shift_sec})"
        bucket = {
            'hour' : f"DATE_FORMAT({dt_expr},'%%Y-%%m-%%d %%H:00')",
            'day'  : f"DATE_FORMAT({dt_expr},'%%Y-%%m-%%d')",
            'week' : f"DATE_FORMAT({dt_expr},'%%x-W%%v')",
            'month': f"DATE_FORMAT({dt_expr},'%%Y-%%m')",
        }[grain]
    else:
        dt_expr = f"datetime((start_time/1000)+{shift_sec}, 'unixepoch')"
        bucket = {
            'hour' : f"strftime('%Y-%m-%d %H:00', {dt_expr})",
            'day'  : f"strftime('%Y-%m-%d', {dt_expr})",
            'week' : f"strftime('%Y-W%W', {dt_expr})",
            'month': f"strftime('%Y-%m', {dt_expr})",
        }[grain]
    sql = f"""
SELECT
    {bucket} AS bucket,
    COUNT(*) AS total_calls,
    SUM(CASE WHEN answered=1 THEN 1 ELSE 0 END) AS answered,
    COALESCE(SUM(CASE WHEN answered=1 THEN duration_secs ELSE 0 END), 0) AS talk_seconds
FROM stringee_calls
WHERE country = § AND start_time >= § AND start_time < §
GROUP BY bucket
ORDER BY bucket
"""
    rows = db.q(sql, (country, bs_ms, be_ms))
    for r in rows:
        r['total_calls'] = _int(r.get('total_calls'))
        r['answered']    = _int(r.get('answered'))
        r['no_answer']   = r['total_calls'] - r['answered']
        r['failed']      = 0
        talk = _int(r.get('talk_seconds'))
        r['talk_seconds']    = talk
        r['billed_minutes']  = round(talk / 60)
        r['cost']  = 0
        r['asr']   = pct(r['answered'], r['total_calls'])
        r['acd']   = round(talk / r['answered'], 1) if r['answered'] else 0.0
    return rows

def stringee_hourly(db, s, e, country=DEFAULT_COUNTRY):
    if not db.table_exists('stringee_calls'):
        return []
    bs_ms, be_ms = _stringee_ms_bounds(s, e, country)
    shift_sec = country_tz_shift(country) * 60
    if db.driver == 'mysql':
        hour_expr = f"HOUR(FROM_UNIXTIME((start_time/1000) + {shift_sec}))"
    else:
        hour_expr = f"CAST(strftime('%H', (start_time/1000)+{shift_sec}, 'unixepoch') AS INTEGER)"
    sql = f"""
SELECT {hour_expr} AS hh,
       COUNT(*) AS total_calls,
       SUM(CASE WHEN answered=1 THEN 1 ELSE 0 END) AS answered
FROM stringee_calls
WHERE country = § AND start_time >= § AND start_time < §
GROUP BY hh
"""
    rows = db.q(sql, (country, bs_ms, be_ms))
    buckets = {h: {'hour': h, 'total_calls': 0, 'answered': 0, 'accounts': 0, 'asr': 0.0} for h in range(24)}
    for r in rows:
        b = buckets[int(r['hh']) % 24]
        b['total_calls'] += _int(r.get('total_calls'))
        b['answered']    += _int(r.get('answered'))
    for b in buckets.values():
        b['asr'] = pct(b['answered'], b['total_calls'])
    active = [h for h in range(24) if buckets[h]['total_calls'] > 0]
    if not active:
        return []
    lo, hi = min(active), max(active)
    return [buckets[h] for h in range(lo, hi + 1)]

def stringee_funnel_timeseries(db, s, e, grain='day', country=DEFAULT_COUNTRY):
    """
    Mismo shape que funnel_timeseries() (bucket/attempts/answered/
    real_conv/accounts/pct_*), pero attempts/answered/real_conv salen de
    stringee_calls (no del CDR) y accounts de panel_conversions filtrado
    por lead_id que efectivamente pasó por Stringee en ese bucket.
    """
    if not db.table_exists('stringee_calls'):
        return []
    bs_ms, be_ms = _stringee_ms_bounds(s, e, country)
    shift_sec = country_tz_shift(country) * 60
    if db.driver == 'mysql':
        dt_expr = f"FROM_UNIXTIME((start_time/1000) + {shift_sec})"
        bucket = {
            '30min': f"DATE_FORMAT(DATE_SUB({dt_expr}, INTERVAL (MINUTE({dt_expr}) MOD 30) MINUTE),'%%Y-%%m-%%d %%H:%%i')",
            'hour' : f"DATE_FORMAT({dt_expr},'%%Y-%%m-%%d %%H:00')",
            'day'  : f"DATE_FORMAT({dt_expr},'%%Y-%%m-%%d')",
            'week' : f"DATE_FORMAT({dt_expr},'%%x-W%%v')",
            'month': f"DATE_FORMAT({dt_expr},'%%Y-%%m')",
        }[grain]
    else:
        dt_expr = f"datetime((start_time/1000)+{shift_sec}, 'unixepoch')"
        bucket = {
            '30min': f"strftime('%Y-%m-%d %H:', {dt_expr}) || printf('%02d', (CAST(strftime('%M', {dt_expr}) AS INTEGER) / 30) * 30)",
            'hour' : f"strftime('%Y-%m-%d %H:00', {dt_expr})",
            'day'  : f"strftime('%Y-%m-%d', {dt_expr})",
            'week' : f"strftime('%Y-W%W', {dt_expr})",
            'month': f"strftime('%Y-%m', {dt_expr})",
        }[grain]

    sql = f"""
SELECT
    {bucket}                                                     AS bucket,
    COUNT(*)                                                     AS attempts,
    SUM(CASE WHEN answered=1 THEN 1 ELSE 0 END)                  AS answered,
    SUM(CASE WHEN answered=1 AND duration_secs >= 60 THEN 1 ELSE 0 END) AS real_conv
FROM stringee_calls
WHERE country = § AND start_time >= § AND start_time < §
GROUP BY bucket
ORDER BY bucket
"""
    rows = db.q(sql, (country, bs_ms, be_ms))
    by_bucket = {}
    for r in rows:
        attempts  = _int(r.get('attempts'))
        answered  = _int(r.get('answered'))
        real_conv = _int(r.get('real_conv'))
        by_bucket[r['bucket']] = {
            'bucket': r['bucket'], 'attempts': attempts,
            'answered': answered, 'real_conv': real_conv,
            'accounts': 0,
            'pct_answered': pct(answered, attempts),
            'pct_real_conv': pct(real_conv, attempts),
            'pct_accounts': 0.0,
        }

    if db.table_exists('panel_conversions'):
        shift_sec_pc = country_tz_shift(country) * 60
        local_col_pc = local_expr(db, 'created_at', country)
        if db.driver == 'mysql':
            bucket_pc = {
                '30min': f"DATE_FORMAT(DATE_SUB({local_col_pc}, INTERVAL (MINUTE({local_col_pc}) MOD 30) MINUTE),'%%Y-%%m-%%d %%H:%%i')",
                'hour' : f"DATE_FORMAT({local_col_pc},'%%Y-%%m-%%d %%H:00')",
                'day'  : f"DATE_FORMAT({local_col_pc},'%%Y-%%m-%%d')",
                'week' : f"DATE_FORMAT({local_col_pc},'%%x-W%%v')",
                'month': f"DATE_FORMAT({local_col_pc},'%%Y-%%m')",
            }[grain]
        else:
            bucket_pc = {
                '30min': f"strftime('%Y-%m-%d %H:', {local_col_pc}) || printf('%02d', (CAST(strftime('%M', {local_col_pc}) AS INTEGER) / 30) * 30)",
                'hour' : f"strftime('%Y-%m-%d %H:00', {local_col_pc})",
                'day'  : f"strftime('%Y-%m-%d', {local_col_pc})",
                'week' : f"strftime('%Y-W%W', {local_col_pc})",
                'month': f"strftime('%Y-%m', {local_col_pc})",
            }[grain]
        sql_pc = f"""
SELECT {bucket_pc} AS bucket, COUNT(*) AS n
FROM panel_conversions pc
WHERE created_at >= § AND created_at < § AND LOWER(country) = §
  AND lead_id COLLATE utf8mb4_unicode_ci IN (
      SELECT DISTINCT lead_id COLLATE utf8mb4_unicode_ci FROM stringee_calls
      WHERE country = § AND start_time >= § AND start_time < §
        AND lead_id IS NOT NULL AND lead_id != ''
  )
GROUP BY bucket
"""
        bs_pc, be_pc = utc_bounds(s, e, country)
        pc_rows = db.q(sql_pc, (bs_pc, be_pc, country.lower(), country, bs_ms, be_ms))
        for r in pc_rows:
            n = _int(r.get('n'))
            if r['bucket'] in by_bucket:
                b = by_bucket[r['bucket']]
                b['accounts'] = n
                b['pct_accounts'] = pct(n, b['attempts'])
            else:
                by_bucket[r['bucket']] = {
                    'bucket': r['bucket'], 'attempts': 0, 'answered': 0,
                    'real_conv': 0, 'accounts': n,
                    'pct_answered': 0.0, 'pct_real_conv': 0.0, 'pct_accounts': 0.0,
                }

    return [by_bucket[k] for k in sorted(by_bucket.keys())]


def stringee_funnel(db, s, e, country=DEFAULT_COUNTRY):
    """
    Funnel comercial, pero SOLO para leads que pasaron por Stringee en
    este período — via el cruce de lead_id que WF15 ya arma (stringee_
    calls.lead_id se llena cruzando to_number contra panel_leads.phone).
    Mismo shape de salida que funnel() para reusar el template sin tocarlo.
    """
    if not db.table_exists('panel_leads') or not db.table_exists('stringee_calls'):
        return {'available': False}

    bs_ms, be_ms = _stringee_ms_bounds(s, e, country)

    by_status = db.q("""
        SELECT pl.status, COUNT(*) AS n
        FROM panel_leads pl
        WHERE LOWER(pl.country) = §
          AND pl.lead_id COLLATE utf8mb4_unicode_ci IN (
              SELECT DISTINCT lead_id COLLATE utf8mb4_unicode_ci FROM stringee_calls
              WHERE country = § AND start_time >= § AND start_time < §
                AND lead_id IS NOT NULL AND lead_id != ''
          )
        GROUP BY pl.status ORDER BY n DESC
    """, (country.lower(), country, bs_ms, be_ms))

    total_touched = sum(r['n'] for r in by_status)
    d = {r['status']: r['n'] for r in by_status}

    contacted  = d.get('SUCCESSFUL', 0) + d.get('INTERESTED', 0) + d.get('CONVERTED', 0)
    voicemail  = d.get('VOICEMAIL', 0)
    no_answer  = d.get('NO_ANSWER', 0)
    scheduled  = d.get('SCHEDULED', 0)
    interested = d.get('INTERESTED', 0) + d.get('CONVERTED', 0)

    accounts = 0
    if db.table_exists('panel_conversions'):
        r = db.one("""
            SELECT COUNT(*) AS n FROM panel_conversions pc
            WHERE LOWER(pc.country) = §
              AND pc.lead_id COLLATE utf8mb4_unicode_ci IN (
                  SELECT DISTINCT lead_id COLLATE utf8mb4_unicode_ci FROM stringee_calls
                  WHERE country = § AND start_time >= § AND start_time < §
                    AND lead_id IS NOT NULL AND lead_id != ''
              )
        """, (country.lower(), country, bs_ms, be_ms))
        accounts = r.get('n') or 0

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
        'cpa'              : 0.0,  # Stringee no tiene costo por llamada
        'cost_per_contact' : 0.0,
        'callback_rate'    : pct(scheduled, contacted),
    }


def stringee_durations(db, s, e, country=DEFAULT_COUNTRY):
    """
    Solo lo que necesita charts.ladder(): 'meaningful' = llamadas
    contestadas de 60s o más, directo de stringee_calls (no del CDR de
    Asterisk, que no tiene ninguna de estas llamadas).
    """
    if not db.table_exists('stringee_calls'):
        return {'buckets': [], 'total': 0, 'meaningful': 0, 'meaningful_pct': 0.0}
    bs_ms, be_ms = _stringee_ms_bounds(s, e, country)
    r = db.one("""
        SELECT
            COUNT(*) AS total,
            SUM(CASE WHEN duration_secs >= 60 THEN 1 ELSE 0 END) AS meaningful
        FROM stringee_calls
        WHERE country = § AND answered = 1 AND start_time >= § AND start_time < §
    """, (country, bs_ms, be_ms))
    total      = _int(r.get('total'))
    meaningful = _int(r.get('meaningful'))
    return {'buckets': [], 'total': total, 'meaningful': meaningful,
            'meaningful_pct': pct(meaningful, total)}


def build_stringee_report(db, s, e, country=DEFAULT_COUNTRY):
    t = stringee_traffic(db, s, e, country)
    if not t or t['total_calls'] == 0:
        return None
    return {
        'traffic': t,
        'hourly' : stringee_hourly(db, s, e, country),
    }
