"""
test_migration_v2_2.py — MIGRATION_001 V2.2 contra MariaDB REAL.

Escenarios:
  A base limpia (paridad, constraints, índices, UNIQUE en vuelo impuesto por la base)
  B re-ejecución no-op · C ediciones del panel preservadas · D limpia interrumpida
  E upgrade V2.1 → V2.2 con datos · F upgrade interrumpido y reanudado
  G upgrade ≡ instalación limpia (esquema funcional) · H aborto explícito y reanudación
  I sin sentencias destructivas · J tablas productivas intactas · K cliente `mariadb`
"""
import re
import subprocess

from _harness import (Suite, MIGRATION, V21_MIGRATION, MYSQL, mysql_available, mysql_conn,
                      fresh_mysql_db, run_sql_file, split_sql)

S = Suite('MIGRACIÓN V2.2 (MariaDB)')

CONFIG_TABLES = {
    'countries': 'iso',
    'voice_providers': 'id',
    'followup_policies': 'id',
    'call_routes': 'id',
    'route_capacity_windows': 'id',
    'route_telegram_targets': 'id',
    'country_tool_configs': 'id',
}
TS_COLS = {'created_at', 'updated_at', 'applied_at', 'claimed_at', 'changed_at'}


def q(db, sql, args=None):
    c = mysql_conn(db)
    with c.cursor() as cur:
        cur.execute(sql, args)
        rows = cur.fetchall()
        cols = [d[0] for d in cur.description] if cur.description else []
    c.close()
    return [dict(zip(cols, r)) for r in rows]


def ex(db, sql, args=None):
    c = mysql_conn(db)
    with c.cursor() as cur:
        cur.execute(sql, args)
    c.close()


def snapshot(db):
    """Contenido de todas las tablas de config, sin timestamps."""
    snap = {}
    for t, pk in CONFIG_TABLES.items():
        rows = q(db, f"SELECT * FROM {t} ORDER BY {pk}")
        snap[t] = [{k: v for k, v in r.items() if k not in TS_COLS} for r in rows]
    snap['_markers'] = [r['migration_id'] for r in
                        q(db, "SELECT migration_id FROM schema_migrations ORDER BY migration_id")]
    return snap


def diff(a, b):
    out = []
    for t in a:
        if a[t] != b.get(t):
            out.append(f"{t}: {len(a[t])} vs {len(b.get(t) or [])} filas, contenido distinto")
    return out


OUR_TABLES = list(CONFIG_TABLES) + ['route_audit', 'wf_call_jobs', 'wf_conversation_ledger',
                                    'wf_events', 'wf_reconciliation_issues', 'schema_migrations']


def jobs(db):
    return [{k: v for k, v in r.items() if k not in TS_COLS | {'dispatched_at', 'completed_at',
                                                            'next_tech_retry_at'}}
            for r in q(db, "SELECT * FROM wf_call_jobs ORDER BY id")]


def schema(db):
    cols = {(r['TABLE_NAME'], r['COLUMN_NAME']): (r['COLUMN_TYPE'].lower(), r['IS_NULLABLE'])
            for r in q(db, "SELECT TABLE_NAME, COLUMN_NAME, COLUMN_TYPE, IS_NULLABLE FROM "
                           "information_schema.COLUMNS WHERE TABLE_SCHEMA=%s", (db,))
            if r['TABLE_NAME'] in OUR_TABLES}
    idx = {(r['TABLE_NAME'], r['INDEX_NAME']): (r['cols'], int(r['NON_UNIQUE']))
           for r in q(db, "SELECT TABLE_NAME, INDEX_NAME, NON_UNIQUE, GROUP_CONCAT(COLUMN_NAME ORDER BY "
                          "SEQ_IN_INDEX) cols FROM information_schema.STATISTICS WHERE TABLE_SCHEMA=%s "
                          "GROUP BY TABLE_NAME, INDEX_NAME, NON_UNIQUE", (db,))
           if r['TABLE_NAME'] in OUR_TABLES}
    return cols, idx


def v21_with_data(db):
    """Base con V2.1 aplicada y uso real simulado."""
    fresh_mysql_db(db)
    run_sql_file(db, V21_MIGRATION)
    ex(db, "UPDATE call_routes SET capacity_default=4, notes='editada' WHERE route_key='IN_STRINGEE'")
    ex(db, "UPDATE route_telegram_targets SET enabled=0 WHERE chat_id='-1003984044945'")
    ex(db, """INSERT INTO wf_call_jobs (call_job_id,lead_id,route_id,route_key,country_iso,provider,attempt,
              state,error_code,conversation_id) VALUES
              ('j1','L1',1,'IN_PROVEEDOR1','IN','proveedor1',1,'COMPLETED',NULL,'conv_1'),
              ('j2','L2',1,'IN_PROVEEDOR1','IN','proveedor1',1,'FAILED','PRE_DISPATCH',NULL),
              ('j3','L3',2,'IN_STRINGEE','IN','stringee',3,'DISPATCHED',NULL,NULL),
              ('j4','L4',1,'IN_PROVEEDOR1','IN','proveedor1',2,'FAILED','PROVIDER_ERROR',NULL)""")
    ex(db, "UPDATE wf_call_jobs SET dispatched_at=NOW() WHERE call_job_id='j4'")


# ══════════════════════════════════════════════════════════════════════

def t_A_fresh():
    fresh_mysql_db('mig_a')
    run_sql_file('mig_a', MIGRATION)
    r = q('mig_a', """SELECT r.route_key, p.code, p.adapter_key, p.enabled pe, c.enabled ce, r.enabled,
                             r.capacity_default, c.dial_prefix, c.timezone, r.caller_id, fp.policy_key
                        FROM call_routes r JOIN voice_providers p ON p.id=r.provider_id
                        JOIN countries c ON c.iso=r.iso
                        LEFT JOIN followup_policies fp ON fp.id=r.followup_policy_id ORDER BY r.priority""")
    got = [(x['route_key'], x['code'], x['adapter_key'], x['pe'], x['ce'], x['enabled'], x['capacity_default'],
            x['dial_prefix'], x['timezone'], x['caller_id'], x['policy_key']) for x in r]
    want = [('IN_PROVEEDOR1', 'proveedor1', 'ELEVENLABS_SIP', 1, 1, 1, 6, '+91', 'Asia/Kolkata', None, 'STANDARD_CALL_RETRY'),
            ('IN_STRINGEE', 'stringee', 'STRINGEE_WORKER', 1, 1, 1, 1, '+91', 'Asia/Kolkata', '917971730907', 'STANDARD_CALL_RETRY'),
            ('NP_PROVEEDOR1', 'proveedor1', 'ELEVENLABS_SIP', 1, 1, 0, 6, '+977', 'Asia/Kathmandu', None, 'STANDARD_CALL_RETRY')]
    assert got == want, got
    assert q('mig_a', "SELECT COUNT(*) n FROM voice_providers")[0]['n'] == 2, 'UN proveedor SIP, no uno por país'
    tools = {(t['country_iso'], t['tool_type']): (t['enabled'], t['market'], t['currency'])
             for t in q('mig_a', "SELECT * FROM country_tool_configs")}
    assert tools[('IN', 'CREATE_ACCOUNT')] == (1, 'IND', None)
    assert tools[('IN', 'CREATE_PAYMENT_LINK')] == (1, None, 'INR')
    assert tools[('NP', 'CREATE_PAYMENT_LINK')] == (0, None, 'NPR')
    dflt = q('mig_a', "SELECT COLUMN_DEFAULT d FROM information_schema.COLUMNS WHERE TABLE_SCHEMA='mig_a' "
                      "AND TABLE_NAME='countries' AND COLUMN_NAME='enabled'")[0]['d']
    assert str(dflt) == '0', f'un país nuevo debe nacer OFF (default={dflt})'


def t_A_tablas_y_constraints():
    cols, idx = schema('mig_a')
    u = {(t, c) for (t, _), (c, nu) in idx.items() if nu == 0}
    for need in [('wf_call_jobs', 'lead_id,attempt'), ('wf_call_jobs', 'call_job_id'),
                 ('wf_call_jobs', 'inflight_lead'), ('wf_events', 'event_key'),
                 ('wf_reconciliation_issues', 'issue_key'), ('call_routes', 'route_key'),
                 ('wf_conversation_ledger', 'conversation_id')]:
        assert need in u, f'falta UNIQUE {need}'
    assert ('call_routes', 'iso,provider_id') not in u, 'no debe haber UNIQUE(iso, provider_id)'
    assert ('voice_providers', 'provider_kind') not in cols, 'instalación limpia: sin provider_kind'
    assert cols[('voice_providers', 'adapter_key')] == ('varchar(64)', 'NO'), 'adapter_key VARCHAR, no ENUM'
    ix = {(t, c) for (t, _), (c, nu) in idx.items()}
    for need in [('wf_events', 'occurred_at'), ('wf_events', 'event_type,occurred_at'),
                 ('wf_events', 'country_iso,occurred_at'), ('wf_events', 'route_key,occurred_at'),
                 ('wf_events', 'provider,occurred_at'), ('wf_events', 'lead_id'), ('wf_events', 'call_job_id'),
                 ('wf_events', 'conversation_id'), ('wf_call_jobs', 'country_iso,created_at'),
                 ('wf_call_jobs', 'route_key,created_at'), ('wf_call_jobs', 'provider,created_at'),
                 ('wf_call_jobs', 'conversation_id'), ('wf_call_jobs', 'lead_id'),
                 ('wf_call_jobs', 'created_at,country_iso,route_key,provider,state,result,duration_seconds,dispatched_at'),
                 ('wf_events', 'event_type,occurred_at,country_iso,route_key,provider,result')]:
        assert need in ix, f'falta índice {need}'
    gen = q('mig_a', "SELECT EXTRA FROM information_schema.COLUMNS WHERE TABLE_SCHEMA='mig_a' "
                     "AND TABLE_NAME='wf_call_jobs' AND COLUMN_NAME='inflight_lead'")[0]['EXTRA']
    assert 'GENERATED' in gen.upper(), gen


def t_A_inflight_impuesto_por_la_base():
    """La garantía no depende del código: la base rechaza la segunda llamada en vuelo."""
    ins = ("INSERT INTO wf_call_jobs (call_job_id,lead_id,route_id,route_key,country_iso,provider,attempt,state) "
           "VALUES (%s,'LX',1,'IN_PROVEEDOR1','IN','proveedor1',%s,%s)")
    ex('mig_a', ins, ('x1', 1, 'DISPATCHED'))
    try:
        ex('mig_a', ins, ('x2', 2, 'CLAIMED'))
        raise AssertionError('la base aceptó dos llamadas en vuelo para el mismo lead')
    except AssertionError:
        raise
    except Exception as e:
        assert '1062' in str(e) and 'inflight' in str(e), e
    ex('mig_a', "UPDATE wf_call_jobs SET state='COMPLETED', result='NO_ANSWER' WHERE call_job_id='x1'")
    ex('mig_a', ins, ('x2', 2, 'CLAIMED'))
    ex('mig_a', "UPDATE wf_call_jobs SET state='RELEASED' WHERE call_job_id='x2'")
    ex('mig_a', ins, ('x3', 3, 'CLAIMED'))          # RELEASED no está en vuelo
    ex('mig_a', "DELETE FROM wf_call_jobs WHERE lead_id='LX'")   # limpiar para B


def t_B_rerun_noop():
    before = snapshot('mig_a')
    run_sql_file('mig_a', MIGRATION)
    run_sql_file('mig_a', MIGRATION)
    assert snapshot('mig_a') == before, diff(before, snapshot('mig_a'))


def t_C_ediciones_preservadas():
    db = 'mig_c'
    fresh_mysql_db(db)
    run_sql_file(db, MIGRATION)
    ex(db, "UPDATE call_routes SET capacity_default=6 WHERE route_key='IN_STRINGEE'")
    ex(db, "UPDATE call_routes SET notes='nota del usuario', elevenlabs_agent_id='agent_NUEVO' "
           "WHERE route_key='IN_PROVEEDOR1'")
    ex(db, "UPDATE route_telegram_targets SET enabled=0 WHERE chat_id='-1003984044945'")
    ex(db, "DELETE FROM route_telegram_targets WHERE purpose='account'")
    ex(db, "UPDATE call_routes SET archived_at=NOW(), enabled=0 WHERE route_key='NP_PROVEEDOR1'")
    ex(db, "UPDATE followup_policies SET policy_json=REPLACE(policy_json,'\"+2h\"','\"+90m\"')")
    ex(db, "UPDATE country_tool_configs SET enabled=0 WHERE country_iso='IN' AND tool_type='CREATE_PAYMENT_LINK'")
    ex(db, "UPDATE countries SET enabled=0 WHERE iso='IN'")                 # interruptor de país
    ex(db, "UPDATE voice_providers SET enabled=0 WHERE code='stringee'")    # interruptor de proveedor
    ex(db, "UPDATE voice_providers SET adapter_key='ELEVENLABS_SIP_V2' WHERE code='proveedor1'")
    ex(db, "INSERT INTO route_capacity_windows (route_id,day_mask,start_local,end_local,capacity) "
           "SELECT id,'mon','09:00','14:00',5 FROM call_routes WHERE route_key='IN_PROVEEDOR1'")
    before = snapshot(db)
    run_sql_file(db, MIGRATION)
    after = snapshot(db)
    assert before == after, diff(before, after)
    assert q(db, "SELECT enabled FROM countries WHERE iso='IN'")[0]['enabled'] == 0, 'reencendió India'
    assert q(db, "SELECT COUNT(*) n FROM route_telegram_targets WHERE purpose='account'")[0]['n'] == 0


def t_D_interrumpida():
    fresh_mysql_db('mig_d_ref')
    total = run_sql_file('mig_d_ref', MIGRATION)
    ref = snapshot('mig_d_ref')
    stmts = split_sql(open(MIGRATION).read())
    seed = next(i for i, s in enumerate(stmts) if s.startswith('INSERT INTO countries'))
    for k in sorted({5, 30, 60, seed, seed + 3, seed + 6, seed + 9, total - 1}):
        fresh_mysql_db('mig_d')
        run_sql_file('mig_d', MIGRATION, limit=k)
        run_sql_file('mig_d', MIGRATION)
        got = snapshot('mig_d')
        assert got == ref, f'corte tras {k}/{total}: ' + '; '.join(diff(ref, got))


def t_E_upgrade_desde_v21():
    db = 'mig_e'
    v21_with_data(db)
    run_sql_file(db, MIGRATION)
    once = (snapshot(db), jobs(db))
    run_sql_file(db, MIGRATION)
    assert (snapshot(db), jobs(db)) == once, 'la 2ª ejecución del upgrade cambió algo'
    cs = {r['iso']: r['enabled'] for r in q(db, "SELECT iso, enabled FROM countries")}
    assert cs == {'IN': 1, 'NP': 1}, f'upgrade no debe apagar países que ya llamaban: {cs}'
    pv = {r['code']: (r['adapter_key'], r['provider_kind']) for r in q(db, "SELECT * FROM voice_providers")}
    assert pv == {'proveedor1': ('ELEVENLABS_SIP', 'sip'), 'stringee': ('STRINGEE_WORKER', 'stringee')}, pv
    r = q(db, "SELECT capacity_default, notes FROM call_routes WHERE route_key='IN_STRINGEE'")[0]
    assert (r['capacity_default'], r['notes']) == (4, 'editada'), 'el upgrade pisó configuración'
    assert q(db, "SELECT COUNT(*) n FROM route_telegram_targets WHERE enabled=0")[0]['n'] == 4
    st = {j['call_job_id']: (j['state'], j['error_class'], j['inflight_lead']) for j in jobs(db)}
    assert st['j1'] == ('COMPLETED', None, None)
    assert st['j2'] == ('RELEASED', 'TECHNICAL', None), 'PRE_DISPATCH de V2.1 → RELEASED'
    assert st['j3'] == ('DISPATCHED', None, 'L3'), 'inflight calculado para datos existentes'
    assert st['j4'][0] == 'FAILED', 'un FAILED que sí se despachó NO se libera'
    m = [x['migration_id'] for x in q(db, "SELECT migration_id FROM schema_migrations")]
    assert '001_multi_country_config_v2_2:seed' not in m, 'el seed V2.2 no corre sobre una base ya sembrada'
    ex(db, "INSERT INTO voice_providers (code, display_name, adapter_key) VALUES ('p2','P2','ELEVENLABS_SIP')")


def t_F_upgrade_interrumpido():
    v21_with_data('mig_f_ref')
    run_sql_file('mig_f_ref', MIGRATION)
    ref = (snapshot('mig_f_ref'), jobs('mig_f_ref'), schema('mig_f_ref'))
    total = len(split_sql(open(MIGRATION).read()))
    for k in sorted({3, 10, 20, 35, 50, 70, 90, total - 1}):
        v21_with_data('mig_f')
        run_sql_file('mig_f', MIGRATION, limit=k)
        run_sql_file('mig_f', MIGRATION)
        got = (snapshot('mig_f'), jobs('mig_f'), schema('mig_f'))
        assert got[0] == ref[0], f'corte {k}/{total} config: ' + '; '.join(diff(ref[0], got[0]))
        assert got[1] == ref[1], f'corte {k}/{total}: wf_call_jobs distinto'
        assert got[2] == ref[2], f'corte {k}/{total}: esquema distinto'


def t_G_upgrade_equivale_a_limpia():
    """Todo lo que tiene una instalación limpia existe, con el mismo tipo, en una
    base actualizada desde V2.1. (La actualizada conserva además provider_kind
    NULL-able, porque la migración no borra columnas.)"""
    fresh_mysql_db('mig_g_new')
    run_sql_file('mig_g_new', MIGRATION)
    new_cols, new_idx = schema('mig_g_new')
    v21_with_data('mig_g_up')
    run_sql_file('mig_g_up', MIGRATION)
    up_cols, up_idx = schema('mig_g_up')
    miss_c = {k: v for k, v in new_cols.items() if up_cols.get(k) != v}
    miss_i = {k: v for k, v in new_idx.items() if k not in up_idx and v not in
              {val for (t, _), val in up_idx.items() if t == k[0]}}
    assert not miss_c, f'columnas distintas tras upgrade: {miss_c}'
    assert not miss_i, f'índices ausentes tras upgrade: {miss_i}'
    extra = sorted(k for k in up_cols if k not in new_cols)
    assert extra == [('voice_providers', 'provider_kind')], extra
    assert up_cols[('voice_providers', 'provider_kind')][1] == 'YES'


def t_H_aborto_y_reanudacion():
    db = 'mig_h'
    fresh_mysql_db(db)
    run_sql_file(db, V21_MIGRATION)
    ex(db, """INSERT INTO wf_call_jobs (call_job_id,lead_id,route_id,route_key,country_iso,provider,attempt,state)
              VALUES ('a','LX',1,'IN_PROVEEDOR1','IN','p',1,'DISPATCHED'),('b','LX',2,'IN_STRINGEE','IN','s',2,'DISPATCHING')""")
    p = subprocess.run(['mariadb', f"-u{MYSQL['user']}", f"-p{MYSQL['password']}", db],
                       stdin=open(MIGRATION), capture_output=True, text=True)
    assert p.returncode != 0 and 'ABORT_lead_has_2_inflight_calls' in p.stderr, p.stderr[:200]
    ex(db, "UPDATE wf_call_jobs SET state='COMPLETED', result='UNKNOWN' WHERE call_job_id='a'")  # decisión humana
    run_sql_file(db, MIGRATION)
    assert q(db, "SELECT inflight_lead FROM wf_call_jobs WHERE call_job_id='b'")[0]['inflight_lead'] == 'LX'
    assert '001_multi_country_config_v2_2' in [x['migration_id'] for x in q(db, "SELECT migration_id FROM schema_migrations")]


def t_I_sin_sentencias_destructivas():
    stmts = split_sql(open(MIGRATION).read())
    bad = [s[:70] for s in stmts if re.match(r'^\s*(DROP|TRUNCATE|DELETE)\b', s, re.I)
           or re.search(r'\bDROP\s+(TABLE|DATABASE|COLUMN|INDEX)\b', s, re.I)]
    assert not bad, bad


def t_J_tablas_productivas_intactas():
    db = 'mig_j'
    fresh_mysql_db(db)
    prod = {
        'crm_leads': "CREATE TABLE crm_leads (lead_id VARCHAR(64) PRIMARY KEY, status VARCHAR(48), provider VARCHAR(32))",
        'crm_conversions': "CREATE TABLE crm_conversions (id INT AUTO_INCREMENT PRIMARY KEY, lead_id VARCHAR(64), account_opened TINYINT)",
        'wf_call_followups': "CREATE TABLE wf_call_followups (id INT AUTO_INCREMENT PRIMARY KEY, phone VARCHAR(32), followup_id VARCHAR(64), provider VARCHAR(32), recording_synced TINYINT)",
        'wf2_provider_config': "CREATE TABLE wf2_provider_config (provider VARCHAR(32) PRIMARY KEY, enabled TINYINT)",
        'stringee_calls': "CREATE TABLE stringee_calls (call_id VARCHAR(64) PRIMARY KEY, phone VARCHAR(32), conversation_id VARCHAR(128))",
        'n8n_switches': "CREATE TABLE n8n_switches (id INT AUTO_INCREMENT PRIMARY KEY, label VARCHAR(64), workflow_ids TEXT)",
    }
    for ddl in prod.values():
        ex(db, ddl)
    ex(db, "INSERT INTO crm_leads VALUES ('L1','NOT_CONTACTED','asterisk'),('L2','CLOSED','stringee')")
    ex(db, "INSERT INTO crm_conversions (lead_id, account_opened) VALUES ('L2', 1)")
    ex(db, "INSERT INTO wf_call_followups (phone,followup_id,provider,recording_synced) VALUES ('919876543210','F1','asterisk',0)")
    ex(db, "INSERT INTO wf2_provider_config VALUES ('asterisk',1),('stringee',1)")
    ex(db, "INSERT INTO stringee_calls VALUES ('C1','919876543210',NULL)")
    ex(db, "INSERT INTO n8n_switches (label,workflow_ids) VALUES ('India','a,b')")

    def fp():
        return {t: (q(db, f"SHOW CREATE TABLE {t}")[0]['Create Table'], q(db, f"SELECT * FROM {t} ORDER BY 1"))
                for t in prod}
    before = fp()
    run_sql_file(db, MIGRATION)
    run_sql_file(db, MIGRATION)
    changed = [t for t in prod if before[t] != fp()[t]]
    assert not changed, f'tablas productivas modificadas: {changed}'


def t_K_cliente_mariadb():
    fresh_mysql_db('mig_k')
    for n in (1, 2):
        p = subprocess.run(['mariadb', f"-u{MYSQL['user']}", f"-p{MYSQL['password']}", 'mig_k'],
                           stdin=open(MIGRATION), capture_output=True, text=True)
        assert p.returncode == 0, f'run #{n}: {p.stderr[:300]}'
    assert q('mig_k', "SELECT COUNT(*) n FROM call_routes")[0]['n'] == 3


def main():
    if not mysql_available():
        S.skip('suite completa', 'MariaDB no disponible')
        return S.finish()
    S.section('A · instalación limpia')
    S.check('seed = config de los JSON v1; UN proveedor SIP; país nace OFF por default', t_A_fresh)
    S.check('UNIQUEs, índices de analytics, adapter_key VARCHAR, inflight generada', t_A_tablas_y_constraints)
    S.check('la BASE rechaza la 2ª llamada en vuelo del mismo lead (1062)', t_A_inflight_impuesto_por_la_base)
    S.section('B · C · D · idempotencia')
    S.check('run #2 y #3 son NO-OP funcional', t_B_rerun_noop)
    S.check('no pisa ediciones: capacity, notas, Telegram, switches de país/proveedor, adapter', t_C_ediciones_preservadas)
    S.check('instalación limpia cortada en 8 puntos + reanudación = idéntica', t_D_interrumpida)
    S.section('E · F · G · H · upgrade V2.1 → V2.2')
    S.check('upgrade con datos ×2: switches, adapter_key, RELEASED, inflight, sin re-seed', t_E_upgrade_desde_v21)
    S.check('upgrade cortado en 8 puntos + reanudación = idéntico (config, jobs y esquema)', t_F_upgrade_interrumpido)
    S.check('base actualizada ≡ instalación limpia (columnas, tipos, índices)', t_G_upgrade_equivale_a_limpia)
    S.check('2 llamadas en vuelo → aborto explícito; decisión humana; reanuda OK', t_H_aborto_y_reanudacion)
    S.section('I · J · K · seguridad')
    S.check('sin DROP / TRUNCATE / DELETE ejecutables', t_I_sin_sentencias_destructivas)
    S.check('6 tablas productivas: DDL y datos idénticos tras 2 ejecuciones', t_J_tablas_productivas_intactas)
    S.check('cliente mariadb < archivo.sql, dos veces', t_K_cliente_mariadb)
    return S.finish()


if __name__ == '__main__':
    raise SystemExit(main())
