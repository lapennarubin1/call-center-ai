"""
test_ops_analytics_v2_2.py — Event store, métricas locales, atribución,
reintento técnico y una-llamada-en-vuelo. Corre en sqlite y en MariaDB con la
migración real.
"""
import os
import sqlite3
from datetime import datetime, timezone

from _harness import Suite, MIGRATION, mysql_available, mysql_conn, fresh_mysql_db, run_sql_file

import call_jobs as cj        # noqa: E402
import ops_events as oe       # noqa: E402
import analytics_v2 as av     # noqa: E402
from analytics import DB      # noqa: E402

S = Suite('EVENTOS Y ANALYTICS V2.2')
_n = [0]


def make_db(backend):
    if backend == 'sqlite':
        db = DB(sqlite3.connect(':memory:'), 'sqlite')
        oe.ensure_tables_sqlite(db)
        return db
    _n[0] += 1
    name = f'ev_{_n[0]}'
    fresh_mysql_db(name)
    run_sql_file(name, MIGRATION)
    return DB(mysql_conn(name), 'mysql')


D0 = datetime(2026, 9, 21, 0, 0, tzinfo=timezone.utc)
D1 = datetime(2026, 9, 22, 0, 0, tzinfo=timezone.utc)


def job(db, lead, attempt=1, route='IN_PROVEEDOR1', iso='IN', prov='proveedor1',
        adapter='ELEVENLABS_SIP', created='2026-09-21 10:15:00'):
    """Una llamada completa hasta DISPATCHED, con created_at controlado."""
    j = cj.make_call_job_id(route)
    assert cj.claim_job(db, j, lead, 1, route, iso, prov, attempt, 'exec', adapter)
    assert cj.mark_dispatching(db, j)
    assert cj.mark_dispatched(db, j, conversation_id=f'conv_{j}', http_status=200)
    db.execute("UPDATE wf_call_jobs SET created_at=§ WHERE call_job_id=§", (created, j))
    return j


def set_completed(db, j, ts):
    db.execute("UPDATE wf_call_jobs SET completed_at=§ WHERE call_job_id=§", (ts, j))


def count(db, sql, args=()):
    return int(list(db.one(sql, args).values())[0] or 0)


# ── idempotencia (punto 24) ───────────────────────────────────────────
def c_answered_dos_veces(db):
    j = job(db, 'L1')
    assert oe.record_call_result(db, j, 'ANSWERED', 327) == 'RECORDED'
    assert oe.record_call_result(db, j, 'ANSWERED', 327) == 'DUPLICATE'      # polling después del webhook
    assert count(db, "SELECT COUNT(*) FROM wf_events WHERE event_type='CALL_RESULT' AND call_job_id=§", (j,)) == 1
    m = av.call_metrics(db, D0, D1)
    assert (m['answered'], m['talk_seconds']) == (1, 327), m
    assert m['talk_minutes'] == 5.5 and m['avg_talk_seconds'] == 327.0


def c_duracion_no_se_duplica(db):
    """Aunque el segundo aviso traiga otra duración, la primera gana."""
    j = job(db, 'L1')
    oe.record_call_result(db, j, 'ANSWERED', 300)
    oe.record_call_result(db, j, 'ANSWERED', 999)
    assert cj.job_get(db, j)['duration_seconds'] == 300
    assert av.call_metrics(db, D0, D1)['talk_seconds'] == 300


def c_resultado_en_conflicto(db):
    j = job(db, 'L1')
    oe.record_call_result(db, j, 'ANSWERED', 120)
    assert oe.record_call_result(db, j, 'NO_ANSWER', 0) == 'CONFLICT'
    assert cj.job_get(db, j)['result'] == 'ANSWERED', 'el primero gana'
    iss = db.one("SELECT * FROM wf_reconciliation_issues WHERE issue_type='RESULT_CONFLICT'")
    assert iss and iss['entity_id'] == j and iss['state'] == 'OPEN'
    assert count(db, "SELECT COUNT(*) FROM wf_events WHERE event_type='CALL_RESULT'") == 1


def c_account_duplicado(db):
    for _ in range(3):
        oe.record_event(db, 'ACCOUNT_CREATED', occurred_at='2026-09-21 12:00:00', provider='cashstudio',
                        lead_id='L1', country_iso='IN', source_workflow='WF3')
    assert count(db, "SELECT COUNT(*) FROM wf_events WHERE event_type='ACCOUNT_CREATED'") == 1
    assert av.business_metrics(db, D0, D1)['accounts_opened'] == 1


def c_payment_links_distintos(db):
    """Varios links para el mismo lead son hechos distintos (order_ref distinto)."""
    for ref in ('ORD-1', 'ORD-2', 'ORD-2'):
        oe.record_event(db, 'PAYMENT_LINK_CREATED', occurred_at='2026-09-21 12:00:00', provider='okpay',
                        order_ref=ref, lead_id='L1', amount=5000, currency='INR')
    assert av.business_metrics(db, D0, D1)['payment_links_created'] == 2
    meta = db.one("SELECT metadata_json FROM wf_events WHERE event_key='PAYMENT_LINK_CREATED:okpay:ORD-1'")
    assert 'ORD-1' in meta['metadata_json'], 'order_ref se conserva en metadata'


def c_event_key_valida(db):
    for bad in (lambda: oe.record_event(db, 'CALL_ANSWERED_X', call_job_id='x'),
                lambda: oe.record_event(db, 'ACCOUNT_CREATED', lead_id='L1'),
                lambda: oe.record_event(db, 'CALL_RESULT')):
        try:
            bad()
            raise AssertionError('debía rechazar')
        except ValueError as ex:
            assert 'VALIDATION_ERROR' in str(ex)


def c_postcall_tardio_resuelve(db):
    """UNKNOWN → NEEDS_RECONCILIATION; si el post-call aparece, lo resuelve."""
    j = cj.make_call_job_id('IN_STRINGEE')
    cj.claim_job(db, j, 'L5', 2, 'IN_STRINGEE', 'IN', 'stringee', 1)
    cj.mark_dispatching(db, j)
    cj.mark_unknown(db, j, 'timeout')
    db.execute("UPDATE wf_call_jobs SET updated_at='2000-01-01 00:00:00' WHERE call_job_id=§", (j,))
    cj.reconcile_stale_jobs(db)
    assert cj.job_get(db, j)['state'] == 'NEEDS_RECONCILIATION'
    assert oe.record_call_result(db, j, 'ANSWERED', 60) == 'RECORDED'
    assert cj.job_get(db, j)['state'] == 'COMPLETED'


def c_postcall_huerfano(db):
    assert oe.record_call_result(db, 'NO-EXISTE', 'ANSWERED', 10) == 'NOT_FOUND'
    assert db.one("SELECT issue_type FROM wf_reconciliation_issues")['issue_type'] == 'ORPHAN_POSTCALL'


# ── reintento técnico vs intento de negocio ───────────────────────────
def c_tecnico_no_consume(db):
    j = cj.make_call_job_id('IN_PROVEEDOR1')
    cj.claim_job(db, j, 'L1', 1, 'IN_PROVEEDOR1', 'IN', 'proveedor1', 4)
    cj.mark_dispatching(db, j)
    cj.mark_released(db, j, 'AUTH_ERROR', 'HTTP 401', 401)
    oe.record_event(db, 'CALL_TECH_FAILED', occurred_at='2026-09-21 10:00:00', call_job_id=j,
                    lead_id='L1', country_iso='IN', route_key='IN_PROVEEDOR1', provider='proveedor1')
    assert cj.next_attempt(db, 'L1', 3) == 4, 'sigue siendo el intento 4'
    m = av.call_metrics(db, D0, D1)
    assert (m['attempted'], m['failed_technical'], m['with_result']) == (0, 1, 0), m


def c_backoff(db):
    assert [cj.tech_backoff_minutes(n) for n in range(1, 10)] == [1, 2, 4, 8, 16, 32, 60, 60, 60]


def c_reintentos_tecnicos_acumulan(db):
    """Cada fallo técnico suma tech_retry_count, nunca attempt."""
    lead = 'LT'
    for n in range(1, 4):
        j = cj.make_call_job_id('IN_STRINGEE')
        db.execute("UPDATE wf_call_jobs SET next_tech_retry_at='2000-01-01 00:00:00' WHERE lead_id=§", (lead,))
        assert cj.claim_job(db, j, lead, 2, 'IN_STRINGEE', 'IN', 'stringee', 1)
        cj.mark_released(db, j, 'PROVIDER_DOWN', 'connection refused')
        row = cj.job_get(db, j)
        assert (row['attempt'], row['tech_retry_count']) == (1, n), row
    assert count(db, "SELECT COUNT(*) FROM wf_call_jobs WHERE lead_id='LT'") == 1


def c_next_attempt_crm_desfasado(db):
    j = job(db, 'L1', attempt=3)
    oe.record_call_result(db, j, 'NO_ANSWER', 0)
    assert cj.next_attempt(db, 'L1', crm_attempts=0) == 4, 'si el CRM no incrementa, manda lo local'
    assert cj.next_attempt(db, 'L1', crm_attempts=7) == 8, 'si el CRM va adelante, manda el CRM'


# ── una llamada en vuelo por lead ─────────────────────────────────────
def c_inflight(db):
    j1 = job(db, 'L1', attempt=1)
    j2 = cj.make_call_job_id('IN_STRINGEE')
    assert cj.claim_job(db, j2, 'L1', 2, 'IN_STRINGEE', 'IN', 'stringee', 2) is False
    found = cj.find_inflight_job(db, 'L1')
    assert found['call_job_id'] == j1, 'WF9: lead_id → call_job_id sin ambigüedad'
    oe.record_call_result(db, j1, 'NO_ANSWER', 0)
    assert cj.find_inflight_job(db, 'L1') is None
    assert cj.claim_job(db, j2, 'L1', 2, 'IN_STRINGEE', 'IN', 'stringee', 2) is True


# ── métricas sobre un dataset conocido ────────────────────────────────
def _dataset(db):
    """India: 3 llamadas (2 contestan, 1 no). Nepal: 2 (1 buzón, 1 callback).
    Stringee India: 1 en vuelo. Más un fallo técnico y una del día anterior."""
    a = job(db, 'A', route='IN_PROVEEDOR1', created='2026-09-21 09:05:00')
    b = job(db, 'B', route='IN_PROVEEDOR1', created='2026-09-21 09:40:00')
    c = job(db, 'C', route='IN_PROVEEDOR1', created='2026-09-21 10:10:00')
    d = job(db, 'D', route='NP_PROVEEDOR1', iso='NP', created='2026-09-21 10:20:00')
    e = job(db, 'E', route='NP_PROVEEDOR1', iso='NP', created='2026-09-21 11:00:00')
    job(db, 'F', route='IN_STRINGEE', prov='stringee', adapter='STRINGEE_WORKER', created='2026-09-21 11:30:00')
    old = job(db, 'G', created='2026-09-20 23:59:00')                     # fuera de rango
    oe.record_call_result(db, a, 'ANSWERED', 300)
    oe.record_call_result(db, b, 'ANSWERED', 180)
    oe.record_call_result(db, c, 'NO_ANSWER', 0)
    oe.record_call_result(db, d, 'VOICEMAIL', 20)
    oe.record_call_result(db, e, 'CALLBACK', 60, callback_at='2026-09-22 10:00:00')
    oe.record_call_result(db, old, 'ANSWERED', 999)
    return a, b, c, d, e


def c_metricas_globales(db):
    _dataset(db)
    m = av.call_metrics(db, D0, D1)
    want = {'attempted': 6, 'dispatched': 6, 'with_result': 5, 'answered': 3, 'no_answer': 1,
            'voicemail': 1, 'callbacks': 1, 'in_flight': 1, 'talk_seconds': 540}
    assert {k: m[k] for k in want} == want, {k: m[k] for k in want}
    assert m['answer_rate'] == 0.6 and m['talk_minutes'] == 9.0 and m['avg_talk_seconds'] == 180.0


def c_metricas_por_pais_proveedor_ruta(db):
    _dataset(db)
    by_c = {r['country']: r for r in av.call_metrics(db, D0, D1, 'country')}
    assert (by_c['IN']['attempted'], by_c['IN']['answered'], by_c['IN']['talk_seconds']) == (4, 2, 480)
    assert (by_c['NP']['attempted'], by_c['NP']['answered'], by_c['NP']['talk_seconds']) == (2, 1, 60)
    by_p = {r['provider']: r['attempted'] for r in av.call_metrics(db, D0, D1, 'provider')}
    assert by_p == {'proveedor1': 5, 'stringee': 1}, by_p
    by_r = {r['route']: r['answered'] for r in av.call_metrics(db, D0, D1, 'route')}
    assert by_r == {'IN_PROVEEDOR1': 2, 'IN_STRINGEE': 0, 'NP_PROVEEDOR1': 1}, by_r


def c_timeseries(db):
    _dataset(db)
    calls = {p['bucket'][:13]: p['value'] for p in av.timeseries(db, D0, D1, 'hour', 'calls')}
    assert calls == {'2026-09-21 09': 2, '2026-09-21 10': 2, '2026-09-21 11': 2}, calls
    ans = {p['bucket'][:13]: p['value'] for p in av.timeseries(db, D0, D1, 'hour', 'answered')}
    assert ans == {'2026-09-21 09': 2, '2026-09-21 10': 0, '2026-09-21 11': 1}, ans
    talk = av.timeseries(db, D0, D1, 'day', 'talk_minutes')
    assert len(talk) == 1 and talk[0]['value'] == 9.0, talk


def c_overview_vacio(db):
    o = av.overview(db, D0, D1)
    assert (o['calls'], o['answered'], o['answer_rate'], o['accounts_opened'], o['conversion_rate']) == \
        (0, 0, None, 0, None), o


def c_overview(db):
    _dataset(db)
    oe.record_event(db, 'ACCOUNT_CREATED', occurred_at='2026-09-21 15:00:00', provider='cashstudio',
                    lead_id='A', country_iso='IN')
    o = av.overview(db, D0, D1)
    assert (o['calls'], o['answered'], o['accounts_opened']) == (6, 3, 1)
    assert o['conversion_rate'] == round(1 / 3, 4)


def c_day_range_tz(db):
    s, e = av.day_range_utc(datetime(2026, 9, 21).date(), 'Asia/Kolkata')
    assert (s.strftime('%Y-%m-%d %H:%M'), e.strftime('%Y-%m-%d %H:%M')) == ('2026-09-20 18:30', '2026-09-21 18:30')


# ── atribución ────────────────────────────────────────────────────────
def c_atribucion(db):
    # lead A: dos llamadas contestadas; la cuenta se abre después de la segunda
    a1 = job(db, 'A', attempt=1, route='IN_STRINGEE', prov='stringee', created='2026-09-20 08:00:00')
    oe.record_call_result(db, a1, 'ANSWERED', 100)
    set_completed(db, a1, '2026-09-20 08:05:00')
    a2 = job(db, 'A', attempt=2, route='IN_PROVEEDOR1', created='2026-09-21 09:00:00')
    oe.record_call_result(db, a2, 'ANSWERED', 200)
    set_completed(db, a2, '2026-09-21 09:05:00')
    # llamada POSTERIOR a la apertura: no puede atribuirse
    a3 = job(db, 'A', attempt=3, route='IN_STRINGEE', prov='stringee', created='2026-09-21 16:00:00')
    oe.record_call_result(db, a3, 'ANSWERED', 50)
    set_completed(db, a3, '2026-09-21 16:05:00')
    oe.record_event(db, 'ACCOUNT_CREATED', occurred_at='2026-09-21 12:00:00', provider='cashstudio', lead_id='A')
    # lead B: ninguna llamada contestada → UNATTRIBUTED
    b1 = job(db, 'B', created='2026-09-21 08:00:00')
    oe.record_call_result(db, b1, 'NO_ANSWER', 0)
    oe.record_event(db, 'ACCOUNT_CREATED', occurred_at='2026-09-21 13:00:00', provider='cashstudio', lead_id='B')
    # lead C: contestó hace 40 días → fuera de ventana → UNATTRIBUTED
    c1 = job(db, 'C', created='2026-08-12 08:00:00')
    oe.record_call_result(db, c1, 'ANSWERED', 90)
    set_completed(db, c1, '2026-08-12 08:05:00')
    oe.record_event(db, 'ACCOUNT_CREATED', occurred_at='2026-09-21 14:00:00', provider='cashstudio', lead_id='C')

    got = {x['lead_id']: x for x in av.accounts_attributed(db, D0, D1)}
    assert got['A']['call_job_id'] == a2 and got['A']['route_key'] == 'IN_PROVEEDOR1', got['A']
    assert got['B']['attributed'] is False and got['B']['route_key'] == 'UNATTRIBUTED'
    assert got['C']['attributed'] is False, 'fuera de la ventana de 30 días'
    assert all(x['attribution_model'] == 'LAST_CONNECTED_CALL_V1' for x in got.values())
    assert {r['route']: r['accounts_opened'] for r in av.accounts_by(db, D0, D1, 'route')} == \
        {'IN_PROVEEDOR1': 1, 'UNATTRIBUTED': 2}


# ── reconciliación ────────────────────────────────────────────────────
def c_reconciliacion(db):
    oe.record_event(db, 'ACCOUNT_CREATED', occurred_at='2026-09-21 12:00:00', provider='cashstudio', lead_id='L1')
    assert oe.reconcile_account(db, 'L1', crm_account_opened=True) is None
    k = oe.reconcile_account(db, 'L1', crm_account_opened=False)
    oe.reconcile_account(db, 'L1', crm_account_opened=False)            # próxima corrida de WF14
    rows = db.q("SELECT * FROM wf_reconciliation_issues")
    assert len(rows) == 1 and int(rows[0]['occurrences']) == 2 and rows[0]['issue_type'] == 'CRM_ACCOUNT_MISSING'
    assert count(db, "SELECT COUNT(*) FROM wf_events WHERE event_type='ACCOUNT_CREATED'") == 1, \
        'la diferencia con el CRM NUNCA borra el dato local'
    assert oe.resolve_issue(db, k, 'master')
    assert not oe.resolve_issue(db, k, 'master')


CASES = [
    ('idempotencia (punto 24)', [
        ('mismo CALL_ANSWERED ×2 → una métrica, un evento', c_answered_dos_veces),
        ('la duración no se duplica ni se pisa', c_duracion_no_se_duplica),
        ('resultado contradictorio → primero gana + issue RESULT_CONFLICT', c_resultado_en_conflicto),
        ('ACCOUNT_CREATED ×3 → una cuenta', c_account_duplicado),
        ('links de pago distintos cuentan; el repetido no', c_payment_links_distintos),
        ('clave de evento incompleta o tipo desconocido → VALIDATION_ERROR', c_event_key_valida),
        ('post-call tardío resuelve NEEDS_RECONCILIATION', c_postcall_tardio_resuelve),
        ('post-call sin job → issue ORPHAN_POSTCALL', c_postcall_huerfano)]),
    ('técnico vs negocio', [
        ('401 no consume intento ni cuenta como llamada; sí como failed_technical', c_tecnico_no_consume),
        ('backoff 1,2,4…60 min', c_backoff),
        ('3 fallos técnicos: tech_retry_count 1→3, attempt fijo, una sola fila', c_reintentos_tecnicos_acumulan),
        ('next_attempt con CRM desfasado (R-4)', c_next_attempt_crm_desfasado)]),
    ('una llamada en vuelo por lead', [
        ('intento 2 bloqueado con el 1 en vuelo; lead_id → call_job_id', c_inflight)]),
    ('métricas locales', [
        ('globales: attempted, answered, answer_rate, minutos', c_metricas_globales),
        ('por país, proveedor y ruta', c_metricas_por_pais_proveedor_ruta),
        ('timeseries por hora y por día', c_timeseries),
        ('overview vacío: sin divisiones por cero', c_overview_vacio),
        ('overview con cuentas y conversión', c_overview),
        ('rango de "hoy" en Asia/Kolkata', c_day_range_tz)]),
    ('atribución', [
        ('última llamada conectada previa; posterior ignorada; sin evidencia → UNATTRIBUTED', c_atribucion)]),
    ('reconciliación', [
        ('CRM no refleja la cuenta → issue único, occurrences, dato local intacto', c_reconciliacion)]),
]


def main():
    backends = os.getenv('LM_TEST_BACKENDS', 'sqlite,mysql').split(',')
    if 'mysql' in backends and not mysql_available():
        S.skip('backend mysql', 'MariaDB no disponible')
        backends = [b for b in backends if b != 'mysql']
    for be in backends:
        for section, cases in CASES:
            S.section(f'[{be}] {section}')
            for name, fn in cases:
                S.check(f'[{be}] {name}', lambda fn=fn, be=be: fn(make_db(be)))
    return S.finish()


if __name__ == '__main__':
    raise SystemExit(main())
