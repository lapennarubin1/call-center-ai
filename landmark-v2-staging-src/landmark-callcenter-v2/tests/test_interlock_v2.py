#!/usr/bin/env python3
"""EL ENCLAVAMIENTO — que el modo se cumpla, no que se muestre.

La versión anterior mostraba el modo en su pantalla y hacía un preflight
al cambiarlo. No bastaba: con V2_PRIMARY puesto, un horario legacy podía
despertar al despachador viejo a las 09:00 y llamar a los mismos
clientes. El cartel decía una cosa y el sistema hacía otra.

Estas pruebas recorren el CAMINO REAL:
  · la función que enciende de verdad (`n8n_switch_set_state`)
  · el barrido del planificador (`run_due_schedules`)
  · el payload que consume WF2 (`routes_active_payload`)
  · el código JS del nodo de WF2, ejecutado bajo node

No se da por buena ninguna propiedad porque la documente un comentario.
"""
import json
import os
import re
import sys

sys.dont_write_bytecode = True
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from _suite import (Suite, ROOT, WORKFLOWS, mysql_available, migrated_db,   # noqa: E402
                    mysql_conn, DB, enable_seeded_baseline, run_node)

import legacy_mode as lm                                                    # noqa: E402
import routes_config as rc                                                  # noqa: E402

GROUPS = [
    ('INDIA - NEPAL', 'DISPATCH'),
    ('MEXICO', 'DISPATCH'),
    ('PANEL', 'CRM_SYNC'),
    ('CRM PANEL', 'CRM_SYNC'),
    ('GRUPO SIN CLASIFICAR', None),
]


class FakeAnalytics:
    """Un `analytics` de mentira que registra lo que se le pide y simula
    n8n. Permite probar el guard sin una instancia de n8n de verdad —
    incluyendo el caso que más importa: n8n caído."""

    def __init__(self, active_workflows=(), reachable=True):
        self.calls = []
        self.active = set(active_workflows)
        self.reachable = reachable
        self.switch_rows = {}

    # lo que el guard envuelve
    def n8n_switch_set_state(self, db, switch_id, turn_on):
        self.calls.append((switch_id, turn_on))
        row = db.one("SELECT workflow_ids FROM n8n_switches WHERE id=§", (switch_id,))
        for wid in (row.get('workflow_ids') or '').split(','):
            if not wid:
                continue
            if turn_on:
                self.active.add(wid)
            else:
                self.active.discard(wid)
        return {'changed': True}

    def n8n_switches_with_status(self, db):
        if not self.reachable:
            return [], 'n8n unreachable: connection refused'
        out = []
        for r in db.q("SELECT id, label, workflow_ids FROM n8n_switches ORDER BY id"):
            ids = [w for w in (r['workflow_ids'] or '').split(',') if w]
            out.append({'id': r['id'], 'label': r['label'],
                        'workflow_ids': ids,
                        'workflows': [{'id': w, 'name': f'wf {w}',
                                       'active': w in self.active} for w in ids]})
        return out, None

    def run_due_schedules(self, db):
        """Reproduce lo esencial del barrido real: para cada horario
        habilitado dispara ON y guarda el error en last_error."""
        for sc in db.q("SELECT id, switch_id FROM n8n_switch_schedules WHERE enabled=1"):
            try:
                self.n8n_switch_set_state(db, sc['switch_id'], True)
                db.execute("UPDATE n8n_switch_schedules SET last_error=NULL WHERE id=§",
                           (sc['id'],))
            except Exception as ex:
                db.execute("UPDATE n8n_switch_schedules SET last_error=§ WHERE id=§",
                           (str(ex)[:500], sc['id']))


def build(name):
    migrated_db(name)
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
        for i, (label, _cat) in enumerate(GROUPS, start=1):
            cur.execute("INSERT INTO n8n_switches (id, label, workflow_ids) "
                        "VALUES (%s,%s,%s)", (i, label, f'wf{i}a,wf{i}b'))
            cur.execute("""INSERT INTO n8n_switch_schedules
                (switch_id, timezone, on_time, off_time, days, enabled)
                VALUES (%s,'Asia/Kolkata','09:00','20:00','1,2,3,4,5',1)""", (i,))
    c.close()
    db = DB(mysql_conn(name))
    # clasificar los conocidos; el último queda UNKNOWN a propósito
    for label, cat in GROUPS:
        if cat:
            db.execute("""INSERT INTO legacy_group_classification
                            (switch_label, category, coexists_with_v2, classified_by)
                          VALUES (§,§,§,'test')
                          ON DUPLICATE KEY UPDATE category=VALUES(category),
                            coexists_with_v2=VALUES(coexists_with_v2)""",
                       (label, cat, 1 if cat in lm.COEXISTING_CATEGORIES else 0))
    db.execute("""UPDATE legacy_group_classification g
                    JOIN n8n_switches s ON s.label = g.switch_label
                    SET g.switch_id = s.id""")
    db.execute("DELETE FROM legacy_group_classification WHERE switch_label='GRUPO SIN CLASIFICAR'")
    return db


def set_mode_raw(db, mode):
    db.execute("UPDATE app_settings SET setting_value=§ WHERE setting_key='lm_operating_mode'",
               (mode,))


def sid(db, label):
    return db.one("SELECT id FROM n8n_switches WHERE label=§", (label,))['id']


def main():
    s = Suite('enclavamiento del modo de operación')

    # ══ el nodo de WF2, ejecutado de verdad ══════════════════════════
    s.section('WF2 no despacha en LEGACY_BACKUP (código real del nodo)')

    wf2 = json.load(open(os.path.join(WORKFLOWS,
                    'TEMPLATE_WF2_CALL_DISPATCHER_V2.json'), encoding='utf-8'))
    nodo = next((n for n in wf2['nodes']
                 if n['name'] == '[CONFIG] Check Config Response'), None)

    def wf2_node_exists():
        assert nodo, 'no existe el nodo que comprueba la configuración en WF2'
        assert 'operating_mode' in nodo['parameters']['jsCode'], \
            'el nodo de WF2 no mira operating_mode'
    s.check('WF2 tiene un nodo que lee operating_mode', wf2_node_exists)

    def run_wf2_node(rbody):
        """Ejecuta el JS REAL del nodo bajo node, con $() simulado."""
        js = nodo['parameters']['jsCode']
        harness = f"""
const __routes = {json.dumps({'statusCode': 200, 'body': rbody})};
const __settings = {json.dumps({'statusCode': 200, 'body': {'settings': {}}})};
function $(name) {{
  if (name === '[CONFIG] Load Active Routes')
    return {{ first: () => ({{ json: __routes }}) }};
  throw new Error('nodo inesperado: ' + name);
}}
const $input = {{ first: () => ({{ json: __settings }}) }};
const $execution = {{ id: 'test-exec' }};
const __out = (function () {{ {js} }})();
console.log(JSON.stringify(__out[0].json));
"""
        return run_node('', harness)

    def wf2_blocks_in_legacy():
        r = run_wf2_node({'operating_mode': 'LEGACY_BACKUP', 'dispatch_allowed': False,
                          'routes': [], 'blocked_reason': 'mode is LEGACY_BACKUP'})
        assert r['config_ok'] is False, 'WF2 seguiría despachando en LEGACY_BACKUP'
        assert r['error_code'] == 'OPERATING_MODE_BLOCKED', \
            f"WF2 abortó con {r['error_code']}, no por el modo"
        assert r['route_count'] == 0
    s.check('el nodo real de WF2 aborta con OPERATING_MODE_BLOCKED en LEGACY',
            wf2_blocks_in_legacy)

    def wf2_blocks_even_if_routes_arrive():
        """Defensa en profundidad: aunque el panel mandase rutas por error,
        WF2 no las usa si el modo lo prohíbe."""
        r = run_wf2_node({'operating_mode': 'LEGACY_BACKUP', 'dispatch_allowed': False,
                          'routes': [{'route_key': 'IN_PROVEEDOR1', 'calling_now': True}]})
        assert r['config_ok'] is False and r['route_count'] == 0, \
            'WF2 despacharía rutas que el panel mandó por error en LEGACY_BACKUP'
    s.check('WF2 ignora rutas recibidas por error cuando el modo lo prohíbe',
            wf2_blocks_even_if_routes_arrive)

    def wf2_dispatches_in_v2_primary():
        r = run_wf2_node({'operating_mode': 'V2_PRIMARY', 'dispatch_allowed': True,
                          'routes': [{'route_key': 'IN_PROVEEDOR1', 'calling_now': True}]})
        assert r['config_ok'] is True, 'WF2 no despacha en V2_PRIMARY con rutas activas'
        assert r['error_code'] is None
    s.check('en V2_PRIMARY con rutas activas, WF2 sí despacha',
            wf2_dispatches_in_v2_primary)

    def wf2_refuses_an_answer_without_a_mode():
        """Una respuesta sin `operating_mode` NO autoriza a llamar.

        Esta prueba afirmaba lo contrario: que WF2 asumiera `V2_PRIMARY`
        para seguir siendo compatible con un panel anterior. Era una
        puerta abierta con forma de cortesía. Los dos casos en los que
        una respuesta llega sin modo son exactamente los dos en los que
        no se puede llamar:

          · el panel es anterior, y entonces no sabe nada del modo ni
            del enclavamiento: puede estar el legacy despachando;
          · alguien está apuntando WF2 a algo que no es el panel.

        No saber en qué modo está el sistema no es permiso para llamar.
        Se aborta con CONFIG_ERROR / OPERATING_MODE_UNKNOWN y no se
        despacha ninguna ruta, aunque vengan rutas en la respuesta.
        """
        r = run_wf2_node({'routes': [{'route_key': 'IN_PROVEEDOR1',
                                      'calling_now': True}]})
        assert r['config_ok'] is False, \
            'una respuesta sin operating_mode autoriza a despachar'
        assert r['error_code'] == 'OPERATING_MODE_UNKNOWN', \
            f"abortó con {r['error_code']}, no por modo desconocido"
        assert r['route_count'] == 0, \
            'despacha rutas de una respuesta sin modo'
    s.check('una respuesta sin operating_mode no autoriza a llamar',
            wf2_refuses_an_answer_without_a_mode)

    def wf2_refuses_an_invalid_mode():
        """Un valor que no es ninguno de los dos modos tampoco vale."""
        for malo in ('', 'v2primary', 'PRIMARY', 'TRUE', None):
            r = run_wf2_node({'operating_mode': malo, 'dispatch_allowed': True,
                              'routes': [{'route_key': 'IN_PROVEEDOR1',
                                          'calling_now': True}]})
            assert r['config_ok'] is False and r['route_count'] == 0, \
                f'operating_mode={malo!r} dejó despachar'

        # Y el modo correcto sin la autorización explícita tampoco:
        r = run_wf2_node({'operating_mode': 'V2_PRIMARY',
                          'routes': [{'route_key': 'IN_PROVEEDOR1',
                                      'calling_now': True}]})
        assert r['config_ok'] is False and r['route_count'] == 0, \
            'falta dispatch_allowed y aun así despacha'
    s.check('un modo inválido o incompleto tampoco autoriza a llamar',
            wf2_refuses_an_invalid_mode)

    if not mysql_available():
        s.skip('enclavamiento en base de datos', 'sin MariaDB')
        return s.finish()

    NAME = 'lm_test_interlock'
    db = build(NAME)
    enable_seeded_baseline(NAME)

    # ══ /api/routes/active ═══════════════════════════════════════════
    s.section('/api/routes/active obedece al modo')

    def v2_primary_has_callable_routes():
        set_mode_raw(db, 'V2_PRIMARY')
        p = rc.routes_active_payload(db)
        assert p['count'] > 0, 'en V2_PRIMARY no hay ninguna ruta invocable'
        assert p['operating_mode'] == 'V2_PRIMARY'
        assert p['dispatch_allowed'] is True
    s.check('en V2_PRIMARY hay rutas invocables', v2_primary_has_callable_routes)

    def legacy_backup_zero_routes():
        set_mode_raw(db, 'LEGACY_BACKUP')
        p = rc.routes_active_payload(db)
        assert p['count'] == 0, f'en LEGACY_BACKUP devuelve {p["count"]} rutas'
        assert p['dispatch_allowed'] is False
        assert 'LEGACY_BACKUP' in p.get('blocked_reason', '')
    s.check('en LEGACY_BACKUP devuelve CERO rutas invocables',
            legacy_backup_zero_routes)

    def enabling_everything_still_zero():
        """El caso que pide §1: encender país, proveedor y ruta en
        LEGACY_BACKUP no puede producir una ruta invocable."""
        set_mode_raw(db, 'LEGACY_BACKUP')
        db.execute("UPDATE countries SET enabled=1")
        db.execute("UPDATE voice_providers SET enabled=1")
        db.execute("UPDATE call_routes SET enabled=1 WHERE archived_at IS NULL")
        p = rc.routes_active_payload(db)
        assert p['count'] == 0, \
            f'encender todo en LEGACY_BACKUP produjo {p["count"]} rutas invocables'
    s.check('con país, proveedor y ruta encendidos en LEGACY: sigue en cero',
            enabling_everything_still_zero)

    def config_is_still_readable():
        """Editar y leer configuración sigue permitido: sólo llamar no."""
        p = rc.routes_active_payload(db, include_all=True)
        assert p['count'] > 0, 'en LEGACY_BACKUP no se puede ni leer la configuración'
        assert all(not r['calling_now'] for r in p['routes'])
        assert any('OPERATING_MODE_LEGACY_BACKUP' in (r.get('blocked_by') or [])
                   for r in p['routes']), 'no se dice que el motivo es el modo'
    s.check('la configuración se sigue viendo, y dice que el motivo es el modo',
            config_is_still_readable)

    def ready_is_still_allowed():
        p = rc.routes_active_payload(db, include_all=True)
        assert any(r.get('ready') for r in p['routes']), \
            'en LEGACY_BACKUP ninguna ruta puede figurar READY'
    s.check('una ruta puede seguir siendo READY en LEGACY_BACKUP',
            ready_is_still_allowed)

    # ══ el guard sobre el encendido ══════════════════════════════════
    s.section('el guard bloquea el encendido manual (§1)')

    fake = FakeAnalytics()
    lm.install_activation_guard(fake, None)

    def guard_is_installed():
        assert getattr(fake.n8n_switch_set_state, '_lm_guarded', False), \
            'el guard no se instaló sobre la función que enciende'
    s.check('el guard envuelve la función real de encendido', guard_is_installed)

    def manual_dispatch_blocked_in_v2():
        set_mode_raw(db, 'V2_PRIMARY')
        antes = len(fake.calls)
        try:
            fake.n8n_switch_set_state(db, sid(db, 'INDIA - NEPAL'), True)
            raise AssertionError('se encendió un grupo DISPATCH con V2_PRIMARY')
        except lm.ActivationBlocked as ex:
            assert 'INDIA - NEPAL' in str(ex) and 'DISPATCH' in str(ex)
        assert len(fake.calls) == antes, \
            'el encendido llegó a ejecutarse pese al bloqueo'
    s.check('V2_PRIMARY + encendido manual de un DISPATCH → bloqueado',
            manual_dispatch_blocked_in_v2)

    def unknown_blocked_in_v2():
        try:
            fake.n8n_switch_set_state(db, sid(db, 'GRUPO SIN CLASIFICAR'), True)
            raise AssertionError('se encendió un grupo sin clasificar')
        except lm.ActivationBlocked as ex:
            assert 'not classified' in str(ex)
    s.check('V2_PRIMARY + grupo sin clasificar → bloqueado (fail closed)',
            unknown_blocked_in_v2)

    def crm_sync_allowed_in_v2():
        antes = len(fake.calls)
        fake.n8n_switch_set_state(db, sid(db, 'PANEL'), True)
        assert len(fake.calls) == antes + 1, 'se bloqueó un CRM_SYNC, que sí convive'
    s.check('V2_PRIMARY + encendido de un CRM_SYNC → permitido',
            crm_sync_allowed_in_v2)

    def turning_off_is_never_blocked():
        set_mode_raw(db, 'V2_PRIMARY')
        antes = len(fake.calls)
        fake.n8n_switch_set_state(db, sid(db, 'INDIA - NEPAL'), False)
        assert len(fake.calls) == antes + 1, 'se bloqueó un APAGADO'
    s.check('apagar nunca se bloquea, en ningún modo',
            turning_off_is_never_blocked)

    def dispatch_allowed_in_legacy_mode():
        set_mode_raw(db, 'LEGACY_BACKUP')
        antes = len(fake.calls)
        fake.n8n_switch_set_state(db, sid(db, 'INDIA - NEPAL'), True)
        assert len(fake.calls) == antes + 1, \
            'no se deja encender el despachador legacy en LEGACY_BACKUP'
        fake.n8n_switch_set_state(db, sid(db, 'INDIA - NEPAL'), False)
    s.check('en LEGACY_BACKUP sí se puede encender el despachador legacy',
            dispatch_allowed_in_legacy_mode)

    def block_is_audited():
        set_mode_raw(db, 'V2_PRIMARY')
        antes = len(lm.mode_audit(db, 200))
        try:
            fake.n8n_switch_set_state(db, sid(db, 'MEXICO'), True)
        except lm.ActivationBlocked:
            pass
        filas = [f for f in lm.mode_audit(db, 200)
                 if f['action'] == 'ACTIVATION_BLOCKED']
        assert filas, 'un bloqueo de encendido no dejó rastro'
        assert len(lm.mode_audit(db, 200)) > antes
    s.check('cada bloqueo queda auditado', block_is_audited)

    def guard_is_idempotent():
        antes = fake.n8n_switch_set_state
        lm.install_activation_guard(fake, None)
        assert fake.n8n_switch_set_state is antes, \
            'instalar el guard dos veces anida envolturas'
    s.check('instalar el guard dos veces no anida envolturas',
            guard_is_idempotent)

    # ══ el planificador ══════════════════════════════════════════════
    s.section('el planificador no despierta lo que no debe (§1)')

    def scheduled_dispatch_blocked():
        set_mode_raw(db, 'V2_PRIMARY')
        db.execute("UPDATE n8n_switch_schedules SET last_error=NULL")
        fake.active.clear()
        fake.run_due_schedules(db)
        encendidos = fake.active
        for wid in ('wf1a', 'wf1b', 'wf2a', 'wf2b'):     # INDIA-NEPAL y MEXICO
            assert wid not in encendidos, \
                f'el planificador encendió {wid}, de un grupo DISPATCH, con V2_PRIMARY'
    s.check('V2_PRIMARY + ON programado de un DISPATCH → NO se enciende',
            scheduled_dispatch_blocked)

    def scheduled_crm_sync_allowed():
        assert 'wf3a' in fake.active, \
            'el planificador bloqueó un CRM_SYNC, que sí puede convivir'
    s.check('V2_PRIMARY + ON programado de un CRM_SYNC → sí se enciende',
            scheduled_crm_sync_allowed)

    def blocked_schedule_says_why():
        lm._annotate_blocked_schedules(db)
        filas = db.q("SELECT switch_id, last_error FROM n8n_switch_schedules "
                     "WHERE last_error IS NOT NULL")
        assert filas, 'un ON bloqueado no dejó last_error en el horario'
        textos = ' | '.join(f['last_error'] for f in filas)
        assert 'Scheduled activation blocked' in textos, \
            f'el last_error no explica el bloqueo: {textos[:160]}'
        assert 'V2_PRIMARY' in textos
    s.check("el horario bloqueado guarda 'Scheduled activation blocked: …'",
            blocked_schedule_says_why)

    def scheduled_off_always_runs():
        set_mode_raw(db, 'V2_PRIMARY')
        fake.active.update({'wf1a', 'wf1b'})
        fake.n8n_switch_set_state(db, sid(db, 'INDIA - NEPAL'), False)
        assert 'wf1a' not in fake.active, 'un apagado programado no se ejecutó'
    s.check('un OFF programado se ejecuta siempre', scheduled_off_always_runs)

    # ══ volver a V2: verificación REAL ═══════════════════════════════
    s.section('volver a V2 se verifica contra n8n, no se pregunta (§1)')

    def blocked_when_conflicting_workflow_is_actually_on():
        set_mode_raw(db, 'LEGACY_BACKUP')
        vivo = FakeAnalytics(active_workflows=['wf1a'])   # INDIA-NEPAL encendido
        pf = lm.preflight(db, 'V2_PRIMARY', vivo)
        assert not pf['ok'], 'se permite volver a V2 con el despachador legacy vivo'
        assert any('INDIA - NEPAL' in b for b in pf['blockers']), \
            'el bloqueo no nombra el grupo que sigue corriendo'
    s.check('un workflow legacy conflictivo ENCENDIDO bloquea la vuelta a V2',
            blocked_when_conflicting_workflow_is_actually_on)

    def blocked_when_n8n_unreachable():
        caido = FakeAnalytics(reachable=False)
        pf = lm.preflight(db, 'V2_PRIMARY', caido)
        assert not pf['ok'], 'se permite volver a V2 sin poder verificar n8n'
        assert any('Cannot reach n8n' in b for b in pf['blockers']), \
            'no se dice que el bloqueo es por no poder verificar'
    s.check('n8n inalcanzable bloquea la vuelta a V2 (fail closed)',
            blocked_when_n8n_unreachable)

    def set_mode_refuses_when_n8n_is_down():
        caido = FakeAnalytics(reachable=False)
        try:
            lm.set_mode(db, 'tester', 'master', 'V2_PRIMARY',
                        confirmation='SWITCH TO V2', analytics_module=caido)
            raise AssertionError('cambió a V2 sin poder verificar n8n')
        except lm.ModeError as ex:
            assert 'Cannot reach n8n' in str(ex)
        assert lm.current_mode(db) == 'LEGACY_BACKUP', 'el modo cambió igual'
    s.check('set_mode se niega y no cambia nada si n8n no responde',
            set_mode_refuses_when_n8n_is_down)

    def classify_the_unknown_first():
        """Un operador clasifica el grupo desconocido antes de poder
        cambiar de modo. Hasta aquí el fail-closed lo estaba bloqueando,
        que es justo lo que tiene que hacer."""
        pf_antes = lm.preflight(db, 'V2_PRIMARY', FakeAnalytics())
        assert not pf_antes['ok'], 'el grupo sin clasificar no bloqueaba'
        lm.classify_group(db, 'tester', 'master', 'GRUPO SIN CLASIFICAR',
                          'ANALYTICS', rationale='read-only reporting')
    s.check('el grupo sin clasificar bloquea hasta que alguien lo clasifica',
            classify_the_unknown_first)

    def allowed_when_all_conflicting_are_off():
        apagado = FakeAnalytics(active_workflows=['wf3a', 'wf3b'])   # sólo CRM_SYNC
        pf = lm.preflight(db, 'V2_PRIMARY', apagado)
        assert pf['ok'], f'sigue bloqueado con todo lo conflictivo apagado: {pf["blockers"]}'
        assert any('Verified against n8n' in w for w in pf['warnings']), \
            'no se deja constancia de que se verificó'
    s.check('con todo lo conflictivo apagado, la vuelta a V2 se permite',
            allowed_when_all_conflicting_are_off)

    def coexisting_may_stay_on():
        apagado = FakeAnalytics(active_workflows=['wf3a', 'wf4a'])   # dos CRM_SYNC
        pf = lm.preflight(db, 'V2_PRIMARY', apagado)
        assert pf['ok'], 'un CRM_SYNC encendido bloquea la vuelta a V2'
    s.check('los grupos que conviven pueden quedarse encendidos',
            coexisting_may_stay_on)

    def switch_back_actually_works():
        apagado = FakeAnalytics(active_workflows=['wf3a'])
        r = lm.set_mode(db, 'tester', 'master', 'V2_PRIMARY',
                        confirmation='SWITCH TO V2', reason='drill over',
                        analytics_module=apagado)
        assert r['changed'] and lm.current_mode(db) == 'V2_PRIMARY'
    s.check('verificado y con la frase correcta, el cambio a V2 procede',
            switch_back_actually_works)

    def unverifiable_workflow_blocks():
        """Un grupo conflictivo que referencia workflows que n8n no conoce
        no se puede verificar: no se asume apagado."""
        set_mode_raw(db, 'LEGACY_BACKUP')
        raro = FakeAnalytics()
        orig = raro.n8n_switches_with_status

        def con_desconocido(dbx):
            filas, err = orig(dbx)
            for f in filas:
                if f['label'] == 'MEXICO':
                    f['workflows'] = [{'id': 'x', 'name': '(not found in n8n)',
                                       'active': None}]
            return filas, err
        raro.n8n_switches_with_status = con_desconocido
        pf = lm.preflight(db, 'V2_PRIMARY', raro)
        assert not pf['ok'], 'un grupo no verificable no bloquea'
        assert any('cannot be verified' in b for b in pf['blockers'])
        set_mode_raw(db, 'V2_PRIMARY')
    s.check('un grupo cuyo estado no se puede verificar bloquea la vuelta',
            unverifiable_workflow_blocks)

    # ══ estática ═════════════════════════════════════════════════════
    s.section('el guard no se puede saltar por otra vía')

    def analytics_is_untouched():
        """El guard NO edita analytics.py: ese fichero va byte a byte."""
        src = open(os.path.join(ROOT, 'panel', 'app', 'analytics.py'),
                   encoding='utf-8').read()
        assert 'legacy_mode' not in src and 'operating_mode' not in src, \
            'analytics.py fue modificado para meter el guard'
    s.check('analytics.py sigue sin tocar: el guard se instala por fuera',
            analytics_is_untouched)

    def guard_installed_at_registration():
        src = open(os.path.join(ROOT, 'panel', 'app', 'v2_suite.py'),
                   encoding='utf-8').read()
        i = src.index('def register(')
        j = src.index('@app.route', i)
        assert 'install_activation_guard' in src[i:j], \
            'el guard no se instala al registrar: habría una ventana sin cerrojo'
    s.check('el guard se instala en register(), sin ventana sin cerrojo',
            guard_installed_at_registration)

    def single_guard_no_duplicated_logic():
        """§1: un solo guard, no lógica repetida en cada ruta."""
        src = open(os.path.join(ROOT, 'panel', 'app', 'v2_suite.py'),
                   encoding='utf-8').read()
        assert src.count('guard_legacy_activation') <= 1, \
            'la lógica del guard está duplicada en las rutas'
        lmsrc = open(os.path.join(ROOT, 'panel', 'app', 'legacy_mode.py'),
                     encoding='utf-8').read()
        assert lmsrc.count('def guard_legacy_activation') == 1
    s.check('hay UN solo guard, reutilizado, sin lógica duplicada',
            single_guard_no_duplicated_logic)

    return s.finish()


if __name__ == '__main__':
    sys.exit(main())
