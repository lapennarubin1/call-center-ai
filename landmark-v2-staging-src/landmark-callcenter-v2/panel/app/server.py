"""
Landmark Markets — Control Panel
=================================
Servidor Flask. Lee el CDR de Asterisk (MySQL) y las tablas de leads
sincronizadas desde Google Sheets por WF14.

Arranque:
    python3 server.py
Variables de entorno (todas opcionales, ver DEFAULTS):
    LM_DB_HOST LM_DB_USER LM_DB_PASS LM_DB_NAME LM_PORT
    LM_RATE            tarifa USD/min (default 0.06)
    LM_USER LM_PASS    credenciales del panel, rol viewer — solo dashboard (default admin / cambiar)
    LM_MASTER_USER LM_MASTER_PASS  credenciales de rol master — dashboard + SIP Balance/Extensions/Call Center
    LM_SQLITE          ruta a sqlite (modo demo/test, ignora MySQL)
"""
import os, io, csv, functools, secrets, sys, re, threading, json
from datetime import datetime, timedelta, timezone
from flask import (Flask, render_template, request, jsonify,
                   Response, session, redirect, url_for)

# Gunicorn no siempre añade el directorio de trabajo al path de importación,
# según cómo se invoque. Lo añadimos explícitamente para que `server:app`
# encuentre analytics.py y charts.py sin depender de eso.
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import analytics as an
import charts
import routes_config as rc
import analytics_v2 as av2
import v2_suite as v2            # Call Center V2: Analytics · Reconciliation · Settings
from analytics import DB, build_report, resolve_range, CACHE

# ── Configuración ──────────────────────────────────────────────────
#
# NINGUNA contraseña tiene valor por defecto en el código.
#
# Esta tabla traía cuatro contraseñas reales escritas aquí como
# fallback de os.getenv(). Eso significaba que:
#   · viajaban en cada copia del código, backup y export
#   · el panel arrancaba "bien" con un .env incompleto, usando
#     credenciales que el operador creía haber cambiado
#   · una variable mal escrita en el .env no daba error: daba acceso
#     con la contraseña del código
#
# Ahora una credencial que falta detiene el arranque con un mensaje que
# dice cuál falta. Un nombre de usuario sí puede tener valor por
# defecto: saber que el usuario se llama "master" no abre nada.
def _required_secret(var, para):
    """Lee una credencial obligatoria. Sin valor por defecto, nunca.

    Si falta, el proceso no arranca. Es deliberado: un panel que arranca
    con credenciales desconocidas es peor que un panel que no arranca.
    El mensaje NO imprime el valor de ninguna variable.
    """
    v = os.getenv(var, '')
    if not v:
        raise SystemExit(
            f"\n[landmark-panel] FALTA {var} — {para}.\n"
            f"  Definila en el .env del panel y reiniciá el servicio.\n"
            f"  Plantilla completa: panel/.env.example\n"
            f"  Ninguna contraseña tiene valor por defecto en el código.\n")
    return v


CFG = {
    'DB_HOST' : os.getenv('LM_DB_HOST', 'localhost'),
    'DB_PORT' : int(os.getenv('LM_DB_PORT', '3306')),
    'DB_USER' : os.getenv('LM_DB_USER', 'asterisk_ro'),
    'DB_PASS' : _required_secret('LM_DB_PASS', 'contraseña del usuario de MySQL'),
    'DB_NAME' : os.getenv('LM_DB_NAME', 'asterisk'),
    'PORT'    : int(os.getenv('LM_PORT', '8080')),
    'RATE'    : float(os.getenv('LM_RATE', '0.06')),
    'USER'    : os.getenv('LM_USER', 'admin'),
    'PASS'    : _required_secret('LM_PASS', 'contraseña del usuario viewer'),
    'MASTER_USER': os.getenv('LM_MASTER_USER', 'master'),
    'MASTER_PASS': _required_secret('LM_MASTER_PASS', 'contraseña del rol master'),
    'SUPPORT_USER': os.getenv('LM_SUPPORT_USER', 'support'),
    'SUPPORT_PASS': _required_secret('LM_SUPPORT_PASS', 'contraseña del rol support'),
    'SQLITE'  : os.getenv('LM_SQLITE', ''),
    'N8N_URL' : os.getenv('LM_N8N_BASE_URL', 'https://landmarket-n8n.dhsoig.easypanel.host'),
}

app = Flask(__name__)

# La clave de sesión DEBE ser la misma en todos los procesos de gunicorn.
# Si cada worker generase la suya, el usuario quedaría deslogueado al azar
# según qué worker atienda cada request. Si no viene por entorno, la
# persistimos en disco para que sobreviva reinicios y sea común a todos.
_secret = os.getenv('LM_SECRET')
if not _secret:
    _keyfile = os.path.join(os.path.dirname(os.path.abspath(__file__)), '.session_key')
    try:
        if os.path.exists(_keyfile):
            _secret = open(_keyfile).read().strip()
        else:
            _secret = secrets.token_hex(32)
            with open(_keyfile, 'w') as fh:
                fh.write(_secret)
            os.chmod(_keyfile, 0o600)
    except OSError:
        _secret = secrets.token_hex(32)      # último recurso: sólo 1 worker
app.secret_key = _secret
app.permanent_session_lifetime = timedelta(hours=12)


@app.context_processor
def inject_role():
    """Disponible en TODAS las plantillas sin tener que pasarlo a mano
    en cada render_template — así base.html puede ocultar los links de
    SIP Balance / Extensions / Call Center para el usuario viewer sin
    que cada vista tenga que acordarse de pasar la variable."""
    role = session.get('role')
    return {'is_master': role == 'master', 'is_support': role in ('master', 'support'),
            'sources': an.SOURCES}


# ── Conexión ───────────────────────────────────────────────────────
def get_db():
    """Devuelve un DB() nuevo por request (evita conexiones zombie)."""
    if CFG['SQLITE']:
        import sqlite3
        conn = sqlite3.connect(CFG['SQLITE'])
        return DB(conn, 'sqlite')
    import pymysql
    conn = pymysql.connect(
        host=CFG['DB_HOST'], user=CFG['DB_USER'], password=CFG['DB_PASS'],
        database=CFG['DB_NAME'], charset='utf8mb4', autocommit=True,
        connect_timeout=10, read_timeout=60)
    return DB(conn, 'mysql')


# ── Scheduler de Call Center en background ──────────────────────────
# Un hilo por proceso de gunicorn (--workers 3 -> 3 hilos). No hace
# falta coordinarlos entre sí explícitamente: analytics.run_due_schedules()
# usa un UPDATE atómico en la DB para que, aunque los 3 evalúen "son
# las 10:30 en Dubai" al mismo tiempo, la acción se dispare una sola
# vez. LM_SCHEDULER_DISABLED=1 lo apaga (usado por los tests, para no
# levantar un hilo real pegándole a n8n/DB de verdad en cada import).
if os.getenv('LM_SCHEDULER_DISABLED') != '1':
    threading.Thread(target=an.scheduler_loop, args=(get_db,), daemon=True).start()

# Telegram — long-polling (mismo patrón que el scheduler, no necesita
# webhook HTTPS público). Si no hay token guardado en Settings todavía,
# el loop simplemente no hace nada en cada vuelta (ver telegram_poll_loop).
if os.getenv('LM_TELEGRAM_DISABLED') != '1':
    threading.Thread(target=an.telegram_poll_loop, args=(get_db,), daemon=True).start()


def with_db(fn):
    @functools.wraps(fn)
    def wrap(*a, **kw):
        db = None
        try:
            db = get_db()
            return fn(db, *a, **kw)
        except Exception as ex:
            app.logger.exception('error en %s', fn.__name__)
            if request.path.startswith('/api/'):
                return jsonify({'error': str(ex)}), 500
            return render_template('error.html', error=str(ex)), 500
        finally:
            if db:
                try: db.conn.close()
                except Exception: pass
    return wrap


def auth(fn):
    @functools.wraps(fn)
    def wrap(*a, **kw):
        if not session.get('ok'):
            if request.path.startswith('/api/'):
                return jsonify({'error': 'not authenticated'}), 401
            return redirect(url_for('login', next=request.path))
        return fn(*a, **kw)
    return wrap


def auth_master(fn):
    """Como @auth, pero además exige rol master — para las pestañas que
    el usuario viewer nunca debe poder abrir (SIP Balance, Extensions,
    Call Center), ni siquiera tecleando la URL a mano."""
    @functools.wraps(fn)
    def wrap(*a, **kw):
        if not session.get('ok'):
            if request.path.startswith('/api/'):
                return jsonify({'error': 'not authenticated'}), 401
            return redirect(url_for('login', next=request.path))
        if session.get('role') != 'master':
            if request.path.startswith('/api/'):
                return jsonify({'error': 'forbidden'}), 403
            return redirect(url_for('dashboard'))
        return fn(*a, **kw)
    return wrap


def auth_support(fn):
    """Como @auth, pero exige rol master O support — para /support,
    accesible a ambos (soporte solo ve sus dos formularios; master
    ve además el bloque de configuración de Telegram)."""
    @functools.wraps(fn)
    def wrap(*a, **kw):
        if not session.get('ok'):
            if request.path.startswith('/api/'):
                return jsonify({'error': 'not authenticated'}), 401
            return redirect(url_for('login', next=request.path))
        if session.get('role') not in ('master', 'support'):
            if request.path.startswith('/api/'):
                return jsonify({'error': 'forbidden'}), 403
            return redirect(url_for('dashboard'))
        return fn(*a, **kw)
    return wrap


def auth_service_token(fn):
    """API que consume n8n. Tres capas, todas fail-closed:
      1. LM_ROUTES_API_TOKEN sin configurar → 503 CONFIG_ERROR (nunca abierta)
      2. transporte: HTTP plano desde IP pública → 403 (el token no viaja así)
      3. token incorrecto → 401 AUTH_ERROR (comparación en tiempo constante)
    """
    @functools.wraps(fn)
    def wrap(*a, **kw):
        expected = rc.service_token()
        if not expected:
            return jsonify({'error': 'service token not configured', 'code': 'CONFIG_ERROR',
                            'detail': 'set LM_ROUTES_API_TOKEN in the panel .env'}), 503
        if not rc.transport_is_safe(request.remote_addr, request.is_secure,
                                    request.headers.get('X-Forwarded-Proto')):
            return jsonify({'error': 'insecure transport', 'code': 'CONFIG_ERROR',
                            'detail': 'use the internal Docker/VPS network or HTTPS; '
                                      'rotate LM_ROUTES_API_TOKEN if this was not a test'}), 403
        got = request.headers.get('X-Service-Token', '')
        if not got or not secrets.compare_digest(got, expected):
            return jsonify({'error': 'invalid service token', 'code': 'AUTH_ERROR'}), 401
        return fn(*a, **kw)
    return wrap


def params():
    provider = request.args.get('provider', 'asterisk')
    # La pestaña Sheet es solo para master. normalize_source() lo
    # resuelve en un solo lugar para TODAS las vistas (dashboard, API y
    # exports), así que un viewer que teclee ?source=sheet a mano recibe
    # datos de CRM igual, sin una redirección que delate que existe otra
    # pestaña.
    is_master = session.get('role') == 'master'
    return {
        'period'  : request.args.get('period', 'today'),
        'start'   : request.args.get('start'),
        'end'     : request.args.get('end'),
        'rate'    : float(request.args.get('rate', CFG['RATE'])),
        'country' : an.normalize_dashboard_country(request.args.get('country', an.DEFAULT_COUNTRY)),
        'provider': provider if provider == 'stringee' else 'asterisk',
        'source'  : an.normalize_source(request.args.get('source', an.DEFAULT_SOURCE), is_master),
    }


# ── Autenticación ──────────────────────────────────────────────────
@app.route('/login', methods=['GET', 'POST'])
def login():
    err = None
    if request.method == 'POST':
        submitted_user = request.form.get('user', '')
        submitted_pass = request.form.get('pass', '')
        if (submitted_user == CFG['MASTER_USER'] and
                secrets.compare_digest(submitted_pass, CFG['MASTER_PASS'])):
            session['ok'] = True
            session['role'] = 'master'
            session.permanent = True
            return redirect(request.args.get('next') or url_for('dashboard'))
        if (submitted_user == CFG['USER'] and
                secrets.compare_digest(submitted_pass, CFG['PASS'])):
            session['ok'] = True
            session['role'] = 'viewer'
            session.permanent = True
            return redirect(request.args.get('next') or url_for('dashboard'))
        if (submitted_user == CFG['SUPPORT_USER'] and
                secrets.compare_digest(submitted_pass, CFG['SUPPORT_PASS'])):
            session['ok'] = True
            session['role'] = 'support'
            session.permanent = True
            return redirect(request.args.get('next') or url_for('support_page'))
        err = 'Incorrect username or password'
    return render_template('login.html', error=err)


@app.route('/logout')
def logout():
    session.clear()
    return redirect(url_for('login'))


# ── Vistas ─────────────────────────────────────────────────────────
@app.route('/')
@auth
@with_db
def dashboard(db):
    p = params()
    rep = _report(db, p)
    t = rep['traffic']

    donut = charts.donut([
        {'label': 'Answered',      'value': t['answered'],      'tone': 'ok'},
        {'label': 'No answer', 'value': t['no_answer'],     'tone': 'idle'},
        {'label': 'Busy',       'value': t['busy'],          'tone': 'warn'},
        {'label': 'Technical failure', 'value': t['tech_failures'], 'tone': 'bad'},
    ])
    pipebars = (charts.bars_h(rep['pipeline']['rows'], key='n', label='status')
                if rep['pipeline'].get('available') else [])

    ladder_steps = charts.ladder(t, rep['durations'], rep['funnel'])

    # Stringee (segundo proveedor) — chart opcional, solo si hubo llamadas
    # de ese proveedor en el período. build_stringee_report() ya
    # devuelve None si no hay datos, así que este chart también.
    stringee_hourly_chart = (charts.hourly(rep['stringee']['hourly'])
                              if rep.get('stringee') else None)

    return render_template(
        'dashboard.html', r=rep, cfg=CFG, p=p, countries=an.COUNTRIES,
        trace=charts.trace(rep['series']),
        hourly=charts.hourly(rep['hourly']),
        ladder=ladder_steps,
        funnel_trend=charts.funnel_trend(rep['funnel_series'], grain=('30min' if rep['grain'] == 'hour' else rep['grain'])),
        donut=donut,
        durbars=charts.bars_h(rep['durations']['buckets']),
        pipebars=pipebars,
        stringee_hourly=stringee_hourly_chart,
    )


def _report(db, p):
    """Arma el informe para los parámetros dados, ya resuelto el
    proveedor y aplicada la vista de Stringee si corresponde.

    Punto ÚNICO de construcción: dashboard, /api/report y el CSV de
    summary pasan por acá. Cuando esta lógica estaba escrita inline en
    el dashboard, /api/report devolvía el tráfico de Asterisk aunque se
    pidiera ?provider=stringee — el JSON y la pantalla decían cosas
    distintas para la misma URL.

    OJO con el orden: el provider se corrige ANTES de build_report()
    para que el provider que entra en la clave de caché sea el mismo con
    el que se calculó. Corregirlo después dejaría la entrada de
    Asterisk-México guardada bajo una clave que dice 'stringee'.
    """
    if p['provider'] == 'stringee' and p['country'] != 'india':
        p['provider'] = 'asterisk'

    rep = build_report(db, p['period'], p['start'], p['end'], p['rate'],
                       country=p['country'], source=p['source'], provider=p['provider'])
    # build_report() cachea por (period,start,end,rate,country,source,provider).
    # rep puede ser el MISMO objeto en memoria que el caché, compartido entre
    # requests durante su TTL. Copia superficial obligatoria antes de
    # sobreescribir cualquier key, para no corromper el caché de otros
    # requests.
    rep = dict(rep)

    if p['provider'] == 'stringee':
        s, e, *_ = resolve_range(p['period'], None, p['start'], p['end'], country=p['country'])
        st = an.stringee_traffic(db, s, e, p['country'])
        if st is not None:
            # Tráfico: sale de stringee_calls en AMBAS pestañas. El dato
            # físico de la llamada es el mismo; no depende de dónde se
            # lean los leads.
            rep['traffic']   = st
            rep['series']    = an.stringee_timeseries(db, s, e, rep['grain'], p['country'])
            rep['hourly']    = an.stringee_hourly(db, s, e, p['country'])
            rep['durations'] = an.stringee_durations(db, s, e, p['country'])
            # Funnel: SOLO se reemplaza en la pestaña Sheet. En la pestaña
            # CRM, build_report() ya lo calculó acotado por proveedor con un
            # WHERE directo sobre crm_leads.provider — pisarlo acá con la
            # versión Sheet (que cruza lead_id contra panel_leads) mezclaría
            # las dos fuentes en una misma vista.
            if p['source'] == 'sheet':
                rep['funnel'] = an.stringee_funnel(db, s, e, p['country'])
                rep['funnel_series'] = an.stringee_funnel_timeseries(
                    db, s, e, '30min' if rep['grain'] == 'hour' else rep['grain'], p['country'])
        else:
            p['provider'] = 'asterisk'   # tabla no existe todavía — cae a Asterisk sin romper
    return rep


@app.route('/health')
@with_db
def health(db):
    r = db.one("SELECT COUNT(*) AS n FROM cdr_panel")
    return jsonify({'status': 'ok', 'cdr_rows': r.get('n', 0),
                    'driver': db.driver, 'ts': datetime.now().isoformat()})


# ── SIP Balance ───────────────────────────────────────────────────
@app.route('/sip')
@auth_master
@with_db
def sip_balance(db):
    """
    Cuenta corriente con el/los proveedor(es) SIP: depósitos vs
    consumo real. Independiente del selector de país del dashboard —
    un proveedor puede cubrir varios países a la vez (hoy el único
    proveedor cubre India y México con el mismo trunk lógico).
    """
    providers = an.sip_providers_list(db)
    selected_id = request.args.get('provider', type=int)
    if not selected_id and len(providers) == 1:
        selected_id = providers[0]['id']  # único proveedor -> autoseleccionar

    balance = an.sip_provider_balance(db, selected_id) if selected_id else None
    return render_template('sip.html', cfg=CFG, providers=providers,
                           selected_id=selected_id, balance=balance,
                           valid_countries=an.COUNTRIES_ALL,
                           today=an.now_local(an.DEFAULT_COUNTRY).strftime('%Y-%m-%d'),
                           error=request.args.get('error'))


@app.route('/sip/provider/new', methods=['POST'])
@auth_master
@with_db
def sip_provider_new(db):
    """
    Crea un proveedor nuevo desde el formulario del panel. El form manda
    listas paralelas country[]/trunk_name[]/price[] (una fila por país
    agregada con el botón "+ país" en el HTML) — se combinan en pricing
    acá antes de pasarlas a sip_provider_create(), que valida cada país
    contra COUNTRIES_CFG y no crea nada si alguna fila es inválida.
    """
    name = request.form.get('name', '')
    countries = request.form.getlist('country[]')
    trunk_names = request.form.getlist('trunk_name[]')
    prices = request.form.getlist('price[]')
    pricing = [{'country': c, 'trunk_name': t, 'price': pr}
               for c, t, pr in zip(countries, trunk_names, prices) if c or t or pr]
    try:
        provider_id = an.sip_provider_create(db, name, pricing)
    except ValueError as ex:
        return redirect(url_for('sip_balance', error=str(ex)))
    return redirect(url_for('sip_balance', provider=provider_id))


@app.route('/sip/provider/<int:provider_id>/pricing', methods=['POST'])
@auth_master
@with_db
def sip_provider_pricing_new(db, provider_id):
    """Agrega/actualiza el precio de un país para un proveedor existente."""
    try:
        an.sip_provider_pricing_upsert(db, provider_id, request.form.get('country'),
                                       request.form.get('trunk_name'), request.form.get('price'))
    except ValueError as ex:
        return redirect(url_for('sip_balance', provider=provider_id, error=str(ex)))
    return redirect(url_for('sip_balance', provider=provider_id))


@app.route('/sip/provider/<int:provider_id>/billing-start', methods=['POST'])
@auth_master
@with_db
def sip_provider_billing_start(db, provider_id):
    """
    Fecha de corte del proveedor: todo antes de esa fecha (saldo y
    consumo previos a usar este proveedor/trunk) queda fuera de la
    suma. Mandar el campo vacío la borra y vuelve a contar desde
    siempre.
    """
    try:
        an.sip_provider_set_billing_start(db, provider_id, request.form.get('billing_start_date'))
    except ValueError as ex:
        return redirect(url_for('sip_balance', provider=provider_id, error=str(ex)))
    return redirect(url_for('sip_balance', provider=provider_id))


@app.route('/sip/deposit/new', methods=['POST'])
@auth_master
@with_db
def sip_deposit_new(db):
    provider_id = request.form.get('provider_id', type=int)
    try:
        an.sip_deposit_add(db, provider_id, request.form.get('amount'),
                           request.form.get('reference'), request.form.get('date'))
    except ValueError as ex:
        return redirect(url_for('sip_balance', provider=provider_id, error=str(ex)))
    return redirect(url_for('sip_balance', provider=provider_id))


@app.route('/sip/deposit/<int:deposit_id>/delete', methods=['POST'])
@auth_master
@with_db
def sip_deposit_delete_route(db, deposit_id):
    provider_id = request.form.get('provider_id', type=int)
    an.sip_deposit_delete(db, deposit_id)
    return redirect(url_for('sip_balance', provider=provider_id))


# ── SIP Extensions — llamadas manuales desde softphone ─────────────
@app.route('/sip/extensions')
@auth_master
@with_db
def sip_extensions_page(db):
    providers = an.sip_providers_list(db)
    extensions = an.sip_extensions_list(db)
    for ext in extensions:
        ext['consumption'] = an.sip_extension_consumption(db, ext)
    return render_template('extensions.html', cfg=CFG, providers=providers,
                           extensions=extensions, ast_host=an.AST_HOST, ast_port=an.AST_PORT,
                           error=request.args.get('error'), applied=request.args.get('applied'))


@app.route('/sip/extensions/new', methods=['POST'])
@auth_master
@with_db
def sip_extension_new(db):
    try:
        an.sip_extension_create(db, request.form.get('provider_id', type=int),
                                request.form.get('extension_number'),
                                request.form.get('sip_username'),
                                request.form.get('sip_password'),
                                request.form.get('label'))
        an.sip_extensions_apply(db)
    except ValueError as ex:
        return redirect(url_for('sip_extensions_page', error=str(ex)))
    return redirect(url_for('sip_extensions_page', applied=1))


@app.route('/sip/extensions/<int:extension_id>/toggle', methods=['POST'])
@auth_master
@with_db
def sip_extension_toggle_route(db, extension_id):
    active = request.form.get('active') == '1'
    try:
        an.sip_extension_toggle(db, extension_id, active)
        an.sip_extensions_apply(db)
    except ValueError as ex:
        return redirect(url_for('sip_extensions_page', error=str(ex)))
    return redirect(url_for('sip_extensions_page', applied=1))


@app.route('/sip/extensions/<int:extension_id>/delete', methods=['POST'])
@auth_master
@with_db
def sip_extension_delete_route(db, extension_id):
    try:
        an.sip_extension_delete(db, extension_id)
        an.sip_extensions_apply(db)
    except ValueError as ex:
        return redirect(url_for('sip_extensions_page', error=str(ex)))
    return redirect(url_for('sip_extensions_page', applied=1))


@app.route('/sip/extensions/reapply', methods=['POST'])
@auth_master
@with_db
def sip_extensions_reapply(db):
    """Reescribe y recarga la config sin cambiar nada en la DB — útil
    si alguien tocó algo a mano en Asterisk y hay que forzar que vuelva
    a coincidir con lo que dice el panel."""
    try:
        an.sip_extensions_apply(db)
    except ValueError as ex:
        return redirect(url_for('sip_extensions_page', error=str(ex)))
    return redirect(url_for('sip_extensions_page', applied=1))


# ── Call Center — encender/apagar grupos de workflows n8n ──────────
@app.route('/callcenter')
@auth_master
@with_db
def callcenter_page(db):
    switches, status_error = an.n8n_switches_with_status(db)
    schedules_by_switch = {s['switch_id']: s for s in an.schedules_all(db)}
    for sw in switches:
        sw['schedule'] = schedules_by_switch.get(sw['id'])
    try:
        available_workflows = an.n8n_fetch_workflows()
        fetch_error = None
    except ValueError as ex:
        available_workflows, fetch_error = [], str(ex)
    return render_template('callcenter.html', cfg=CFG, switches=switches,
                           status_error=status_error, available_workflows=available_workflows,
                           fetch_error=fetch_error, error=request.args.get('error'),
                           applied=request.args.get('applied'),
                           common_timezones=an.COMMON_TIMEZONES,
                           weekday_codes=an._WEEKDAY_CODES)


def _collect_workflow_ids(form):
    """Checkboxes (picker con nombres, cuando n8n responde) + campo de
    texto manual (fallback si n8n no está disponible o el ID no aparece
    en el picker) — se combinan, no son excluyentes."""
    ids = list(form.getlist('workflow_ids'))
    manual = form.get('workflow_ids_manual', '')
    ids += [x.strip() for x in re.split(r'[,\s]+', manual) if x.strip()]
    return ids


@app.route('/callcenter/new', methods=['POST'])
@auth_master
@with_db
def callcenter_new(db):
    try:
        an.n8n_switch_create(db, request.form.get('label'), _collect_workflow_ids(request.form))
    except ValueError as ex:
        return redirect(url_for('callcenter_page', error=str(ex)))
    return redirect(url_for('callcenter_page'))


@app.route('/callcenter/<int:switch_id>/edit', methods=['POST'])
@auth_master
@with_db
def callcenter_edit(db, switch_id):
    try:
        an.n8n_switch_update(db, switch_id, request.form.get('label'),
                             _collect_workflow_ids(request.form))
    except ValueError as ex:
        return redirect(url_for('callcenter_page', error=str(ex)))
    return redirect(url_for('callcenter_page'))


@app.route('/callcenter/<int:switch_id>/delete', methods=['POST'])
@auth_master
@with_db
def callcenter_delete(db, switch_id):
    an.n8n_switch_delete(db, switch_id)
    return redirect(url_for('callcenter_page'))


@app.route('/callcenter/<int:switch_id>/on', methods=['POST'])
@auth_master
@with_db
def callcenter_on(db, switch_id):
    try:
        an.n8n_switch_set_state(db, switch_id, turn_on=True)
    except ValueError as ex:
        return redirect(url_for('callcenter_page', error=str(ex)))
    return redirect(url_for('callcenter_page', applied=1))


@app.route('/callcenter/<int:switch_id>/off', methods=['POST'])
@auth_master
@with_db
def callcenter_off(db, switch_id):
    try:
        an.n8n_switch_set_state(db, switch_id, turn_on=False)
    except ValueError as ex:
        return redirect(url_for('callcenter_page', error=str(ex)))
    return redirect(url_for('callcenter_page', applied=1))


@app.route('/callcenter/<int:switch_id>/schedule', methods=['POST'])
@auth_master
@with_db
def callcenter_schedule_save(db, switch_id):
    try:
        an.schedule_upsert(db, switch_id, request.form.get('timezone'),
                           request.form.get('on_time'), request.form.get('off_time'),
                           request.form.getlist('days'),
                           enabled=(request.form.get('enabled') == '1'))
    except ValueError as ex:
        return redirect(url_for('callcenter_page', error=str(ex)))
    return redirect(url_for('callcenter_page', applied=1))


@app.route('/callcenter/<int:switch_id>/schedule/toggle', methods=['POST'])
@auth_master
@with_db
def callcenter_schedule_toggle(db, switch_id):
    an.schedule_set_enabled(db, switch_id, request.form.get('enabled') == '1')
    return redirect(url_for('callcenter_page', applied=1))


@app.route('/callcenter/<int:switch_id>/schedule/delete', methods=['POST'])
@auth_master
@with_db
def callcenter_schedule_delete(db, switch_id):
    an.schedule_delete(db, switch_id)
    return redirect(url_for('callcenter_page'))



# ══════════════════════════════════════════════════════════════════════
#  CALL CENTER › Countries / Routes  (Template V2.1)
#  Solo rol master. Toda la lógica vive en routes_config.py; acá solo hay
#  transporte HTTP. Cada mutación queda en route_audit.
# ══════════════════════════════════════════════════════════════════════

def _actor():
    return session.get('role') or 'unknown'


def _back(default_iso=None, **kw):
    """Vuelve a la página del país si el form lo pidió, si no al listado."""
    iso = (request.form.get('back_iso') or default_iso or '').strip().upper()
    if iso:
        return redirect(url_for('country_page', iso=iso, **kw))
    return redirect(url_for('routes_page', **kw))


def _err(ex):
    """ConfigError trae la lista de faltantes: se muestra como checklist."""
    if isinstance(ex, rc.ConfigError) and ex.issues:
        return str(ex) + ' — Missing: ' + ' · '.join(i['message'] for i in ex.issues)
    return str(ex)


def _mutation(fn, default_iso=None):
    try:
        fn()
    except (ValueError, KeyError) as ex:
        return _back(default_iso, error=_err(ex))
    return _back(default_iso, applied=1)


@app.route('/callcenter/routes')
@auth_master
@with_db
def routes_page(db):
    status = (request.args.get('status') or 'all').lower()
    routes = rc.routes_list(db, status)
    now = datetime.now(timezone.utc)
    cache = {}
    for r in routes:
        api = rc.route_to_api(db, r, now, cache)
        for k in ('calling_now', 'capacity_now', 'capacity_source', 'ready', 'country_ready',
                  'config_issues', 'blocked_by'):
            r[k] = api[k]
    countries = rc.countries_list(db)
    for c in countries:
        c['report'] = rc.validate_country(db, c['iso'])
    return render_template(
        'routes.html', routes=routes, status=status, statuses=rc.ROUTE_STATUSES,
        countries=countries, providers=rc.providers_list(db),
        policies=rc.policies_list(db), adapters=rc.ADAPTERS,
        common_timezones=rc.COMMON_TIMEZONES, audit_rows=rc.audit_recent(db, 30),
        token_configured=bool(rc.service_token()),
        error=request.args.get('error'), applied=request.args.get('applied'))


@app.route('/callcenter/countries/<iso>')
@auth_master
@with_db
def country_page(db, iso):
    country = rc.country_get(db, iso)
    if not country:
        return redirect(url_for('routes_page', error=f'país inexistente: {iso}'))
    now = datetime.now(timezone.utc)
    routes = [r for r in rc.routes_list(db, 'all') if r['iso'] == country['iso']]
    cache = {}
    for r in routes:
        api = rc.route_to_api(db, r, now, cache)
        r['calling_now'], r['capacity_now'] = api['calling_now'], api['capacity_now']
        r['capacity_source'], r['blocked_by'] = api['capacity_source'], api['blocked_by']
        rep = rc.validate_route(db, r)
        r['ready'], r['config_issues'] = rep['ready'], rep['issues']
    country_report = rc.validate_country(db, country['iso'],
                                         routes=[r for r in routes if r['status'] != 'ARCHIVED'])
    tools = {t['tool_type']: t for t in rc.tools_for_country(db, country['iso'])}
    tool_issues = {tt: rc.validate_tool(t) for tt, t in tools.items()}
    return render_template(
        'country.html', country=country, country_report=country_report, routes=routes,
        tools=tools, tool_issues=tool_issues, adapters=rc.ADAPTERS,
        tool_types=rc.TOOL_TYPES, tool_modes=rc.TOOL_MODES, http_methods=rc.HTTP_METHODS,
        providers=rc.providers_list(db), policies=rc.policies_list(db, include_archived=False),
        telegram_purposes=rc.TELEGRAM_PURPOSES, weekday_codes=rc.WEEKDAY_CODES,
        common_timezones=rc.COMMON_TIMEZONES,
        error=request.args.get('error'), applied=request.args.get('applied'))


# ── países ────────────────────────────────────────────────────────────
@app.route('/callcenter/countries/save', methods=['POST'])
@auth_master
@with_db
def country_save(db):
    iso = (request.form.get('iso') or '').upper()
    return _mutation(lambda: rc.country_upsert(db, _actor(), request.form), iso)


@app.route('/callcenter/countries/<iso>/archive', methods=['POST'])
@auth_master
@with_db
def country_archive(db, iso):
    return _mutation(lambda: rc.country_set_archived(db, _actor(), iso, True), iso)


@app.route('/callcenter/countries/<iso>/unarchive', methods=['POST'])
@auth_master
@with_db
def country_unarchive(db, iso):
    return _mutation(lambda: rc.country_set_archived(db, _actor(), iso, False), iso)


@app.route('/callcenter/countries/<iso>/enable', methods=['POST'])
@auth_master
@with_db
def country_enable(db, iso):
    return _mutation(lambda: rc.country_set_enabled(db, _actor(), iso, True), iso)


@app.route('/callcenter/countries/<iso>/disable', methods=['POST'])
@auth_master
@with_db
def country_disable(db, iso):
    return _mutation(lambda: rc.country_set_enabled(db, _actor(), iso, False), iso)


@app.route('/callcenter/providers/<int:provider_id>/enable', methods=['POST'])
@auth_master
@with_db
def provider_enable(db, provider_id):
    return _mutation(lambda: rc.provider_set_enabled(db, _actor(), provider_id, True))


@app.route('/callcenter/providers/<int:provider_id>/disable', methods=['POST'])
@auth_master
@with_db
def provider_disable(db, provider_id):
    return _mutation(lambda: rc.provider_set_enabled(db, _actor(), provider_id, False))


@app.route('/callcenter/countries/<iso>/tools/<tool_type>', methods=['POST'])
@auth_master
@with_db
def country_tool_save(db, iso, tool_type):
    return _mutation(lambda: rc.tool_upsert(db, _actor(), iso, tool_type.upper(), request.form), iso)


# ── rutas ─────────────────────────────────────────────────────────────
@app.route('/callcenter/routes/new', methods=['POST'])
@auth_master
@with_db
def routes_new(db):
    iso = (request.form.get('iso') or '').upper()
    return _mutation(lambda: rc.route_create(db, _actor(), request.form), iso)


@app.route('/callcenter/routes/<int:route_id>/edit', methods=['POST'])
@auth_master
@with_db
def routes_edit(db, route_id):
    return _mutation(lambda: rc.route_update(db, _actor(), route_id, request.form))


@app.route('/callcenter/routes/<int:route_id>/enable', methods=['POST'])
@auth_master
@with_db
def routes_enable(db, route_id):
    return _mutation(lambda: rc.route_set_enabled(db, _actor(), route_id, True))


@app.route('/callcenter/routes/<int:route_id>/disable', methods=['POST'])
@auth_master
@with_db
def routes_disable(db, route_id):
    return _mutation(lambda: rc.route_set_enabled(db, _actor(), route_id, False))


@app.route('/callcenter/routes/<int:route_id>/archive', methods=['POST'])
@auth_master
@with_db
def routes_archive(db, route_id):
    return _mutation(lambda: rc.route_archive(db, _actor(), route_id))


@app.route('/callcenter/routes/<int:route_id>/unarchive', methods=['POST'])
@auth_master
@with_db
def routes_unarchive(db, route_id):
    return _mutation(lambda: rc.route_unarchive(db, _actor(), route_id))


@app.route('/callcenter/routes/<int:route_id>/window/new', methods=['POST'])
@auth_master
@with_db
def routes_window_new(db, route_id):
    f = request.form
    return _mutation(lambda: rc.window_add(db, _actor(), route_id, f.getlist('days'),
                                           f.get('start_local'), f.get('end_local'),
                                           f.get('capacity')))


@app.route('/callcenter/routes/window/<int:window_id>/edit', methods=['POST'])
@auth_master
@with_db
def routes_window_edit(db, window_id):
    f = request.form
    return _mutation(lambda: rc.window_update(db, _actor(), window_id, f.getlist('days'),
                                              f.get('start_local'), f.get('end_local'),
                                              f.get('capacity')))


@app.route('/callcenter/routes/window/<int:window_id>/delete', methods=['POST'])
@auth_master
@with_db
def routes_window_delete(db, window_id):
    return _mutation(lambda: rc.window_delete(db, _actor(), window_id))


@app.route('/callcenter/routes/<int:route_id>/telegram/new', methods=['POST'])
@auth_master
@with_db
def routes_telegram_new(db, route_id):
    return _mutation(lambda: rc.telegram_add(db, _actor(), route_id, request.form.get('purpose'),
                                             request.form.get('chat_id')))


@app.route('/callcenter/routes/telegram/<int:target_id>/delete', methods=['POST'])
@auth_master
@with_db
def routes_telegram_delete(db, target_id):
    return _mutation(lambda: rc.telegram_delete(db, _actor(), target_id))


# ── políticas y proveedores ───────────────────────────────────────────
@app.route('/callcenter/policies/save', methods=['POST'])
@auth_master
@with_db
def policies_save(db):
    f = request.form
    return _mutation(lambda: rc.policy_upsert(db, _actor(), f.get('policy_key'), f.get('name'),
                                              f.get('policy_json'), f.get('description')))


@app.route('/callcenter/policies/<int:policy_id>/archive', methods=['POST'])
@auth_master
@with_db
def policies_archive(db, policy_id):
    return _mutation(lambda: rc.policy_set_archived(db, _actor(), policy_id, True))


@app.route('/callcenter/policies/<int:policy_id>/unarchive', methods=['POST'])
@auth_master
@with_db
def policies_unarchive(db, policy_id):
    return _mutation(lambda: rc.policy_set_archived(db, _actor(), policy_id, False))


@app.route('/callcenter/providers/save', methods=['POST'])
@auth_master
@with_db
def providers_save(db):
    f = request.form
    return _mutation(lambda: rc.provider_upsert(
        db, _actor(), f.get('code'), f.get('display_name'), f.get('adapter_key'),
        endpoint=f.get('endpoint'), account_ref=f.get('account_ref'),
        enabled=(f.get('enabled') == '1'), notes=f.get('notes')))


@app.route('/callcenter/routes/preview.json')
@auth_master
@with_db
def routes_preview(db):
    payload = rc.routes_active_payload(
        db, include_all=request.args.get('all') in ('1', 'true'),
        include_archived=request.args.get('include_archived') in ('1', 'true'))
    return Response(json.dumps(payload, indent=2, ensure_ascii=False, default=str),
                    mimetype='application/json')


# ── Analytics local (master) ──────────────────────────────────────────
#  Todo sale de MySQL local (wf_call_jobs + wf_events). Cero llamadas a
#  LeadStudio. El dashboard visual se construye sobre estas mismas funciones.
@app.route('/callcenter/analytics.json')
@auth_master
@with_db
def analytics_json(db):
    tz = request.args.get('tz') or 'UTC'
    try:
        days = max(1, min(int(request.args.get('days') or 1), 92))
        from zoneinfo import ZoneInfo
        first_day = datetime.now(ZoneInfo(tz)).date() - timedelta(days=days - 1)
        start, end = av2.day_range_utc(first_day, tz, days)
        g = request.args.get('group_by') or None
        payload = {'overview': av2.overview(db, start, end),
                   'by': (av2.call_metrics(db, start, end, g) if g else None),
                   'accounts_by_route': av2.accounts_by(db, start, end, 'route'),
                   'timeseries_calls_hour': av2.timeseries(db, start, end, 'hour', 'calls'),
                   'tz': tz, 'days': days}
    except (ValueError, KeyError) as ex:
        return jsonify({'error': str(ex), 'code': 'VALIDATION_ERROR'}), 400
    except Exception as ex:                                    # tablas ausentes, etc.
        return jsonify({'error': str(ex), 'code': 'CONFIG_ERROR'}), 503
    return Response(json.dumps(payload, default=str), mimetype='application/json')


# ── API para n8n ──────────────────────────────────────────────────────
#  Fail-closed: si esto no responde 200, el workflow NO llama.
@app.route('/api/routes/active')
@auth_service_token
@with_db
def api_routes_active(db):
    try:
        payload = rc.routes_active_payload(
            db, include_all=request.args.get('all') in ('1', 'true'),
            include_archived=request.args.get('include_archived') in ('1', 'true'))
    except rc.ConfigError as ex:
        return jsonify({'error': str(ex), 'code': 'CONFIG_ERROR', 'routes': []}), 503
    return Response(json.dumps(payload, default=str), mimetype='application/json')


@app.route('/api/routes/by-key/<route_key>')
@auth_service_token
@with_db
def api_route_by_key(db, route_key):
    """WF9/WF10: resolver la ruta de una llamada ya hecha, AUNQUE esté archivada."""
    item = rc.route_by_key_payload(db, route_key)
    if not item:
        return jsonify({'error': f'route_key desconocida: {route_key}',
                        'code': 'VALIDATION_ERROR'}), 404
    return Response(json.dumps(item, default=str), mimetype='application/json')


@app.route('/api/countries/<iso>/tools')
@auth_service_token
@with_db
def api_country_tools(db, iso):
    """WF3/WF7 CONFIG_ROUTER: la tool de ElevenLabs manda country_code."""
    item = rc.country_tools_payload(db, iso)
    if not item:
        return jsonify({'error': f'país desconocido o archivado: {iso}',
                        'code': 'VALIDATION_ERROR'}), 404
    return Response(json.dumps(item, default=str), mimetype='application/json')

# ── Support — apertura de cuenta y payment links manuales ──────────
@app.route('/support')
@auth_support
@with_db
def support_page(db):
    settings = {}
    if session.get('role') == 'master':
        settings = {
            'telegram_bot_token': an.get_setting(db, 'telegram_bot_token', ''),
            'telegram_chat_whitelist': an.get_setting(db, 'telegram_chat_whitelist', ''),
        }
    return render_template('support.html', cfg=CFG, countries=an.COUNTRIES,
                           webhooks=an.SUPPORT_WEBHOOKS, settings=settings,
                           actions=an.support_actions_recent(db, 30),
                           error=request.args.get('error'), ok=request.args.get('ok'))


@app.route('/support/account-open', methods=['POST'])
@auth_support
@with_db
def support_account_open(db):
    try:
        an.trigger_account_creation(db, session.get('role'), request.form.get('country'),
                                    request.form.get('full_name'), request.form.get('phone'))
    except ValueError as ex:
        return redirect(url_for('support_page', error=str(ex)))
    return redirect(url_for('support_page', ok='Account request sent — SMS with credentials incoming.'))


@app.route('/support/payment-link', methods=['POST'])
@auth_support
@with_db
def support_payment_link(db):
    try:
        an.trigger_payment_link(db, session.get('role'), request.form.get('country'),
                                request.form.get('full_name'), request.form.get('phone'),
                                request.form.get('amount'))
    except ValueError as ex:
        return redirect(url_for('support_page', error=str(ex)))
    return redirect(url_for('support_page', ok='Payment link requested — SMS incoming.'))


@app.route('/support/settings', methods=['POST'])
@auth_master
@with_db
def support_settings_save(db):
    an.set_setting(db, 'telegram_bot_token', (request.form.get('telegram_bot_token') or '').strip())
    an.set_setting(db, 'telegram_chat_whitelist', (request.form.get('telegram_chat_whitelist') or '').strip())
    return redirect(url_for('support_page', ok='Telegram settings saved.'))


def _conversions(db, s, e, limit, p):
    """Cuentas abiertas de la fuente que se está viendo. Un solo punto
    de despacho para API y CSV, así no puede pasar que el export salga
    de una tabla distinta a la que muestra el dashboard."""
    if p['source'] == 'crm':
        return an.crm_conversions_list(db, s, e, limit, country=p['country'],
                                       provider=p['provider'])
    return an.conversions_list(db, s, e, limit, country=p['country'])


# ── API ────────────────────────────────────────────────────────────
@app.route('/api/report')
@auth
@with_db
def api_report(db):
    return jsonify(_report(db, params()))


@app.route('/api/conversions')
@auth
@with_db
def api_conversions(db):
    p = params()
    s, e, *_ = resolve_range(p['period'], None, p['start'], p['end'], country=p['country'])
    return jsonify(_conversions(db, s, e, 500, p))


@app.route('/api/crm/diagnose')
@auth_master
@with_db
def api_crm_diagnose(db):
    """Por qué una cuenta abierta no aparece en la pestaña CRM.

    Devuelve el estado CRUDO de crm_leads/crm_conversions — sin filtro
    de país, de proveedor ni de fecha — para poder separar en un solo
    request las tres causas posibles, que se ven idénticas desde la
    pantalla ("no aparece"):

      a) WF14-CRM no escribió la fila     -> totals en 0 / lead ausente
      b) la escribió con country vacío
         o provider NULL                  -> aparece en `latest` pero
                                             con esos campos en blanco
      c) está bien escrita pero cae fuera
         del rango de fechas de la vista  -> comparar el timestamp

    Con ?phone=<últimos dígitos> busca ese número puntual en las dos
    tablas. Solo master: es data cruda de leads.
    """
    out = {'now_utc': datetime.utcnow().isoformat(timespec='seconds')}

    out['crm_leads'] = {'exists': db.table_exists('crm_leads')}
    if out['crm_leads']['exists']:
        out['crm_leads']['total'] = db.one("SELECT COUNT(*) AS n FROM crm_leads").get('n')
        out['crm_leads']['by_country'] = db.q(
            "SELECT country, COUNT(*) AS n FROM crm_leads GROUP BY country ORDER BY n DESC")
        out['crm_leads']['interested'] = db.q("""
            SELECT lead_id, full_name, phone, country, provider, status, stage,
                   last_contacted_at, synced_at
            FROM crm_leads WHERE UPPER(COALESCE(stage, '')) = 'INTERESTED'
            ORDER BY last_contacted_at DESC LIMIT 25""")

    out['crm_conversions'] = {'exists': db.table_exists('crm_conversions')}
    if out['crm_conversions']['exists']:
        out['crm_conversions']['total'] = db.one(
            "SELECT COUNT(*) AS n FROM crm_conversions").get('n')
        out['crm_conversions']['by_country'] = db.q(
            "SELECT country, COUNT(*) AS n FROM crm_conversions GROUP BY country ORDER BY n DESC")
        out['crm_conversions']['by_provider'] = db.q(
            "SELECT provider, COUNT(*) AS n FROM crm_conversions GROUP BY provider ORDER BY n DESC")
        out['crm_conversions']['latest'] = db.q("""
            SELECT lead_id, full_name, phone, country, provider, stage, created_at, synced_at
            FROM crm_conversions ORDER BY created_at DESC LIMIT 25""")

    phone = re.sub(r'\D', '', request.args.get('phone', ''))
    if phone:
        like = '%' + phone[-9:]
        out['phone_lookup'] = {'searched': phone[-9:]}
        if out['crm_leads']['exists']:
            out['phone_lookup']['crm_leads'] = db.q("""
                SELECT lead_id, full_name, phone, country, provider, status, stage,
                       call_attempts, last_contacted_at, synced_at
                FROM crm_leads WHERE phone LIKE § LIMIT 10""", (like,))
        if out['crm_conversions']['exists']:
            out['phone_lookup']['crm_conversions'] = db.q("""
                SELECT lead_id, full_name, phone, country, provider, stage, created_at, synced_at
                FROM crm_conversions WHERE phone LIKE § LIMIT 10""", (like,))

    return jsonify(out)


@app.route('/api/cache/clear', methods=['POST'])
@auth
def api_cache_clear():
    CACHE.clear()
    return jsonify({'cleared': True})


# ── API — SIP Balance ────────────────────────────────────────────────
@app.route('/api/sip/providers')
@auth_master
@with_db
def api_sip_providers(db):
    return jsonify(an.sip_providers_list(db))


@app.route('/api/sip/provider/<int:provider_id>')
@auth_master
@with_db
def api_sip_provider(db, provider_id):
    data = an.sip_provider_balance(db, provider_id)
    if data is None:
        return jsonify({'error': 'provider not found'}), 404
    return jsonify(data)


@app.route('/api/sip/provider', methods=['POST'])
@auth_master
@with_db
def api_sip_provider_create(db):
    data = request.get_json(force=True, silent=True) or {}
    try:
        provider_id = an.sip_provider_create(db, data.get('name'), data.get('pricing') or [])
    except ValueError as ex:
        return jsonify({'error': str(ex)}), 400
    return jsonify({'provider_id': provider_id, 'status': 'ok'})


@app.route('/api/sip/provider/<int:provider_id>/pricing', methods=['POST'])
@auth_master
@with_db
def api_sip_provider_pricing(db, provider_id):
    data = request.get_json(force=True, silent=True) or {}
    try:
        an.sip_provider_pricing_upsert(db, provider_id, data.get('country'),
                                       data.get('trunk_name'), data.get('price'))
    except ValueError as ex:
        return jsonify({'error': str(ex)}), 400
    return jsonify({'status': 'ok'})


@app.route('/api/sip/deposit', methods=['POST'])
@auth_master
@with_db
def api_sip_deposit_add(db):
    data = request.get_json(force=True, silent=True) or {}
    try:
        deposit_id = an.sip_deposit_add(db, data.get('provider_id'), data.get('amount'),
                                        data.get('reference'), data.get('date'))
    except ValueError as ex:
        return jsonify({'error': str(ex)}), 400
    return jsonify({'deposit_id': deposit_id, 'status': 'ok'})


@app.route('/api/sip/deposit/<int:deposit_id>', methods=['DELETE'])
@auth_master
@with_db
def api_sip_deposit_delete(db, deposit_id):
    an.sip_deposit_delete(db, deposit_id)
    return jsonify({'status': 'ok'})


# ── Exportación ────────────────────────────────────────────────────
def _csv(rows, headers, filename):
    buf = io.StringIO()
    buf.write('\ufeff')                      # BOM para que Excel abra UTF-8 bien
    w = csv.writer(buf, delimiter=';')       # ; = separador que Excel-ES espera
    w.writerow([h[1] for h in headers])
    for r in rows:
        w.writerow([r.get(h[0], '') for h in headers])
    return Response(buf.getvalue(), mimetype='text/csv; charset=utf-8',
                    headers={'Content-Disposition': f'attachment; filename="{filename}"'})


@app.route('/export/daily.csv')
@auth
@with_db
def export_daily(db):
    p = params()
    s, e, *_ = resolve_range(p['period'], None, p['start'], p['end'], country=p['country'])
    rows = an.timeseries(db, s, e, 'day', p['rate'], country=p['country'])
    tz = an.country_tz_label(p['country'])
    return _csv(rows, [
        ('bucket', f'Date ({tz})'), ('total_calls', 'Calls'), ('answered', 'Answered'),
        ('no_answer', 'No answer'), ('failed', 'Failed'), ('asr', 'Answer rate %'),
        ('billed_minutes', 'Billed minutes'), ('acd', 'Avg duration (s)'),
        ('cost', 'Cost USD'),
    ], f'landmark_daily_{s:%Y%m%d}_{e:%Y%m%d}.csv')


@app.route('/export/conversions.csv')
@auth
@with_db
def export_conversions(db):
    p = params()
    s, e, *_ = resolve_range(p['period'], None, p['start'], p['end'], country=p['country'])
    rows = _conversions(db, s, e, 5000, p)
    tz = an.country_tz_label(p['country'])
    return _csv(rows, [
        ('lead_id', 'Lead ID'), ('full_name', 'Full name'), ('phone', 'Phone'),
        ('atlantis_user', 'Atlantis user'), ('account_id', 'Account'),
        ('created_at_local', f'Account opened ({tz})'),
    ], f"landmark_accounts_{p['source']}_{s:%Y%m%d}_{e:%Y%m%d}.csv")


@app.route('/export/summary.csv')
@auth
@with_db
def export_summary(db):
    p = params()
    rep = _report(db, p)
    t, f = rep['traffic'], rep['funnel']
    rows = [
        {'k': 'Period',                 'v': rep['label']},
        {'k': 'Total calls',        'v': t['total_calls']},
        {'k': 'Answered',             'v': t['answered']},
        {'k': 'No answer',           'v': t['no_answer']},
        {'k': 'Busy',                 'v': t['busy']},
        {'k': 'Technical failures',         'v': t['tech_failures']},
        {'k': 'Answer rate ASR (%)', 'v': t['asr']},
        {'k': 'Network effectiveness NER (%)',   'v': t['ner']},
        {'k': 'Avg call duration ACD (s)',  'v': t['acd']},
        {'k': 'Billed minutes',      'v': t['billed_minutes']},
        {'k': 'Talk time (min)', 'v': t['talk_minutes']},
        {'k': 'Total cost USD',         'v': t['cost']},
        {'k': 'Cost per answered USD','v': t['cost_per_answered']},
        {'k': 'Real conversations +60s', 'v': rep['durations']['meaningful']},
    ]
    if f.get('available'):
        rows += [
            {'k': 'Contacted leads',   'v': f['contacted']},
            {'k': 'Accounts opened',    'v': f['accounts_opened']},
            {'k': 'Conversion rate (%)', 'v': f['conversion_rate']},
            {'k': 'Cost per acquisition USD', 'v': f['cpa']},
        ]
    return _csv(rows, [('k', 'Metric'), ('v', 'Value')],
                f"landmark_summary_{p['source']}_{rep['range']['start']}.csv")


# ── Number format (Spanish convention: . thousands, , decimals) ────
def _es(v, dec):
    return f'{float(v):,.{dec}f}'.replace(',', '@').replace('.', ',').replace('@', '.')


@app.template_filter('n')
def fmt_n(v):
    """Integer with thousands separator: 49.426"""
    try:    return f'{int(v):,}'.replace(',', '.')
    except Exception: return v


@app.template_filter('m')
def fmt_m(v):
    """Currency with 2 decimal places: 508.20"""
    try:    return _es(v, 2)
    except Exception: return v


@app.template_filter('p')
def fmt_p(v):
    """Porcentaje: 11,35 — sin decimales innecesarios."""
    try:
        f = float(v)
        return _es(f, 0) if f == int(f) else _es(f, 1)
    except Exception: return v


@app.template_filter('sec')
def fmt_sec(v):
    """Seconds with one decimal: 51.9"""
    try:    return _es(v, 1)
    except Exception: return v


@app.template_filter('usd4')
def fmt_usd4(v):
    """Small amounts: 0.0906"""
    try:    return _es(v, 4)
    except Exception: return v


# ── Etiquetas legibles para los estados internos ───────────────────
STATUS_ES = {
    'READY_TO_CALL'       : 'Pending call',
    'DIALING'             : 'Dialing',
    'CALL_IN_PROGRESS'    : 'Call in progress',
    'NO_ANSWER'           : 'No answer',
    'VOICEMAIL'           : 'Voicemail',
    'FAILED'              : 'Technical failure',
    'DIAL_FAILED'         : 'Dial failed',
    'SUCCESSFUL'          : 'Successful conversation',
    'SCHEDULED'           : 'Callback scheduled',
    'INTERESTED'          : 'Account opened',
    'CONVERTED'           : 'Active client',
    'SALES_HANDOFF'       : 'Sales handoff',
    'NOT_INTERESTED'      : 'Not interested',
    'DO_NOT_CALL'         : 'Do not call',
    'MAX_ATTEMPTS_REACHED': 'Max attempts reached',
    'INVALID_LEAD'        : 'Invalid lead',
    # ── Taxonomía LeadStudio (pestaña CRM) ──
    'NOT_CONTACTED'       : 'Not contacted yet',
    'ATTEMPTING'          : 'Dialing now',
    'CONTACTED'           : 'Contacted',
    'SNOOZED'             : 'Voicemail',
    'UNREACHABLE'         : 'Unreachable',
    'CLOSED'              : 'Closed',
}


@app.template_filter('estado')
def fmt_estado(v):
    key = (v or '').strip().upper()
    return STATUS_ES.get(key, key.replace('_', ' ').title() if key else 'No status')


# ── Call Center V2 ─────────────────────────────────────────────────
#  Las pantallas y la API del suite V2 se registran acá, sobre esta misma app,
#  reusando los decoradores de autenticación de arriba. Nada del panel anterior
#  se reescribe; si este import fallara, el resto del panel sigue funcionando.
v2.register(app, auth_master, auth_service_token, with_db)


if __name__ == '__main__':
    print(f"→ Panel en http://0.0.0.0:{CFG['PORT']}  (driver: "
          f"{'sqlite' if CFG['SQLITE'] else 'mysql'})")
    app.run(host='0.0.0.0', port=CFG['PORT'], debug=False, threaded=True)
