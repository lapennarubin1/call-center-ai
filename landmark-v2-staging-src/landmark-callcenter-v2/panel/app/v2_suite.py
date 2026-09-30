"""
v2_suite.py — Pantallas y API que agrega el Call Center V2 al panel
====================================================================

Se registra sobre la app Flask existente con `register(app, deps)` desde
server.py (3 líneas de hook). El panel de la fundación V2.2 no se reescribe:
lo que había —Countries, Providers, Routes, Follow-up Policies, Tool
Configurations, /api/routes/*— queda intacto.

Qué agrega
----------
  /callcenter/analytics        Analytics del call center, 100% desde MySQL LOCAL
  /callcenter/analytics.csv    export del mismo corte
  /callcenter/issues           reconciliación: issues abiertos, resolver/ignorar
  /callcenter/settings         wf_settings (parámetros operativos del suite)
  /api/settings                lo que n8n lee para no llevar umbrales hardcodeados
  /api/analytics/summary.json  el mismo corte, para tableros externos

Principio que no se negocia (ANALYTICS_ARCHITECTURE §1): ninguna de estas
pantallas abre una conexión a LeadStudio. Ni para un KPI, ni para un refresh,
ni para una vista por país. Si LeadStudio está caído, Analytics sigue andando.
"""

import csv
import io
import json
from datetime import datetime, timedelta, timezone as _tz
from zoneinfo import ZoneInfo

from flask import Response, jsonify, redirect, render_template, request, session

import analytics as an
import analytics_v2 as av2
import billing as bi
import legacy_mode as lm
import ops_events as oe
import routes_config as rc
import wf_settings as ws

GROUPS = ['country', 'provider', 'route']
# Tope del rango consultable, en días.
#
# 366 para que el preset "Year" quepa entero. No es arbitrario: todas las
# consultas de analytics son agregados con GROUP BY sobre columnas
# indexadas (`created_at`, `occurred_at`), así que el coste crece con el
# número de filas, no con el de días. Un año de un call center que hace
# unos miles de llamadas al día son cientos de miles de filas, que MySQL
# agrega sin despeinarse.
#
# El tope existe para que una URL escrita a mano (`?days=100000`) no
# monte una consulta de años. Pedir más no falla: se recorta a este
# valor. Documentado en ANALYTICS_GUIDE.md.
MAX_DAYS = 366


def _actor():
    return session.get('user') or session.get('role') or 'panel'


# Presets del selector. Son los mismos nombres que usa el dashboard de
# siempre, para que quien salte de una pantalla a otra no tenga que
# aprender un vocabulario nuevo.
PRESETS = {
    'today':     {'label': 'Today',     'days': 1,   'offset': 0},
    'yesterday': {'label': 'Yesterday', 'days': 1,   'offset': 1},
    'week':      {'label': 'Week',      'days': 7,   'offset': 0},
    'month':     {'label': 'Month',     'days': 30,  'offset': 0},
    '7d':        {'label': '7 days',    'days': 7,   'offset': 0},
    '30d':       {'label': '30 days',   'days': 30,  'offset': 0},
    '90d':       {'label': '90 days',   'days': 90,  'offset': 0},
    'year':      {'label': 'Year',      'days': 365, 'offset': 0},
}
DEFAULT_PRESET = 'today'


def _range(args):
    """(start, end, tz, days). El rango es [start, end) en UTC; los días se
    cuentan en la ZONA HORARIA pedida, no en UTC.

    Tres formas de pedir un rango, por orden de precedencia:
      1. `start` y `end` explícitos (YYYY-MM-DD) — rango exacto
      2. `period` — uno de los presets
      3. `days` — compatibilidad con la versión anterior

    El límite de MAX_DAYS existe por rendimiento y está documentado en
    ANALYTICS_GUIDE.md; pedir más no falla, se recorta y se avisa.
    """
    tz = args.get('tz') or 'UTC'
    try:
        zona = ZoneInfo(tz)
    except Exception:
        raise ValueError(f'timezone inválida: {tz!r}')

    inicio = (args.get('start') or '').strip()
    fin = (args.get('end') or '').strip()
    if inicio and fin:
        try:
            d0 = datetime.strptime(inicio, '%Y-%m-%d').date()
            d1 = datetime.strptime(fin, '%Y-%m-%d').date()
        except ValueError:
            raise ValueError('start y end deben tener formato YYYY-MM-DD')
        if d1 < d0:
            raise ValueError('end no puede ser anterior a start')
        days = (d1 - d0).days + 1          # rango inclusivo, como lo lee la gente
        if days > MAX_DAYS:
            days = MAX_DAYS
            d0 = d1 - timedelta(days=days - 1)
        start, end = av2.day_range_utc(d0, tz, days)
        return start, end, tz, days

    period = (args.get('period') or '').strip().lower()
    if period in PRESETS:
        pr = PRESETS[period]
        days = min(pr['days'], MAX_DAYS)
        ultimo = datetime.now(zona).date() - timedelta(days=pr['offset'])
        primero = ultimo - timedelta(days=days - 1)
        start, end = av2.day_range_utc(primero, tz, days)
        return start, end, tz, days

    try:
        days = max(1, min(int(args.get('days') or 1), MAX_DAYS))
    except (TypeError, ValueError):
        raise ValueError('days debe ser un entero')
    primero = datetime.now(zona).date() - timedelta(days=days - 1)
    start, end = av2.day_range_utc(primero, tz, days)
    return start, end, tz, days


def analytics_payload(db, args):
    """El corte completo que consumen la pantalla, el CSV y la API.

    Todas las métricas que pide el panel V2 salen de acá:
      intentadas · despachadas · contestadas · no contestadas · tasa de respuesta
      minutos de conversación · duración media · cuentas abiertas · conversión
      callbacks · follow-ups · fallos técnicos
    y los mismos números desglosados por país, proveedor, ruta y hora/día.
    """
    start, end, tz, days = _range(args)
    unit = 'day' if days > 2 else 'hour'
    # §32/§33: los mismos filtros alimentan los KPI, el desglose por hora y
    # el CSV. Se calculan UNA vez y se pasan a todo, para que la pantalla y
    # el fichero exportado no puedan divergir.
    filters = av2.normalize_filters(args)
    # UN SOLO objeto de recorte alimenta KPI, desgloses, series, hora y CSV.
    # Se construye aquí y se pasa a TODO. Antes `overview()` se calculaba
    # sin filtros, así que la pantalla podía decir "India" y las tarjetas
    # mostrar los totales globales.
    cut = {'start': start, 'end': end, 'tz': tz, 'days': days,
           'unit': unit, 'filters': filters,
           'period': (args.get('period') or '').strip().lower() or None,
           'start_date': (args.get('start') or '').strip() or None,
           'end_date': (args.get('end') or '').strip() or None}
    out = {
        'generated_at': datetime.now(_tz.utc).strftime('%Y-%m-%dT%H:%M:%SZ'),
        'tz': tz, 'days': days, 'bucket_unit': unit,
        'range_utc': [av2._ts(start), av2._ts(end)],
        'source': 'LOCAL_MYSQL',
        'cut': cut,
        'query': _cut_query(cut),
        'presets': PRESETS,
        'filters': filters,
        'filters_label': av2.describe_filters(filters),
        'totals': av2.call_metrics(db, start, end, filters=filters),
        'overview': av2.overview(db, start, end, filters=filters),
        'by': {g: av2.call_metrics(db, start, end, g, filters=filters) for g in GROUPS},
        'business_by': {g: av2.business_metrics(db, start, end, g, filters=filters)
                        for g in ('country', 'route')},
        'accounts_by': {g: av2.accounts_by(db, start, end, g, filters=filters)
                        for g in GROUPS},
        'timeseries': {
            'calls': av2.timeseries(db, start, end, unit, 'calls', filters=filters),
            'answered': av2.timeseries(db, start, end, unit, 'answered', filters=filters),
            'talk_minutes': av2.timeseries(db, start, end, unit, 'talk_minutes', filters=filters),
            'accounts': av2.timeseries(db, start, end, unit, 'accounts', filters=filters),
        },
        'attribution_model': av2.ATTRIBUTION_MODEL,
    }
    out['options'] = filter_options(db)
    out['health'] = health_counters(db)
    return out


def _cut_query(cut, **extra):
    """El recorte activo, como query string.

    Los enlaces de CSV se construyen SIEMPRE con esto, para que sea
    imposible generar una URL de descarga que pierda un filtro (§3).
    """
    from urllib.parse import urlencode
    q = {'tz': cut['tz']}
    if cut.get('start_date') and cut.get('end_date'):
        q['start'] = cut['start_date']
        q['end'] = cut['end_date']
    elif cut.get('period'):
        q['period'] = cut['period']
    else:
        q['days'] = cut['days']
    for clave, v in (cut.get('filters') or {}).items():
        q[clave] = ','.join(v) if isinstance(v, (list, tuple)) else v
    q.update({k: v for k, v in extra.items() if v not in (None, '')})
    return urlencode(q)


def filter_options(db):
    """Qué puede elegir el usuario en los selectores. Sale de lo que hay
    configurado, no de una lista fija: un país nuevo aparece solo."""
    return {
        'country': [r['iso'] for r in db.q(
            'SELECT iso FROM countries WHERE archived_at IS NULL ORDER BY iso')],
        'provider': [r['code'] for r in db.q(
            'SELECT code FROM voice_providers ORDER BY code')],
        'route': [r['route_key'] for r in db.q(
            'SELECT route_key FROM call_routes ORDER BY route_key')],
    }


def health_counters(db):
    """Lo que un operador mira antes que cualquier KPI: qué quedó a medias."""
    def _n(sql, params=()):
        r = db.one(sql, params)
        return int((r or {}).get('n') or 0)
    return {
        'jobs_in_flight': _n("SELECT COUNT(*) n FROM wf_call_jobs WHERE state IN "
                             "('CLAIMED','DISPATCHING','DISPATCHED')"),
        'jobs_needs_reconciliation': _n("SELECT COUNT(*) n FROM wf_call_jobs WHERE state IN "
                                        "('UNKNOWN','NEEDS_RECONCILIATION')"),
        'jobs_released': _n("SELECT COUNT(*) n FROM wf_call_jobs WHERE state='RELEASED'"),
        'ledger_stuck': _n("SELECT COUNT(*) n FROM wf_conversation_ledger "
                           "WHERE state IN ('CLAIMED','NEEDS_RECONCILIATION')"),
        'issues_open': _n("SELECT COUNT(*) n FROM wf_reconciliation_issues WHERE state='OPEN'"),
        'recordings_orphan': _n("SELECT COUNT(*) n FROM wf_recording_ledger WHERE state='ORPHAN'"),
        'tool_requests_stuck': _n("SELECT COUNT(*) n FROM wf_tool_requests WHERE state IN "
                                  "('CLAIMED','NEEDS_RECONCILIATION')"),
    }


def issues_list(db, state='OPEN', limit=200):
    if state not in ('OPEN', 'RESOLVED', 'IGNORED', 'ALL'):
        raise ValueError(f'state inválido: {state!r}')
    where = '' if state == 'ALL' else 'WHERE state=§'
    params = () if state == 'ALL' else (state,)
    rows = db.q(f"SELECT * FROM wf_reconciliation_issues {where} "
                f"ORDER BY last_seen_at DESC LIMIT {int(limit)}", params)
    for r in rows:
        try:
            r['detail'] = json.loads(r.get('detail_json') or '{}')
        except (ValueError, TypeError):
            r['detail'] = {}
    return rows


def issues_summary(db):
    return db.q("SELECT issue_type, state, COUNT(*) AS n, MAX(last_seen_at) AS last_seen "
                "FROM wf_reconciliation_issues GROUP BY issue_type, state "
                "ORDER BY state, n DESC")


# ══════════════════════════════════════════════════════════════════════
#  Registro sobre la app existente
# ══════════════════════════════════════════════════════════════════════

def register(app, auth_master, auth_service_token, with_db):
    """server.py llama a esto pasándole sus propios decoradores. Así las
    pantallas nuevas heredan exactamente la misma autenticación y el mismo
    manejo de conexión que el resto del panel: no hay una segunda forma de
    autenticarse que auditar."""

    # El cerrojo ya está puesto desde el import de este módulo (ver la
    # nota al final del fichero). Se vuelve a llamar aquí sólo por si
    # alguien importó `legacy_mode` y recargó `analytics` por su cuenta:
    # la función es idempotente y no anida envolturas.
    lm.install_activation_guard(an)

    # ── Analytics (master) ────────────────────────────────────────────
    @app.route('/callcenter/analytics')
    @auth_master
    @with_db
    def cc_analytics(db):
        try:
            data = analytics_payload(db, request.args)
        except ValueError as ex:
            return render_template('error.html', error=str(ex)), 400
        except Exception as ex:
            return render_template('error.html',
                                   error=f'Analytics no disponible: {ex}. '
                                         'Revisar que migration.sql esté aplicada.'), 503
        return render_template('analytics.html', a=data, groups=GROUPS,
                               tz=data['tz'], days=data['days'])

    @app.route('/callcenter/analytics.csv')
    @auth_master
    @with_db
    def cc_analytics_csv(db):
        group = request.args.get('group_by') or 'route'
        # 'hour' exporta el mismo desglose temporal que se ve en pantalla
        # (§3: el Breakdown by Hour se puede descargar para el recorte
        # elegido). Los demás agrupan por país/proveedor/ruta.
        if group not in GROUPS and group != 'hour':
            return jsonify({'error': 'group_by inválido',
                            'valid': list(GROUPS) + ['hour'],
                            'code': 'VALIDATION_ERROR'}), 400
        try:
            data = analytics_payload(db, request.args)
        except ValueError as ex:
            return jsonify({'error': str(ex), 'code': 'VALIDATION_ERROR'}), 400

        buf = io.StringIO()
        if group == 'hour':
            # Se reconstruye desde las MISMAS series que pinta la pantalla,
            # con los MISMOS filtros: el fichero no puede discrepar del
            # gráfico porque sale del mismo cálculo.
            serie = {r['bucket']: r['value'] for r in data['timeseries']['calls']}
            cont = {r['bucket']: r['value'] for r in data['timeseries']['answered']}
            mins = {r['bucket']: r['value'] for r in data['timeseries']['talk_minutes']}
            cuentas = {r['bucket']: r['value'] for r in data['timeseries']['accounts']}
            cols = ['bucket', 'calls', 'answered', 'answer_rate', 'talk_minutes',
                    'accounts']
            rows = []
            for b in sorted(set(serie) | set(cont) | set(mins) | set(cuentas)):
                c, an_ = serie.get(b, 0), cont.get(b, 0)
                # Las cuentas van como ENTEROS: un CSV que dice "3.0
                # llamadas" se lee mal y descoloca cualquier suma en Excel.
                # Los minutos sí llevan decimal, que es su unidad natural.
                rows.append({'bucket': b, 'calls': int(c), 'answered': int(an_),
                             'answer_rate': round(an_ / c, 4) if c else None,
                             'talk_minutes': round(float(mins.get(b, 0)), 1),
                             'accounts': int(cuentas.get(b, 0))})
        else:
            rows = data['by'][group]
            cols = [group, 'attempted', 'dispatched', 'with_result', 'answered',
                    'no_answer', 'busy', 'voicemail', 'callbacks', 'wrong_number',
                    'dnc', 'failed_call', 'failed_technical', 'needs_reconciliation',
                    'answer_rate', 'talk_minutes', 'avg_talk_seconds']
        # CSV LIMPIO: cabecera en la primera línea, como el resto de los
        # exports del panel. Un preámbulo de comentarios se ve como filas
        # basura en Excel y descoloca a pandas, así que el contexto del
        # recorte viaja en cabeceras HTTP y en el nombre del fichero — no
        # dentro de los datos (§33: no romper el export que ya existe).
        w = csv.DictWriter(buf, fieldnames=cols, extrasaction='ignore')
        w.writeheader()
        for r in rows:
            w.writerow(r)
        sufijo = '-filtered' if data['filters'] else ''
        return Response(buf.getvalue(), mimetype='text/csv', headers={
            'Content-Disposition': (f'attachment; filename=callcenter-{group}'
                                    f'{sufijo}-{data["days"]}d.csv'),
            # Lo que se exportó, inspeccionable sin abrir el fichero.
            'X-Landmark-Filters': data['filters_label'],
            'X-Landmark-Range-Utc': f'{data["range_utc"][0]}..{data["range_utc"][1]}',
            'X-Landmark-Source': data['source'],
        })

    @app.route('/api/analytics/summary.json')
    @auth_service_token
    @with_db
    def api_analytics_summary(db):
        try:
            data = analytics_payload(db, request.args)
        except ValueError as ex:
            return jsonify({'error': str(ex), 'code': 'VALIDATION_ERROR'}), 400
        except Exception as ex:
            return jsonify({'error': str(ex), 'code': 'CONFIG_ERROR'}), 503
        return Response(json.dumps(data, default=str), mimetype='application/json')

    # ── Facturación (master) · §34-42 ─────────────────────────────────
    def _billing_view(db, ok=None, err=None):
        provs = bi.providers_billing(db)
        rutas = db.q("""SELECT route_key, iso, provider_id, trunk_name,
                               price_per_minute, price_per_call, archived_at
                          FROM call_routes ORDER BY route_key""")
        por_prov = {}
        for r in rutas:
            por_prov.setdefault(r['provider_id'], []).append(r)
        for p in provs:
            p['routes'] = por_prov.get(p['id'], [])
            p['issues'] = bi.validate_provider_billing(p, p['routes'])
            # El coste efectivo sólo se calcula donde significa algo.
            if p['billing_model'] == 'MONTHLY_FLAT':
                uso = _provider_usage(db, p['code'])
                p['effective'] = bi.effective_cost(p, uso)
            else:
                p['effective'] = None
        for p in provs:
            p['needs_sip'] = bi.adapter_needs_sip_balance(p.get('adapter_key'))
            p['link_options'] = (bi.unlinked_sip_providers(db, p['code'])
                                 if p['needs_sip'] else [])
        return render_template('billing.html', providers=provs,
                               models=bi.BILLING_MODELS,
                               adapters=list(rc.ADAPTERS),
                               orphans=bi.orphan_sip_providers(db),
                               audit=bi.audit_log(db, limit=40),
                               ok=ok, err=err)

    def _provider_usage(db, code):
        """Uso del período de facturación en curso, para el coste efectivo."""
        r = db.one("""SELECT COUNT(*) AS calls,
                             SUM(CASE WHEN result IN ('ANSWERED','CALLBACK')
                                      THEN 1 ELSE 0 END) AS answered,
                             SUM(CASE WHEN result IN ('ANSWERED','CALLBACK')
                                      THEN COALESCE(duration_seconds,0)
                                      ELSE 0 END) AS talk_seconds
                        FROM wf_call_jobs
                       WHERE provider = § 
                         AND created_at >= DATE_FORMAT(UTC_TIMESTAMP(), '%%Y-%%m-01')""",
                   (code,))
        return {'calls': int(r.get('calls') or 0),
                'answered': int(r.get('answered') or 0),
                'talk_minutes': round((r.get('talk_seconds') or 0) / 60, 1)}

    @app.route('/callcenter/billing')
    @auth_master
    @with_db
    def cc_billing(db):
        try:
            return _billing_view(db)
        except Exception as ex:
            return render_template('error.html',
                                   error=f'Billing no disponible: {ex}. '
                                         'Revisar que migration.sql esté aplicada.'), 503

    @app.route('/callcenter/billing/<code>/save', methods=['POST'])
    @auth_master
    @with_db
    def cc_billing_save(db, code):
        f = request.form
        try:
            bi.set_provider_billing(
                db, _actor(), code, f.get('billing_model'),
                currency=f.get('billing_currency'),
                monthly_fee=f.get('monthly_fee') or None,
                start_date=f.get('billing_start_date') or None,
                notes=f.get('billing_notes') or None,
                reason=f.get('reason') or None)
            return _billing_view(db, ok=f'Billing updated for {code}.')
        except bi.BillingError as ex:
            return _billing_view(db, err=str(ex)), 400

    @app.route('/callcenter/billing/route/<route_key>/save', methods=['POST'])
    @auth_master
    @with_db
    def cc_billing_route_save(db, route_key):
        f = request.form
        try:
            bi.set_route_rate(db, _actor(), route_key,
                              price_per_minute=f.get('price_per_minute') or None,
                              price_per_call=f.get('price_per_call') or None,
                              notes=f.get('billing_notes') or None,
                              reason=f.get('reason') or None)
            return _billing_view(db, ok=f'Rate updated for {route_key}.')
        except bi.BillingError as ex:
            return _billing_view(db, err=str(ex)), 400

    @app.route('/api/billing/providers.json')
    @auth_service_token
    @with_db
    def api_billing_providers(db):
        try:
            provs = bi.providers_billing(db)
            return Response(json.dumps({'providers': provs,
                                        'models': list(bi.BILLING_MODELS)},
                                       default=str),
                            mimetype='application/json')
        except Exception as ex:
            return jsonify({'error': str(ex), 'code': 'CONFIG_ERROR'}), 503

    @app.route('/callcenter/billing/<code>/link', methods=['POST'])
    @auth_master
    @with_db
    def cc_billing_link(db, code):
        """Vincula (o desvincula, con valor vacío) la ficha de SIP Balance.

        Expone `link_legacy_provider`, que existía pero no tenía ruta: sin
        UI, un proveedor NOT LINKED no se podía arreglar desde el panel.
        """
        f = request.form
        try:
            bi.link_legacy_provider(db, _actor(), code,
                                    f.get('legacy_sip_provider_id') or None,
                                    reason=f.get('reason') or None)
            destino = f.get('legacy_sip_provider_id')
            return _billing_view(db, ok=(f'{code} linked to SIP Balance record '
                                         f'{destino}.' if destino
                                         else f'{code} unlinked from SIP Balance.'))
        except bi.BillingError as ex:
            return _billing_view(db, err=str(ex)), 400

    @app.route('/callcenter/billing/new', methods=['POST'])
    @auth_master
    @with_db
    def cc_billing_new(db):
        """Alta de proveedor en UN solo paso, resolviendo también su
        identidad de facturación. Nadie tiene que ir a SIP Balance a
        crear el mismo proveedor por segunda vez (§4)."""
        f = request.form
        try:
            r = bi.create_provider(
                db, _actor(),
                code=f.get('code'), display_name=f.get('display_name'),
                adapter_key=f.get('adapter_key'),
                billing_model=f.get('billing_model'),
                billing_currency=f.get('billing_currency'),
                monthly_fee=f.get('monthly_fee') or None,
                endpoint=f.get('endpoint') or None,
                notes=f.get('notes') or None,
                legacy_sip_provider_id=f.get('legacy_sip_provider_id') or None,
                create_legacy_name=f.get('create_legacy_name') or None,
                reason=f.get('reason') or None)
            return _billing_view(db, ok=(
                f'Provider {r["code"]} created, disabled. '
                + ('Linked to SIP Balance.' if r['legacy_sip_provider_id']
                   else 'No SIP Balance link needed.')))
        except bi.BillingError as ex:
            return _billing_view(db, err=str(ex)), 400

    # ── Legacy Backup / modo de operación (master) · §46-53 ───────────
    def _legacy_view(db, ok=None, err=None):
        info = lm.mode_info(db)
        # El estado REAL de los grupos, preguntándole a n8n. Si n8n no
        # contesta, la plantilla lo dice y el cambio a V2 queda bloqueado.
        live = lm.legacy_live_status(db, an)
        return render_template(
            'legacy.html', mode=info,
            groups=lm.legacy_groups(db),
            matrix=lm.compatibility_matrix(db),
            live_routes=lm.v2_dispatch_active(db),
            live=live,
            preflight=lm.preflight(db, info['other'], an),
            categories=lm.CATEGORIES,
            audit=lm.mode_audit(db, 40), ok=ok, err=err)

    @app.route('/callcenter/legacy')
    @auth_master
    @with_db
    def cc_legacy(db):
        try:
            return _legacy_view(db)
        except Exception as ex:
            return render_template('error.html',
                                   error=f'Legacy Backup no disponible: {ex}. '
                                         'Revisar que migration.sql esté aplicada.'), 503

    @app.route('/callcenter/legacy/mode', methods=['POST'])
    @auth_master
    @with_db
    def cc_legacy_mode(db):
        f = request.form
        try:
            r = lm.set_mode(db, _actor(), session.get('role'),
                            f.get('to_mode'),
                            confirmation=f.get('confirmation'),
                            reason=f.get('reason') or None,
                            # verificación REAL contra n8n antes de volver a V2
                            analytics_module=an)
            return _legacy_view(db, ok=r['message'])
        except lm.PermissionDenied as ex:
            return _legacy_view(db, err=str(ex)), 403
        except lm.ModeError as ex:
            return _legacy_view(db, err=str(ex)), 400

    @app.route('/callcenter/legacy/classify', methods=['POST'])
    @auth_master
    @with_db
    def cc_legacy_classify(db):
        f = request.form
        try:
            lm.classify_group(db, _actor(), session.get('role'),
                              f.get('label'), f.get('category'),
                              rationale=f.get('rationale') or None)
            return _legacy_view(db, ok=f'Classified {f.get("label")}.')
        except lm.PermissionDenied as ex:
            return _legacy_view(db, err=str(ex)), 403
        except lm.ModeError as ex:
            return _legacy_view(db, err=str(ex)), 400

    @app.route('/api/legacy/mode.json')
    @auth_service_token
    @with_db
    def api_legacy_mode(db):
        try:
            info = lm.mode_info(db)
            return Response(json.dumps({
                'mode': info['mode'],
                'dispatch_allowed': info['mode'] == 'V2_PRIMARY',
                'changed_at': info['changed_at'],
                'v2_routes_live': lm.v2_dispatch_active(db),
                'groups': lm.legacy_groups(db),
                'legacy_live': lm.legacy_live_status(db, an),
            }, default=str), mimetype='application/json')
        except Exception as ex:
            return jsonify({'error': str(ex), 'code': 'CONFIG_ERROR'}), 503

    # ── Reconciliación (master) ───────────────────────────────────────
    @app.route('/callcenter/issues')
    @auth_master
    @with_db
    def cc_issues(db):
        state = request.args.get('state', 'OPEN').upper()
        try:
            rows = issues_list(db, state)
            summary = issues_summary(db)
            health = health_counters(db)
        except Exception as ex:
            return render_template('error.html',
                                   error=f'Reconciliación no disponible: {ex}'), 503
        return render_template('issues.html', issues=rows, summary=summary,
                               health=health, state=state,
                               issue_types=oe.ISSUE_TYPES)

    @app.route('/callcenter/issues/<path:issue_key>/resolve', methods=['POST'])
    @auth_master
    @with_db
    def cc_issue_resolve(db, issue_key):
        state = 'IGNORED' if request.form.get('ignore') else 'RESOLVED'
        oe.resolve_issue(db, issue_key, _actor(), state)
        return redirect(request.form.get('back') or '/callcenter/issues')

    @app.route('/api/issues')
    @auth_service_token
    @with_db
    def api_issues(db):
        state = request.args.get('state', 'OPEN').upper()
        try:
            rows = issues_list(db, state, int(request.args.get('limit') or 200))
        except ValueError as ex:
            return jsonify({'error': str(ex), 'code': 'VALIDATION_ERROR'}), 400
        return Response(json.dumps({'count': len(rows), 'issues': rows}, default=str),
                        mimetype='application/json')

    # ── Settings operativos (master) ──────────────────────────────────
    @app.route('/callcenter/settings')
    @auth_master
    @with_db
    def cc_settings(db):
        try:
            data = ws.all_settings(db)
        except Exception as ex:
            return render_template('error.html', error=f'Settings no disponibles: {ex}'), 503
        by_scope = {}
        for k, v in sorted(data.items()):
            by_scope.setdefault(v['scope'], []).append(dict(v, key=k))
        return render_template('settings.html', by_scope=by_scope,
                               adapters=rc.ADAPTERS, err=request.args.get('err'))

    @app.route('/callcenter/settings/save', methods=['POST'])
    @auth_master
    @with_db
    def cc_settings_save(db):
        key = request.form.get('setting_key', '')
        try:
            ws.set_setting(db, _actor(), key, request.form.get('setting_value'),
                           value_type=request.form.get('value_type') or None,
                           description=request.form.get('description') or None,
                           scope=request.form.get('scope') or None)
        except ws.SettingError as ex:
            return redirect('/callcenter/settings?err=' + str(ex)[:200])
        return redirect('/callcenter/settings')

    @app.route('/api/settings')
    @auth_service_token
    @with_db
    def api_settings(db):
        """Lo que leen los workflows: umbrales, ventanas y la ruta de
        compatibilidad. Fail-closed igual que /api/routes/active: si esto no
        responde 200, el workflow usa sus defaults documentados y lo LOGUEA."""
        try:
            payload = ws.settings_payload(db)
        except Exception as ex:
            return jsonify({'error': str(ex), 'code': 'CONFIG_ERROR'}), 503
        return Response(json.dumps(payload, default=str), mimetype='application/json')

    return app


# ══════════════════════════════════════════════════════════════════════
#  EL CERROJO SE INSTALA AL IMPORTAR ESTE MÓDULO
#  ────────────────────────────────────────────────────────────────────
#  No en `register()`. La diferencia no es de estilo: en `server.py`
#
#      línea  30   import v2_suite as v2
#      línea 108   Thread(target=an.scheduler_loop, ...).start()
#      línea 1356  v2.register(app, ...)
#
#  el hilo del planificador arranca MIL LÍNEAS antes de que se registren
#  las rutas. Con el guard instalado en `register()` existía una ventana
#  —todo el tiempo que tarda el resto del módulo en cargarse— en la que
#  `run_due_schedules()` podía despertar un grupo legacy de DISPATCH sin
#  pasar por el modo de operación. Una ventana pequeña sigue siendo una
#  ventana cuando lo que se cuela al otro lado es llamar dos veces al
#  mismo cliente.
#
#  Instalándolo aquí, el guard existe en la línea 30: antes de que se
#  pueda crear ningún hilo.
#
#  `server.py` no cambia. El orden correcto sale de dónde está el import,
#  que ya estaba bien colocado.
# ══════════════════════════════════════════════════════════════════════
lm.install_activation_guard(an)
