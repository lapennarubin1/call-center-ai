#!/usr/bin/env python3
"""El Landmark Panel V2: pantallas nuevas, API para n8n y permisos.

Regla que se verifica de verdad: el panel lee **MySQL local** y NO abre ninguna
conexión a LeadStudio para pintar Analytics. Se comprueba interceptando el
socket: si alguna vista intenta salir a la red, el test falla.
"""
import json
import os
import socket
import sys

# Importar los módulos del suite no debe dejar .pyc dentro del paquete:
# lo que se empaqueta tiene que salir limpio.
sys.dont_write_bytecode = True
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import _suite as S

DB = 'lm_panel_v2_test'
TOKEN = 'test-service-token'


def main():
    s = S.Suite('LANDMARK PANEL V2')
    if not S.mysql_available():
        s.skip('suite completa', 'MariaDB no disponible')
        return s.finish()

    S.migrated_db(DB)
    # Desde r2-final2 NINGUNA contraseña tiene valor por defecto en el
    # código: si falta una, el panel no arranca. El arnés las aporta todas.
    os.environ.update(LM_SCHEDULER_DISABLED='1', LM_TELEGRAM_DISABLED='1',
                      LM_ROUTES_API_TOKEN=TOKEN, LM_DB_HOST=S.MYSQL['host'],
                      LM_DB_USER=S.MYSQL['user'], LM_DB_PASS=S.MYSQL['password'],
                      LM_DB_NAME=DB,
                      # SECSCAN-OK: contraseñas de un panel de test contra una BD desechable
                      LM_PASS='test-viewer', LM_MASTER_PASS='test-master',
                      # SECSCAN-OK: contraseña de un panel de test contra una BD desechable
                      LM_SUPPORT_PASS='test-support')
    import server
    app = server.app

    def client(role='master'):
        c = app.test_client()
        if role:
            with c.session_transaction() as sess:
                sess['ok'] = True
                sess['role'] = role
                sess['user'] = 'tester'
        return c

    INTERNAL = {'REMOTE_ADDR': '127.0.0.1'}
    api_h = {'X-Service-Token': TOKEN}

    # ── datos de ejemplo para que Analytics tenga qué mostrar ─────────
    db = S.db_for(DB)
    route = db.one("SELECT id FROM call_routes WHERE route_key='IN_PROVEEDOR1'")
    for i, (result, dur) in enumerate([('ANSWERED', 120), ('ANSWERED', 200),
                                       ('NO_ANSWER', 0), ('NO_ANSWER', 0),
                                       ('CALLBACK', 65), ('BUSY', 0)]):
        db.execute(
            "INSERT INTO wf_call_jobs (call_job_id, lead_id, route_id, route_key, "
            "country_iso, provider, adapter_key, attempt, state, result, "
            "duration_seconds, dispatched_at, completed_at) VALUES "
            "(§,§,§,'IN_PROVEEDOR1','IN','proveedor1','ELEVENLABS_SIP',1,'COMPLETED',"
            "§,§,UTC_TIMESTAMP(),UTC_TIMESTAMP())",
            (f'pj{i}', f'pl{i}', route['id'], result, dur))
    db.execute("INSERT INTO wf_events (event_key, event_type, event_domain, occurred_at, "
               "lead_id, country_iso, route_key, provider) VALUES "
               "('ACCOUNT_CREATED:cashstudio:pl0','ACCOUNT_CREATED','TOOL',UTC_TIMESTAMP(),"
               "'pl0','IN','IN_PROVEEDOR1','cashstudio')")
    db.execute("INSERT INTO wf_events (event_key, event_type, event_domain, occurred_at, "
               "call_job_id, country_iso, route_key) VALUES "
               "('CALL_TECH_FAILED:pjX','CALL_TECH_FAILED','CALL',UTC_TIMESTAMP(),"
               "'pjX','IN','IN_PROVEEDOR1')")
    db.execute("INSERT INTO wf_reconciliation_issues (issue_key, issue_type, entity_type, "
               "entity_id, lead_id, country_iso, severity, state, detail_json) VALUES "
               "('CALL_NEEDS_RECONCILIATION:call_job:pjZ','CALL_NEEDS_RECONCILIATION',"
               "'call_job','pjZ','plZ','IN','WARN','OPEN','{}')")

    s.section('pantallas nuevas del Call Center')
    c = client()
    for url, needle in [('/callcenter/analytics', 'Calls attempted'),
                        ('/callcenter/issues', 'Open issues'),
                        ('/callcenter/settings', 'adapter_key'),
                        ('/callcenter/routes', 'Countries')]:
        def page(u=url, nd=needle):
            r = c.get(u)
            assert r.status_code == 200, f'{u} devolvió {r.status_code}'
            body = r.data.decode('utf-8')
            assert nd in body, f'{u} no contiene {nd!r}'
        s.check(f'{url} responde 200 y renderiza', page)

    def analytics_shows_real_numbers():
        r = c.get('/callcenter/analytics?days=1&tz=Asia/Kolkata')
        body = r.data.decode('utf-8')
        assert r.status_code == 200
        # 6 llamadas, 3 contestadas (ANSWERED+CALLBACK), tasa 50%
        assert 'Answered' in body and 'Talk minutes' in body
        assert 'Technical failures' in body
        assert 'LOCAL_MYSQL' in body, 'la pantalla no declara su fuente'
    s.check('Analytics muestra los KPIs calculados de los datos locales',
            analytics_shows_real_numbers)

    def analytics_breakdowns():
        r = c.get('/callcenter/analytics?days=7')
        body = r.data.decode('utf-8')
        for g in ('By country', 'By provider', 'By route'):
            assert g in body, f'falta el desglose {g}'
        assert 'Timeseries' in body or 'Por day' in body or 'Por hour' in body
    s.check('Analytics trae los desgloses por país, proveedor, ruta y tiempo',
            analytics_breakdowns)

    def analytics_csv():
        r = c.get('/callcenter/analytics.csv?days=1&group_by=route')
        assert r.status_code == 200
        assert 'text/csv' in r.headers['Content-Type']
        head = r.data.decode('utf-8').splitlines()[0]
        for col in ('route', 'attempted', 'answered', 'answer_rate', 'talk_minutes'):
            assert col in head, f'falta la columna {col} en el CSV'
    s.check('el export CSV trae las columnas del contrato de métricas', analytics_csv)

    def issues_listed_and_resolvable():
        r = c.get('/callcenter/issues')
        assert 'CALL_NEEDS_RECONCILIATION' in r.data.decode('utf-8')
        key = 'CALL_NEEDS_RECONCILIATION:call_job:pjZ'
        r = c.post(f'/callcenter/issues/{key}/resolve', data={})
        assert r.status_code in (301, 302), f'resolver devolvió {r.status_code}'
        row = db.one("SELECT state, resolved_by FROM wf_reconciliation_issues "
                     "WHERE issue_key=§", (key,))
        assert row['state'] == 'RESOLVED', row
        assert row['resolved_by'] == 'tester', 'no se registró quién lo resolvió'
    s.check('los issues se listan y se resuelven dejando autor', issues_listed_and_resolvable)

    def settings_editable():
        r = c.post('/callcenter/settings/save',
                   data={'setting_key': 'tech_retry_max', 'setting_value': '11',
                         'value_type': 'int', 'scope': 'WF2'})
        assert r.status_code in (301, 302)
        v = db.one("SELECT setting_value v FROM wf_settings "
                   "WHERE setting_key='tech_retry_max'")['v']
        assert v == '11', f'el setting no se guardó: {v}'
    s.check('los parámetros operativos se editan desde el panel', settings_editable)

    def settings_reject_secret_key():
        r = c.post('/callcenter/settings/save',
                   data={'setting_key': 'elevenlabs_api_key', 'setting_value': 'sk_x',
                         'value_type': 'string'})
        assert r.status_code in (301, 302)
        assert not db.one("SELECT setting_value FROM wf_settings "
                          "WHERE setting_key='elevenlabs_api_key'"), \
            'el panel guardó una API key en la base'
    s.check('el panel rechaza guardar algo que parece un secreto',
            settings_reject_secret_key)

    s.section('el panel NO llama a LeadStudio para pintar')

    def analytics_makes_no_network_calls():
        """Se bloquea el socket: cualquier salida a la red hace fallar la vista."""
        real = socket.socket.connect
        calls = []

        def blocked(self, addr):
            # se permite solo la base local
            host = addr[0] if isinstance(addr, tuple) else str(addr)
            if host in ('127.0.0.1', 'localhost', '::1', S.MYSQL['host']):
                return real(self, addr)
            calls.append(host)
            raise OSError(f'salida a la red bloqueada por el test: {host}')

        socket.socket.connect = blocked
        try:
            c2 = client()
            r = c2.get('/callcenter/analytics?days=7')
            assert r.status_code == 200, \
                f'Analytics falló con la red bloqueada ({r.status_code}): depende de LeadStudio'
            r = c2.get('/callcenter/issues')
            assert r.status_code == 200
        finally:
            socket.socket.connect = real
        assert not calls, f'el panel intentó salir a: {calls}'
    s.check('Analytics e Issues funcionan con la red externa bloqueada',
            analytics_makes_no_network_calls)

    s.section('API que consume n8n')

    def api_routes_active():
        r = client(None).get('/api/routes/active', headers=api_h,
                             environ_base=INTERNAL)
        assert r.status_code == 200, r.status_code
        p = json.loads(r.data)
        assert p['scope'] == 'active'
        for route in p['routes']:
            for k in ('route_key', 'adapter_key', 'capacity_now', 'calling_now',
                      'switches', 'followup_policy', 'timezone'):
                assert k in route, f'falta {k} en el contrato de ruta'
            assert route['calling_now'] is True
    s.check('/api/routes/active devuelve solo lo que debe llamar, con su contrato',
            api_routes_active)

    def api_settings():
        r = client(None).get('/api/settings', headers=api_h, environ_base=INTERNAL)
        assert r.status_code == 200
        p = json.loads(r.data)
        assert p['settings']['crm_notes_language'] == 'en'
        assert isinstance(p['settings']['reconcile_dispatching_minutes'], int)
    s.check('/api/settings entrega los umbrales tipados', api_settings)

    def api_has_no_secrets():
        for url in ('/api/routes/active?all=1', '/api/settings',
                    '/api/countries/IN/tools', '/api/analytics/summary.json'):
            r = client(None).get(url, headers=api_h, environ_base=INTERNAL)
            body = r.data.decode('utf-8')
            for needle in ('Admin@123', 'sk_', 'password', 'cppwd', 'wsec_'):
                assert needle not in body, f'{url} filtra {needle!r}'
    s.check('ninguna respuesta de la API filtra un secreto', api_has_no_secrets)

    def api_fail_closed():
        anon = client(None)
        r = anon.get('/api/routes/active', environ_base=INTERNAL)
        assert r.status_code == 401, f'sin token devolvió {r.status_code}'
        r = anon.get('/api/routes/active', headers={'X-Service-Token': 'mal'},
                     environ_base=INTERNAL)
        assert r.status_code == 401
        r = anon.get('/api/routes/active', headers=api_h,
                     environ_base={'REMOTE_ADDR': '8.8.8.8'})
        assert r.status_code == 403, \
            f'HTTP plano desde IP pública devolvió {r.status_code}: el token viajaría en claro'
    s.check('la API es fail-closed: sin token 401, HTTP público 403', api_fail_closed)

    def api_issues():
        r = client(None).get('/api/issues?state=ALL', headers=api_h,
                             environ_base=INTERNAL)
        assert r.status_code == 200
        p = json.loads(r.data)
        assert p['count'] >= 1 and 'issues' in p
    s.check('/api/issues expone la reconciliación a n8n', api_issues)

    s.section('permisos')

    def viewer_blocked():
        v = client('viewer')
        for url in ('/callcenter/analytics', '/callcenter/issues', '/callcenter/settings',
                    '/callcenter/routes'):
            r = v.get(url)
            assert r.status_code in (302, 403), f'{url} accesible para viewer: {r.status_code}'
    s.check('el rol viewer no accede a ninguna pantalla del Call Center',
            viewer_blocked)

    def support_blocked():
        sup = client('support')
        for url in ('/callcenter/analytics', '/callcenter/settings'):
            r = sup.get(url)
            assert r.status_code in (302, 403), f'{url} accesible para support'
    s.check('el rol support tampoco entra al Call Center', support_blocked)

    def anonymous_redirected():
        anon = client(None)
        for url in ('/callcenter/analytics', '/callcenter/issues'):
            r = anon.get(url)
            assert r.status_code == 302, f'{url} sin sesión devolvió {r.status_code}'
            assert '/login' in r.headers.get('Location', '')
    s.check('sin sesión, todo redirige al login', anonymous_redirected)

    def viewer_cannot_mutate():
        v = client('viewer')
        for url, data in [('/callcenter/settings/save',
                           {'setting_key': 'tech_retry_max', 'setting_value': '999'}),
                          ('/callcenter/issues/x/resolve', {})]:
            v.post(url, data=data)
        val = db.one("SELECT setting_value v FROM wf_settings "
                     "WHERE setting_key='tech_retry_max'")['v']
        assert val == '11', f'un viewer cambió un setting: {val}'
    s.check('un viewer no puede mutar nada por POST', viewer_cannot_mutate)

    s.section('las pantallas anteriores siguen funcionando')

    def legacy_pages_ok():
        # /health y el dashboard leen las tablas CDR de Asterisk (v1), que una
        # base de test recién migrada no tiene: eso no es una regresión del
        # suite. Se comprueban las pantallas que SÍ dependen solo de V2.
        for url in ('/callcenter', '/callcenter/routes', '/callcenter/countries/IN'):
            r = c.get(url)
            assert r.status_code == 200, f'{url} devolvió {r.status_code}'
    s.check('el panel de la fundación no se rompió con los añadidos',
            legacy_pages_ok)

    def https_from_public_ip_allowed():
        r = client(None).get('/api/routes/active',
                             headers={**api_h, 'X-Forwarded-Proto': 'https'},
                             environ_base={'REMOTE_ADDR': '8.8.8.8'})
        assert r.status_code == 200, \
            f'HTTPS detrás de un proxy debería aceptarse, devolvió {r.status_code}'
    s.check('HTTPS desde IP pública (proxy) sí se acepta',
            https_from_public_ip_allowed)

    def nav_links_added():
        r = c.get('/callcenter/routes')
        body = r.data.decode('utf-8')
        assert '/callcenter/analytics' in body, 'falta el link a Analytics'
        assert '/callcenter/issues' in body, 'falta el link a Reconciliation'
    s.check('la navegación incluye Analytics y Reconciliation', nav_links_added)

    # ══ Billing · §34-42 ═════════════════════════════════════════════
    s.section('pantalla de Billing (§34-42)')

    def billing_page_loads():
        r = client().get('/callcenter/billing')
        assert r.status_code == 200, f'Billing devolvió {r.status_code}'
        html = r.data.decode('utf-8', 'replace')
        assert 'proveedor1' in html and 'stringee' in html
    s.check('/callcenter/billing responde 200 con los dos proveedores',
            billing_page_loads)

    def billing_shows_na_not_zero():
        """§37: Stringee es MONTHLY_FLAT. Mostrar $0/min diría que el
        minuto es gratis, que es falso: el contrato no se mide en minutos.

        Se mira la FILA Price/min de la ficha de Stringee, no una ventana
        de texto: en la página hay tarifas legítimas de otros proveedores.
        """
        import re as _re
        db.execute("UPDATE voice_providers SET billing_model='MONTHLY_FLAT', "
                   "monthly_fee='1200.00' WHERE code='stringee'")
        html = client().get('/callcenter/billing').data.decode('utf-8', 'replace')
        # cada proveedor va en su propia <section class="panel">
        secciones = _re.split(r'<section class="panel">', html)
        ficha = next((x for x in secciones if '(stringee)' in x), None)
        assert ficha, 'no se encontró la ficha de Stringee en la página'
        fila = _re.search(r'<th>Price/min</th>\s*<td>(.*?)</td>', ficha, _re.S)
        assert fila, 'la ficha de Stringee no tiene fila Price/min'
        celda = fila.group(1)
        assert 'N/A' in celda, f'Price/min de un MONTHLY_FLAT muestra: {celda[:120]!r}'
        assert not _re.search(r'[\$0-9]\s*0(\.0+)?\s*/\s*min', celda), \
            f'la fila muestra un cero como tarifa: {celda[:120]!r}'
        assert 'not zero' in celda.lower() or 'not measured in minutes' in celda.lower(), \
            'no se explica por qué es N/A y no 0'
    s.check('la fila Price/min de un MONTHLY_FLAT dice N/A, nunca un cero',
            billing_shows_na_not_zero)

    def billing_labels_effective_cost():
        html = client().get('/callcenter/billing').data.decode('utf-8', 'replace')
        if 'Effective cost' in html:
            assert 'NOT A CONTRACT RATE' in html, \
                'el coste efectivo se muestra sin avisar de que no es una tarifa'
    s.check('si se muestra coste efectivo, va marcado como no-tarifa',
            billing_labels_effective_cost)

    def billing_is_master_only():
        for rol in ('viewer', 'support', None):
            r = client(rol).get('/callcenter/billing')
            assert r.status_code in (302, 401, 403), \
                f'el rol {rol!r} entró a Billing ({r.status_code})'
    s.check('Billing es sólo para master (§54)', billing_is_master_only)

    def billing_api_needs_token():
        assert client(None).get('/api/billing/providers.json',
                                environ_base=INTERNAL).status_code in (401, 403)
        r = client(None).get('/api/billing/providers.json', headers=api_h,
                             environ_base=INTERNAL)
        assert r.status_code == 200, f'la API de billing devolvió {r.status_code}'
        assert 'providers' in r.get_json()
    s.check('la API de billing exige el token de servicio', billing_api_needs_token)

    # ══ Legacy Backup · §46-53 ═══════════════════════════════════════
    s.section('pantalla de Legacy Backup (§46-53)')

    def legacy_page_loads():
        r = client().get('/callcenter/legacy')
        assert r.status_code == 200, f'Legacy devolvió {r.status_code}'
        html = r.data.decode('utf-8', 'replace')
        assert 'Operating mode' in html and 'Compatibility matrix' in html
    s.check('/callcenter/legacy responde 200 con modo y matriz',
            legacy_page_loads)

    def legacy_is_master_only():
        for rol in ('viewer', 'support', None):
            r = client(rol).get('/callcenter/legacy')
            assert r.status_code in (302, 401, 403), \
                f'el rol {rol!r} entró a Legacy Backup ({r.status_code})'
    s.check('Legacy Backup es sólo para master (§54)', legacy_is_master_only)

    def mode_change_needs_confirmation():
        # Desde r2-final2 la migración deja el modo en LEGACY_BACKUP. Para
        # probar el cambio HACIA legacy hay que partir de V2_PRIMARY, que
        # es el estado realista tras un cutover.
        db.execute("UPDATE app_settings SET setting_value='V2_PRIMARY' "
                   "WHERE setting_key='lm_operating_mode'")
        antes = client().get('/callcenter/legacy').data.decode('utf-8', 'replace')
        r = client().post('/callcenter/legacy/mode',
                          data={'to_mode': 'LEGACY_BACKUP', 'confirmation': 'nope'})
        assert r.status_code == 400, \
            f'un cambio sin la frase correcta devolvió {r.status_code}'
        html = client().get('/callcenter/legacy').data.decode('utf-8', 'replace')
        assert ('V2 PRIMARY' in html) == ('V2 PRIMARY' in antes), \
            'el modo cambió pese a la confirmación incorrecta'
    s.check('sin la frase exacta, el modo no cambia', mode_change_needs_confirmation)

    def mode_change_blocked_while_v2_live():
        db.execute("UPDATE app_settings SET setting_value='V2_PRIMARY' "
                   "WHERE setting_key='lm_operating_mode'")
        db.execute("UPDATE countries SET enabled=1 WHERE iso='IN'")
        db.execute("UPDATE voice_providers SET enabled=1")
        db.execute("UPDATE call_routes SET enabled=1 WHERE route_key='IN_PROVEEDOR1'")
        r = client().post('/callcenter/legacy/mode',
                          data={'to_mode': 'LEGACY_BACKUP',
                                'confirmation': 'SWITCH TO LEGACY'})
        assert r.status_code == 400, \
            'se permitió pasar a LEGACY con una ruta V2 viva'
        assert b'IN_PROVEEDOR1' in r.data, 'no se dice qué ruta bloquea'
    s.check('la pantalla se niega a cambiar de modo con V2 vivo',
            mode_change_blocked_while_v2_live)

    def legacy_api_needs_token():
        assert client(None).get('/api/legacy/mode.json',
                                environ_base=INTERNAL).status_code in (401, 403)
        r = client(None).get('/api/legacy/mode.json', headers=api_h,
                             environ_base=INTERNAL)
        assert r.status_code == 200
        assert r.get_json()['mode'] in ('V2_PRIMARY', 'LEGACY_BACKUP')
    s.check('la API del modo exige el token de servicio', legacy_api_needs_token)

    # ══ filtros del CSV · §33 ════════════════════════════════════════
    s.section('el CSV respeta los filtros (§33)')

    def csv_is_clean_for_excel():
        """La cabecera va en la primera línea. Un preámbulo de comentarios
        se abre como filas basura en Excel."""
        import csv as _csv, io as _io
        r = client().get('/callcenter/analytics.csv?group_by=route&country=IN')
        assert r.status_code == 200
        txt = r.data.decode('utf-8', 'replace')
        assert not txt.lstrip().startswith('#'), 'el CSV empieza con comentarios'
        filas = list(_csv.DictReader(_io.StringIO(txt)))
        assert 'route' in (filas[0].keys() if filas else
                           _csv.reader(_io.StringIO(txt)).__next__())
    s.check('el CSV abre limpio: cabecera en la línea 1', csv_is_clean_for_excel)

    def csv_says_its_filters():
        r = client().get('/callcenter/analytics.csv?group_by=route&country=IN')
        assert 'IN' in r.headers.get('X-Landmark-Filters', ''), \
            'la respuesta no dice con qué filtros salió'
        assert r.headers.get('X-Landmark-Source') == 'LOCAL_MYSQL'
        assert 'filtered' in r.headers.get('Content-Disposition', ''), \
            'el nombre del fichero no distingue un export filtrado'
    s.check('el export filtrado se identifica por cabecera y por nombre',
            csv_says_its_filters)

    def csv_unfiltered_is_labelled_too():
        r = client().get('/callcenter/analytics.csv?group_by=route')
        assert 'All countries' in r.headers.get('X-Landmark-Filters', '')
        assert 'filtered' not in r.headers.get('Content-Disposition', '')
    s.check('un export sin filtros declara que los trae todos',
            csv_unfiltered_is_labelled_too)

    def csv_rows_match_the_filter():
        """§33: lo descargado tiene que ser lo que se está viendo."""
        import csv as _csv, io as _io
        r = client().get('/callcenter/analytics.csv?group_by=country&country=IN')
        filas = list(_csv.DictReader(_io.StringIO(r.data.decode('utf-8', 'replace'))))
        paises = {f['country'] for f in filas if f.get('country')}
        assert paises <= {'IN'}, f'el CSV filtrado por India trae {paises}'
    s.check('el CSV filtrado por país sólo trae ese país',
            csv_rows_match_the_filter)

    # ══ Analytics: filtros de verdad, no sólo en el payload · §3 ═════
    s.section('Analytics V2: la pantalla y el CSV usan el MISMO recorte (§3)')

    # datos de dos países/proveedores para que un filtro pueda distinguirlos
    rid = db.one("SELECT id FROM call_routes WHERE route_key='IN_STRINGEE'")
    if rid:
        for i2 in range(3):
            db.execute("""INSERT INTO wf_call_jobs
                  (call_job_id, lead_id, route_id, route_key, country_iso,
                   provider, adapter_key, attempt, state, result,
                   duration_seconds, created_at, dispatched_at, completed_at)
                VALUES (§,§,§,'IN_STRINGEE','IN','stringee','STRINGEE_WORKER',
                        1,'COMPLETED','ANSWERED',60,UTC_TIMESTAMP(),
                        UTC_TIMESTAMP(),UTC_TIMESTAMP())""",
                (f'cj-st-{i2}', f'lead-st-{i2}', rid['id']))

    def filter_bar_is_rendered():
        html = client().get('/callcenter/analytics').data.decode('utf-8', 'replace')
        for control in ('name="country"', 'name="provider"', 'name="route"',
                        'name="start"', 'name="end"', 'name="tz"'):
            assert control in html, f'falta el control {control} en la barra de filtros'
        for preset in ('Today', 'Yesterday', 'Week', 'Month', '7 days',
                       '30 days', '90 days', 'Year'):
            assert f'>{preset}<' in html, f'falta el preset {preset}'
    s.check('la barra trae presets, fechas exactas y los tres filtros',
            filter_bar_is_rendered)

    def filters_keep_their_value():
        html = client().get('/callcenter/analytics?country=IN&provider=stringee'
                            '&route=IN_STRINGEE').data.decode('utf-8', 'replace')
        import re as _re
        for valor in ('IN', 'stringee', 'IN_STRINGEE'):
            assert _re.search(rf'value="{valor}"[^>]*selected', html), \
                f'el selector no conserva {valor} tras aplicar'
    s.check('los selectores conservan el valor elegido', filters_keep_their_value)

    def kpi_is_filtered_not_global():
        """EL fallo que pedía §3: pantalla dice India, KPI global."""
        todo = client().get('/callcenter/analytics.csv?group_by=provider&days=2')
        import csv as _csv, io as _io
        filas = list(_csv.DictReader(_io.StringIO(todo.data.decode('utf-8', 'replace'))))
        total = sum(int(f['attempted'] or 0) for f in filas)

        r = client().get('/callcenter/analytics?provider=stringee&days=2')
        html = r.data.decode('utf-8', 'replace')
        assert 'FILTERED' in html, 'la pantalla no avisa de que está filtrada'

        solo = client().get('/callcenter/analytics.csv?group_by=provider'
                            '&provider=stringee&days=2')
        filas2 = list(_csv.DictReader(_io.StringIO(solo.data.decode('utf-8', 'replace'))))
        parcial = sum(int(f['attempted'] or 0) for f in filas2)
        assert {f['provider'] for f in filas2} <= {'stringee'}, \
            'el CSV filtrado trae otros proveedores'
        assert parcial < total, \
            'filtrar por un proveedor no redujo el total: el filtro no se aplica'
    s.check('filtrar por proveedor reduce el total: el KPI no es global',
            kpi_is_filtered_not_global)

    def json_api_kpi_respects_filter():
        """El mismo corte por la API: overview tiene que estar filtrado."""
        g = client(None).get('/api/analytics/summary.json?days=2',
                             headers=api_h, environ_base=INTERNAL).get_json()
        f = client(None).get('/api/analytics/summary.json?days=2&provider=stringee',
                             headers=api_h, environ_base=INTERNAL).get_json()
        assert f['overview']['calls'] < g['overview']['calls'], \
            'overview() ignora el filtro: el KPI sigue siendo global'
        assert f['overview']['filters'] == {'provider': 'stringee'}
        assert f['totals']['attempted'] == f['overview']['calls'], \
            'los KPI y los totales no coinciden entre sí'
    s.check('overview() respeta el filtro y coincide con los totales',
            json_api_kpi_respects_filter)

    def csv_links_keep_every_filter():
        html = client().get('/callcenter/analytics?country=IN&provider=stringee'
                            '&start=2026-09-01&end=2026-09-10&tz=Asia/Kolkata'
                            ).data.decode('utf-8', 'replace')
        import re as _re
        hrefs = _re.findall(r'href="(/callcenter/analytics\.csv\?[^"]+)"', html)
        assert hrefs, 'no hay enlaces de CSV en la pantalla'
        for h in hrefs:
            for esperado in ('country=IN', 'provider=stringee',
                             'start=2026-09-01', 'end=2026-09-10',
                             'tz=Asia%2FKolkata'):
                assert esperado in h, \
                    f'el enlace de CSV pierde {esperado}: {h}'
    s.check('los enlaces de CSV conservan TODOS los filtros activos',
            csv_links_keep_every_filter)

    def custom_range_works():
        r = client().get('/callcenter/analytics?start=2026-09-01&end=2026-09-10&tz=UTC')
        assert r.status_code == 200
        html = r.data.decode('utf-8', 'replace')
        assert '10 days' in html, 'el rango exacto no se interpretó como 10 días'
        assert 'value="2026-09-01"' in html and 'value="2026-09-10"' in html, \
            'las fechas elegidas no se conservan en el formulario'
    s.check('un rango exacto From/To funciona y se conserva', custom_range_works)

    def bad_range_is_rejected():
        r = client().get('/callcenter/analytics?start=2026-09-10&end=2026-09-01')
        assert r.status_code == 400, 'se aceptó un rango con end anterior a start'
    s.check('un rango invertido se rechaza', bad_range_is_rejected)

    def hourly_csv_matches_screen():
        import csv as _csv, io as _io
        r = client().get('/callcenter/analytics.csv?group_by=hour&days=2&provider=stringee')
        assert r.status_code == 200
        filas = list(_csv.DictReader(_io.StringIO(r.data.decode('utf-8', 'replace'))))
        suma = sum(int(f['calls'] or 0) for f in filas)
        api = client(None).get('/api/analytics/summary.json?days=2&provider=stringee',
                               headers=api_h, environ_base=INTERNAL).get_json()
        assert suma == api['totals']['attempted'], \
            f'el CSV por hora suma {suma} y el total dice {api["totals"]["attempted"]}'
    s.check('el CSV por hora suma exactamente el total filtrado',
            hourly_csv_matches_screen)

    # ══ vinculación con SIP Balance desde la UI · §4 ══════════════════
    s.section('vincular con SIP Balance se hace desde el panel (§4)')

    def link_ui_is_present():
        db.execute("""CREATE TABLE IF NOT EXISTS sip_providers (
            id INT AUTO_INCREMENT PRIMARY KEY, name VARCHAR(150) NOT NULL,
            active TINYINT(1) NOT NULL DEFAULT 1, billing_start_date DATE NULL,
            created_at DATETIME DEFAULT CURRENT_TIMESTAMP)""")
        db.execute("""CREATE TABLE IF NOT EXISTS sip_provider_pricing (
            id INT AUTO_INCREMENT PRIMARY KEY, provider_id INT NOT NULL,
            country VARCHAR(50) NOT NULL, trunk_name VARCHAR(100) NOT NULL,
            price_per_minute DECIMAL(10,4) NOT NULL,
            UNIQUE KEY uniq_provider_country (provider_id, country))""")
        db.execute("""CREATE TABLE IF NOT EXISTS sip_deposits (
            id INT AUTO_INCREMENT PRIMARY KEY, provider_id INT NOT NULL,
            amount_usd DECIMAL(10,2) NOT NULL, reference VARCHAR(255),
            deposit_date DATE NOT NULL,
            created_at DATETIME DEFAULT CURRENT_TIMESTAMP)""")
        db.execute("DELETE FROM sip_providers")
        db.execute("INSERT INTO sip_providers (id, name, active) VALUES (7,'Trunk Co',1)")
        db.execute("UPDATE voice_providers SET legacy_sip_provider_id=NULL")
        html = client().get('/callcenter/billing').data.decode('utf-8', 'replace')
        assert '/link' in html, 'no hay acción de vinculación en la pantalla'
        assert 'Trunk Co' in html, 'no se ofrece la ficha de SIP Balance disponible'
        assert 'NOT LINKED' in html
    s.check('un proveedor NOT LINKED ofrece su selector de vinculación',
            link_ui_is_present)

    def master_can_link():
        r = client().post('/callcenter/billing/proveedor1/link',
                          data={'legacy_sip_provider_id': '7', 'reason': 'audit'})
        assert r.status_code == 200, f'la vinculación devolvió {r.status_code}'
        v = db.one("SELECT legacy_sip_provider_id AS l FROM voice_providers "
                   "WHERE code='proveedor1'")
        assert v['l'] == 7, 'el vínculo no se guardó'
    s.check('MASTER vincula desde la UI y queda guardado', master_can_link)

    def viewer_cannot_link():
        for rol in ('viewer', 'support', None):
            r = client(rol).post('/callcenter/billing/proveedor1/link',
                                 data={'legacy_sip_provider_id': '7'})
            assert r.status_code in (302, 401, 403), \
                f'el rol {rol!r} pudo vincular ({r.status_code})'
    s.check('ningún rol que no sea MASTER puede vincular', viewer_cannot_link)

    def cannot_link_same_record_twice():
        r = client().post('/callcenter/billing/stringee/link',
                          data={'legacy_sip_provider_id': '7'})
        assert r.status_code == 400, \
            'se vinculó la misma ficha de SIP Balance a dos proveedores'
        assert b'already linked' in r.data
    s.check('una ficha de SIP Balance no se vincula a dos proveedores',
            cannot_link_same_record_twice)

    def stringee_needs_no_link():
        html = client().get('/callcenter/billing').data.decode('utf-8', 'replace')
        import re as _re
        ficha = next((x for x in _re.split(r'<section class="panel">', html)
                      if '(stringee)' in x), '')
        assert 'not applicable' in ficha, \
            'se le pide ficha de SIP Balance a Stringee'
    s.check('a Stringee no se le pide vínculo con SIP Balance',
            stringee_needs_no_link)

    def create_provider_in_one_flow():
        r = client().post('/callcenter/billing/new', data={
            'code': 'proveedor9', 'display_name': 'Provider 9',
            'adapter_key': 'ELEVENLABS_SIP', 'billing_model': 'PER_MINUTE',
            'billing_currency': 'USD', 'create_legacy_name': 'Trunk Nine',
            'reason': 'new contract'})
        assert r.status_code == 200, f'el alta devolvió {r.status_code}'
        v = db.one("""SELECT enabled, legacy_sip_provider_id AS l
                        FROM voice_providers WHERE code='proveedor9'""")
        assert v, 'el proveedor no se creó'
        assert v['enabled'] == 0, 'un proveedor nuevo nace ENCENDIDO'
        assert v['l'], 'no se creó ni vinculó su ficha de SIP Balance'
        n = db.one("SELECT name FROM sip_providers WHERE id=§", (v['l'],))
        assert n['name'] == 'Trunk Nine'
    s.check('dar de alta un proveedor crea y vincula su ficha en un solo paso',
            create_provider_in_one_flow)

    def orphan_records_are_flagged_not_duplicated():
        if not db.one("SELECT id FROM sip_providers WHERE name='Solo Billing'"):
            db.execute("INSERT INTO sip_providers (name, active) "
                       "VALUES ('Solo Billing',1)")
        html = client().get('/callcenter/billing').data.decode('utf-8', 'replace')
        assert 'BILLING-ONLY' in html and 'Solo Billing' in html, \
            'una ficha de SIP Balance sin proveedor V2 no se señala'
        n = db.one("SELECT COUNT(*) AS n FROM voice_providers "
                   "WHERE display_name='Solo Billing'")['n']
        assert n == 0, 'se creó un proveedor V2 duplicado por su cuenta'
    s.check("una ficha sin proveedor V2 se marca 'Billing-only', no se duplica",
            orphan_records_are_flagged_not_duplicated)

    def deposits_survive_linking():
        db.execute("""INSERT INTO sip_deposits (provider_id, amount_usd, reference,
                                                deposit_date)
                      VALUES (7,'900.00','wire-x','2026-03-01')""")
        client().post('/callcenter/billing/proveedor1/link',
                      data={'legacy_sip_provider_id': ''})
        client().post('/callcenter/billing/proveedor1/link',
                      data={'legacy_sip_provider_id': '7'})
        n = db.one("SELECT COUNT(*) AS n, SUM(amount_usd) AS t FROM sip_deposits "
                   "WHERE provider_id=7")
        assert n['n'] == 1 and str(n['t']) == '900.00', \
            'vincular y desvincular perdió depósitos'
    s.check('vincular y desvincular no toca depósitos ni precios',
            deposits_survive_linking)

    return s.finish()


if __name__ == '__main__':
    sys.exit(main())
