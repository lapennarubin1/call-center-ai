"""
JARVIS — Skills de métricas del call center
============================================
Todo lo que se consulta de la base del panel: llamadas, cuentas
abiertas, tasa de conversión, mejores horarios, comparativas.

Nota sobre husos horarios: el CDR guarda todo en la hora del servidor
(UTC). Cuando el operador pregunta "¿a qué hora rinde más India?" no
quiere saberlo en UTC — quiere la hora local de India. Por eso cada
país tiene su corrimiento y las agrupaciones por hora lo aplican antes
de agrupar. Sin esto, los "mejores horarios" saldrían corridos varias
horas y las decisiones que se tomen con ese dato serían malas.
"""
from datetime import datetime, timedelta
from .base import skill, SkillError, READ

# Un solo lugar donde vive la definición de cada país. Agregar un país
# nuevo es agregar una entrada acá — ninguna query se toca.
COUNTRIES = {
    'india': {
        'label': 'India',
        'dst_regex': "dst REGEXP '^[+]?91[0-9]{10}$'",
        'tz_shift_min': 330,      # UTC+5:30
        'tz_label': 'IST',
    },
    'mexico': {
        'label': 'México',
        'dst_regex': "dst REGEXP '^[+]?52[0-9]{10}$'",
        'tz_shift_min': -360,     # UTC-6:00
        'tz_label': 'CST',
    },
    'venezuela': {
        'label': 'Venezuela',
        'dst_regex': "dst REGEXP '^[+]?58[0-9]{10}$'",
        'tz_shift_min': -240,     # UTC-4:00
        'tz_label': 'VET',
    },
    'colombia': {
        'label': 'Colombia',
        'dst_regex': "dst REGEXP '^[+]?57[0-9]{10}$'",
        'tz_shift_min': -300,     # UTC-5:00
        'tz_label': 'COT',
    },
}

COUNTRY_ALIASES = {
    'in': 'india', 'ind': 'india', 'hindi': 'india',
    'mx': 'mexico', 'méxico': 'mexico', 'mex': 'mexico',
    've': 'venezuela', 'vzla': 'venezuela',
    'co': 'colombia', 'col': 'colombia',
}


def resolve_country(name):
    if not name:
        raise SkillError('falta indicar el país')
    key = str(name).strip().lower()
    key = COUNTRY_ALIASES.get(key, key)
    if key not in COUNTRIES:
        disponibles = ', '.join(COUNTRIES)
        raise SkillError(f'país desconocido: {name!r}. Disponibles: {disponibles}')
    return key


def resolve_period(period=None, days=None):
    """
    Devuelve (desde, hasta, etiqueta). Acepta tanto un nombre de
    período ("hoy", "semana") como una cantidad de días — la gente
    habla de las dos formas y ninguna es más correcta que la otra.
    """
    now = datetime.utcnow()
    today = now.replace(hour=0, minute=0, second=0, microsecond=0)

    if days is not None:
        try:
            n = int(days)
        except (TypeError, ValueError):
            raise SkillError(f'días inválido: {days!r}')
        if n < 0:
            raise SkillError('los días no pueden ser negativos')
        if n == 0:
            return today, now, 'hoy'
        return today - timedelta(days=n), now, f'últimos {n} días'

    p = (period or 'hoy').strip().lower()
    if p in ('hoy', 'today'):
        return today, now, 'hoy'
    if p in ('ayer', 'yesterday'):
        return today - timedelta(days=1), today, 'ayer'
    if p in ('semana', 'week', 'esta semana', 'this week'):
        return today - timedelta(days=today.weekday()), now, 'esta semana'
    if p in ('semana pasada', 'last week'):
        start = today - timedelta(days=today.weekday() + 7)
        return start, start + timedelta(days=7), 'la semana pasada'
    if p in ('mes', 'month', 'este mes', 'this month'):
        return today.replace(day=1), now, 'este mes'
    if p in ('mes pasado', 'last month'):
        first = today.replace(day=1)
        prev_end = first
        prev_start = (first - timedelta(days=1)).replace(day=1)
        return prev_start, prev_end, 'el mes pasado'
    if p in ('7d', '7 dias', '7 días'):
        return today - timedelta(days=7), now, 'los últimos 7 días'
    if p in ('30d', '30 dias', '30 días'):
        return today - timedelta(days=30), now, 'los últimos 30 días'
    raise SkillError(
        f'período no reconocido: {period!r}. Usá: hoy, ayer, semana, '
        f'semana pasada, mes, mes pasado, 7d, 30d — o pasá días=N')


def _local_hour_expr(country, column='calldate'):
    """
    Extrae la hora LOCAL del país desde una fecha guardada en UTC.
    Se escribe distinto en MySQL y SQLite, por eso se resuelve acá y
    no en cada query.
    """
    shift = COUNTRIES[country]['tz_shift_min']
    return {
        'mysql': f"HOUR(DATE_ADD({column}, INTERVAL {shift} MINUTE))",
        'sqlite': f"CAST(strftime('%H', datetime({column}, '{shift} minutes')) AS INTEGER)",
    }


def _hour_expr(db, country, column='calldate'):
    return _local_hour_expr(country, column)[db.driver]


def _date_expr(db, column='calldate'):
    return {'mysql': f"DATE({column})", 'sqlite': f"date({column})"}[db.driver]


# ══════════════════════════════════════════════════════════════════
#  SKILLS
# ══════════════════════════════════════════════════════════════════

@skill(
    name='resumen_llamadas',
    description=(
        'Métricas de llamadas de un país en un período: total de llamadas, '
        'contestadas, tasa de respuesta (ASR), minutos facturados y costo estimado.'),
    params={
        'pais': {'type': 'string', 'description': 'india, mexico, venezuela o colombia'},
        'periodo': {'type': 'string', 'description': 'hoy, ayer, semana, semana pasada, mes, mes pasado, 7d, 30d'},
        'dias': {'type': 'integer', 'description': 'Alternativa a periodo: cantidad de días hacia atrás'},
    },
    required=['pais'],
    examples=['cuántas llamadas hicimos hoy en India', 'cómo viene México esta semana'],
)
def resumen_llamadas(pais, ctx, periodo=None, dias=None):
    country = resolve_country(pais)
    desde, hasta, label = resolve_period(periodo, dias)
    db = ctx.panel_db
    cfg = COUNTRIES[country]

    row = db.one(f"""
        SELECT COUNT(*) AS total,
               SUM(CASE WHEN disposition = 'ANSWERED' THEN 1 ELSE 0 END) AS contestadas,
               COALESCE(SUM(CASE WHEN disposition = 'ANSWERED'
                    THEN CAST((billsec + 59) / 60 AS INTEGER) ELSE 0 END), 0) AS minutos
        FROM cdr
        WHERE calldate >= § AND calldate < § AND {cfg['dst_regex']}""",
        (desde.strftime('%Y-%m-%d %H:%M:%S'), hasta.strftime('%Y-%m-%d %H:%M:%S')))

    total = int(row.get('total') or 0)
    contestadas = int(row.get('contestadas') or 0)
    minutos = int(row.get('minutos') or 0)

    return {
        'pais': cfg['label'],
        'periodo': label,
        'llamadas_totales': total,
        'contestadas': contestadas,
        'asr_pct': round(contestadas / total * 100, 1) if total else 0.0,
        'minutos_facturados': minutos,
        'costo_estimado_usd': round(minutos * 0.06, 2),
    }


@skill(
    name='cuentas_abiertas',
    description=(
        'Cuentas/conversiones creadas en un período. Sin país devuelve el '
        'desglose de todos los países; con país, solo ese.'),
    params={
        'periodo': {'type': 'string', 'description': 'hoy, ayer, semana, semana pasada, mes, mes pasado, 7d, 30d'},
        'dias': {'type': 'integer', 'description': 'Alternativa a periodo: días hacia atrás'},
        'pais': {'type': 'string', 'description': 'Opcional: filtrar por un país'},
    },
    required=[],
    examples=['cuántas cuentas se abrieron esta semana', 'conversiones de México este mes'],
)
def cuentas_abiertas(ctx, periodo=None, dias=None, pais=None):
    desde, hasta, label = resolve_period(periodo, dias)
    db = ctx.panel_db
    params = [desde.strftime('%Y-%m-%d %H:%M:%S'), hasta.strftime('%Y-%m-%d %H:%M:%S')]
    where_pais = ''
    if pais:
        country = resolve_country(pais)
        where_pais = ' AND LOWER(country) = §'
        params.append(country)

    rows = db.q(f"""
        SELECT LOWER(COALESCE(country, 'desconocido')) AS pais, COUNT(*) AS cuentas
        FROM panel_conversions
        WHERE created_at >= § AND created_at < §{where_pais}
        GROUP BY LOWER(COALESCE(country, 'desconocido'))
        ORDER BY cuentas DESC""", tuple(params))

    desglose = [{
        'pais': COUNTRIES.get(r['pais'], {}).get('label', (r['pais'] or '').title()),
        'cuentas': int(r['cuentas'] or 0),
    } for r in rows]

    return {
        'periodo': label,
        'total': sum(d['cuentas'] for d in desglose),
        'por_pais': desglose,
    }


@skill(
    name='mejores_horarios',
    description=(
        'Analiza en qué horas del día rinden más las llamadas de un país, en '
        'HORA LOCAL de ese país. Devuelve por hora: llamadas, contestadas y '
        'tasa de respuesta, para detectar las franjas de mayor alcance.'),
    params={
        'pais': {'type': 'string', 'description': 'india, mexico, venezuela o colombia'},
        'periodo': {'type': 'string', 'description': 'hoy, semana, mes, 7d, 30d'},
        'dias': {'type': 'integer', 'description': 'Alternativa a periodo'},
    },
    required=['pais'],
    examples=['a qué hora conviene llamar en India', 'analizá los mejores horarios de México'],
)
def mejores_horarios(pais, ctx, periodo=None, dias=None):
    country = resolve_country(pais)
    desde, hasta, label = resolve_period(periodo or '7d', dias)
    db = ctx.panel_db
    cfg = COUNTRIES[country]
    hour_expr = _hour_expr(db, country)

    rows = db.q(f"""
        SELECT {hour_expr} AS hora,
               COUNT(*) AS llamadas,
               SUM(CASE WHEN disposition = 'ANSWERED' THEN 1 ELSE 0 END) AS contestadas
        FROM cdr
        WHERE calldate >= § AND calldate < § AND {cfg['dst_regex']}
        GROUP BY {hour_expr}
        ORDER BY hora""",
        (desde.strftime('%Y-%m-%d %H:%M:%S'), hasta.strftime('%Y-%m-%d %H:%M:%S')))

    horas = []
    for r in rows:
        llamadas = int(r['llamadas'] or 0)
        contestadas = int(r['contestadas'] or 0)
        horas.append({
            'hora': int(r['hora'] or 0),
            'llamadas': llamadas,
            'contestadas': contestadas,
            'asr_pct': round(contestadas / llamadas * 100, 1) if llamadas else 0.0,
        })

    # "Mejor hora" con un mínimo de volumen: una hora con 2 llamadas y
    # 100% de respuesta no es la mejor franja, es ruido estadístico.
    significativas = [h for h in horas if h['llamadas'] >= 10] or horas
    mejor = max(significativas, key=lambda h: h['asr_pct'], default=None)
    mas_volumen = max(horas, key=lambda h: h['llamadas'], default=None)

    return {
        'pais': cfg['label'],
        'periodo': label,
        'zona_horaria': cfg['tz_label'],
        'por_hora': horas,
        'mejor_hora_respuesta': mejor,
        'hora_mas_volumen': mas_volumen,
        'nota': 'Horas en hora local del país. "Mejor hora" considera solo franjas con 10+ llamadas.',
    }


@skill(
    name='tendencia_diaria',
    description=(
        'Serie por día de llamadas, contestadas y cuentas abiertas — para ver '
        'la evolución y detectar caídas o picos.'),
    params={
        'pais': {'type': 'string', 'description': 'india, mexico, venezuela o colombia'},
        'dias': {'type': 'integer', 'description': 'Cuántos días hacia atrás (default 14)'},
    },
    required=['pais'],
    examples=['mostrame la tendencia de India de los últimos 15 días', 'cómo viene evolucionando México'],
)
def tendencia_diaria(pais, ctx, dias=14):
    country = resolve_country(pais)
    desde, hasta, label = resolve_period(None, dias or 14)
    db = ctx.panel_db
    cfg = COUNTRIES[country]
    date_expr = _date_expr(db, 'calldate')
    p = (desde.strftime('%Y-%m-%d %H:%M:%S'), hasta.strftime('%Y-%m-%d %H:%M:%S'))

    llamadas = db.q(f"""
        SELECT {date_expr} AS dia,
               COUNT(*) AS llamadas,
               SUM(CASE WHEN disposition = 'ANSWERED' THEN 1 ELSE 0 END) AS contestadas
        FROM cdr
        WHERE calldate >= § AND calldate < § AND {cfg['dst_regex']}
        GROUP BY {date_expr} ORDER BY dia""", p)

    conv_date = _date_expr(db, 'created_at')
    cuentas = db.q(f"""
        SELECT {conv_date} AS dia, COUNT(*) AS cuentas
        FROM panel_conversions
        WHERE created_at >= § AND created_at < § AND LOWER(country) = §
        GROUP BY {conv_date} ORDER BY dia""", p + (country,))

    cuentas_por_dia = {str(r['dia']): int(r['cuentas'] or 0) for r in cuentas}
    serie = []
    for r in llamadas:
        dia = str(r['dia'])
        total = int(r['llamadas'] or 0)
        contestadas = int(r['contestadas'] or 0)
        serie.append({
            'dia': dia,
            'llamadas': total,
            'contestadas': contestadas,
            'asr_pct': round(contestadas / total * 100, 1) if total else 0.0,
            'cuentas': cuentas_por_dia.get(dia, 0),
        })

    return {'pais': cfg['label'], 'periodo': label, 'serie': serie}


@skill(
    name='embudo_conversion',
    description=(
        'Embudo completo de un país: llamadas → contestadas → cuentas abiertas, '
        'con las tasas de conversión de cada paso.'),
    params={
        'pais': {'type': 'string', 'description': 'india, mexico, venezuela o colombia'},
        'periodo': {'type': 'string', 'description': 'hoy, semana, mes, 7d, 30d'},
        'dias': {'type': 'integer', 'description': 'Alternativa a periodo'},
    },
    required=['pais'],
    examples=['cómo está el embudo de India', 'qué tasa de conversión tenemos en México'],
)
def embudo_conversion(pais, ctx, periodo=None, dias=None):
    country = resolve_country(pais)
    desde, hasta, label = resolve_period(periodo or 'semana', dias)
    db = ctx.panel_db
    cfg = COUNTRIES[country]
    p = (desde.strftime('%Y-%m-%d %H:%M:%S'), hasta.strftime('%Y-%m-%d %H:%M:%S'))

    llamadas = db.one(f"""
        SELECT COUNT(*) AS total,
               SUM(CASE WHEN disposition = 'ANSWERED' THEN 1 ELSE 0 END) AS contestadas
        FROM cdr WHERE calldate >= § AND calldate < § AND {cfg['dst_regex']}""", p)

    cuentas = db.one("""
        SELECT COUNT(*) AS cuentas FROM panel_conversions
        WHERE created_at >= § AND created_at < § AND LOWER(country) = §""", p + (country,))

    total = int(llamadas.get('total') or 0)
    contestadas = int(llamadas.get('contestadas') or 0)
    abiertas = int(cuentas.get('cuentas') or 0)

    return {
        'pais': cfg['label'],
        'periodo': label,
        'llamadas': total,
        'contestadas': contestadas,
        'cuentas_abiertas': abiertas,
        'asr_pct': round(contestadas / total * 100, 1) if total else 0.0,
        'conversion_sobre_contestadas_pct': round(abiertas / contestadas * 100, 2) if contestadas else 0.0,
        'conversion_sobre_llamadas_pct': round(abiertas / total * 100, 2) if total else 0.0,
        'llamadas_por_cuenta': round(total / abiertas, 1) if abiertas else None,
    }


@skill(
    name='comparar_paises',
    description=(
        'Compara todos los países lado a lado en el mismo período: llamadas, '
        'ASR, cuentas y costo. Para saber cuál rinde mejor.'),
    params={
        'periodo': {'type': 'string', 'description': 'hoy, ayer, semana, mes, 7d, 30d'},
        'dias': {'type': 'integer', 'description': 'Alternativa a periodo'},
    },
    required=[],
    examples=['compará India contra México', 'qué país está rindiendo mejor'],
)
def comparar_paises(ctx, periodo=None, dias=None):
    desde, hasta, label = resolve_period(periodo or 'semana', dias)
    comparativa = []
    for country in COUNTRIES:
        try:
            resumen = resumen_llamadas(country, ctx=ctx, periodo=periodo, dias=dias)
            embudo = embudo_conversion(country, ctx=ctx, periodo=periodo, dias=dias)
        except SkillError:
            continue
        # Países sin actividad en el período no aportan a la comparación
        # y ensucian la lectura — se omiten.
        if resumen['llamadas_totales'] == 0 and embudo['cuentas_abiertas'] == 0:
            continue
        comparativa.append({
            'pais': resumen['pais'],
            'llamadas': resumen['llamadas_totales'],
            'contestadas': resumen['contestadas'],
            'asr_pct': resumen['asr_pct'],
            'cuentas': embudo['cuentas_abiertas'],
            'conversion_pct': embudo['conversion_sobre_contestadas_pct'],
            'costo_usd': resumen['costo_estimado_usd'],
            'costo_por_cuenta_usd': round(
                resumen['costo_estimado_usd'] / embudo['cuentas_abiertas'], 2
            ) if embudo['cuentas_abiertas'] else None,
        })
    return {'periodo': label, 'paises': comparativa}


@skill(
    name='estado_leads',
    description=(
        'Distribución de leads por estado (pendientes, en progreso, contestados, '
        'fallidos...) para un país. Sirve para ver cuánta cola queda por trabajar.'),
    params={
        'pais': {'type': 'string', 'description': 'india, mexico, venezuela o colombia'},
    },
    required=['pais'],
    examples=['cuántos leads quedan en India', 'qué estados tienen los leads de México'],
)
def estado_leads(pais, ctx):
    country = resolve_country(pais)
    db = ctx.panel_db
    rows = db.q("""
        SELECT COALESCE(status, 'SIN_ESTADO') AS estado, COUNT(*) AS n
        FROM panel_leads WHERE LOWER(country) = §
        GROUP BY COALESCE(status, 'SIN_ESTADO') ORDER BY n DESC""", (country,))
    estados = [{'estado': r['estado'], 'cantidad': int(r['n'] or 0)} for r in rows]
    return {
        'pais': COUNTRIES[country]['label'],
        'total': sum(e['cantidad'] for e in estados),
        'por_estado': estados,
    }
