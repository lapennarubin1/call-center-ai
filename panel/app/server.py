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
    LM_USER LM_PASS    credenciales del panel (default admin / cambiar)
    LM_SQLITE          ruta a sqlite (modo demo/test, ignora MySQL)
"""
import os, io, csv, functools, secrets, sys
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
    'SQLITE'  : os.getenv('LM_SQLITE', ''),
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
                return jsonify({'error': 'no autenticado'}), 401
            return redirect(url_for('login', next=request.path))
        return fn(*a, **kw)
    return wrap


def params():
    return {
        'period': request.args.get('period', 'today'),
        'start' : request.args.get('start'),
        'end'   : request.args.get('end'),
        'rate'  : float(request.args.get('rate', CFG['RATE'])),
    }


# ── Autenticación ──────────────────────────────────────────────────
@app.route('/login', methods=['GET', 'POST'])
def login():
    err = None
    if request.method == 'POST':
        if (request.form.get('user') == CFG['USER'] and
                secrets.compare_digest(request.form.get('pass', ''), CFG['PASS'])):
            session['ok'] = True
            session.permanent = True
            return redirect(request.args.get('next') or url_for('dashboard'))
        err = 'Usuario o contraseña incorrectos'
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
    rep = build_report(db, p['period'], p['start'], p['end'], p['rate'])
    t = rep['traffic']

    donut = charts.donut([
        {'label': 'Contestadas',   'value': t['answered'],      'tone': 'ok'},
        {'label': 'Sin respuesta', 'value': t['no_answer'],     'tone': 'idle'},
        {'label': 'Ocupado',       'value': t['busy'],          'tone': 'warn'},
        {'label': 'Fallo técnico', 'value': t['tech_failures'], 'tone': 'bad'},
    ])
    pipebars = (charts.bars_h(rep['pipeline']['rows'], key='n', label='status')
                if rep['pipeline'].get('available') else [])

    return render_template(
        'dashboard.html', r=rep, cfg=CFG, p=p,
        trace=charts.trace(rep['series']),
        hourly=charts.hourly(rep['hourly']),
        ladder=charts.ladder(t, rep['durations'], rep['funnel']),
        donut=donut,
        durbars=charts.bars_h(rep['durations']['buckets']),
        pipebars=pipebars,
    )


@app.route('/health')
@with_db
def health(db):
    r = db.one("SELECT COUNT(*) AS n FROM cdr")
    return jsonify({'status': 'ok', 'cdr_rows': r.get('n', 0),
                    'driver': db.driver, 'ts': datetime.now().isoformat()})


# ── API ────────────────────────────────────────────────────────────
@app.route('/api/report')
@auth
@with_db
def api_report(db):
    p = params()
    return jsonify(build_report(db, p['period'], p['start'], p['end'], p['rate']))


@app.route('/api/conversions')
@auth
@with_db
def api_conversions(db):
    p = params()
    s, e, *_ = resolve_range(p['period'], None, p['start'], p['end'])
    return jsonify(an.conversions_list(db, s, e, 500))


@app.route('/api/cache/clear', methods=['POST'])
@auth
def api_cache_clear():
    CACHE.clear()
    return jsonify({'cleared': True})


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
    s, e, *_ = resolve_range(p['period'], None, p['start'], p['end'])
    rows = an.timeseries(db, s, e, 'day', p['rate'])
    return _csv(rows, [
        ('bucket', 'Fecha'), ('total_calls', 'Llamadas'), ('answered', 'Contestadas'),
        ('no_answer', 'Sin respuesta'), ('failed', 'Fallidas'), ('asr', 'Tasa contestacion %'),
        ('billed_minutes', 'Minutos facturados'), ('acd', 'Duracion media (s)'),
        ('cost', 'Costo USD'),
    ], f'landmark_diario_{s:%Y%m%d}_{e:%Y%m%d}.csv')


@app.route('/export/conversions.csv')
@auth
@with_db
def export_conversions(db):
    p = params()
    s, e, *_ = resolve_range(p['period'], None, p['start'], p['end'])
    rows = an.conversions_list(db, s, e, 5000)
    return _csv(rows, [
        ('lead_id', 'Lead ID'), ('full_name', 'Nombre'), ('phone', 'Telefono'),
        ('atlantis_user', 'Usuario Atlantis'), ('account_id', 'Cuenta'),
        ('created_at', 'Fecha apertura'),
    ], f'landmark_cuentas_{s:%Y%m%d}_{e:%Y%m%d}.csv')


@app.route('/export/summary.csv')
@auth
@with_db
def export_summary(db):
    p = params()
    rep = build_report(db, p['period'], p['start'], p['end'], p['rate'])
    t, f = rep['traffic'], rep['funnel']
    rows = [
        {'k': 'Periodo',                 'v': rep['label']},
        {'k': 'Llamadas totales',        'v': t['total_calls']},
        {'k': 'Contestadas',             'v': t['answered']},
        {'k': 'Sin respuesta',           'v': t['no_answer']},
        {'k': 'Ocupado',                 'v': t['busy']},
        {'k': 'Fallos tecnicos',         'v': t['tech_failures']},
        {'k': 'Tasa de contestacion ASR (%)', 'v': t['asr']},
        {'k': 'Efectividad de red NER (%)',   'v': t['ner']},
        {'k': 'Duracion media ACD (s)',  'v': t['acd']},
        {'k': 'Minutos facturados',      'v': t['billed_minutes']},
        {'k': 'Tiempo en conversacion (min)', 'v': t['talk_minutes']},
        {'k': 'Costo total USD',         'v': t['cost']},
        {'k': 'Costo por contestada USD','v': t['cost_per_answered']},
        {'k': 'Conversaciones reales +30s', 'v': rep['durations']['meaningful']},
    ]
    if f.get('available'):
        rows += [
            {'k': 'Leads contactados',   'v': f['contacted']},
            {'k': 'Cuentas abiertas',    'v': f['accounts_opened']},
            {'k': 'Tasa de conversion (%)', 'v': f['conversion_rate']},
            {'k': 'Costo por adquisicion USD', 'v': f['cpa']},
        ]
    return _csv(rows, [('k', 'Metrica'), ('v', 'Valor')],
                f"landmark_resumen_{rep['range']['start']}.csv")


# ── Formato numérico (convención española: . miles, , decimales) ───
def _es(v, dec):
    return f'{float(v):,.{dec}f}'.replace(',', '@').replace('.', ',').replace('@', '.')


@app.template_filter('n')
def fmt_n(v):
    """Entero con separador de miles: 49.426"""
    try:    return f'{int(v):,}'.replace(',', '.')
    except Exception: return v


@app.template_filter('m')
def fmt_m(v):
    """Moneda con 2 decimales: 508,20"""
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
    """Segundos con un decimal: 51,9"""
    try:    return _es(v, 1)
    except Exception: return v


@app.template_filter('usd4')
def fmt_usd4(v):
    """Importes pequeños: 0,0906"""
    try:    return _es(v, 4)
    except Exception: return v


# ── Etiquetas legibles para los estados internos ───────────────────
STATUS_ES = {
    'READY_TO_CALL'       : 'Pendiente de llamar',
    'DIALING'             : 'Marcando',
    'CALL_IN_PROGRESS'    : 'Llamada en curso',
    'NO_ANSWER'           : 'Sin respuesta',
    'VOICEMAIL'           : 'Buzón de voz',
    'FAILED'              : 'Fallo técnico',
    'DIAL_FAILED'         : 'Fallo de marcación',
    'SUCCESSFUL'          : 'Contestó y conversó',
    'SCHEDULED'           : 'Devolución agendada',
    'INTERESTED'          : 'Cuenta abierta',
    'CONVERTED'           : 'Cliente activo',
    'SALES_HANDOFF'       : 'Pasado a ventas',
    'NOT_INTERESTED'      : 'Sin interés',
    'DO_NOT_CALL'         : 'No contactar',
    'MAX_ATTEMPTS_REACHED': 'Intentos agotados',
    'INVALID_LEAD'        : 'Lead inválido',
}


@app.template_filter('estado')
def fmt_estado(v):
    key = (v or '').strip().upper()
    return STATUS_ES.get(key, key.replace('_', ' ').capitalize() if key else 'Sin estado')


if __name__ == '__main__':
    print(f"→ Panel en http://0.0.0.0:{CFG['PORT']}  (driver: "
          f"{'sqlite' if CFG['SQLITE'] else 'mysql'})")
    app.run(host='0.0.0.0', port=CFG['PORT'], debug=False, threaded=True)
