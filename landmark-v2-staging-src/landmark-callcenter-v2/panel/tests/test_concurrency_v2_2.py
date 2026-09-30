"""
test_concurrency_v2_1.py — Claims atómicos con concurrencia REAL en MariaDB.

Cada hilo abre SU PROPIA conexión (como dos workers de n8n o dos ramas en
paralelo) y todos arrancan a la vez detrás de una barrera.
"""
import threading

from _harness import Suite, MIGRATION, mysql_available, mysql_conn, fresh_mysql_db, run_sql_file

import call_jobs as cj     # noqa: E402
import ops_events as oe    # noqa: E402
from analytics import DB   # noqa: E402

S = Suite('CONCURRENCIA (MariaDB)')
DBNAME = 'conc_v22'
N = 40


def race(n, fn):
    barrier = threading.Barrier(n)
    results, errors = [None] * n, []

    def worker(i):
        conn = mysql_conn(DBNAME)
        db = DB(conn, 'mysql')
        try:
            barrier.wait()
            results[i] = fn(db, i)
        except Exception as ex:          # noqa: BLE001
            errors.append(f'{type(ex).__name__}: {ex}')
        finally:
            conn.close()
    threads = [threading.Thread(target=worker, args=(i,)) for i in range(n)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert not errors, errors[:3]
    return results


def rows(sql, args=None):
    c = mysql_conn(DBNAME)
    with c.cursor() as cur:
        cur.execute(sql, args)
        out = cur.fetchall()
    c.close()
    return out


def t_mismo_lead_mismo_intento():
    """40 despachadores de 2 rutas del mismo país pelean por el mismo lead."""
    def fn(db, i):
        key = 'IN_PROVEEDOR1' if i % 2 else 'IN_STRINGEE'
        return cj.claim_job(db, cj.make_call_job_id(key), 'LEAD-RACE', 1, key, 'IN', 'x', 1, f'exec-{i}')
    res = race(N, fn)
    assert res.count(True) == 1, f'ganadores: {res.count(True)}'
    assert rows("SELECT COUNT(*) FROM wf_call_jobs WHERE lead_id='LEAD-RACE' AND attempt=1")[0][0] == 1


def t_leads_distintos():
    res = race(N, lambda db, i: cj.claim_job(db, cj.make_call_job_id('IN_PROVEEDOR1'), f'LEAD-{i}', 1,
                                             'IN_PROVEEDOR1', 'IN', 'proveedor1', 1))
    assert all(res), f'{res.count(False)} perdieron sin competir'


def t_intentos_distintos():
    """V2.2: 10 intentos distintos del MISMO lead a la vez desde 2 rutas = serían
    10 llamadas simultáneas a la misma persona. Gana exactamente uno."""
    res = race(10, lambda db, i: cj.claim_job(db, cj.make_call_job_id('IN_STRINGEE' if i % 2 else 'IN_PROVEEDOR1'),
                                              'LEAD-MULTI', 1, 'IN_X', 'IN', 'x', i + 1))
    assert res.count(True) == 1, f'{res.count(True)} llamadas en vuelo al mismo lead'
    assert rows("SELECT COUNT(*) FROM wf_call_jobs WHERE inflight_lead='LEAD-MULTI'")[0][0] == 1


def t_mezcla():
    """10 leads × 4 competidores cada uno → exactamente 10 ganadores."""
    res = race(N, lambda db, i: cj.claim_job(db, cj.make_call_job_id('IN_PROVEEDOR1'), f'MIX-{i % 10}', 1,
                                             'IN_PROVEEDOR1', 'IN', 'proveedor1', 1))
    assert res.count(True) == 10, res.count(True)


def t_dispatching_una_vez():
    db = DB(mysql_conn(DBNAME), 'mysql')
    job = cj.make_call_job_id('IN_PROVEEDOR1')
    assert cj.claim_job(db, job, 'LEAD-DISP', 1, 'IN_PROVEEDOR1', 'IN', 'proveedor1', 1)
    res = race(N, lambda db, i: cj.mark_dispatching(db, job))
    assert res.count(True) == 1, f'{res.count(True)} hilos habrían despachado la misma llamada'


def t_reclaim_pre_dispatch():
    db = DB(mysql_conn(DBNAME), 'mysql')
    job = cj.make_call_job_id('IN_PROVEEDOR1')
    cj.claim_job(db, job, 'LEAD-RE', 1, 'IN_PROVEEDOR1', 'IN', 'proveedor1', 1)
    cj.mark_released(db, job, 'AUTH_ERROR', 'HTTP 401', 401)
    db.execute("UPDATE wf_call_jobs SET next_tech_retry_at='2000-01-01' WHERE lead_id='LEAD-RE'")
    res = race(N, lambda db, i: cj.claim_job(db, cj.make_call_job_id('IN_STRINGEE'), 'LEAD-RE', 1,
                                             'IN_STRINGEE', 'IN', 'stringee', 1))
    assert res.count(True) == 1, res.count(True)
    assert rows("SELECT attempt, tech_retry_count FROM wf_call_jobs WHERE lead_id='LEAD-RE'")[0] == (1, 1)


def t_webhook_vs_polling():
    """La misma conversación llega por webhook y por polling, a la vez, muchas veces."""
    res = race(N, lambda db, i: cj.claim_conversation(db, 'conv_RACE', 'webhook' if i % 2 else 'polling',
                                                      route_key='IN_STRINGEE', lead_id='L1'))
    assert res.count(True) == 1, res.count(True)
    assert rows("SELECT state FROM wf_conversation_ledger WHERE conversation_id='conv_RACE'")[0][0] == 'CLAIMED'


def t_complete_una_vez():
    db = DB(mysql_conn(DBNAME), 'mysql')
    cj.claim_conversation(db, 'conv_DONE', 'webhook')
    res = race(N, lambda db, i: cj.complete_conversation(db, 'conv_DONE', f'fu_{i}'))
    assert res.count(True) == 1
    fu = rows("SELECT followup_id FROM wf_conversation_ledger WHERE conversation_id='conv_DONE'")[0][0]
    assert fu.startswith('fu_')


def t_evento_mismo_hecho():
    res = race(N, lambda db, i: oe.record_event(db, 'ACCOUNT_CREATED', provider='cashstudio',
                                                lead_id='LEAD-ACC', country_iso='IN'))
    assert rows("SELECT COUNT(*) FROM wf_events WHERE event_key='ACCOUNT_CREATED:cashstudio:LEAD-ACC'")[0][0] == 1


def t_resultado_concurrente():
    """Webhook y polling (×20 cada uno) entregan el resultado a la vez."""
    db = DB(mysql_conn(DBNAME), 'mysql')
    job = cj.make_call_job_id('IN_STRINGEE')
    cj.claim_job(db, job, 'LEAD-RES', 2, 'IN_STRINGEE', 'IN', 'stringee', 1)
    cj.mark_dispatching(db, job)
    cj.mark_dispatched(db, job, provider_job_id='job_1')
    res = race(N, lambda db, i: oe.record_call_result(db, job, 'ANSWERED', 300 + i))
    assert res.count('RECORDED') == 1, res
    assert set(res) <= {'RECORDED', 'DUPLICATE'}, set(res)
    assert rows("SELECT COUNT(*) FROM wf_events WHERE event_type='CALL_RESULT' AND call_job_id=%s", (job,))[0][0] == 1
    d = rows("SELECT duration_seconds FROM wf_call_jobs WHERE call_job_id=%s", (job,))[0][0]
    assert 300 <= d < 300 + N, 'una sola duración, la del ganador'


def main():
    if not mysql_available():
        S.skip('suite completa', 'MariaDB no disponible')
        return S.finish()
    fresh_mysql_db(DBNAME)
    run_sql_file(DBNAME, MIGRATION)
    S.section(f'wf_call_jobs · {N} hilos, una conexión cada uno')
    S.check(f'mismo lead + mismo intento, 2 rutas del mismo país → 1 ganador de {N}', t_mismo_lead_mismo_intento)
    S.check(f'{N} leads distintos → {N} ganadores', t_leads_distintos)
    S.check('mismo lead, intentos 1..10 simultáneos → 1 ganador (una llamada en vuelo)', t_intentos_distintos)
    S.check('10 leads × 4 competidores → exactamente 10 ganadores', t_mezcla)
    S.check(f'mark_dispatching concurrente → 1 solo despacho de {N}', t_dispatching_una_vez)
    S.check(f're-claim de intento RELEASED (401) tras backoff → 1 ganador de {N}, attempt fijo', t_reclaim_pre_dispatch)
    S.section(f'wf_conversation_ledger · {N} hilos')
    S.check(f'webhook vs polling misma conversación → 1 ganador de {N}', t_webhook_vs_polling)
    S.check(f'complete concurrente → 1 solo followup_id registrado', t_complete_una_vez)
    S.section(f'event store · {N} hilos')
    S.check(f'ACCOUNT_CREATED ×{N} simultáneos → 1 fila', t_evento_mismo_hecho)
    S.check(f'resultado ×{N} simultáneos → 1 RECORDED, 1 evento, 1 duración', t_resultado_concurrente)
    return S.finish()


if __name__ == '__main__':
    raise SystemExit(main())
