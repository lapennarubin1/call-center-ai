#!/usr/bin/env python3
"""EL CAMINO REAL — sin dobles donde importa.

La suite anterior probaba el guard contra un `FakeAnalytics` cuyo
`run_due_schedules` era una reimplementación mía. Eso escondió dos
defectos reales:

  · el panel captura `ValueError` alrededor del encendido; un
    `RuntimeError` habría dado HTTP 500 y habría roto el barrido del
    planificador en el primer horario bloqueado
  · el guard se instalaba en `register()`, mil líneas después de que
    `server.py` arranque el hilo del planificador

Aquí se ejecuta el `analytics.run_due_schedules()` DE VERDAD, con el
guard puesto, y se simula ÚNICAMENTE la llamada de red a n8n
(`_n8n_request`). Todo lo demás —el barrido, el claim atómico, el
manejo de errores, la escritura de `last_error`— es el código real.
"""
import datetime as dt
import importlib
import os
import re
import sys
from zoneinfo import ZoneInfo

sys.dont_write_bytecode = True
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from _suite import (Suite, ROOT, APP, mysql_available, migrated_db,    # noqa: E402
                    mysql_conn, DB, enable_seeded_baseline)

TZ = 'Asia/Kolkata'
GRUPOS = [
    ('INDIA - NEPAL', 'DISPATCH', 0),      # conflictivo
    ('PANEL', 'CRM_SYNC', 1),              # convive
    ('MEXICO', 'DISPATCH', 0),             # conflictivo
]


class N8nSpy:
    """Sustituye SÓLO la llamada de red. Registra qué se le pidió a n8n."""

    def __init__(self, active=()):
        self.calls = []
        self.active = set(active)

    def __call__(self, method, path, body=None):
        self.calls.append((method, path))
        m = re.match(r'/api/v1/workflows/([^/]+)/(activate|deactivate)$', path)
        if m:
            wid, accion = m.groups()
            (self.active.add if accion == 'activate' else self.active.discard)(wid)
            return {}
        if path.startswith('/api/v1/workflows'):
            return {'data': [{'id': w, 'name': f'wf {w}', 'active': w in self.active}
                             for w in ('wf1a', 'wf1b', 'wf2a', 'wf2b', 'wf3a', 'wf3b')]}
        return {}

    @property
    def activations(self):
        return [p for m, p in self.calls if p.endswith('/activate')]


def build(name, on_time=None, off_time='23:59'):
    """Base migrada + grupos legacy + horarios. `on_time` por defecto es
    AHORA en la zona del horario, para que el barrido real lo dispare."""
    migrated_db(name)
    enable_seeded_baseline(name)
    ahora = dt.datetime.now(ZoneInfo(TZ))
    on_time = on_time or ahora.strftime('%H:%M')
    # Los días son CÓDIGOS ('mon','tue',…), no números: así los guarda el
    # panel real. Con números el barrido no dispara NADA y los tests
    # pasarían en vacío, que es peor que fallar.
    #
    # Se usa el código de HOY en la zona del horario: el barrido dispara
    # siempre y el test es determinista corra el día que corra. La columna
    # es VARCHAR(24), así que los siete códigos tampoco cabrían.
    dias = ['mon', 'tue', 'wed', 'thu', 'fri', 'sat', 'sun'][ahora.weekday()]
    c = mysql_conn(name)
    with c.cursor() as cur:
        cur.execute("""CREATE TABLE IF NOT EXISTS n8n_switches (
            id INT AUTO_INCREMENT PRIMARY KEY, label VARCHAR(80) NOT NULL,
            workflow_ids TEXT NOT NULL,
            created_at DATETIME DEFAULT CURRENT_TIMESTAMP)""")
        cur.execute("""CREATE TABLE IF NOT EXISTS n8n_switch_schedules (
            id INT AUTO_INCREMENT PRIMARY KEY, switch_id INT NOT NULL UNIQUE,
            timezone VARCHAR(64) NOT NULL, on_time VARCHAR(5) NOT NULL,
            off_time VARCHAR(5) NOT NULL, days VARCHAR(24) NOT NULL,
            enabled TINYINT NOT NULL DEFAULT 1, last_on_date VARCHAR(10),
            last_off_date VARCHAR(10), last_error TEXT, last_checked_at DATETIME,
            created_at DATETIME DEFAULT CURRENT_TIMESTAMP)""")
        for i, (label, _c, _x) in enumerate(GRUPOS, start=1):
            cur.execute("INSERT INTO n8n_switches (id,label,workflow_ids) "
                        "VALUES (%s,%s,%s)", (i, label, f'wf{i}a,wf{i}b'))
            cur.execute("""INSERT INTO n8n_switch_schedules
                (switch_id, timezone, on_time, off_time, days, enabled)
                VALUES (%s,%s,%s,%s,%s,1)""", (i, TZ, on_time, off_time, dias))
    c.close()
    db = DB(mysql_conn(name))
    for label, cat, co in GRUPOS:
        db.execute("""INSERT INTO legacy_group_classification
                        (switch_label, category, coexists_with_v2, classified_by)
                      VALUES (§,§,§,'test')
                      ON DUPLICATE KEY UPDATE category=VALUES(category),
                        coexists_with_v2=VALUES(coexists_with_v2)""", (label, cat, co))
    db.execute("""UPDATE legacy_group_classification g
                    JOIN n8n_switches s ON s.label=g.switch_label
                    SET g.switch_id = s.id""")
    return db


def set_mode(db, mode):
    db.execute("""INSERT INTO app_settings (setting_key, setting_value)
                  VALUES ('lm_operating_mode', §)
                  ON DUPLICATE KEY UPDATE setting_value=VALUES(setting_value)""",
               (mode,))


def main():
    s = Suite('camino real del enclavamiento')

    # ══ A · orden de arranque ════════════════════════════════════════
    s.section('A · el guard existe antes de que pueda arrancar el hilo')

    def guard_installed_on_import():
        """Importar v2_suite tiene que dejar el guard puesto, porque es lo
        que server.py hace en su línea 30 — antes del hilo."""
        for m in ('analytics', 'legacy_mode', 'v2_suite', 'routes_config'):
            sys.modules.pop(m, None)
        an = importlib.import_module('analytics')
        assert not getattr(an.n8n_switch_set_state, '_lm_guarded', False), \
            'partimos de un estado ya parcheado; el test no probaría nada'
        importlib.import_module('v2_suite')
        assert getattr(an.n8n_switch_set_state, '_lm_guarded', False), \
            'importar v2_suite NO instala el guard'
        assert getattr(an.run_due_schedules, '_lm_guarded', False), \
            'importar v2_suite no envuelve el barrido del planificador'
    s.check('importar v2_suite instala el guard', guard_installed_on_import)

    def import_precedes_thread_in_server():
        """En server.py, el import de v2_suite tiene que estar ANTES de
        cualquier hilo capaz de encender workflows."""
        src = open(os.path.join(APP, 'server.py'), encoding='utf-8').read()
        lineas = src.splitlines()
        imp = next((i for i, l in enumerate(lineas)
                    if re.match(r'\s*import v2_suite', l)), None)
        assert imp is not None, 'server.py no importa v2_suite'
        hilos = [i for i, l in enumerate(lineas) if 'threading.Thread' in l]
        assert hilos, 'server.py no arranca ningún hilo (¿cambió el arranque?)'
        primero = min(hilos)
        assert imp < primero, (
            f'el primer hilo arranca en la línea {primero + 1} y v2_suite se '
            f'importa en la {imp + 1}: hay una ventana sin guard')
    s.check('el import de v2_suite precede al primer hilo en server.py',
            import_precedes_thread_in_server)

    def scheduler_thread_is_guarded_even_if_started_first():
        """Aunque alguien reordenara server.py, el hilo llama a
        run_due_schedules por el global del módulo, así que el guard lo
        alcanza igual. Cinturón y tirantes."""
        an = sys.modules['analytics']
        import inspect
        src = inspect.getsource(an.scheduler_loop)
        assert 'run_due_schedules' in src, \
            'el loop ya no llama a run_due_schedules: revisar el enganche'
        assert getattr(an.run_due_schedules, '_lm_guarded', False)
    s.check('el hilo resuelve run_due_schedules por el módulo, ya envuelto',
            scheduler_thread_is_guarded_even_if_started_first)

    # ══ B · tipo de excepción ════════════════════════════════════════
    s.section('B · un bloqueo viaja como el panel espera, no como un 500')

    import legacy_mode as lm                                     # noqa: E402

    def blocked_is_a_valueerror():
        assert issubclass(lm.ModeError, ValueError), \
            'ModeError no es ValueError: el panel daría HTTP 500'
        assert issubclass(lm.ActivationBlocked, ValueError)
    s.check('ActivationBlocked es ValueError', blocked_is_a_valueerror)

    def real_routes_catch_valueerror():
        """Las rutas reales del panel envuelven el encendido en
        `except ValueError`. Si no fuera así, lo anterior no bastaría."""
        src = open(os.path.join(APP, 'server.py'), encoding='utf-8').read()
        i = src.index("def callcenter_on(")
        bloque = src[i:i + 400]
        assert 'n8n_switch_set_state' in bloque and 'except ValueError' in bloque, \
            'la ruta real de encendido ya no captura ValueError'
    s.check('la ruta real de encendido captura ValueError',
            real_routes_catch_valueerror)

    def real_scheduler_catches_valueerror():
        src = open(os.path.join(APP, 'analytics.py'), encoding='utf-8').read()
        i = src.index('def run_due_schedules(')
        bloque = src[i:src.index('\ndef ', i + 10)]
        assert bloque.count('except ValueError') >= 2, \
            'el barrido real ya no captura ValueError en ON y OFF'
    s.check('el barrido real captura ValueError en ON y en OFF',
            real_scheduler_catches_valueerror)

    if not mysql_available():
        s.skip('camino real con base de datos', 'sin MariaDB')
        return s.finish()

    # ══ C · el barrido REAL ══════════════════════════════════════════
    s.section('C · analytics.run_due_schedules() real, con el guard puesto')

    an = sys.modules['analytics']
    NAME = 'lm_test_real_interlock'
    db = build(NAME)
    espia = N8nSpy()
    an._n8n_request = espia          # se simula SÓLO la red

    def real_sweep_blocks_dispatch():
        set_mode(db, 'V2_PRIMARY')
        db.execute("UPDATE n8n_switch_schedules SET last_error=NULL, last_on_date=NULL")
        espia.calls.clear()
        an.run_due_schedules(db)     # el barrido REAL

        activados = espia.activations
        for wid in ('wf1a', 'wf1b', 'wf3a', 'wf3b'):   # INDIA-NEPAL y MEXICO
            assert not any(wid in a for a in activados), \
                f'el barrido real activó {wid}, de un grupo DISPATCH, con V2_PRIMARY'
    s.check('V2_PRIMARY + ON programado de un DISPATCH → n8n NO recibe activate',
            real_sweep_blocks_dispatch)

    def other_schedules_keep_being_processed():
        """Lo que rompía el diseño anterior: un bloqueo que no fuera
        ValueError abortaba el barrido y los demás horarios se quedaban
        sin procesar."""
        activados = espia.activations
        assert any('wf2a' in a for a in activados), \
            ('tras bloquear el primer horario, el barrido no llegó a procesar '
             'el grupo CRM_SYNC: un horario bloqueado está rompiendo el resto')
    s.check('los demás horarios se siguen procesando tras un bloqueo',
            other_schedules_keep_being_processed)

    def last_error_is_readable():
        filas = db.q("""SELECT s.switch_id, w.label, s.last_error
                          FROM n8n_switch_schedules s
                          JOIN n8n_switches w ON w.id = s.switch_id
                         WHERE s.last_error IS NOT NULL
                           AND s.last_error <> ''""")
        por_grupo = {f['label']: f['last_error'] for f in filas}
        assert 'INDIA - NEPAL' in por_grupo, \
            'el horario bloqueado no guardó last_error'
        txt = por_grupo['INDIA - NEPAL']
        assert 'V2_PRIMARY' in txt, f'el last_error no dice el modo: {txt[:120]!r}'
        assert 'DISPATCH' in txt or 'blocked' in txt.lower(), \
            f'el last_error no explica el motivo: {txt[:120]!r}'
        assert 'PANEL' not in por_grupo, \
            'se marcó como error un grupo que sí podía encenderse'
    s.check('el horario bloqueado guarda un last_error legible',
            last_error_is_readable)

    def block_is_audited():
        filas = [f for f in lm.mode_audit(db, 200)
                 if f['action'] == 'ACTIVATION_BLOCKED']
        assert filas, 'un bloqueo del planificador no dejó auditoría'
    s.check('el bloqueo del planificador queda auditado', block_is_audited)

    def scheduled_off_still_runs():
        """Un OFF programado nunca se bloquea."""
        ahora = dt.datetime.now(ZoneInfo(TZ)).strftime('%H:%M')
        db.execute("UPDATE n8n_switch_schedules SET off_time=§, on_time='03:03', "
                   "last_off_date=NULL, last_error=NULL", (ahora,))
        espia.active.update({'wf1a', 'wf1b'})
        espia.calls.clear()
        an.run_due_schedules(db)
        desactivados = [p for m, p in espia.calls if p.endswith('/deactivate')]
        assert any('wf1a' in d for d in desactivados), \
            'un OFF programado de un grupo DISPATCH se bloqueó'
    s.check('un OFF programado se ejecuta aunque el grupo sea conflictivo',
            scheduled_off_still_runs)

    def legacy_mode_allows_dispatch_sweep():
        set_mode(db, 'LEGACY_BACKUP')
        ahora = dt.datetime.now(ZoneInfo(TZ)).strftime('%H:%M')
        db.execute("UPDATE n8n_switch_schedules SET on_time=§, off_time='03:03', "
                   "last_on_date=NULL, last_error=NULL", (ahora,))
        espia.calls.clear()
        an.run_due_schedules(db)
        assert any('wf1a' in a for a in espia.activations), \
            'en LEGACY_BACKUP el planificador sigue sin poder encender el legacy'
    s.check('en LEGACY_BACKUP el barrido real SÍ enciende el legacy',
            legacy_mode_allows_dispatch_sweep)

    def manual_on_is_a_controlled_error():
        """El encendido manual devuelve un error de usuario, no un 500."""
        set_mode(db, 'V2_PRIMARY')
        try:
            an.n8n_switch_set_state(db, 1, turn_on=True)
            raise AssertionError('se encendió un DISPATCH con V2_PRIMARY')
        except ValueError as ex:          # ← lo que la ruta real captura
            assert 'INDIA - NEPAL' in str(ex)
    s.check('el encendido manual bloqueado se captura como ValueError',
            manual_on_is_a_controlled_error)

    # ══ D/E · fail closed del modo ═══════════════════════════════════
    s.section('D/E · un modo ausente o corrupto NO autoriza a llamar')

    import routes_config as rc                                   # noqa: E402

    def missing_mode_fails_closed():
        db.execute("DELETE FROM app_settings WHERE setting_key='lm_operating_mode'")
        assert rc.operating_mode(db) == 'UNKNOWN'
        p = rc.routes_active_payload(db)
        assert p['count'] == 0, 'con el modo ausente se devuelven rutas invocables'
        assert p['dispatch_allowed'] is False
        assert p.get('error_code') == 'OPERATING_MODE_UNKNOWN'
    s.check('modo AUSENTE → cero rutas y OPERATING_MODE_UNKNOWN',
            missing_mode_fails_closed)

    def corrupt_mode_fails_closed():
        for basura in ('BASURA', '', 'v2primary', 'V2_PRIMARY_X', '1'):
            set_mode(db, basura)
            assert rc.operating_mode(db) == 'UNKNOWN', \
                f'{basura!r} se interpretó como un modo válido'
            assert rc.routes_active_payload(db)['count'] == 0
    s.check('modo CORRUPTO → cero rutas, en los cinco casos probados',
            corrupt_mode_fails_closed)

    def unreadable_mode_fails_closed():
        class DbRoto:
            driver = 'mysql'

            def one(self, *a, **kw):
                raise RuntimeError('tabla inaccesible')
        assert rc.operating_mode(DbRoto()) == 'UNKNOWN', \
            'un error al leer el modo se interpretó como permiso para llamar'
    s.check('si el modo NO se puede leer → UNKNOWN, no V2_PRIMARY',
            unreadable_mode_fails_closed)

    def config_is_still_visible():
        set_mode(db, 'BASURA')
        p = rc.routes_active_payload(db, include_all=True)
        assert p['count'] > 0, 'con el modo corrupto no se puede ni ver la config'
        assert all(not r['calling_now'] for r in p['routes'])
        assert any('OPERATING_MODE_UNKNOWN' in (r.get('blocked_by') or [])
                   for r in p['routes'])
    s.check('con el modo corrupto la configuración se sigue viendo, sin llamar',
            config_is_still_visible)

    # ══ F · panel real → nodo real de WF2 ════════════════════════════
    s.section('F · lo que el panel responde de verdad, leído por WF2 de verdad')

    def real_payload_into_the_real_wf2_node():
        """El extremo a extremo que ninguna de las dos mitades prueba sola.

        D/E comprueban lo que el panel responde. El nodo de WF2 se
        comprueba aparte con respuestas escritas a mano. Entre las dos
        queda un hueco: que la respuesta REAL del panel y el lector REAL
        de WF2 se entiendan. Aquí se toma el payload que produce
        `routes_active_payload()` con el modo ausente, corrupto y en
        LEGACY, y se mete tal cual en el `jsCode` del nodo.
        """
        import json
        import subprocess
        import tempfile

        wf2 = json.load(open(os.path.join(
            ROOT, 'workflows', 'TEMPLATE_WF2_CALL_DISPATCHER_V2.json'),
            encoding='utf-8'))
        nodo = next(n for n in wf2['nodes']
                    if n['type'] == 'n8n-nodes-base.code'
                    and 'operating_mode' in (n['parameters'].get('jsCode') or ''))
        js = nodo['parameters']['jsCode']

        def correr(body):
            arnes = ("const __routes = %s;\n"
                     "function $(name) { return { first: () => ({ json: __routes }) }; }\n"
                     "const $input = { first: () => ({ json: "
                     "{statusCode:200, body:{settings:{}}} }) };\n"
                     "const $execution = { id: 'test-exec' };\n"
                     "const __out = (function () { %s })();\n"
                     "console.log(JSON.stringify(__out[0].json));\n"
                     % (json.dumps({'statusCode': 200, 'body': body}), js))
            with tempfile.NamedTemporaryFile('w', suffix='.js', delete=False) as fh:
                fh.write(arnes)
                ruta = fh.name
            try:
                r = subprocess.run(['node', ruta], capture_output=True,
                                   text=True, timeout=60)
                assert r.returncode == 0, f'el nodo de WF2 falló: {r.stderr[:300]}'
                return json.loads(r.stdout.strip().splitlines()[-1])
            finally:
                os.unlink(ruta)

        # 1 · modo ausente — el caso del panel anterior
        db.execute("DELETE FROM app_settings WHERE setting_key='lm_operating_mode'")
        ausente = rc.routes_active_payload(db)
        r = correr(ausente)
        assert r['config_ok'] is False, \
            'con el modo ausente, WF2 se pone a despachar'
        assert r['error_code'] == 'OPERATING_MODE_UNKNOWN'
        assert r['route_count'] == 0

        # 2 · modo corrupto
        set_mode(db, 'BASURA')
        r = correr(rc.routes_active_payload(db))
        assert r['config_ok'] is False and r['route_count'] == 0, \
            'con el modo corrupto, WF2 despacha'

        # 3 · LEGACY_BACKUP con las rutas encendidas: el panel no las manda
        #     y WF2 tampoco llamaría aunque las mandara
        set_mode(db, 'LEGACY_BACKUP')
        legacy = rc.routes_active_payload(db)
        assert legacy['count'] == 0
        r = correr(legacy)
        assert r['config_ok'] is False and r['route_count'] == 0

        # 4 · el control positivo: en V2_PRIMARY SÍ se despacha, o este
        #     test estaría pasando porque WF2 nunca despacha nada
        set_mode(db, 'V2_PRIMARY')
        v2 = rc.routes_active_payload(db)
        assert v2['count'] > 0, 'el control positivo no tiene rutas que despachar'
        r = correr(v2)
        assert r['config_ok'] is True, \
            'en V2_PRIMARY el panel y WF2 no se entienden: no despacha nada'
        assert r['route_count'] == v2['count']
    s.check('payload real del panel + nodo real de WF2: sin modo, no se llama',
            real_payload_into_the_real_wf2_node)

    # ══ G · modo inicial ═════════════════════════════════════════════
    s.section('G · la instalación exige un cutover deliberado')

    def fresh_install_is_legacy_backup():
        N2 = 'lm_test_initial_mode'
        db2 = DB(mysql_conn(migrated_db(N2)))
        assert rc.operating_mode(db2) == 'LEGACY_BACKUP', \
            ('una instalación limpia arranca en V2_PRIMARY: se podría encender '
             'una ruta V2 sin pasar nunca por el preflight contra n8n')
        # mode=None: se encienden los tres interruptores SIN tocar el modo,
        # que es exactamente lo que alguien haría recién instalado.
        enable_seeded_baseline(N2, mode=None)
        assert rc.operating_mode(db2) == 'LEGACY_BACKUP', \
            'encender interruptores cambió el modo de operación'
        assert rc.routes_active_payload(db2)['count'] == 0, \
            'encender país, proveedor y ruta basta para llamar sin cutover'
    s.check('instalación limpia → LEGACY_BACKUP y cero rutas invocables',
            fresh_install_is_legacy_backup)

    def migration_does_not_promote_to_v2():
        import subprocess
        N3 = 'lm_test_no_promote'
        db3 = DB(mysql_conn(migrated_db(N3)))
        db3.execute("UPDATE app_settings SET setting_value='V2_PRIMARY' "
                    "WHERE setting_key='lm_operating_mode'")
        from _suite import run_sql_file
        run_sql_file(N3, os.path.join(ROOT, 'sql', 'migration.sql'))
        assert rc.operating_mode(db3) == 'V2_PRIMARY', \
            're-ejecutar la migración pisó el modo elegido por el usuario'
    s.check('re-ejecutar la migración no cambia un modo ya elegido',
            migration_does_not_promote_to_v2)

    # ══ H · el cutover exige verificación ════════════════════════════
    s.section('H · el cutover a V2 exige comprobar n8n de verdad')

    def cutover_requires_live_check():
        set_mode(db, 'LEGACY_BACKUP')
        espia.active.update({'wf1a'})            # INDIA - NEPAL encendido
        pf = lm.preflight(db, 'V2_PRIMARY', an)
        assert not pf['ok'], 'el cutover pasa con el despachador legacy vivo'
        assert any('INDIA - NEPAL' in b for b in pf['blockers'])
    s.check('con un DISPATCH legacy encendido, el cutover se bloquea',
            cutover_requires_live_check)

    def cutover_blocked_when_n8n_down():
        def cae(*a, **kw):
            raise ValueError('n8n unreachable: connection refused')
        original = an._n8n_request
        an._n8n_request = cae
        try:
            pf = lm.preflight(db, 'V2_PRIMARY', an)
            assert not pf['ok'], 'el cutover pasa sin poder consultar n8n'
            assert any('Cannot reach n8n' in b for b in pf['blockers'])
        finally:
            an._n8n_request = original
    s.check('con n8n caído, el cutover se bloquea (fail closed)',
            cutover_blocked_when_n8n_down)

    def cutover_proceeds_when_verified():
        espia.active.clear()
        espia.active.update({'wf2a'})            # sólo el CRM_SYNC
        r = lm.set_mode(db, 'tester', 'master', 'V2_PRIMARY',
                        confirmation='SWITCH TO V2', reason='cutover',
                        analytics_module=an)
        assert r['changed'] and rc.operating_mode(db) == 'V2_PRIMARY'
        assert rc.routes_active_payload(db)['count'] > 0, \
            'tras el cutover V2 sigue sin poder llamar'
    s.check('verificado contra n8n, el cutover procede y V2 puede llamar',
            cutover_proceeds_when_verified)

    # ══ I/J · credenciales en el código ══════════════════════════════
    s.section('I/J · ninguna contraseña por defecto en el código')

    PWD_FALLBACK = re.compile(
        r'''(?ix)(?:os\.)?(?:environ\.get|getenv)\s*\(\s*
            ["'][A-Z0-9_]*(?:PASS|SECRET|TOKEN|API_?KEY)[A-Z0-9_]*["']
            \s*,\s*["'][^"'\n]{4,}["']''')

    def scanner_detects_getenv_fallback():
        """El escáner reportaba 0 hallazgos mientras `server.py` tenía
        CUATRO contraseñas reales como fallback de os.getenv. Un escáner
        que no ve el caso real es peor que no tenerlo: da una confianza
        que no corresponde."""
        import subprocess
        import tempfile
        # Valores INVENTADOS. La contraseña real que había en server.py no
        # viaja ni siquiera aquí: un control positivo no necesita el
        # secreto de verdad para demostrar que el detector funciona.
        falsa = 'Not' + 'AllowedHere' + '987'
        sembrado = (
            'import os\n'
            'CFG = {\n'
            '    "MASTER_PASS": os.getenv("LM_MASTER_PASS", "' + falsa + '"),\n'
            '    "DB_PASS": os.getenv("LM_DB_PASS", "' + falsa + 'Db"),\n'
            '    "TOKEN": os.environ.get("LM_ROUTES_API_TOKEN", "' + falsa + 'Tk"),\n'
            '    "USER": os.getenv("LM_USER", "admin"),\n'
            '}\n')
        with tempfile.TemporaryDirectory() as tmp:
            with open(os.path.join(tmp, 'sembrado.py'), 'w') as fh:
                fh.write(sembrado)
            r = subprocess.run([sys.executable,
                                os.path.join(ROOT, 'tools', 'secret_scan.py')],
                               cwd=tmp, capture_output=True, text=True)
        assert r.returncode == 1, \
            'el escáner NO detecta una contraseña por defecto en os.getenv'
        for sufijo in ('', 'Db', 'Tk'):
            assert falsa + sufijo in r.stdout, \
                f'el escáner no vio la contraseña sembrada {falsa + sufijo}'
        assert '"value": "admin"' not in r.stdout, \
            'el escáner marca un nombre de usuario como secreto'
    s.check('el escáner detecta os.getenv("...PASS", "literal")',
            scanner_detects_getenv_fallback)

    def server_source_has_no_password_fallbacks():
        """Y el paquete real queda limpio."""
        malos = []
        for dp, dn, fn in os.walk(APP):
            dn[:] = [d for d in dn if d != '__pycache__']
            for f in fn:
                if not f.endswith('.py'):
                    continue
                ruta = os.path.join(dp, f)
                for m in PWD_FALLBACK.finditer(open(ruta, encoding='utf-8').read()):
                    malos.append(f'{f}: {m.group(0)[:60]}')
        assert not malos, 'contraseñas por defecto en el panel: ' + '; '.join(malos)
    s.check('el código del panel no tiene ni una contraseña por defecto',
            server_source_has_no_password_fallbacks)

    def missing_credential_stops_startup():
        """Una credencial que falta detiene el arranque, en vez de dejarlo
        correr con una contraseña que el operador no eligió."""
        import subprocess
        entorno = dict(os.environ)
        for v in ('LM_DB_PASS', 'LM_PASS', 'LM_MASTER_PASS', 'LM_SUPPORT_PASS'):
            entorno.pop(v, None)
        entorno.update(LM_SCHEDULER_DISABLED='1', LM_TELEGRAM_DISABLED='1')
        r = subprocess.run([sys.executable, '-c', 'import server'],
                           cwd=APP, env=entorno, capture_output=True, text=True)
        assert r.returncode != 0, \
            'el panel arranca sin credenciales: estaría usando las del código'
        salida = r.stdout + r.stderr
        assert 'LM_DB_PASS' in salida, 'no dice qué credencial falta'
        assert '.env.example' in salida, 'no dice dónde está la plantilla'
    s.check('sin credenciales, el panel NO arranca y dice cuál falta',
            missing_credential_stops_startup)

    def env_example_documents_every_required_var():
        ej = open(os.path.join(ROOT, 'panel', '.env.example'),
                  encoding='utf-8').read()
        faltan = [v for v in ('LM_DB_HOST', 'LM_DB_PORT', 'LM_DB_USER',
                              'LM_DB_PASS', 'LM_DB_NAME', 'LM_PORT',
                              'LM_USER', 'LM_PASS', 'LM_MASTER_USER',
                              'LM_MASTER_PASS', 'LM_SUPPORT_USER',
                              'LM_SUPPORT_PASS', 'LM_SECRET',
                              'LM_ROUTES_API_TOKEN', 'LM_N8N_BASE_URL',
                              'LM_N8N_API_KEY', 'LM_SCHEDULER_DISABLED',
                              'LM_TELEGRAM_DISABLED')
                  if v + '=' not in ej]
        assert not faltan, f'.env.example no documenta: {faltan}'
        con_valor = [l for l in ej.splitlines()
                     if re.match(r'^LM_\w*(PASS|SECRET|TOKEN|KEY)\w*=\S', l)]
        assert not con_valor, \
            f'.env.example trae valores en credenciales: {con_valor}'
    s.check('.env.example documenta todo, con las credenciales vacías',
            env_example_documents_every_required_var)

    # ══ K/L · el script de actualización ═════════════════════════════
    s.section('K/L · el script de actualización')

    UPG = os.path.join(ROOT, 'panel', 'upgrade_callcenter_v2.sh')
    BASE_REAL = '/home/claude/src/panel-real/panel-share'

    def installer_merges_the_env_instead_of_rewriting_it():
        """L (instalador): el .env se fusiona, no se reescribe.

        El script de actualización es una vía; la otra es volver a correr
        `install.sh` sobre una instalación que ya funciona, que es lo que
        alguien hace cuando algo va mal. La versión anterior hacía
        `cat > .env` con nueve variables: borraba LM_MASTER_PASS,
        LM_SUPPORT_PASS, LM_ROUTES_API_TOKEN y la configuración de n8n, y
        regeneraba LM_SECRET deslogueando a todo el mundo.

        Aquí se ejecuta el TROZO REAL del install.sh que maneja el .env
        —recortado del fichero que se va a entregar, no una copia— sobre
        un .env que ya tiene de todo.
        """
        import subprocess
        import tempfile

        INST = os.path.join(ROOT, 'panel', 'deploy', 'install.sh')
        texto = open(INST, encoding='utf-8').read()
        ini = texto.index('ENV_FILE="$APP_DIR/.env"')
        fin = texto.index('c_head "6/6')
        trozo = texto[ini:fin]
        assert 'env_set_if_empty' in trozo and 'MISSING_CREDS' in trozo, \
            'el recorte no contiene el manejo del .env: el test no probaría nada'

        YA_CONFIGURADO = (
            "LM_DB_HOST=localhost\n"
            "LM_DB_USER=panel_rw\n"
            "LM_DB_PASS=vieja-de-la-bd\n"
            "LM_DB_NAME=asterisk\n"
            "LM_PORT=8080\n"
            "LM_USER=admin\n"
            "LM_PASS=la-del-viewer\n"
            "LM_SECRET=" + "a" * 64 + "\n"
            "LM_MASTER_PASS=la-del-master\n"
            "LM_SUPPORT_PASS=la-de-support\n"
            "LM_ROUTES_API_TOKEN=" + "b" * 64 + "\n"
            "LM_N8N_BASE_URL=http://127.0.0.1:5678\n"
            "LM_N8N_API_KEY=clave-de-n8n\n"
            "LM_VARIABLE_QUE_ALGUIEN_ANADIO=no-me-borres\n")

        with tempfile.TemporaryDirectory() as tmp:
            env = os.path.join(tmp, '.env')
            open(env, 'w').write(YA_CONFIGURADO)
            arnes = f"""set -euo pipefail
APP_DIR={tmp}
DB_USER=panel_rw
DB_PASS=nueva-de-la-bd
DB_NAME=asterisk
PORT=8080
PANEL_USER=admin
PANEL_PASS=una-generada-nueva
SECRET={'c' * 64}
MISSING_CREDS=()
MISSING_N8N=()
c_ok()   {{ echo "ok $*"; }}
c_info() {{ echo "info $*"; }}
c_warn() {{ echo "warn $*"; }}
{trozo}
"""
            r = subprocess.run(['bash', '-c', arnes], capture_output=True,
                               text=True, timeout=120)
            assert r.returncode == 0, f'el trozo falló: {r.stderr[-400:]}'

            despues = dict(
                ln.split('=', 1) for ln in open(env).read().splitlines()
                if '=' in ln and not ln.startswith('#'))

            # Nada de lo ya configurado se resetea. Los valores de abajo
            # son los del .env inventado tres líneas más arriba: no son
            # credenciales de nada, son la prueba de que no se pisaron.
            intactas = {
                # SECSCAN-OK: valor inventado del .env de prueba
                'LM_PASS': 'la-del-viewer',
                'LM_SECRET': 'a' * 64,
                # SECSCAN-OK: valor inventado del .env de prueba
                'LM_MASTER_PASS': 'la-del-master',
                # SECSCAN-OK: valor inventado del .env de prueba
                'LM_SUPPORT_PASS': 'la-de-support',
                'LM_ROUTES_API_TOKEN': 'b' * 64,
                'LM_N8N_BASE_URL': 'http://127.0.0.1:5678',
                # SECSCAN-OK: valor inventado del .env de prueba
                'LM_N8N_API_KEY': 'clave-de-n8n',
                'LM_VARIABLE_QUE_ALGUIEN_ANADIO': 'no-me-borres',
            }
            rotas = {k: (v, despues.get(k)) for k, v in intactas.items()
                     if despues.get(k) != v}
            assert not rotas, f'el instalador pisó variables ya configuradas: {rotas}'

            # Lo que el instalador SÍ aporta se actualiza.
            assert despues['LM_DB_PASS'] == 'nueva-de-la-bd', \
                'el instalador no actualizó la contraseña de la BD que le dieron'

            # Y queda un respaldo del anterior.
            bak = [f for f in os.listdir(tmp) if f.startswith('.env.bak.')]
            assert bak, 'el instalador no respaldó el .env anterior'
            assert open(os.path.join(tmp, bak[0])).read() == YA_CONFIGURADO, \
                'el respaldo no es el .env anterior'

        # Sobre una instalación NUEVA, las credenciales que el instalador
        # no inventa quedan vacías y se reclaman por pantalla.
        with tempfile.TemporaryDirectory() as tmp:
            r = subprocess.run(['bash', '-c', arnes.replace(
                f'APP_DIR={os.path.dirname(env)}', f'APP_DIR={tmp}')],
                capture_output=True, text=True, timeout=120)
            assert r.returncode == 0, f'instalación nueva falló: {r.stderr[-300:]}'
            nuevo = dict(
                ln.split('=', 1) for ln in
                open(os.path.join(tmp, '.env')).read().splitlines() if '=' in ln)
            for vacia in ('LM_MASTER_PASS', 'LM_SUPPORT_PASS'):
                assert nuevo.get(vacia) == '', \
                    f'{vacia} se inventó una contraseña en vez de reclamarla'
            assert 'FALTAN credenciales' in r.stdout, \
                'no se avisa de las credenciales que faltan'
            assert 'una-generada-nueva' not in r.stdout, \
                'el instalador imprime la contraseña generada en los logs'
    s.check('install.sh fusiona el .env: Master, Support y n8n sobreviven',
            installer_merges_the_env_instead_of_rewriting_it)

    def upgrade_script_exists_and_parses():
        import subprocess
        assert os.path.exists(UPG), 'falta panel/upgrade_callcenter_v2.sh'
        r = subprocess.run(['bash', '-n', UPG], capture_output=True, text=True)
        assert r.returncode == 0, f'el script no parsea: {r.stderr[:200]}'
    s.check('el script existe y su sintaxis es válida',
            upgrade_script_exists_and_parses)

    def dry_run_changes_nothing():
        """K: en modo prueba no puede escribir ni un byte."""
        import hashlib
        import shutil
        import subprocess
        import tempfile
        base = BASE_REAL if os.path.isdir(BASE_REAL) else os.path.join(ROOT, 'panel')
        with tempfile.TemporaryDirectory() as tmp:
            destino = os.path.join(tmp, 'panel')
            shutil.copytree(base, destino,
                            ignore=shutil.ignore_patterns('__pycache__'))
            with open(os.path.join(destino, '.env'), 'w') as fh:
                fh.write('LM_MASTER_PASS=noTocar\nLM_N8N_API_KEY=noTocar\n')

            def huella():
                h = hashlib.sha256()
                for dp, dn, fn in sorted(os.walk(destino)):
                    dn[:] = sorted(d for d in dn if d != '__pycache__')
                    for f in sorted(fn):
                        ruta = os.path.join(dp, f)
                        h.update(os.path.relpath(ruta, destino).encode())
                        h.update(open(ruta, 'rb').read())
                return h.hexdigest()

            antes = huella()
            r = subprocess.run(['bash', UPG], capture_output=True, text=True,
                               env=dict(os.environ, DRY_RUN='1',
                                        LM_APP_DIR=destino,
                                        LM_BACKUP_DIR=os.path.join(tmp, 'bk')),
                               timeout=180)
            assert r.returncode == 0, f'el modo prueba falló: {r.stdout[-400:]}'
            assert huella() == antes, 'el modo prueba MODIFICÓ ficheros'
            assert not os.path.exists(os.path.join(tmp, 'bk')), \
                'el modo prueba creó un respaldo'
            assert 'NUEVO' in r.stdout, 'el modo prueba no enseña qué cambiaría'
    s.check('DRY_RUN=1 no modifica ni un fichero', dry_run_changes_nothing)

    def upgrade_preserves_env_and_data():
        """L: Master, Support y n8n sobreviven a la actualización."""
        import hashlib
        import shutil
        import subprocess
        import tempfile
        if not os.path.isdir(BASE_REAL):
            raise AssertionError('SKIP')
        with tempfile.TemporaryDirectory() as tmp:
            destino = os.path.join(tmp, 'panel')
            shutil.copytree(BASE_REAL, destino,
                            ignore=shutil.ignore_patterns('__pycache__'))
            env_txt = ('LM_DB_PASS=ClaveProd\nLM_PASS=ViewerProd\n'
                       'LM_MASTER_PASS=MasterProd\nLM_SUPPORT_PASS=SupportProd\n'
                       'LM_SECRET=secreto-vivo\nLM_ROUTES_API_TOKEN=token-vivo\n'
                       'LM_N8N_BASE_URL=https://n8n.example\n'
                       'LM_N8N_API_KEY=clave-n8n\n'
                       'LM_VARIABLE_PROPIA=valor-propio\n')
            with open(os.path.join(destino, '.env'), 'w') as fh:
                fh.write(env_txt)
            os.makedirs(os.path.join(destino, 'logs'), exist_ok=True)
            with open(os.path.join(destino, 'logs', 'panel.log'), 'w') as fh:
                fh.write('log antiguo')

            r = subprocess.run(['bash', UPG], capture_output=True, text=True,
                               env=dict(os.environ, LM_APP_DIR=destino,
                                        LM_BACKUP_DIR=os.path.join(tmp, 'bk'),
                                        LM_SERVICE='no-existe'),
                               timeout=300)
            assert r.returncode == 0, f'la actualización falló: {r.stdout[-600:]}'

            final = open(os.path.join(destino, '.env'), encoding='utf-8').read()
            for linea in env_txt.strip().splitlines():
                assert linea in final, \
                    f'la actualización perdió {linea.split("=")[0]}'
            assert os.path.exists(os.path.join(destino, 'logs', 'panel.log')), \
                'la actualización borró los logs'
            for m in ('legacy_mode.py', 'routes_config.py', 'v2_suite.py',
                      'billing.py', 'payments.py', 'analytics_v2.py'):
                assert os.path.exists(os.path.join(destino, 'app', m)), \
                    f'la actualización no copió {m}'
            for t in ('legacy.html', 'billing.html', 'analytics.html'):
                assert os.path.exists(os.path.join(destino, 'app', 'templates', t))

            def sha(p):
                return hashlib.sha256(open(p, 'rb').read()).hexdigest()
            for f in ('app/analytics.py', 'app/templates/extensions.html',
                      'app/templates/support.html', 'app/templates/dashboard.html'):
                assert sha(os.path.join(destino, f)) == sha(os.path.join(BASE_REAL, f)), \
                    f'la actualización modificó {f}'
            assert os.path.isdir(os.path.join(tmp, 'bk')), 'no se creó respaldo'
    s.check('la actualización preserva .env, logs y datos, y copia V2',
            upgrade_preserves_env_and_data)

    def patch_manifest_matches_reality():
        """El inventario de despliegue se genera, no se escribe a mano."""
        pm = os.path.join(ROOT, 'PANEL_PRODUCTION_PATCH_MANIFEST.md')
        assert os.path.exists(pm), 'falta PANEL_PRODUCTION_PATCH_MANIFEST.md'
        txt = open(pm, encoding='utf-8').read()
        for m in ('routes_config.py', 'legacy_mode.py', 'v2_suite.py',
                  'billing.py', 'payments.py', 'analytics_v2.py',
                  'call_jobs.py', 'followup_engine.py', 'ops_events.py',
                  'crm_notes.py', 'wf_settings.py', 'tool_requests.py',
                  'recording_ledger.py'):
            assert m in txt, f'el inventario no menciona {m}'
        assert 'upgrade_callcenter_v2.sh' in txt
        sin_motivo = [c for c in re.findall(r'\| `[^`]+` \| ([^|]*) \|', txt)
                      if c.strip() in ('—', '')]
        assert not sin_motivo, \
            f'{len(sin_motivo)} ficheros nuevos sin explicar qué aportan'
    s.check('el inventario de parche nombra los 13 módulos nuevos',
            patch_manifest_matches_reality)

    return s.finish()


if __name__ == '__main__':
    sys.exit(main())
