"""
analytics_v2.py — Capa de consultas del dashboard V2.2 (SQL local)
=================================================================

El panel lee de acá. NUNCA llama a LeadStudio para un KPI, un gráfico, un
refresh o una vista por país. LeadStudio queda para operación y reconciliación.

Fuentes
-------
  llamadas  → wf_call_jobs   (una fila por llamada; filtro por created_at = hora del claim)
  negocio   → wf_events      (append-only, idempotente; filtro por occurred_at)

Todo en UTC. Los rangos son [start, end). La zona horaria del reporte se aplica
al calcular start/end (day_range_utc); los buckets de timeseries son UTC.

DEFINICIONES (son contrato: si cambian, cambia el número del panel)
--------------------------------------------------------------------
  attempted        llamadas que pudieron salir: state ∈ DISPATCHING, DISPATCHED,
                   UNKNOWN, COMPLETED, NEEDS_RECONCILIATION. Excluye RELEASED
                   (técnico, no salió), CLAIMED (todavía no) y FAILED (dato inválido).
  dispatched       aceptadas por el proveedor o con resultado: dispatched_at no nulo
  with_result      con resultado final registrado
  answered         CONTESTÓ = result ∈ ANSWERED, CALLBACK  (pedir callback implica
                   haber hablado). callbacks se informa además por separado.
  answer_rate      answered / with_result   (no / attempted: las pendientes no
                   deben bajar la tasa)
  talk_seconds     SUM(duration_seconds) de las answered
  avg_talk_seconds talk_seconds / answered con duración
  failed_technical eventos CALL_TECH_FAILED (no consumen intento de negocio)
  needs_reconc.    state ∈ UNKNOWN, NEEDS_RECONCILIATION
  conversion_rate  accounts_opened / answered del mismo rango — PENDING_PRODUCT_DECISION
                   (alternativa: cohortes por fecha de llamada)
"""

from datetime import datetime, timedelta, timezone as _tz
from zoneinfo import ZoneInfo

GROUPS = {'country': 'country_iso', 'provider': 'provider', 'route': 'route_key'}
ATTEMPTED = ('DISPATCHING', 'DISPATCHED', 'UNKNOWN', 'COMPLETED', 'NEEDS_RECONCILIATION')
ANSWERED = ('ANSWERED', 'CALLBACK')
ATTRIBUTION_MODEL = 'LAST_CONNECTED_CALL_V1'
ATTRIBUTION_WINDOW_DAYS = 30


def _ts(v):
    if isinstance(v, str):
        v = datetime.fromisoformat(v.replace('Z', '+00:00'))
    if v.tzinfo is None:
        v = v.replace(tzinfo=_tz.utc)
    return v.astimezone(_tz.utc).strftime('%Y-%m-%d %H:%M:%S')


def day_range_utc(day=None, tz_name='UTC', days=1):
    """[inicio, fin) en UTC de `days` días locales empezando en `day` (fecha local)."""
    tz = ZoneInfo(tz_name)
    day = day or datetime.now(tz).date()
    start = datetime(day.year, day.month, day.day, tzinfo=tz)
    return start.astimezone(_tz.utc), (start + timedelta(days=days)).astimezone(_tz.utc)


def _in(values):
    return "(" + ",".join(f"'{v}'" for v in values) + ")"


def _bucket(db, col, unit):
    fmt = {'hour': '%Y-%m-%d %H:00:00', 'day': '%Y-%m-%d'}[unit]
    if db.driver == 'mysql':
        return f"DATE_FORMAT({col}, '{fmt.replace('%', '%%')}')"   # pymysql formatea con %
    return f"strftime('{fmt}', {col})"


def _group(group_by):
    if group_by is None:
        return None
    if group_by not in GROUPS:
        raise ValueError(f'group_by debe ser uno de {list(GROUPS)}')
    return GROUPS[group_by]


def _num(v):
    return int(v or 0)


# ══════════════════════════════════════════════════════════════════════
#  Filtros — §32/§33: el desglose y el export respetan lo que se ve
# ══════════════════════════════════════════════════════════════════════
#  El panel agrupaba por país/proveedor/ruta, pero no FILTRABA por ellos:
#  al elegir "India" el desglose por hora seguía mostrando todos los
#  países, y el CSV exportaba un conjunto distinto del que estaba en
#  pantalla. Aquí se construye un WHERE parametrizado que se aplica a
#  TODAS las consultas por igual — métricas, series, desglose horario y
#  export — para que no puedan divergir.

FILTER_FIELDS = {
    'country':  'country_iso',
    'provider': 'provider',
    'route':    'route_key',
}


def _filters(filters, alias=''):
    """(fragmento_sql, params). Un filtro vacío o None no añade nada.

    Acepta un valor o una lista: country='IN' y country=['IN','NP'] son
    ambos válidos, porque el selector del panel permite varios.
    """
    if not filters:
        return '', []
    pre = f'{alias}.' if alias else ''
    frags, params = [], []
    for clave, col in FILTER_FIELDS.items():
        v = filters.get(clave)
        if v in (None, '', [], ()):
            continue
        vals = [v] if isinstance(v, str) else list(v)
        vals = [str(x).strip() for x in vals if str(x).strip()]
        if not vals:
            continue
        marcas = ','.join(['§'] * len(vals))
        frags.append(f'{pre}{col} IN ({marcas})')
        params.extend(vals)
    return (' AND ' + ' AND '.join(frags) if frags else ''), params


def normalize_filters(args):
    """Lee los filtros de una query string. Devuelve sólo lo que venga
    informado, para que un filtro ausente signifique 'todos'."""
    out = {}
    for clave in FILTER_FIELDS:
        v = args.get(clave)
        if v in (None, '', 'all', 'ALL'):
            continue
        if isinstance(v, str) and ',' in v:
            v = [x for x in (p.strip() for p in v.split(',')) if x]
        out[clave] = v
    return out


def describe_filters(filters):
    """Cómo se describe el recorte en la pantalla y en el CSV. Que el
    fichero exportado diga con qué filtros se sacó evita que alguien
    compare dos CSV distintos creyendo que son lo mismo."""
    if not filters:
        return 'All countries, providers and routes'
    partes = []
    for clave, etiqueta in (('country', 'Countries'), ('provider', 'Providers'),
                            ('route', 'Routes')):
        v = filters.get(clave)
        if not v:
            continue
        vals = [v] if isinstance(v, str) else list(v)
        partes.append(f'{etiqueta}: ' + ', '.join(vals))
    return ' · '.join(partes) if partes else 'All countries, providers and routes'



# ══════════════════════════════════════════════════════════════════════
#  Llamadas — desde wf_call_jobs
# ══════════════════════════════════════════════════════════════════════

def call_metrics(db, start, end, group_by=None, filters=None):
    g = _group(group_by)
    fsql, fparams = _filters(filters)
    sel_g = f"{g} AS grp, " if g else ""
    grp = f"GROUP BY {g} ORDER BY {g}" if g else ""
    rows = db.q(f"""
        SELECT {sel_g}
          SUM(CASE WHEN state IN {_in(ATTEMPTED)} THEN 1 ELSE 0 END)          AS attempted,
          SUM(CASE WHEN dispatched_at IS NOT NULL THEN 1 ELSE 0 END)         AS dispatched,
          SUM(CASE WHEN result IS NOT NULL THEN 1 ELSE 0 END)                AS with_result,
          SUM(CASE WHEN result IN {_in(ANSWERED)} THEN 1 ELSE 0 END)         AS answered,
          SUM(CASE WHEN result = 'NO_ANSWER' THEN 1 ELSE 0 END)              AS no_answer,
          SUM(CASE WHEN result = 'BUSY' THEN 1 ELSE 0 END)                   AS busy,
          SUM(CASE WHEN result = 'VOICEMAIL' THEN 1 ELSE 0 END)              AS voicemail,
          SUM(CASE WHEN result = 'WRONG_NUMBER' THEN 1 ELSE 0 END)           AS wrong_number,
          SUM(CASE WHEN result = 'CALLBACK' THEN 1 ELSE 0 END)               AS callbacks,
          SUM(CASE WHEN result = 'DNC' THEN 1 ELSE 0 END)                    AS dnc,
          SUM(CASE WHEN result = 'FAILED' THEN 1 ELSE 0 END)                 AS failed_call,
          SUM(CASE WHEN state IN ('UNKNOWN','NEEDS_RECONCILIATION') THEN 1 ELSE 0 END) AS needs_reconciliation,
          SUM(CASE WHEN state IN ('CLAIMED','DISPATCHING','DISPATCHED') THEN 1 ELSE 0 END) AS in_flight,
          SUM(CASE WHEN result IN {_in(ANSWERED)} THEN COALESCE(duration_seconds,0) ELSE 0 END) AS talk_seconds,
          SUM(CASE WHEN result IN {_in(ANSWERED)} AND duration_seconds IS NOT NULL THEN 1 ELSE 0 END) AS talk_n
        FROM wf_call_jobs
        WHERE created_at >= § AND created_at < §{fsql}
        {grp}""", (_ts(start), _ts(end), *fparams))

    tech = {r['grp'] if g else None: _num(r['n']) for r in db.q(f"""
        SELECT {sel_g} COUNT(*) AS n FROM wf_events
        WHERE event_type='CALL_TECH_FAILED' AND occurred_at >= § AND occurred_at < §{fsql} {grp}""",
        (_ts(start), _ts(end), *fparams))}

    out = []
    for r in rows:
        key = r.get('grp') if g else None
        if g is None and r.get('attempted') is None:
            r = {k: 0 for k in r}
        m = {k: _num(v) for k, v in r.items() if k not in ('grp',)}
        m['failed_technical'] = tech.get(key, 0)
        m['answer_rate'] = round(m['answered'] / m['with_result'], 4) if m['with_result'] else None
        m['talk_minutes'] = round(m['talk_seconds'] / 60, 1)
        m['avg_talk_seconds'] = round(m['talk_seconds'] / m['talk_n'], 1) if m['talk_n'] else None
        m.pop('talk_n')
        if g:
            m[group_by] = key
        out.append(m)
    return out if g else (out[0] if out else {})


# ══════════════════════════════════════════════════════════════════════
#  Negocio — desde wf_events
# ══════════════════════════════════════════════════════════════════════

_BUSINESS = {
    'accounts_opened': "event_type='ACCOUNT_CREATED'",
    'account_requests': "event_type='ACCOUNT_REQUESTED'",
    'account_failures': "event_type='ACCOUNT_FAILED'",
    'account_already_existed': "event_type='ACCOUNT_ALREADY_EXISTS'",
    'payment_link_requests': "event_type='PAYMENT_LINK_REQUESTED'",
    'payment_links_created': "event_type='PAYMENT_LINK_CREATED'",
    'payment_link_failures': "event_type='PAYMENT_LINK_FAILED'",
    'payments_confirmed': "event_type='PAYMENT_CONFIRMED'",
    'followups_created': "event_type='FOLLOWUP_CREATED'",
    'callbacks_scheduled': "event_type='CALLBACK_SCHEDULED'",
    'leads_closed': "event_type='LEAD_CLOSED'",
    'closed_max_attempts': "event_type='LEAD_CLOSED' AND result='MAX_ATTEMPTS'",
    'recordings_attached': "event_type='RECORDING_ATTACHED'",
    'recordings_below_min': "event_type='RECORDING_SKIPPED_SHORT'",
    'recordings_missing': "event_type='RECORDING_MISSING'",
}


def business_metrics(db, start, end, group_by=None, filters=None):
    """Cuentas, pagos, tools, follow-ups, grabaciones.

    Ojo con group_by='provider': en eventos de TOOL, `provider` es el proveedor
    de la tool (cashstudio, okpay), no el de voz. Para cuentas por ruta/proveedor
    de VOZ usar accounts_attributed()."""
    g = _group(group_by)
    sel = ",\n".join(f"SUM(CASE WHEN {cond} THEN 1 ELSE 0 END) AS {name}"
                     for name, cond in _BUSINESS.items())
    sel_g = f"{g} AS grp, " if g else ""
    grp = f"GROUP BY {g} ORDER BY {g}" if g else ""
    # Solo los tipos que alimentan estas métricas: los eventos de CALL (2/3 del
    # volumen) no se leen. Con idx_ev_metrics la consulta es solo-índice.
    types = sorted({c.split("'")[1] for c in _BUSINESS.values()})
    fsql, fparams = _filters(filters)
    rows = db.q(f"SELECT {sel_g} {sel} FROM wf_events WHERE event_type IN {_in(types)} "
                f"AND occurred_at >= § AND occurred_at < §{fsql} {grp}",
                (_ts(start), _ts(end), *fparams))
    out = []
    for r in rows:
        m = {k: _num(v) for k, v in r.items() if k != 'grp'}
        if g:
            m[group_by] = r['grp']
        out.append(m)
    return out if g else (out[0] if out else {k: 0 for k in _BUSINESS})


def overview(db, start, end, filters=None):
    """Los KPI de cabecera.

    FILTRA. Antes no: la pantalla podía decir "India" y las tarjetas de
    KPI mostrar los totales globales, porque `overview()` ignoraba el
    recorte. Es el peor tipo de error de un panel — no falla, miente con
    números que parecen correctos.
    """
    c = call_metrics(db, start, end, filters=filters)
    b = business_metrics(db, start, end, filters=filters)
    a = accounts_by(db, start, end, 'country', filters=filters)
    cuentas_atribuidas = sum(x['accounts_opened'] for x in a)
    return {
        'range_utc': [_ts(start), _ts(end)],
        'calls': c.get('attempted', 0), 'answered': c.get('answered', 0),
        'answer_rate': c.get('answer_rate'), 'talk_minutes': c.get('talk_minutes', 0.0),
        # Con filtro se usan las cuentas ATRIBUIDAS a llamadas del recorte:
        # un evento ACCOUNT_CREATED no lleva la ruta de voz, así que
        # filtrarlo directamente no diría nada del recorte elegido.
        'accounts_opened': (cuentas_atribuidas if filters
                            else b.get('accounts_opened', 0)),
        'accounts_source': 'attributed' if filters else 'events',
        'conversion_rate': (round((cuentas_atribuidas if filters
                                   else b['accounts_opened']) / c['answered'], 4)
                            if c.get('answered') else None),
        'conversion_rate_definition': 'accounts_opened / answered (mismo rango) — PENDING_PRODUCT_DECISION',
        'filters': filters or {},
        'calls_detail': c, 'business_detail': b,
    }


def timeseries(db, start, end, unit='hour', metric='calls', filters=None):
    """calls/hour · answered/hour · talk_minutes/day · accounts/day …

    Con `filters` el resultado es EXACTAMENTE el recorte que se está
    viendo: es lo que alimenta el desglose por hora (§32) y el CSV (§33).
    """
    fsql, fparams = _filters(filters)
    if metric in ('calls', 'answered', 'talk_minutes'):
        expr = {'calls': f"SUM(CASE WHEN state IN {_in(ATTEMPTED)} THEN 1 ELSE 0 END)",
                'answered': f"SUM(CASE WHEN result IN {_in(ANSWERED)} THEN 1 ELSE 0 END)",
                'talk_minutes': f"SUM(CASE WHEN result IN {_in(ANSWERED)} THEN COALESCE(duration_seconds,0) ELSE 0 END)/60.0"}[metric]
        b = _bucket(db, 'created_at', unit)
        sql = (f"SELECT {b} AS bucket, {expr} AS value FROM wf_call_jobs "
               f"WHERE created_at >= § AND created_at < §{fsql} "
               f"GROUP BY bucket ORDER BY bucket")
    elif metric == 'accounts':
        b = _bucket(db, 'occurred_at', unit)
        sql = (f"SELECT {b} AS bucket, COUNT(*) AS value FROM wf_events WHERE event_type='ACCOUNT_CREATED' "
               f"AND occurred_at >= § AND occurred_at < §{fsql} "
               f"GROUP BY bucket ORDER BY bucket")
    else:
        raise ValueError(f'métrica de timeseries desconocida: {metric!r}')
    return [{'bucket': str(r['bucket']), 'value': round(float(r['value'] or 0), 2)}
            for r in db.q(sql, (_ts(start), _ts(end), *fparams))]


# ══════════════════════════════════════════════════════════════════════
#  Atribución — ANALÍTICA, no hecho
# ══════════════════════════════════════════════════════════════════════

def _window_start(db, col, days):
    if db.driver == 'mysql':
        return f"DATE_SUB({col}, INTERVAL {int(days)} DAY)"
    return f"datetime({col}, '-{int(days)} days')"


def accounts_attributed(db, start, end, window_days=ATTRIBUTION_WINDOW_DAYS):
    """Cada ACCOUNT_CREATED del rango, con la llamada a la que se ATRIBUYE.

    Modelo LAST_CONNECTED_CALL_V1: la última llamada del mismo lead con resultado
    ANSWERED o CALLBACK, completada ANTES O EN el momento de la apertura y dentro
    de los `window_days` previos. Si no existe, la cuenta queda UNATTRIBUTED:
    no se inventa atribución. Es una convención de reporting, no un hecho.
    PENDING_PRODUCT_DECISION: modelo y ventana.
    """
    ws = _window_start(db, 'e.occurred_at', window_days)
    rows = db.q(f"""
        SELECT a.event_key, a.lead_id, a.occurred_at, a.account_country,
               j.call_job_id, j.route_key, j.provider, j.country_iso, j.completed_at
        FROM (
          SELECT e.event_key, e.lead_id, e.occurred_at, e.country_iso AS account_country,
            (SELECT j2.call_job_id FROM wf_call_jobs j2
              WHERE j2.lead_id = e.lead_id AND j2.result IN {_in(ANSWERED)}
                AND j2.completed_at <= e.occurred_at AND j2.completed_at >= {ws}
              ORDER BY j2.completed_at DESC LIMIT 1) AS job
          FROM wf_events e
          WHERE e.event_type = 'ACCOUNT_CREATED' AND e.occurred_at >= § AND e.occurred_at < §
        ) a
        LEFT JOIN wf_call_jobs j ON j.call_job_id = a.job
        ORDER BY a.occurred_at""", (_ts(start), _ts(end)))
    out = []
    for r in rows:
        out.append({'event_key': r['event_key'], 'lead_id': r['lead_id'],
                    'occurred_at': str(r['occurred_at']),
                    'attribution_model': ATTRIBUTION_MODEL,
                    'attributed': bool(r['call_job_id']),
                    'call_job_id': r['call_job_id'], 'route_key': r['route_key'] or 'UNATTRIBUTED',
                    'provider': r['provider'] or 'UNATTRIBUTED',
                    'country_iso': r['country_iso'] or r['account_country']})
    return out


def accounts_by(db, start, end, group_by='route', filters=None):
    """Cuentas abiertas, agrupadas y —desde r2-final— FILTRADAS.

    El filtro se aplica sobre la atribución ya calculada: una cuenta
    cuenta para el recorte si la llamada a la que se atribuye cae dentro
    del recorte. Filtrar los eventos de cuenta directamente daría otro
    número, porque un evento ACCOUNT_CREATED no lleva la ruta de voz.
    """
    field = {'route': 'route_key', 'provider': 'provider', 'country': 'country_iso'}[group_by]
    agg = {}
    for a in accounts_attributed(db, start, end):
        if not _matches(a, filters):
            continue
        agg[a[field]] = agg.get(a[field], 0) + 1
    return [{group_by: k, 'accounts_opened': v, 'attribution_model': ATTRIBUTION_MODEL}
            for k, v in sorted(agg.items())]


def _matches(row, filters):
    """¿Esta fila ya materializada entra en el recorte?"""
    if not filters:
        return True
    for clave, col in FILTER_FIELDS.items():
        v = filters.get(clave)
        if v in (None, '', [], ()):
            continue
        vals = [v] if isinstance(v, str) else list(v)
        if str(row.get(col) or '') not in [str(x) for x in vals]:
            return False
    return True
