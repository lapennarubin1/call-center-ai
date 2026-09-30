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
import os, io, csv, functools, secrets, sys, re, threading
from datetime import datetime, timedelta
from flask import (Flask, render_template, request, jsonify,
                   Response, session, redirect, url_for)

# Gunicorn no siempre añade el directorio de trabajo al path de importación,
# según cómo se invoque. Lo añadimos explícitamente para que `server:app`
# encuentre analytics.py y charts.py sin depender de eso.
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import analytics as an
import charts
from analytics import DB, build_report, resolve_range, CACHE

# ── Configuración ──────────────────────────────────────────────────
CFG = {
    'DB_HOST' : os.getenv('LM_DB_HOST', 'localhost'),
    'DB_USER' : os.getenv('LM_DB_USER', 'asterisk_ro'),
    'DB_PASS' : os.getenv('LM_DB_PASS', 'RoPass2026xK'),
    'DB_NAME' : os.getenv('LM_DB_NAME', 'asterisk'),
    'PORT'    : int(os.getenv('LM_PORT', '8080')),
    'RATE'    : float(os.getenv('LM_RATE', '0.06')),
    'USER'    : os.getenv('LM_USER', 'admin'),
    'PASS'    : os.getenv('LM_PASS', 'LandmarkPanel2026'),
    'MASTER_USER': os.getenv('LM_MASTER_USER', 'master'),
    'MASTER_PASS': os.getenv('LM_MASTER_PASS', 'LandmarkMaster2026'),
    'SUPPORT_USER': os.getenv('LM_SUPPORT_USER', 'support'),
    'SUPPORT_PASS': os.getenv('LM_SUPPORT_PASS', 'support123'),
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
    return {'is_master': role == 'master', 'is_support': role in ('master', 'support')}


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


def params():
    provider = request.args.get('provider', 'asterisk')
    return {
        'period'  : request.args.get('period', 'today'),
        'start'   : request.args.get('start'),
        'end'     : request.args.get('end'),
        'rate'    : float(request.args.get('rate', CFG['RATE'])),
        'country' : an.normalize_dashboard_country(request.args.get('country', an.DEFAULT_COUNTRY)),
        'provider': provider if provider == 'stringee' else 'asterisk',
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
    rep = build_report(db, p['period'], p['start'], p['end'], p['rate'], country=p['country'])
    # build_report() cachea por (period,start,end,rate,country) — NO por provider.
    # rep puede ser el MISMO objeto en memoria que el cache, compartido entre
    # requests durante su TTL. Copia superficial obligatoria antes de
    # sobreescribir cualquier key, para no corromper el cache para otros
    # requests (ej. alguien pidiendo Provider1 justo después de un request
    # de Stringee con los mismos period/country/rate).
    rep = dict(rep)

    # Stringee (segundo proveedor, cuota fija) — reemplaza traffic/series/
    # hourly con la vista de stringee_calls. Mismo shape de datos que
    # Asterisk (ver stringee_traffic/stringee_timeseries en analytics.py),
    # así el resto del template (KPIs, gráfico, donut) no necesita saber
    # cuál proveedor está viendo. Funnel/Pipeline NO se tocan: esos datos
    # no distinguen proveedor (viven en el Sheet, compartidos).
    # Stringee solo disponible para India por ahora — cualquier otro país
    # cae a Asterisk sin romper, aunque alguien escriba ?provider=stringee
    # a mano en la URL.
    if p['provider'] == 'stringee' and p['country'] != 'india':
        p['provider'] = 'asterisk'

    if p['provider'] == 'stringee':
        s, e, *_ = resolve_range(p['period'], None, p['start'], p['end'], country=p['country'])
        st = an.stringee_traffic(db, s, e, p['country'])
        if st is not None:
            rep['traffic']   = st
            rep['series']    = an.stringee_timeseries(db, s, e, rep['grain'], p['country'])
            rep['hourly']    = an.stringee_hourly(db, s, e, p['country'])
            # Funnel/durations también scoped a Stringee — via el cruce de
            # lead_id (stringee_calls.lead_id ya viene cruzado por WF15).
            # Pipeline status queda compartido a propósito: es cola
            # operativa actual del lead, no algo atribuible a un proveedor.
            rep['funnel']    = an.stringee_funnel(db, s, e, p['country'])
            rep['durations'] = an.stringee_durations(db, s, e, p['country'])
            rep['funnel_series'] = an.stringee_funnel_timeseries(
                db, s, e, '30min' if rep['grain'] == 'hour' else rep['grain'], p['country'])
        else:
            p['provider'] = 'asterisk'  # tabla no existe todavía — cae a Asterisk sin romper

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


# ── API ────────────────────────────────────────────────────────────
@app.route('/api/report')
@auth
@with_db
def api_report(db):
    p = params()
    return jsonify(build_report(db, p['period'], p['start'], p['end'], p['rate'], country=p['country']))


@app.route('/api/conversions')
@auth
@with_db
def api_conversions(db):
    p = params()
    s, e, *_ = resolve_range(p['period'], None, p['start'], p['end'], country=p['country'])
    return jsonify(an.conversions_list(db, s, e, 500, country=p['country']))


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
    rows = an.conversions_list(db, s, e, 5000, country=p['country'])
    tz = an.country_tz_label(p['country'])
    return _csv(rows, [
        ('lead_id', 'Lead ID'), ('full_name', 'Full name'), ('phone', 'Phone'),
        ('atlantis_user', 'Atlantis user'), ('account_id', 'Account'),
        ('created_at_local', f'Account opened ({tz})'),
    ], f'landmark_accounts_{s:%Y%m%d}_{e:%Y%m%d}.csv')


@app.route('/export/summary.csv')
@auth
@with_db
def export_summary(db):
    p = params()
    rep = build_report(db, p['period'], p['start'], p['end'], p['rate'], country=p['country'])
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
                f"landmark_summary_{rep['range']['start']}.csv")


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
}


@app.template_filter('estado')
def fmt_estado(v):
    key = (v or '').strip().upper()
    return STATUS_ES.get(key, key.replace('_', ' ').title() if key else 'No status')


if __name__ == '__main__':
    print(f"→ Panel en http://0.0.0.0:{CFG['PORT']}  (driver: "
          f"{'sqlite' if CFG['SQLITE'] else 'mysql'})")
    app.run(host='0.0.0.0', port=CFG['PORT'], debug=False, threaded=True)
