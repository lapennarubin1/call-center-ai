#!/usr/bin/env python3
"""La migración completa del suite: idempotente, segura, rerunnable, no destructiva.

`sql/migration.sql` = 001 (fundación V2.2, aprobada) + 002 (suite V2) +
003 (tablas de compatibilidad con v1). Se ejecuta contra MariaDB real.
"""
import os
import sys

# Importar los módulos del suite no debe dejar .pyc dentro del paquete:
# lo que se empaqueta tiene que salir limpio.
sys.dont_write_bytecode = True
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import _suite as S

DB = 'lm_migration_suite_test'
V1_TABLES = ['crm_leads', 'crm_conversions', 'stringee_calls', 'panel_sync_log',
             'wf10_sent_recordings', 'wf2_provider_config']
V2_TABLES = ['countries', 'voice_providers', 'followup_policies', 'call_routes',
             'route_capacity_windows', 'route_telegram_targets', 'route_audit',
             'country_tool_configs', 'wf_call_jobs', 'wf_conversation_ledger',
             'wf_events', 'wf_reconciliation_issues', 'wf_settings',
             'wf_recording_ledger', 'wf_tool_requests']


def tables(db):
    c = S.mysql_conn(db)
    with c.cursor() as cur:
        cur.execute("SELECT TABLE_NAME FROM information_schema.TABLES "
                    "WHERE TABLE_SCHEMA=DATABASE()")
        out = {r[0] for r in cur.fetchall()}
    c.close()
    return out


def main():
    s = S.Suite('MIGRACIÓN DEL SUITE V2 (MariaDB)')
    if not S.mysql_available():
        s.skip('suite completa', 'MariaDB no disponible')
        return s.finish()

    s.section('instalación limpia')
    S.fresh_mysql_db(DB)
    n = S.run_sql_file(DB, S.MIGRATION)

    def creates_everything():
        have = tables(DB)
        missing = [t for t in V2_TABLES + V1_TABLES if t not in have]
        assert not missing, f'faltan tablas: {missing}'
    s.check(f'{n} sentencias crean las {len(V2_TABLES)} tablas V2 y las {len(V1_TABLES)} de compatibilidad',
            creates_everything)

    def seeds_config():
        db = S.db_for(DB)
        assert db.one("SELECT COUNT(*) c FROM countries")['c'] >= 2
        assert db.one("SELECT COUNT(*) c FROM voice_providers")['c'] >= 2
        assert db.one("SELECT COUNT(*) c FROM call_routes")['c'] >= 3
        p = db.one("SELECT policy_json FROM followup_policies "
                   "WHERE policy_key='STANDARD_CALL_RETRY'")
        assert p, 'no se sembró STANDARD_CALL_RETRY'
        import json
        pol = json.loads(p['policy_json'])
        assert len(pol['rules']) >= 14
        assert db.one("SELECT COUNT(*) c FROM wf_settings")['c'] >= 12
    s.check('seed: países, proveedores, rutas, política y settings', seeds_config)

    def one_provider_many_routes():
        db = S.db_for(DB)
        rows = db.q("SELECT p.code, COUNT(*) n FROM call_routes r "
                    "JOIN voice_providers p ON p.id=r.provider_id GROUP BY p.code")
        by = {r['code']: r['n'] for r in rows}
        assert by.get('proveedor1', 0) >= 2, \
            f'PROVEEDOR1 debería tener varias rutas: {by}'
        assert len(by) == 2, f'se sembró un proveedor por país: {by}'
    s.check('el seed modela UN proveedor con varias rutas, no uno por país',
            one_provider_many_routes)

    s.section('rerun: idempotente y sin pisar ediciones')

    def rerun_is_noop():
        db = S.db_for(DB)
        before = {t: db.one(f"SELECT COUNT(*) c FROM {t}")['c'] for t in V2_TABLES}
        S.run_sql_file(DB, S.MIGRATION)
        S.run_sql_file(DB, S.MIGRATION)
        after = {t: db.one(f"SELECT COUNT(*) c FROM {t}")['c'] for t in V2_TABLES}
        diff = {t: (before[t], after[t]) for t in before if before[t] != after[t]}
        assert not diff, f'la reejecución cambió filas: {diff}'
    s.check('dos reejecuciones más no cambian ninguna fila', rerun_is_noop)

    def rerun_preserves_edits():
        db = S.db_for(DB)
        db.execute("UPDATE countries SET enabled=0, language='xx' WHERE iso='IN'")
        db.execute("UPDATE call_routes SET capacity_default=99 WHERE route_key='IN_STRINGEE'")
        db.execute("UPDATE wf_settings SET setting_value='42' WHERE setting_key='tech_retry_max'")
        db.execute("UPDATE followup_policies SET name='Editada' "
                   "WHERE policy_key='STANDARD_CALL_RETRY'")
        S.run_sql_file(DB, S.MIGRATION)
        assert db.one("SELECT enabled, language FROM countries WHERE iso='IN'") == \
            {'enabled': 0, 'language': 'xx'}, 'el rerun pisó la config del país'
        assert db.one("SELECT capacity_default c FROM call_routes "
                      "WHERE route_key='IN_STRINGEE'")['c'] == 99, \
            'el rerun pisó la capacidad editada'
        assert db.one("SELECT setting_value v FROM wf_settings "
                      "WHERE setting_key='tech_retry_max'")['v'] == '42', \
            'el rerun pisó un setting editado'
        assert db.one("SELECT name n FROM followup_policies "
                      "WHERE policy_key='STANDARD_CALL_RETRY'")['n'] == 'Editada', \
            'el rerun pisó la política editada'
    s.check('una config editada desde el panel sobrevive al rerun',
            rerun_preserves_edits)

    def rerun_preserves_operational_data():
        db = S.db_for(DB)
        route = db.one("SELECT id FROM call_routes WHERE route_key='IN_PROVEEDOR1'")
        db.execute("INSERT INTO wf_call_jobs (call_job_id, lead_id, route_id, route_key, "
                   "country_iso, provider, attempt, state, result, duration_seconds) "
                   "VALUES ('mj1','ml1',§,'IN_PROVEEDOR1','IN','proveedor1',1,"
                   "'COMPLETED','ANSWERED',77)", (route['id'],))
        db.execute("INSERT INTO wf_events (event_key, event_type, event_domain, "
                   "occurred_at, call_job_id, result) VALUES "
                   "('CALL_RESULT:mj1','CALL_RESULT','CALL',UTC_TIMESTAMP(),'mj1','ANSWERED')")
        S.run_sql_file(DB, S.MIGRATION)
        j = db.one("SELECT result, duration_seconds FROM wf_call_jobs WHERE call_job_id='mj1'")
        assert j == {'result': 'ANSWERED', 'duration_seconds': 77}, \
            f'el rerun tocó el historial de llamadas: {j}'
        assert db.one("SELECT COUNT(*) c FROM wf_events "
                      "WHERE event_key='CALL_RESULT:mj1'")['c'] == 1
    s.check('el historial de llamadas y los eventos no se tocan en el rerun',
            rerun_preserves_operational_data)

    s.section('no destructiva')

    def no_destructive_statements():
        text = open(S.MIGRATION).read()
        for stmt in S.split_sql(text):
            up = stmt.upper().strip()
            assert not up.startswith('DROP '), f'DROP ejecutable: {stmt[:80]}'
            assert not up.startswith('TRUNCATE'), f'TRUNCATE ejecutable: {stmt[:80]}'
            assert not up.startswith('DELETE '), f'DELETE ejecutable: {stmt[:80]}'
    s.check('la migración no contiene DROP, TRUNCATE ni DELETE ejecutables',
            no_destructive_statements)

    def v1_tables_untouched():
        """003 usa CREATE TABLE IF NOT EXISTS: si la tabla de v1 ya existe con
        OTRA forma, la migración no la altera."""
        db = S.db_for(DB)
        db.execute("DROP TABLE IF EXISTS crm_leads")
        db.execute("CREATE TABLE crm_leads (lead_id VARCHAR(64) PRIMARY KEY, "
                   "columna_rara VARCHAR(10))")
        db.execute("INSERT INTO crm_leads VALUES ('viejo','dato')")
        S.run_sql_file(DB, S.MIGRATION)
        cols = {r['Field'] for r in db.q("SHOW COLUMNS FROM crm_leads")}
        assert cols == {'lead_id', 'columna_rara'}, \
            f'la migración alteró una tabla de v1 existente: {cols}'
        assert db.one("SELECT COUNT(*) c FROM crm_leads")['c'] == 1, \
            'la migración borró datos de v1'
    s.check('una tabla de v1 preexistente no se altera ni se vacía',
            v1_tables_untouched)

    def seeds_only_once():
        db = S.db_for(DB)
        n_before = db.one("SELECT COUNT(*) c FROM countries")['c']
        db.execute("DELETE FROM countries WHERE iso='NP'")
        S.run_sql_file(DB, S.MIGRATION)
        n_after = db.one("SELECT COUNT(*) c FROM countries")['c']
        assert n_after == n_before - 1, \
            ('el seed volvió a insertar un país borrado a propósito: '
             'el marcador de seed no está funcionando')
    s.check('el seed corre una sola vez en la vida de la base', seeds_only_once)

    s.section('rollback')

    def rollback_part_a_is_safe():
        db = S.db_for(DB)
        S.run_sql_file(DB, S.ROLLBACK)
        assert db.one("SELECT COUNT(*) c FROM call_routes WHERE enabled=1")['c'] == 0, \
            'quedaron rutas encendidas tras el rollback'
        assert db.one("SELECT COUNT(*) c FROM countries WHERE enabled=1")['c'] == 0
        have = tables(DB)
        assert all(t in have for t in V2_TABLES), 'el rollback borró tablas'
        assert db.one("SELECT COUNT(*) c FROM wf_call_jobs WHERE call_job_id='mj1'")['c'] == 1, \
            'el rollback borró el historial de llamadas'
    s.check('rollback PARTE A: apaga todo sin borrar nada', rollback_part_a_is_safe)

    def rollback_is_reversible():
        db = S.db_for(DB)
        r = db.one("SELECT id FROM call_routes WHERE route_key='IN_PROVEEDOR1'")
        db.execute("UPDATE countries SET enabled=1 WHERE iso='IN'")
        db.execute("UPDATE call_routes SET enabled=1 WHERE id=§", (r['id'],))
        assert db.one("SELECT enabled e FROM call_routes WHERE id=§", (r['id'],))['e'] == 1
    s.check('el rollback es reversible: volver a encender es un UPDATE',
            rollback_is_reversible)

    def rollback_audited():
        db = S.db_for(DB)
        n = db.one("SELECT COUNT(*) c FROM route_audit WHERE actor='rollback.sql'")['c']
        assert n > 0, 'el rollback no dejó rastro en la auditoría'
    s.check('el rollback queda registrado en route_audit', rollback_audited)

    def rollback_destructive_part_commented():
        text = open(S.ROLLBACK).read()
        for stmt in S.split_sql(text):
            up = stmt.upper().strip()
            assert not up.startswith('DROP '), f'DROP ejecutable en rollback: {stmt[:70]}'
            assert not up.startswith('DELETE '), f'DELETE ejecutable: {stmt[:70]}'
        assert 'DROP TABLE IF EXISTS wf_call_jobs' in text, \
            'la parte destructiva no está documentada'
        assert '-- DROP TABLE IF EXISTS wf_call_jobs' in text, \
            'la parte destructiva NO está comentada'
    s.check('la parte destructiva del rollback está escrita pero comentada',
            rollback_destructive_part_commented)

    s.section('interrupción a mitad de camino')

    def partial_then_complete():
        S.fresh_mysql_db(DB + '_partial')
        total = len(S.split_sql(open(S.MIGRATION).read()))
        S.run_sql_file(DB + '_partial', S.MIGRATION, limit=total // 2)
        # se completa en una segunda pasada, sin errores
        S.run_sql_file(DB + '_partial', S.MIGRATION)
        have = tables(DB + '_partial')
        missing = [t for t in V2_TABLES if t not in have]
        assert not missing, f'tras completar una migración interrumpida faltan: {missing}'
        db = S.db_for(DB + '_partial')
        assert db.one("SELECT COUNT(*) c FROM wf_settings")['c'] >= 12
    s.check('una migración interrumpida se completa al reejecutarla',
            partial_then_complete)

    return s.finish()


if __name__ == '__main__':
    sys.exit(main())
