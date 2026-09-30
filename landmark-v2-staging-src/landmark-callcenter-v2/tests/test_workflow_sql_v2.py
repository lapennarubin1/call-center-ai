#!/usr/bin/env python3
"""Cada sentencia SQL de los siete workflows, contra el esquema REAL.

Un JSON de n8n puede pasar todos los checks de estilo y seguir teniendo un SQL
que referencia una columna inexistente. Eso no se descubre hasta que corre en
producción, con datos reales.

Acá se extrae el `query` de cada nodo MySQL de los siete templates y:

  1. se PREPARA contra una MariaDB con `migration.sql` aplicada — `PREPARE`
     valida sintaxis Y existencia de tablas y columnas, sin escribir nada;
  2. se EJECUTAN de verdad las del camino crítico (claim, transiciones,
     eventos, ledgers, reconciliación) para comprobar que las claves únicas y
     los tipos se comportan como el diseño dice.
"""
import os
import re
import sys

# Importar los módulos del suite no debe dejar .pyc dentro del paquete:
# lo que se empaqueta tiene que salir limpio.
sys.dont_write_bytecode = True
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import _suite as S

DB_NAME = 'lm_wf_sql_test'


def main():
    s = S.Suite('SQL DE LOS WORKFLOWS · contra el esquema real')

    if not S.mysql_available():
        s.skip('suite completa', 'MariaDB no disponible')
        return s.finish()

    S.migrated_db(DB_NAME)
    wfs = S.load_workflows()

    stmts = []
    for fname, wf in wfs.items():
        for n in S.mysql_nodes(wf):
            stmts.append((fname, n['name'], n['parameters']['query']))

    def pick(prefix, node):
        """Los nombres de nodo se repiten entre workflows (es legal en n8n):
        hay que elegir el del template correcto, no el primero que aparezca."""
        hits = [q for f, n, q in stmts if n == node and f.startswith(prefix)]
        assert hits, f'no se encontró {node} en {prefix}'
        return hits[0]

    def q2py(q):
        """'?' de n8n -> placeholder de pymysql, escapando los '%' literales de
        los patrones LIKE (si no, pymysql intenta interpolarlos)."""
        return q.replace('%', '%%').replace('?', '%' + 's')

    def run(q, params=()):
        with conn.cursor() as cur:
            cur.execute(q2py(q), params)
            try:
                return cur.fetchall()
            except Exception:
                return []

    # ── inventario ────────────────────────────────────────────────────
    s.section('se extrajo SQL de los siete templates')

    def has_sql():
        assert len(stmts) >= 40, f'solo {len(stmts)} sentencias: ¿se extrajo bien?'
        files = {f for f, _, _ in stmts}
        assert len(files) >= 6, f'solo {len(files)} workflows tienen SQL'
    s.check(f'{len(stmts)} sentencias SQL en {len({f for f,_,_ in stmts})} workflows',
            has_sql)

    # ── PREPARE ───────────────────────────────────────────────────────
    s.section('PREPARE contra el esquema migrado (sintaxis + tablas + columnas)')
    failures = []
    for fname, node, q in stmts:
        # Conexión nueva por sentencia: un PREPARE fallido deja la conexión
        # desincronizada y contaminaría las comprobaciones siguientes.
        c = S.mysql_conn(DB_NAME)
        try:
            with c.cursor() as cur:
                cur.execute("SET @sql := %s", (q,))
                cur.execute("PREPARE _t FROM @sql")
                cur.execute("DEALLOCATE PREPARE _t")
        except Exception as ex:
            failures.append((fname, node, str(ex)[:160]))
        finally:
            c.close()

    def all_prepared():
        assert not failures, (f'{len(failures)}/{len(stmts)} no preparan:\n  ' +
                              '\n  '.join(f'{f}::{n}: {e}' for f, n, e in failures[:8]))
    s.check(f'las {len(stmts)} sentencias preparan sin error', all_prepared)

    # ── análisis estático del SQL ─────────────────────────────────────
    s.section('lo que el SQL toca y lo que no')

    def tables_exist():
        c = S.mysql_conn(DB_NAME)
        with c.cursor() as cur:
            cur.execute("SELECT TABLE_NAME FROM information_schema.TABLES "
                        "WHERE TABLE_SCHEMA = DATABASE()")
            have = {r[0].lower() for r in cur.fetchall()}
        c.close()
        referenced = set()
        for _, _, q in stmts:
            # 'ON DUPLICATE KEY UPDATE col = ...' NO nombra una tabla
            qq = re.sub(r'ON\s+DUPLICATE\s+KEY\s+UPDATE', 'ON_DUP_SET', q, flags=re.I)
            for m in re.finditer(r'\b(?:FROM|JOIN)\s+([a-z_][a-z0-9_]*)|'
                                 r'\bINSERT\s+(?:IGNORE\s+)?INTO\s+([a-z_][a-z0-9_]*)|'
                                 r'\bUPDATE\s+([a-z_][a-z0-9_]*)', qq, re.I):
                t = (m.group(1) or m.group(2) or m.group(3)).lower()
                if t not in ('dual', 'select', 'json_table'):
                    referenced.add(t)
        missing = sorted(referenced - have)
        assert not missing, (f'tablas referenciadas que la migración no crea: {missing}. '
                             'Si son tablas de v1, van en 003_legacy_compat_tables.sql')
        assert {'wf_call_jobs', 'wf_events'} <= referenced
    s.check('toda tabla referenciada existe tras la migración', tables_exist)

    def no_writes_to_v1_tables():
        forbidden = {'wf_call_followups', 'wf10_sent_recordings', 'wf2_provider_config'}
        offenders = []
        for fname, node, q in stmts:
            qq = re.sub(r'ON\s+DUPLICATE\s+KEY\s+UPDATE', 'ON_DUP_SET', q, flags=re.I)
            for m in re.finditer(r'\b(?:INSERT\s+(?:IGNORE\s+)?INTO|UPDATE|DELETE\s+FROM)'
                                 r'\s+([a-z_][a-z0-9_]*)', qq, re.I):
                if m.group(1).lower() in forbidden:
                    offenders.append(f'{fname}::{node} -> {m.group(1)}')
        assert not offenders, ('V2 escribe en tablas de v1 (deben quedar intactas mientras '
                               'v1 siga activo):\n  ' + '\n  '.join(offenders))
    s.check('V2 no escribe en las tablas propias de v1', no_writes_to_v1_tables)

    def no_destructive_sql():
        offenders = []
        for fname, node, q in stmts:
            if re.search(r'\b(DROP|TRUNCATE)\b', q, re.I):
                offenders.append(f'{fname}::{node}: DROP/TRUNCATE')
            if re.search(r'\bDELETE\s+FROM\b', q, re.I):
                offenders.append(f'{fname}::{node}: DELETE')
        assert not offenders, 'SQL destructivo en un workflow:\n  ' + '\n  '.join(offenders)
    s.check('ningún workflow borra filas (DROP/TRUNCATE/DELETE)', no_destructive_sql)

    def config_tables_read_only():
        """Los workflows NO leen ni escriben la configuración: usan la API."""
        cfg = {'countries', 'voice_providers', 'call_routes', 'route_capacity_windows',
               'country_tool_configs', 'followup_policies', 'route_telegram_targets'}
        offenders = []
        for fname, node, q in stmts:
            qq = re.sub(r'ON\s+DUPLICATE\s+KEY\s+UPDATE', 'ON_DUP_SET', q, flags=re.I)
            for m in re.finditer(r'\b(?:FROM|JOIN|INSERT\s+(?:IGNORE\s+)?INTO|UPDATE)'
                                 r'\s+([a-z_][a-z0-9_]*)', qq, re.I):
                if m.group(1).lower() in cfg:
                    offenders.append(f'{fname}::{node} -> {m.group(1)}')
        assert not offenders, ('un workflow lee la configuración directamente de la base '
                               '(debe usar /api/routes/*):\n  ' + '\n  '.join(offenders))
    s.check('ningún workflow consulta las tablas de configuración', config_tables_read_only)

    # ── ejecución real ────────────────────────────────────────────────
    s.section('ejecución real del camino crítico')
    db = S.db_for(DB_NAME)
    conn = S.mysql_conn(DB_NAME)

    route = db.one("SELECT id, route_key, iso FROM call_routes "
                   "WHERE route_key = 'IN_PROVEEDOR1'")
    assert route, 'la migración no sembró IN_PROVEEDOR1'

    INS = pick('TEMPLATE_WF2', '[DB] Insert Claim')
    OWNER = pick('TEMPLATE_WF2', '[DB] Read Claim Owner')

    def claim(job, lead, attempt, provider='proveedor1', adapter='ELEVENLABS_SIP'):
        run(INS, (job, lead, route['id'], route['route_key'], route['iso'],
                  provider, adapter, attempt, 'exec-test'))
        rows = run(OWNER, (lead, attempt, lead, attempt))
        return rows[0][2] if rows else None

    def claim_executes():
        assert claim('job-A', 'lead-1', 1) == 'job-A', 'la relectura no devolvió el dueño'
    s.check('[DB] Insert Claim + [DB] Read Claim Owner funcionan', claim_executes)

    def claim_collision():
        owner = claim('job-B', 'lead-1', 1, 'stringee', 'STRINGEE_WORKER')
        assert owner == 'job-A', f'la segunda ejecución ganó el claim ({owner})'
        n = db.one("SELECT COUNT(*) c FROM wf_call_jobs WHERE lead_id='lead-1'")['c']
        assert n == 1, f'se crearon {n} llamadas para el mismo (lead, intento)'
    s.check('colisión de claim: el segundo pierde y no duplica la llamada', claim_collision)

    def transitions_execute():
        run(pick('TEMPLATE_WF2', '[DB] Mark Dispatching'), ('job-A',))
        run(pick('TEMPLATE_WF2', '[DB] Mark Dispatched'), ('conv-1', None, None, 200, 'job-A'))
        row = db.one("SELECT state, conversation_id FROM wf_call_jobs "
                     "WHERE call_job_id='job-A'")
        assert row['state'] == 'DISPATCHED', f'estado {row["state"]}'
        assert row['conversation_id'] == 'conv-1'
    s.check('transiciones CLAIMED -> DISPATCHING -> DISPATCHED', transitions_execute)

    def event_idempotent():
        q = pick('TEMPLATE_WF2', '[DB] Record Event Call Dispatched')
        p = ('CALL_DISPATCHED:job-A', 'job-A', 'lead-1', 'IN', 'IN_PROVEEDOR1',
             'proveedor1', 'ELEVENLABS_SIP', 1, 'conv-1', None, 'exec-test')
        run(q, p)
        run(q, p)                              # el mismo hecho, otra vez
        n = db.one("SELECT COUNT(*) c FROM wf_events "
                   "WHERE event_key='CALL_DISPATCHED:job-A'")['c']
        assert n == 1, f'el evento se guardó {n} veces: la idempotencia falló'
    s.check('un evento escrito dos veces queda una sola vez (event_key UNIQUE)',
            event_idempotent)

    def record_result_first_writer_wins():
        q = pick('TEMPLATE_FOLLOWUP', '[DB] Record Call Result')
        run(q, ('ANSWERED', 120, 'conv-1', None, None, 'job-A'))
        run(q, ('NO_ANSWER', 0, 'conv-1', None, None, 'job-A'))
        row = db.one("SELECT result, duration_seconds FROM wf_call_jobs "
                     "WHERE call_job_id='job-A'")
        assert row['result'] == 'ANSWERED', f'el segundo escritor pisó el resultado: {row}'
        assert row['duration_seconds'] == 120, 'los minutos se contaron dos veces'
    s.check('el primer resultado gana: un duplicado no suma minutos',
            record_result_first_writer_wins)

    def released_does_not_consume_attempt():
        claim('job-C', 'lead-2', 1)
        run(pick('TEMPLATE_WF2', '[DB] Mark Released'),
            ('AUTH_ERROR', 'HTTP 401', 401, 60, 'job-C'))
        row = run(pick('TEMPLATE_WF2', '[DB] Resolve Next Attempt'), ('lead-2',) * 6)[0]
        # released_any=1 (el intento sigue disponible), max_attempt=1, inflight=0
        assert row[2] == 1, f'released_any debería ser 1, es {row[2]}'
        assert row[3] == 1, f'max_attempt debería ser 1, es {row[3]}'
        assert row[4] == 0, f'un RELEASED no es una llamada en vuelo, inflight={row[4]}'
    s.check('un 401 libera el intento: no lo consume ni deja la llamada en vuelo',
            released_does_not_consume_attempt)

    def tech_backoff_blocks_immediate_reclaim():
        row = run(pick('TEMPLATE_WF2', '[DB] Resolve Next Attempt'), ('lead-2',) * 6)[0]
        # released_attempt (con backoff vencido) debe ser NULL: todavía no toca
        assert row[1] is None, ('el intento liberado se re-reclamó sin esperar el backoff '
                                f'(released_attempt={row[1]})')
    s.check('el backoff técnico impide re-reclamar el intento de inmediato',
            tech_backoff_blocks_immediate_reclaim)

    def unknown_stays_inflight():
        claim('job-D', 'lead-3', 1)
        run(pick('TEMPLATE_WF2', '[DB] Mark Dispatching'), ('job-D',))
        run(pick('TEMPLATE_WF2', '[DB] Mark Unknown'), ('timeout', 0, 'job-D'))
        row = run(pick('TEMPLATE_WF2', '[DB] Resolve Next Attempt'), ('lead-3',) * 6)[0]
        assert row[4] == 1, ('UNKNOWN debe contar como llamada en vuelo para que el lead '
                             f'no se re-despache; inflight={row[4]}')
    s.check('UNKNOWN deja el lead en vuelo: no se re-despacha', unknown_stays_inflight)

    def inflight_unique_blocks_second_call():
        claim('job-E', 'lead-3', 2, 'stringee', 'STRINGEE_WORKER')
        n = db.one("SELECT COUNT(*) c FROM wf_call_jobs WHERE lead_id='lead-3'")['c']
        assert n == 1, (f'la base aceptó una segunda llamada en vuelo para el mismo lead '
                        f'({n} filas)')
    s.check('UNIQUE(inflight_lead): un lead nunca tiene dos llamadas en vuelo',
            inflight_unique_blocks_second_call)

    def different_attempt_allowed_when_free():
        """Con el intento anterior CERRADO, el siguiente sí se puede reclamar."""
        owner = claim('job-F', 'lead-1', 2)
        assert owner == 'job-F', f'no se pudo reclamar el intento 2 de un lead libre ({owner})'
    s.check('otro intento del mismo lead se reclama cuando el anterior cerró',
            different_attempt_allowed_when_free)

    def tool_claim_executes():
        q = pick('TEMPLATE_WF3', '[DB] Claim Tool Request')
        read = pick('TEMPLATE_WF3', '[DB] Read Tool Claim')
        key = 'CREATE_ACCOUNT:lead-9:conv-9'
        for tok in ('tok-1', 'tok-2'):
            run(q, (key, 'lead-9', 'IN', 'conv-9', 'conv-9', 'cashstudio',
                    'CONFIG_ROUTER', tok, 'exec-9'))
        row = run(read, (key, key))[0]
        assert row[1] == 'tok-1', f'el segundo pedido ganó el claim ({row[1]})'
        n = db.one("SELECT COUNT(*) c FROM wf_tool_requests WHERE request_key=§",
                   (key,))['c']
        assert n == 1, f'se crearon {n} pedidos para el mismo tool-call'
    s.check('claim de tool: dos reintentos del mismo tool-call son UN pedido',
            tool_claim_executes)

    def different_tool_request_is_separate():
        """Dos llamadas DISTINTAS del mismo lead son dos pedidos distintos."""
        q = pick('TEMPLATE_WF3', '[DB] Claim Tool Request')
        run(q, ('CREATE_ACCOUNT:lead-9:conv-10', 'lead-9', 'IN', 'conv-10', 'conv-10',
                'cashstudio', 'CONFIG_ROUTER', 'tok-3', 'exec-12'))
        n = db.one("SELECT COUNT(*) c FROM wf_tool_requests "
                   "WHERE lead_id='lead-9' AND tool_type='CREATE_ACCOUNT'")['c']
        assert n == 2, f'se esperaban 2 pedidos (dos conversaciones), hay {n}'
    s.check('dos conversaciones distintas del mismo lead son dos pedidos',
            different_tool_request_is_separate)

    def account_event_idempotent():
        q = pick('TEMPLATE_WF3', '[DB] Record Event Account Created')
        p = ('ACCOUNT_CREATED:cashstudio:lead-9', 'lead-9', 'IN', 'conv-9',
             'cashstudio', 'exec-9', '{"market":"IND"}')
        run(q, p)
        run(q, ('ACCOUNT_CREATED:cashstudio:lead-9', 'lead-9', 'IN', 'conv-10',
                'cashstudio', 'exec-12', '{"market":"IND"}'))
        n = db.one("SELECT COUNT(*) c FROM wf_events "
                   "WHERE event_type='ACCOUNT_CREATED' AND lead_id='lead-9'")['c']
        assert n == 1, (f'ACCOUNT_CREATED se contó {n} veces: una cuenta por lead y '
                        'proveedor')
    s.check('ACCOUNT_CREATED duplicado no cuenta dos cuentas', account_event_idempotent)

    def payment_event_idempotent():
        q = pick('TEMPLATE_WF7_8', '[DB] Record Event Payment Confirmed')
        p = ('PAYMENT_CONFIRMED:okpay:LM_x_1', 'lead-9', 'IN', 'conv-9', 'okpay',
             5000, 'INR', 'exec-13', '{"order_ref":"LM_x_1"}')
        run(q, p)
        run(q, p)
        n = db.one("SELECT COUNT(*) c, SUM(amount) a FROM wf_events "
                   "WHERE event_type='PAYMENT_CONFIRMED'")
        assert n['c'] == 1, f'el pago se contó {n["c"]} veces'
        assert int(n['a']) == 5000, f'el importe se duplicó: {n["a"]}'
    s.check('un callback de pago reenviado no cuenta el pago dos veces',
            payment_event_idempotent)

    def recording_claim_executes():
        q = pick('TEMPLATE_WF10', '[DB] Claim Recording')
        read = pick('TEMPLATE_WF10', '[DB] Read Recording Claim')
        ref = 'stringee-919876543210-1758000000000.wav'
        for tok in ('rtok-1', 'rtok-2'):
            run(q, (ref, 'STRINGEE_WORKER', 'job-A', 'conv-1', None, 'lead-1',
                    'IN_PROVEEDOR1', 'IN', 'proveedor1', 180, 900000, 'CALL_JOB',
                    tok, 'exec-11'))
        row = run(read, (ref, ref))[0]
        assert row[1] == 'rtok-1', 'la segunda corrida ganó el claim de la grabación'
        n = db.one("SELECT COUNT(*) c FROM wf_recording_ledger")['c']
        assert n == 1, f'la grabación se registró {n} veces'
    s.check('claim de grabación: la misma grabación no se envía dos veces',
            recording_claim_executes)

    def recording_correlation_query():
        """La consulta de correlación de WF10 encuentra la llamada por
        conversation_id, que es la cadena preferida de V2."""
        q = pick('TEMPLATE_WF10', '[DB] Correlate Recording')
        rows = run(q, ('ref-x', '', '', 'conv-1', 'conv-1', '', 'IN_PROVEEDOR1',
                       '', '2026-09-21 10:00:00', '2026-09-21 10:00:00'))
        assert rows and rows[0][1] == 'job-A', \
            f'la correlación por conversation_id no encontró la llamada: {rows}'
    s.check('WF10 correlaciona por conversation_id (no por teléfono)',
            recording_correlation_query)

    def reconciliation_sql_runs():
        for node in ('[DB] Reconcile Stale Claimed', '[DB] Reconcile Stale Dispatching',
                     '[DB] Reconcile Unknown', '[DB] Reconcile Dispatched Without Postcall',
                     '[DB] Reconcile Conversation Ledger', '[DB] Reconcile Tool Requests',
                     '[DB] Reconcile Recording Ledger'):
            q = pick('TEMPLATE_WF14', node)
            run(q, tuple([5] * q.count('?')))
    s.check('las 7 sentencias de reconciliación de WF14 ejecutan', reconciliation_sql_runs)

    def issue_sql_runs():
        # El reconciliador solo mueve jobs "viejos"; en un test recién creado
        # ninguno lo es. Se deja uno en NEEDS_RECONCILIATION a mano para que el
        # SQL de issues tenga sobre qué trabajar.
        db.execute("UPDATE wf_call_jobs SET state='NEEDS_RECONCILIATION' "
                   "WHERE call_job_id='job-D'")
        for node in ('[DB] Open Call Reconciliation Issues',
                     '[DB] Open Followup Reconciliation Issues',
                     '[DB] Open Tech Retry Issues', '[DB] Compare Accounts',
                     '[DB] Compare Lead Status'):
            q = pick('TEMPLATE_WF14', node)
            run(q, tuple(['x'] * q.count('?')))
        n = db.one("SELECT COUNT(*) c FROM wf_reconciliation_issues "
                   "WHERE issue_type='CALL_NEEDS_RECONCILIATION'")['c']
        assert n >= 1, 'la reconciliación no abrió ningún issue'
    s.check('WF14 abre issues para lo que quedó a medias', issue_sql_runs)

    def issue_idempotent():
        q = pick('TEMPLATE_WF14', '[DB] Open Call Reconciliation Issues')
        before = db.one("SELECT COUNT(*) c FROM wf_reconciliation_issues")['c']
        run(q)
        run(q)
        after = db.one("SELECT COUNT(*) c FROM wf_reconciliation_issues")['c']
        assert before == after, (f'el mismo desajuste creó filas nuevas ({before} -> '
                                 f'{after}): debería sumar occurrences')
        occ = db.one("SELECT MAX(occurrences) m FROM wf_reconciliation_issues")['m']
        assert occ and occ >= 2, f'occurrences no se incrementó (max={occ})'
    s.check('re-detectar un issue suma occurrences, no crea filas', issue_idempotent)

    def json_table_sync():
        q = pick('TEMPLATE_WF14', '[DB] Upsert Crm Leads')
        payload = ('[{"lead_id":"lead-1","full_name":"Test User","phone":"+919876543210",'
                   '"language":"hi","status":"CONTACTED","stage":"INTERESTED",'
                   '"call_attempts":2,"do_not_call":0,"last_contacted_at":null,'
                   '"next_follow_up_at":null}]')
        run(q, (payload,))
        run(q, (payload,))                      # rerun: upsert, no duplica
        row = db.one("SELECT COUNT(*) c, MAX(country) ct, MAX(provider) p FROM crm_leads")
        assert row['c'] == 1, f'el upsert duplicó filas: {row["c"]}'
        assert row['ct'] == 'india', f'el país no se dedujo del prefijo: {row["ct"]}'
        assert row['p'] == 'proveedor1', \
            f'el proveedor no salió de wf_call_jobs: {row["p"]}'
    s.check('WF14: upsert de crm_leads con JSON_TABLE, idempotente', json_table_sync)

    def stringee_log_sync():
        q = pick('TEMPLATE_WF14', '[DB] Upsert Stringee Calls')
        payload = ('[{"call_id":"c1","phone":"919876543210","answered":1,'
                   '"duration_secs":95,"start_time":1758000000,'
                   '"answer_time":1758000005,"stop_time":1758000100}]')
        run(q, (payload,))
        run(q, (payload,))
        row = db.one("SELECT COUNT(*) c, MAX(lead_id) l, MAX(country) ct FROM stringee_calls")
        assert row['c'] == 1, f'duplicó: {row["c"]}'
        assert row['l'] == 'lead-1', f'no cruzó el teléfono con crm_leads: {row["l"]}'
        assert row['ct'] == 'india'
    s.check('WF14: volcado del call-log del proveedor, idempotente', stringee_log_sync)

    conn.close()
    return s.finish()


if __name__ == '__main__':
    sys.exit(main())
