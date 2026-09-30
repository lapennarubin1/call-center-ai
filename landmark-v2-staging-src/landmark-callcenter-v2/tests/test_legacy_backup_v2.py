#!/usr/bin/env python3
"""Legacy Backup / Plan B y modo de operación (§46-53).

Lo que se defiende aquí no es un dato: es que un cliente no reciba dos
llamadas, dos cuentas o dos cobros porque los dos sistemas estaban
despachando a la vez.
"""
import os
import sys

sys.dont_write_bytecode = True
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from _suite import (Suite, ROOT, mysql_available, fresh_mysql_db,     # noqa: E402
                    mysql_conn, run_sql_file, DB)

import legacy_mode as L                                               # noqa: E402

PARTS = ('001_multi_country_config_v2_2.sql', '002_callcenter_suite_v2.sql',
         '003_legacy_compat_tables.sql', '004_billing_v2.sql',
         '005_legacy_backup_v2.sql')
GROUPS = ('INDIA - NEPAL', 'PANEL', 'MEXICO', 'INDIA - PROVEEDOR STRINGEE',
          'CRM INDIA - NEPAL', 'CRM PANEL')
ACTOR = 'test-master'


def build_db(name, extra_groups=()):
    fresh_mysql_db(name)
    c = mysql_conn(name)
    with c.cursor() as cur:
        # Las tablas legacy TAL CUAL las define el panel real.
        cur.execute("""CREATE TABLE n8n_switches (
            id INT AUTO_INCREMENT PRIMARY KEY, label VARCHAR(80) NOT NULL,
            workflow_ids TEXT NOT NULL,
            created_at DATETIME DEFAULT CURRENT_TIMESTAMP)""")
        cur.execute("""CREATE TABLE n8n_switch_schedules (
            id INT AUTO_INCREMENT PRIMARY KEY, switch_id INT NOT NULL UNIQUE,
            timezone VARCHAR(64) NOT NULL, on_time VARCHAR(5) NOT NULL,
            off_time VARCHAR(5) NOT NULL, days VARCHAR(24) NOT NULL,
            enabled TINYINT NOT NULL DEFAULT 1, last_on_date VARCHAR(10),
            last_off_date VARCHAR(10), last_error TEXT, last_checked_at DATETIME,
            created_at DATETIME DEFAULT CURRENT_TIMESTAMP)""")
        for lbl in tuple(GROUPS) + tuple(extra_groups):
            cur.execute("INSERT INTO n8n_switches (label, workflow_ids) VALUES (%s,%s)",
                        (lbl, 'wf_aaa,wf_bbb,wf_ccc'))
        cur.execute("""INSERT INTO n8n_switch_schedules
            (switch_id, timezone, on_time, off_time, days, enabled)
            VALUES (1,'Asia/Kolkata','09:00','20:00','1,2,3,4,5',1)""")
    c.close()
    for p in PARTS:
        run_sql_file(name, os.path.join(ROOT, 'sql', 'parts', p))
    return DB(mysql_conn(name))


class FakeAnalytics:
    """Simula el acceso a n8n. Desde r2-final el preflight VERIFICA el
    estado real de los grupos legacy en vez de pedirle al usuario que
    mire, así que los tests necesitan poder decir qué está encendido —
    y poder simular que n8n no contesta."""

    def __init__(self, active_workflows=(), reachable=True):
        self.active = set(active_workflows)
        self.reachable = reachable

    def n8n_switches_with_status(self, db):
        if not self.reachable:
            return [], 'n8n unreachable: connection refused'
        out = []
        for r in db.q("SELECT id, label, workflow_ids FROM n8n_switches ORDER BY id"):
            ids = [w for w in (r['workflow_ids'] or '').split(',') if w]
            out.append({'id': r['id'], 'label': r['label'], 'workflow_ids': ids,
                        'workflows': [{'id': w, 'name': f'wf {w}',
                                       'active': w in self.active} for w in ids]})
        return out, None


TODO_APAGADO = FakeAnalytics()


def cutover(db):
    """Pone el modo en V2_PRIMARY directamente en la BD.

    Desde r2-final2 la migración deja `LEGACY_BACKUP`: una instalación
    recién migrada NO despacha por V2 hasta que alguien decide el
    cutover. Eso es lo correcto en producción, pero significa que los
    tests de "pasar a LEGACY" ya no pueden partir del estado de la
    migración — `preflight` contesta "Already in LEGACY_BACKUP" y no
    comprueba nada, y el test pasaría sin haber probado nada.

    Así que se escribe el modo a mano, sin pasar por `set_mode`: esto
    representa el sistema DESPUÉS del cutover, que es el único estado
    desde el que la vuelta al legacy tiene sentido. No se usa `set_mode`
    a propósito, para no ensuciar la auditoría que después se examina.
    """
    db.execute("""INSERT INTO app_settings (setting_key, setting_value)
                  VALUES ('lm_operating_mode', 'V2_PRIMARY')
                  ON DUPLICATE KEY UPDATE setting_value='V2_PRIMARY'""")
    return db


def all_v2_routes_off(db):
    db.execute("UPDATE call_routes SET enabled=0")


def main():
    s = Suite('Legacy Backup y modo de operación')

    if not mysql_available():
        s.skip('Legacy Backup', 'sin MariaDB')
        return s.finish()

    NAME = 'lm_test_legacy'
    db = cutover(build_db(NAME, extra_groups=()))

    # ══ preservación · §46, §52 ══════════════════════════════════════
    s.section('el legacy sigue entero (§46, §52)')

    def switches_preserved():
        n = db.one("SELECT COUNT(*) AS n FROM n8n_switches")['n']
        assert n == len(GROUPS), f'se perdieron grupos: quedan {n} de {len(GROUPS)}'
    s.check('los grupos de workflows siguen todos', switches_preserved)

    def workflow_ids_preserved():
        for r in db.q("SELECT label, workflow_ids FROM n8n_switches"):
            assert r['workflow_ids'] == 'wf_aaa,wf_bbb,wf_ccc', \
                f"{r['label']}: se perdieron los workflow_ids"
    s.check('los workflow_ids de cada grupo están intactos', workflow_ids_preserved)

    def schedules_preserved():
        r = db.one("SELECT timezone, on_time, off_time, days, enabled "
                   "FROM n8n_switch_schedules WHERE switch_id=1")
        assert r['timezone'] == 'Asia/Kolkata' and r['on_time'] == '09:00' \
            and r['off_time'] == '20:00' and r['days'] == '1,2,3,4,5' \
            and r['enabled'] == 1, f'el horario legacy cambió: {r}'
    s.check('los horarios legacy (zona, horas, días) están intactos',
            schedules_preserved)

    def migration_does_not_touch_switches():
        sql = ''.join(open(os.path.join(ROOT, 'sql', 'parts', p), encoding='utf-8').read()
                      for p in PARTS)
        import re
        for tabla in ('n8n_switches', 'n8n_switch_schedules'):
            for verbo in ('DROP TABLE', 'TRUNCATE', 'DELETE FROM', 'ALTER TABLE'):
                assert not re.search(rf'(?i){verbo}\s+`?{tabla}\b', sql), \
                    f'la migración hace {verbo} sobre {tabla}'
        # El único UPDATE permitido es el que rellena switch_id en la
        # tabla de clasificación, que NO es una tabla de v1.
        assert not re.search(r'(?i)UPDATE\s+`?n8n_switch', sql), \
            'la migración hace UPDATE sobre una tabla de switches de v1'
    s.check('la migración no borra, trunca ni altera las tablas legacy',
            migration_does_not_touch_switches)

    def plan_b_is_operational_not_documentation():
        grupos = L.legacy_groups(db)
        assert len(grupos) == len(GROUPS)
        assert all(g['workflow_count'] == 3 for g in grupos), \
            'el Plan B perdió el mapeo a workflows: sería documentación, no plan'
    s.check('el Plan B conserva su mapeo real a workflows (§52)',
            plan_b_is_operational_not_documentation)

    # ══ clasificación · §51 ══════════════════════════════════════════
    s.section('clasificación de grupos (§51)')

    def not_everything_off_blindly():
        grupos = {g['label']: g for g in L.legacy_groups(db)}
        assert grupos['PANEL']['coexists_with_v2'], \
            'se marca como conflictivo un job que sólo espeja el CRM'
        assert grupos['CRM PANEL']['coexists_with_v2']
        assert not grupos['INDIA - NEPAL']['coexists_with_v2'], \
            'un grupo que LLAMA se marca como conviviente'
        assert not grupos['MEXICO']['coexists_with_v2']
    s.check('los grupos se clasifican, no se apagan todos a ciegas',
            not_everything_off_blindly)

    def matrix_is_computed_from_data():
        m = L.compatibility_matrix(db)
        cats = {c['category']: c for c in m['categories']}
        assert cats['DISPATCH']['coexists_with_v2'] is False
        assert cats['CRM_SYNC']['coexists_with_v2'] is True
        assert 'INDIA - NEPAL' in cats['DISPATCH']['groups']
        assert not m['unclassified']
    s.check('la matriz de compatibilidad sale de la clasificación real',
            matrix_is_computed_from_data)

    def dispatch_cannot_be_marked_coexisting():
        try:
            L.classify_group(db, ACTOR, 'master', 'MEXICO', 'DISPATCH',
                             coexists=True)
            raise AssertionError('se permitió marcar un DISPATCH como conviviente')
        except L.ModeError as ex:
            assert 'cannot be marked as coexisting' in str(ex)
    s.check('un grupo que llama NO se puede marcar como conviviente',
            dispatch_cannot_be_marked_coexisting)

    def classify_requires_master():
        try:
            L.classify_group(db, 'viewer', 'viewer', 'PANEL', 'ANALYTICS')
            raise AssertionError('un viewer clasificó un grupo')
        except L.PermissionDenied:
            pass
    s.check('clasificar un grupo es sólo de MASTER', classify_requires_master)

    # ══ grupo sin clasificar: fail-closed ════════════════════════════
    s.section('lo desconocido se trata como conflictivo')

    NAME2 = 'lm_test_legacy_unknown'
    db2 = cutover(build_db(NAME2, extra_groups=('GRUPO NUEVO',)))

    def unknown_is_conflicting():
        g = {x['label']: x for x in L.legacy_groups(db2)}['GRUPO NUEVO']
        assert g['category'] == 'UNKNOWN'
        assert g['conflicts'] is True, \
            'un grupo sin clasificar se asume inofensivo'
    s.check('un grupo nuevo entra como UNKNOWN y conflictivo',
            unknown_is_conflicting)

    def unknown_blocks_the_switch():
        all_v2_routes_off(db2)
        pf = L.preflight(db2, 'LEGACY_BACKUP')
        assert not pf['ok'], 'se permite cambiar de modo con grupos sin clasificar'
        assert any('not classified' in b for b in pf['blockers'])
    s.check('un grupo sin clasificar bloquea el cambio de modo',
            unknown_blocks_the_switch)

    def classifying_unblocks():
        L.classify_group(db2, ACTOR, 'master', 'GRUPO NUEVO', 'ANALYTICS',
                         rationale='read-only reporting job')
        pf = L.preflight(db2, 'LEGACY_BACKUP')
        assert pf['ok'], f'sigue bloqueado tras clasificar: {pf["blockers"]}'
    s.check('clasificarlo desbloquea el cambio', classifying_unblocks)

    # ══ interlock · §49, §50 ═════════════════════════════════════════
    s.section('nunca los dos sistemas llamando (§49, §50)')

    def v2_must_be_off_before_legacy():
        db.execute("UPDATE call_routes SET enabled=1 WHERE route_key='IN_PROVEEDOR1'")
        db.execute("UPDATE voice_providers SET enabled=1")
        db.execute("UPDATE countries SET enabled=1 WHERE iso='IN'")
        pf = L.preflight(db, 'LEGACY_BACKUP')
        assert not pf['ok'], 'se deja pasar a LEGACY con V2 pudiendo llamar'
        assert any('IN_PROVEEDOR1' in b for b in pf['blockers']), \
            'el bloqueo no dice qué ruta está viva'
    s.check('no se pasa a LEGACY mientras V2 pueda llamar', v2_must_be_off_before_legacy)

    def set_mode_refuses_while_v2_is_live():
        try:
            L.set_mode(db, ACTOR, 'master', 'LEGACY_BACKUP',
                       confirmation='SWITCH TO LEGACY',
                       analytics_module=TODO_APAGADO)
            raise AssertionError('cambió de modo con V2 vivo')
        except L.ModeError as ex:
            assert 'IN_PROVEEDOR1' in str(ex)
        assert L.current_mode(db) == 'V2_PRIMARY', 'el modo cambió igual'
    s.check('set_mode se niega y NO cambia nada si V2 sigue vivo',
            set_mode_refuses_while_v2_is_live)

    def route_under_disabled_country_is_not_live():
        """Una ruta encendida bajo un país apagado no está llamando: exigir
        apagarla también sería pedir trabajo que no cambia nada."""
        db.execute("UPDATE countries SET enabled=0 WHERE iso='IN'")
        vivas = L.v2_dispatch_active(db)
        assert 'IN_PROVEEDOR1' not in vivas, \
            'una ruta bajo país apagado se cuenta como viva'
        db.execute("UPDATE countries SET enabled=1 WHERE iso='IN'")
    s.check('los tres interruptores deciden si una ruta está viva',
            route_under_disabled_country_is_not_live)

    def switch_to_legacy_when_v2_is_off():
        all_v2_routes_off(db)
        r = L.set_mode(db, ACTOR, 'master', 'LEGACY_BACKUP',
                       confirmation='SWITCH TO LEGACY',
                       reason='planned failover drill',
                       analytics_module=TODO_APAGADO)
        assert r['changed'] and L.current_mode(db) == 'LEGACY_BACKUP'
    s.check('con V2 apagado, el cambio a LEGACY sí procede',
            switch_to_legacy_when_v2_is_off)

    def going_back_names_what_is_actually_running():
        """Ya no se PIDE apagar: se COMPRUEBA. El bloqueo nombra los
        grupos que n8n dice que siguen corriendo."""
        vivo = FakeAnalytics(active_workflows=['wf_aaa'])   # INDIA - NEPAL
        pf = L.preflight(db, 'V2_PRIMARY', vivo)
        assert not pf['ok'], 'se deja volver a V2 con un grupo conflictivo vivo'
        assert any('INDIA - NEPAL' in b for b in pf['blockers']), \
            'el bloqueo no nombra el grupo que sigue corriendo'
        assert 'PANEL' in ' '.join(pf['warnings']), \
            'no se avisa de qué grupos SÍ pueden seguir corriendo'
    s.check('volver a V2 nombra lo que n8n dice que sigue corriendo',
            going_back_names_what_is_actually_running)

    def unverifiable_state_blocks_instead_of_warning():
        """Antes el panel avisaba de que no podía verificar y dejaba pasar.
        Eso convertía el enclavamiento en un cartel: ahora bloquea."""
        caido = FakeAnalytics(reachable=False)
        pf = L.preflight(db, 'V2_PRIMARY', caido)
        assert not pf['ok'], 'no poder verificar ya no bloquea'
        assert any('Cannot reach n8n' in b for b in pf['blockers'])
        assert not any('cannot' in w.lower() and 'ON/OFF' in w
                       for w in pf['warnings']), \
            'sigue el aviso antiguo de "no puedo verificar" en vez del bloqueo'
    s.check('si no se puede verificar, se BLOQUEA (antes sólo avisaba)',
            unverifiable_state_blocks_instead_of_warning)

    # ══ permisos y confirmación · §49, §54 ═══════════════════════════
    s.section('sólo MASTER y con confirmación escrita (§49, §54)')

    def only_master():
        for rol in ('viewer', 'operator', '', None):
            try:
                L.set_mode(db, 'x', rol, 'V2_PRIMARY', confirmation='SWITCH TO V2')
                raise AssertionError(f'el rol {rol!r} cambió el modo')
            except L.PermissionDenied:
                pass
    s.check('ningún rol que no sea MASTER puede cambiar el modo', only_master)

    def confirmation_required():
        for c in (None, '', 'yes', 'switch to v2', 'SWITCH TO LEGACY'):
            try:
                L.set_mode(db, ACTOR, 'master', 'V2_PRIMARY', confirmation=c,
                           analytics_module=TODO_APAGADO)
                raise AssertionError(f'se aceptó la confirmación {c!r}')
            except L.ModeError as ex:
                assert 'SWITCH TO V2' in str(ex) or 'Type' in str(ex)
    s.check('hace falta la frase exacta, y distingue mayúsculas',
            confirmation_required)

    def back_to_v2_works():
        """Con la frase correcta Y con n8n confirmando que no hay nada
        conflictivo corriendo."""
        r = L.set_mode(db, ACTOR, 'master', 'V2_PRIMARY',
                       confirmation='SWITCH TO V2', reason='drill finished',
                       analytics_module=TODO_APAGADO)
        assert r['changed'] and L.current_mode(db) == 'V2_PRIMARY'
    s.check('con la frase correcta y n8n verificado, MASTER vuelve a V2',
            back_to_v2_works)

    def legacy_config_survives_the_round_trip():
        """§50: volver a V2 no puede destruir la configuración del legacy."""
        assert db.one("SELECT COUNT(*) AS n FROM n8n_switches")['n'] == len(GROUPS)
        r = db.one("SELECT timezone, on_time, days FROM n8n_switch_schedules WHERE switch_id=1")
        assert r['timezone'] == 'Asia/Kolkata' and r['on_time'] == '09:00' \
            and r['days'] == '1,2,3,4,5', 'el horario legacy se perdió al volver a V2'
    s.check('ir a LEGACY y volver no destruye horarios ni grupos',
            legacy_config_survives_the_round_trip)

    # ══ auditoría · §53 ══════════════════════════════════════════════
    s.section('auditoría del cambio de modo (§53)')

    def audited():
        filas = L.mode_audit(db)
        cambios = [f for f in filas if f['action'] == 'MODE_CHANGE']
        assert len(cambios) >= 2, 'faltan entradas de auditoría'
        ultimo = cambios[0]
        for campo in ('actor', 'old_value', 'new_value', 'changed_at'):
            assert ultimo.get(campo), f'la auditoría no guarda {campo}'
        assert ultimo['new_value'] == 'V2_PRIMARY'
        assert ultimo['old_value'] == 'LEGACY_BACKUP'
        assert ultimo['reason'] == 'drill finished'
    s.check('cada cambio guarda quién, cuándo, desde, hacia y por qué',
            audited)

    def audit_has_no_secrets():
        for f in L.mode_audit(db, 200):
            blob = ' '.join(str(f.get(k) or '') for k in
                            ('field', 'old_value', 'new_value', 'reason'))
            for mala in ('password', 'token', 'secret', 'api_key'):
                assert mala not in blob.lower(), f'auditoría con secreto: {blob[:80]}'
    s.check('la auditoría del modo no guarda secretos', audit_has_no_secrets)

    def failed_attempts_change_nothing():
        antes = L.current_mode(db)
        n_antes = len(L.mode_audit(db, 500))
        for kw in ({'role': 'viewer'}, {'confirmation': 'nope'}):
            try:
                L.set_mode(db, ACTOR, kw.get('role', 'master'), 'LEGACY_BACKUP',
                           confirmation=kw.get('confirmation'),
                           analytics_module=TODO_APAGADO)
            except L.ModeError:
                pass
        assert L.current_mode(db) == antes, 'un intento fallido cambió el modo'
        assert len(L.mode_audit(db, 500)) == n_antes, \
            'un intento fallido escribió auditoría de cambio'
    s.check('un intento fallido no cambia el modo ni ensucia la auditoría',
            failed_attempts_change_nothing)

    # ══ instalación · §48, §77 ═══════════════════════════════════════
    s.section('la instalación no activa nada (§48, §77)')

    def fresh_install_is_legacy_backup_and_nothing_on():
        """Una instalación recién migrada NO despacha por V2.

        Antes la migración dejaba `V2_PRIMARY`, apoyándose en que las
        rutas estaban todas apagadas. Eso confunde dos cosas distintas:
        "V2 manda" y "V2 no tiene nada encendido todavía". Bastaba que
        alguien encendiera un país, un proveedor y una ruta —tres clics
        que el panel invita a dar— para que el sistema empezara a
        despachar sin que nadie hubiera decidido el cutover.

        Ahora el estado inicial es `LEGACY_BACKUP`: el sistema viejo
        sigue siendo el que manda hasta que un MASTER escribe la frase
        de confirmación. Encender rutas antes de eso no llama a nadie.
        """
        NAME3 = 'lm_test_legacy_fresh'
        db3 = build_db(NAME3)
        assert L.current_mode(db3) == 'LEGACY_BACKUP', (
            'una instalación limpia se declara dueña del despacho sin '
            'que nadie haya hecho el cutover')
        assert L.v2_dispatch_active(db3) == [], \
            'una instalación limpia deja rutas V2 en condiciones de llamar'
        # Y encender los tres interruptores TAMPOCO despacha: el modo manda.
        db3.execute("UPDATE countries SET enabled=1 WHERE iso='IN'")
        db3.execute("UPDATE voice_providers SET enabled=1")
        db3.execute("UPDATE call_routes SET enabled=1 WHERE route_key='IN_PROVEEDOR1'")
        import routes_config as RC
        assert RC.dispatch_allowed(db3) is False, (
            'con el modo en LEGACY_BACKUP y las rutas encendidas, el panel '
            'autoriza despachar')
        assert RC.routes_active_payload(db3)['routes'] == [], \
            'el panel entrega rutas invocables estando en LEGACY_BACKUP'
    s.check('instalación limpia: modo LEGACY_BACKUP y nada puede llamar',
            fresh_install_is_legacy_backup_and_nothing_on)

    def safety_does_not_touch_an_installation_in_use():
        """La contraparte crítica: en una instalación que YA se usa, el paso
        de seguridad no puede apagar lo que alguien encendió a propósito."""
        NAME4 = 'lm_test_legacy_inuse'
        db4 = build_db(NAME4)
        # alguien configura y enciende, como haría desde el panel
        db4.execute("UPDATE countries SET enabled=1 WHERE iso='IN'")
        db4.execute("UPDATE voice_providers SET enabled=1")
        db4.execute("UPDATE call_routes SET enabled=1 WHERE route_key='IN_PROVEEDOR1'")
        db4.execute("""INSERT INTO route_audit (route_key, action, field,
                                                old_value, new_value, actor)
                       VALUES ('IN_PROVEEDOR1','ENABLE','enabled','0','1','someone')""")
        vivas_antes = L.v2_dispatch_active(db4)
        assert 'IN_PROVEEDOR1' in vivas_antes

        for p in PARTS:
            run_sql_file(NAME4, os.path.join(ROOT, 'sql', 'parts', p))

        vivas = L.v2_dispatch_active(db4)
        assert vivas == vivas_antes, (
            'la migración apagó rutas de una instalación en uso: '
            f'antes {vivas_antes}, ahora {vivas}')
    s.check('en una instalación en uso, la migración NO apaga nada',
            safety_does_not_touch_an_installation_in_use)

    def safety_runs_only_once():
        """Si el usuario enciende y re-ejecuta, no se vuelve a apagar."""
        NAME5 = 'lm_test_legacy_once'
        db5 = build_db(NAME5)
        assert L.v2_dispatch_active(db5) == []
        db5.execute("UPDATE countries SET enabled=1 WHERE iso='IN'")
        db5.execute("UPDATE voice_providers SET enabled=1 WHERE code='proveedor1'")
        db5.execute("UPDATE call_routes SET enabled=1 WHERE route_key='IN_PROVEEDOR1'")
        for p in PARTS:
            run_sql_file(NAME5, os.path.join(ROOT, 'sql', 'parts', p))
        assert 'IN_PROVEEDOR1' in L.v2_dispatch_active(db5), \
            'el paso de seguridad se repitió y volvió a apagar lo encendido'
    s.check('el paso de seguridad corre una sola vez, no en cada re-ejecución',
            safety_runs_only_once)

    def migration_does_not_change_mode():
        db.execute("UPDATE app_settings SET setting_value='LEGACY_BACKUP' "
                   "WHERE setting_key='lm_operating_mode'")
        for p in PARTS:
            run_sql_file(NAME, os.path.join(ROOT, 'sql', 'parts', p))
        assert L.current_mode(db) == 'LEGACY_BACKUP', \
            're-ejecutar la migración cambió el modo de operación'
        db.execute("UPDATE app_settings SET setting_value='V2_PRIMARY' "
                   "WHERE setting_key='lm_operating_mode'")
    s.check('re-ejecutar la migración NO cambia el modo (§64)',
            migration_does_not_change_mode)

    return s.finish()


if __name__ == '__main__':
    sys.exit(main())
