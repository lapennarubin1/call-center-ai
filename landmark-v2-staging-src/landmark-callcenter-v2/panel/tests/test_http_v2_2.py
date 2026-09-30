"""
test_http_v2_2.py — Capa HTTP del panel V2.2 (Flask test client, sqlite).

API (token, transporte, códigos), roles (master / viewer / support / sin
sesión), render de las pantallas nuevas y convivencia con las existentes.
"""
import json
import os
import sqlite3
import sys
import tempfile

from _harness import Suite

S = Suite('HTTP V2.2 (API · switches · permisos · UI · analytics)')
TOKEN = 'test-token-0123456789'
PRIVATE = {'REMOTE_ADDR': '172.18.0.5'}     # red Docker interna (como n8n → 172.18.0.1)
PUBLIC = {'REMOTE_ADDR': '8.8.8.8'}


def boot(token=TOKEN):
    path = tempfile.mktemp(suffix='.db')
    os.environ['LM_SQLITE'] = path
    # Desde r2-final2 NINGUNA contraseña tiene valor por defecto en el
    # código del panel: si falta una, `import server` detiene el proceso.
    # El arnés las aporta, igual que las aportaría el .env en el VPS.
    # No son credenciales de nada: este panel habla con un SQLite temporal.
    os.environ.update(
        LM_PASS='test-viewer',           # SECSCAN-OK: arnés de test, SQLite temporal
        LM_MASTER_PASS='test-master',    # SECSCAN-OK: arnés de test, SQLite temporal
        LM_SUPPORT_PASS='test-support',  # SECSCAN-OK: arnés de test, SQLite temporal
        LM_DB_PASS='test-unused')        # SECSCAN-OK: arnés de test, no hay MySQL
    if token is None:
        os.environ.pop('LM_ROUTES_API_TOKEN', None)
    else:
        os.environ['LM_ROUTES_API_TOKEN'] = token
    for m in ('server',):
        sys.modules.pop(m, None)
    import routes_config as rc
    rc._SCHEMA_OK.clear()
    from analytics import DB
    import test_foundation_v2_2 as tf
    conn = sqlite3.connect(path)
    sdb = DB(conn, 'sqlite')
    tf.seed_sqlite(sdb)
    # El cuarto interruptor: sin modo de operación el panel falla cerrado
    # y ninguna ruta sale como invocable. Ver tf.cutover().
    tf.cutover(sdb)
    conn.close()
    import server
    server.app.config['TESTING'] = True
    return server.app


def client(app, role=None):
    c = app.test_client()
    if role:
        with c.session_transaction() as s:
            s['ok'] = True
            s['role'] = role
    return c


def api(c, path, token=TOKEN, env=PRIVATE, headers=None):
    h = dict(headers or {})
    if token:
        h['X-Service-Token'] = token
    return c.get(path, headers=h, environ_base=env)


# ── API ───────────────────────────────────────────────────────────────
def t_sin_token_503():
    r = api(client(boot(token=None)), '/api/routes/active')
    assert r.status_code == 503 and r.get_json()['code'] == 'CONFIG_ERROR'


def t_token_invalido_401():
    c = client(boot())
    assert api(c, '/api/routes/active', token=None).status_code == 401
    r = api(c, '/api/routes/active', token='otro')
    assert r.status_code == 401 and r.get_json()['code'] == 'AUTH_ERROR'


def t_http_publico_403():
    r = api(client(boot()), '/api/routes/active', env=PUBLIC)
    assert r.status_code == 403, r.status_code
    assert 'insecure transport' in r.get_json()['error']


def t_https_publico_ok():
    r = api(client(boot()), '/api/routes/active', env=PUBLIC, headers={'X-Forwarded-Proto': 'https'})
    assert r.status_code == 200, r.status_code


def t_red_interna_ok():
    c = client(boot())
    data = api(c, '/api/routes/active?all=1').get_json()
    assert {x['route_key'] for x in data['routes']} == {'IN_PROVEEDOR1', 'IN_STRINGEE', 'NP_PROVEEDOR1'}
    loop = api(c, '/api/routes/active', env={'REMOTE_ADDR': '127.0.0.1'})
    assert loop.status_code == 200


def t_by_key_e_historico():
    app = boot()
    c = client(app, 'master')
    import routes_config as rc
    np_id = next(r['id'] for r in rc.routes_list(__import__('analytics').DB(
        sqlite3.connect(os.environ['LM_SQLITE']), 'sqlite'), 'all') if r['route_key'] == 'NP_PROVEEDOR1')
    assert c.post(f'/callcenter/routes/{np_id}/archive').status_code == 302
    r = api(c, '/api/routes/by-key/NP_PROVEEDOR1')
    assert r.status_code == 200 and r.get_json()['status'] == 'ARCHIVED'
    assert r.get_json()['followup_policy']['rules'], 'policy resoluble aunque esté archivada'
    assert api(c, '/api/routes/by-key/XX_NADA').status_code == 404
    keys = {x['route_key'] for x in api(c, '/api/routes/active?all=1').get_json()['routes']}
    assert 'NP_PROVEEDOR1' not in keys
    keys = {x['route_key'] for x in api(c, '/api/routes/active?include_archived=1').get_json()['routes']}
    assert 'NP_PROVEEDOR1' in keys


def t_tools_endpoint():
    c = client(boot())
    d = api(c, '/api/countries/IN/tools').get_json()
    assert set(d['tools']) == {'CREATE_ACCOUNT', 'CREATE_PAYMENT_LINK'}, d['tools'].keys()
    assert d['tools']['CREATE_ACCOUNT']['market'] == 'IND'
    np = api(c, '/api/countries/NP/tools').get_json()
    assert set(np['tools']) == {'CREATE_ACCOUNT'}, 'Monetix disabled no aparece'
    assert api(c, '/api/countries/ZZ/tools').status_code == 404


def t_api_no_expone_secretos():
    body = api(client(boot()), '/api/routes/active?include_archived=1').get_data(as_text=True).lower()
    for leak in ('sk_79048', 'admin@123', 'wsec_', 'vduatcc', 'zug8dhgb'):
        assert leak not in body, leak


# ── roles ─────────────────────────────────────────────────────────────
PAGES = ['/callcenter/routes', '/callcenter/countries/IN', '/callcenter/routes/preview.json',
         '/callcenter/analytics.json']
MUTATIONS = ['/callcenter/countries/save', '/callcenter/routes/new', '/callcenter/routes/1/edit',
             '/callcenter/routes/1/enable', '/callcenter/routes/1/disable', '/callcenter/routes/1/archive',
             '/callcenter/routes/1/unarchive', '/callcenter/routes/1/window/new',
             '/callcenter/routes/1/telegram/new', '/callcenter/countries/IN/tools/CREATE_ACCOUNT',
             '/callcenter/policies/save', '/callcenter/policies/1/archive', '/callcenter/providers/save',
             '/callcenter/countries/IN/archive', '/callcenter/countries/IN/disable',
             '/callcenter/countries/NP/enable', '/callcenter/providers/1/disable',
             '/callcenter/providers/2/enable']


def t_sin_sesion():
    c = client(boot())
    for p in PAGES:
        r = c.get(p)
        assert r.status_code == 302 and '/login' in r.headers['Location'], (p, r.status_code)


def t_viewer_y_support_bloqueados():
    app = boot()
    for role in ('viewer', 'support'):
        c = client(app, role)
        for p in PAGES:
            r = c.get(p)
            assert r.status_code in (302, 403) and '/callcenter' not in r.headers.get('Location', ''), (role, p)
        for m in MUTATIONS:
            r = c.post(m, data={'iso': 'IN'})
            assert r.status_code in (302, 403), (role, m, r.status_code)
            assert 'applied' not in r.headers.get('Location', ''), f'{role} ejecutó {m}'
    # y no cambió nada
    import routes_config as rc
    from analytics import DB
    db = DB(sqlite3.connect(os.environ['LM_SQLITE']), 'sqlite')
    assert [r['status'] for r in rc.routes_list(db, 'all')] == ['ACTIVE', 'ACTIVE', 'DISABLED']


def t_master_render():
    c = client(boot(), 'master')
    r = c.get('/callcenter/routes')
    assert r.status_code == 200
    for n in (b'IN_PROVEEDOR1', b'IN_STRINGEE', b'NP_PROVEEDOR1', b'STANDARD_CALL_RETRY',
              b'COUNTRY READY', b'ELEVENLABS_SIP', b'STRINGEE_WORKER', b'ROUTE_DISABLED'):
        assert n in r.data, n
    for st in ('active', 'disabled', 'archived', 'all'):
        assert c.get(f'/callcenter/routes?status={st}').status_code == 200
    r = c.get('/callcenter/countries/IN')
    assert r.status_code == 200
    for n in (b'COUNTRY ON', b'COUNTRY READY', b'ROUTES READY 2/2', b'TOOLS READY',
              b'Operating schedule', b'Account creation', b'Payment link', b'Callback',
              b'Telegram destinations', b'Asia/Kolkata', b'adapter ELEVENLABS_SIP'):
        assert n in r.data, n


def t_flujo_alta_mexico_por_ui():
    """Checklist completo por HTTP. El país se enciende AL FINAL: la ruta queda
    READY y ON, pero no llama hasta que el país se enciende."""
    app = boot()
    c = client(app, 'master')
    import routes_config as rc
    from analytics import DB
    import urllib.parse as up
    db = lambda: DB(sqlite3.connect(os.environ['LM_SQLITE']), 'sqlite')
    c.post('/callcenter/countries/save', data={'iso': 'MX', 'country_name': 'México', 'dial_prefix': '+52',
                                               'timezone': 'America/Mexico_City', 'language': 'es'})
    assert rc.country_get(db(), 'MX')['enabled'] == 0, 'el país nace OFF'
    sip = next(p['id'] for p in rc.providers_list(db()) if p['code'] == 'proveedor1')
    c.post('/callcenter/routes/new', data={'iso': 'MX', 'provider_id': sip, 'capacity_default': '3'})
    mx = next(r for r in rc.routes_list(db(), 'all') if r['route_key'] == 'MX_PROVEEDOR1')
    assert mx['provider_id'] == sip, 'misma PROVEEDOR1 que India, no un proveedor nuevo'
    loc = c.post(f"/callcenter/routes/{mx['id']}/enable").headers['Location']
    assert 'error=' in loc and 'Missing' in up.unquote_plus(loc), loc
    pol = next(p['id'] for p in rc.policies_list(db()) if p['policy_key'] == 'STANDARD_CALL_RETRY')
    c.post(f"/callcenter/routes/{mx['id']}/edit", data={
        'elevenlabs_agent_id': 'agent_mx', 'elevenlabs_phone_number_id': 'phnum_mx',
        'followup_policy_id': str(pol), 'capacity_default': '3'})
    c.post(f"/callcenter/routes/{mx['id']}/telegram/new", data={'purpose': 'recording', 'chat_id': '-1003616406932'})
    assert 'applied=1' in c.post(f"/callcenter/routes/{mx['id']}/enable").headers['Location']
    d = api(c, '/api/routes/by-key/MX_PROVEEDOR1').get_json()
    assert d['ready'] and d['switches']['route_enabled'] and not d['calling_now']
    assert d['blocked_by'] == ['COUNTRY_DISABLED'], d['blocked_by']
    assert 'applied=1' in c.post('/callcenter/countries/MX/enable').headers['Location']
    d = api(c, '/api/routes/by-key/MX_PROVEEDOR1').get_json()
    assert d['calling_now'] and d['capacity_now'] == 3, d['blocked_by']


def t_pantallas_existentes_intactas():
    c = client(boot(), 'master')
    r = c.get('/callcenter')         # switches: n8n inalcanzable en el test, debe degradar bien
    assert r.status_code == 200, r.status_code
    assert b'Countries' in r.data, 'link nuevo en la barra'
    assert c.get('/login').status_code in (200, 302)


def t_switches_por_ui():
    app = boot()
    c = client(app, 'master')
    keys = lambda: sorted(x['route_key'] for x in api(c, '/api/routes/active').get_json()['routes'])
    assert keys() == ['IN_PROVEEDOR1', 'IN_STRINGEE']
    c.post('/callcenter/countries/IN/disable')
    assert keys() == [], 'India OFF: ningún proveedor llama a India'
    c.post('/callcenter/countries/IN/enable')
    import routes_config as rc
    from analytics import DB
    pid = next(p['id'] for p in rc.providers_list(DB(sqlite3.connect(os.environ['LM_SQLITE']), 'sqlite'))
               if p['code'] == 'proveedor1')
    c.post(f'/callcenter/providers/{pid}/disable')
    assert keys() == ['IN_STRINGEE'], 'PROVEEDOR1 OFF, Stringee sigue'
    c.post(f'/callcenter/providers/{pid}/enable')
    assert keys() == ['IN_PROVEEDOR1', 'IN_STRINGEE']
    audit = c.get('/callcenter/routes').data
    for a in (b'country_disable', b'country_enable', b'provider_disable', b'provider_enable'):
        assert a in audit, f'falta auditoría {a}'


def t_analytics_local_sin_red():
    """El endpoint del dashboard responde desde MySQL/sqlite local y NO abre
    ninguna conexión de red (LeadStudio incluido)."""
    import socket
    app = boot()
    import call_jobs as cj, ops_events as oe
    from analytics import DB
    db = DB(sqlite3.connect(os.environ['LM_SQLITE']), 'sqlite')
    j = cj.make_call_job_id('IN_PROVEEDOR1')
    cj.claim_job(db, j, 'L1', 1, 'IN_PROVEEDOR1', 'IN', 'proveedor1', 1)
    cj.mark_dispatching(db, j)
    cj.mark_dispatched(db, j, conversation_id='conv_x')
    oe.record_call_result(db, j, 'ANSWERED', 327)
    oe.record_event(db, 'ACCOUNT_CREATED', provider='cashstudio', lead_id='L1', country_iso='IN')
    db.conn.commit()
    c = client(app, 'master')
    real = socket.socket.connect
    calls = []

    def guard(self, addr):
        calls.append(addr)
        raise OSError('red bloqueada por el test')
    socket.socket.connect = guard
    try:
        r = c.get('/callcenter/analytics.json?group_by=route')
    finally:
        socket.socket.connect = real
    assert not calls, f'el dashboard intentó conectarse a {calls}'
    assert r.status_code == 200, r.data[:200]
    d = r.get_json()
    o = d['overview']
    assert (o['calls'], o['answered'], o['talk_minutes'], o['accounts_opened']) == (1, 1, 5.5, 1), o
    assert d['by'][0]['route'] == 'IN_PROVEEDOR1'
    assert d['accounts_by_route'] == [{'route': 'IN_PROVEEDOR1', 'accounts_opened': 1,
                                       'attribution_model': 'LAST_CONNECTED_CALL_V1'}]
    assert client(app, 'viewer').get('/callcenter/analytics.json').status_code in (302, 403)


def main():
    S.section('API /api/*')
    S.check('sin LM_ROUTES_API_TOKEN → 503 CONFIG_ERROR', t_sin_token_503)
    S.check('token ausente o incorrecto → 401 AUTH_ERROR', t_token_invalido_401)
    S.check('HTTP plano desde IP pública → 403 insecure transport', t_http_publico_403)
    S.check('IP pública con HTTPS (X-Forwarded-Proto) → 200', t_https_publico_ok)
    S.check('red interna Docker/loopback → 200', t_red_interna_ok)
    S.check('by-key resuelve archivadas; active/all las excluyen; include_archived las incluye', t_by_key_e_historico)
    S.check('tools por país: solo enabled; 404 país desconocido', t_tools_endpoint)
    S.check('ninguna respuesta contiene secretos de los workflows v1', t_api_no_expone_secretos)
    S.section('roles')
    S.check('sin sesión → login en todas las pantallas', t_sin_sesion)
    S.check('viewer y support: 4 pantallas y 18 mutaciones bloqueadas, nada cambia', t_viewer_y_support_bloqueados)
    S.check('master: listado (4 filtros) y página de país con todas las secciones', t_master_render)
    S.section('flujo completo')
    S.check('alta de México por UI: NOT READY → READY+ON sin llamar → país ON → llama', t_flujo_alta_mexico_por_ui)
    S.check('switches por UI: país OFF/ON, proveedor OFF/ON, auditados', t_switches_por_ui)
    S.check('pantallas existentes siguen funcionando', t_pantallas_existentes_intactas)
    S.section('analytics local')
    S.check('/callcenter/analytics.json: métricas locales, CERO conexiones de red, solo master', t_analytics_local_sin_red)
    return S.finish()


if __name__ == '__main__':
    raise SystemExit(main())
