"""
ops_events.py — Event store local V2.2 (referencia ejecutable)
=============================================================

Qué resuelve
------------
El panel deja de depender de LeadStudio en cada carga. Los workflows escriben
LOCALMENTE lo que pasa, en el momento en que pasa, y el panel lee MySQL.

Dos capas, cada una con su responsabilidad:

  wf_call_jobs   HECHO por llamada. Resultado y duración viven acá una sola vez.
                 → métricas de llamadas (intentos, respuesta, minutos).
  wf_events      LOG append-only e idempotente de todo lo demás y de la traza.
                 → métricas de negocio (cuentas, pagos, tools, follow-ups,
                   grabaciones) y trazabilidad de cada llamada.

Idempotencia
------------
event_key es UNIQUE y se construye de forma DETERMINISTA a partir de la
identidad del hecho (EVENT_TYPES[tipo]['key']). El mismo hecho registrado dos
veces — webhook y polling, reintento de un nodo, re-ejecución de n8n — choca con
el UNIQUE y se ignora. Un hecho no se cuenta dos veces por construcción, no por
disciplina de quien escribe.

Un solo CALL_RESULT por llamada
-------------------------------
En lugar de CALL_ANSWERED / CALL_NO_ANSWER / … (un tipo por resultado), hay un
único CALL_RESULT con clave CALL_RESULT:{call_job_id} y el resultado en la
columna `result`. Con tipos separados, un webhook que dice ANSWERED y un polling
que dice NO_ANSWER quedarían guardados los dos. Con una clave por llamada, el
segundo choca y — si contradice al primero — abre un issue RESULT_CONFLICT.
"""

import json
from datetime import datetime, timezone as _tz

import call_jobs as cj

# tipo → (dominio, campos que forman la clave). {provider} y similares salen de
# los kwargs de record_event; si falta alguno, es VALIDATION_ERROR.
EVENT_TYPES = {
    # ── llamadas (traza; las métricas salen de wf_call_jobs) ──
    'CALL_CLAIMED':            ('CALL', ['call_job_id']),
    'CALL_DISPATCHED':         ('CALL', ['call_job_id']),
    'CALL_TECH_FAILED':        ('CALL', ['call_job_id']),      # cada re-claim tiene call_job_id nuevo
    'CALL_UNKNOWN':            ('CALL', ['call_job_id']),
    'CALL_RESULT':             ('CALL', ['call_job_id']),      # UNO por llamada
    # ── follow-up (lo escribe SOLO el motor) ──
    'FOLLOWUP_CREATED':        ('FOLLOWUP', ['call_job_id']),
    'CALLBACK_SCHEDULED':      ('FOLLOWUP', ['call_job_id']),
    'LEAD_CLOSED':             ('FOLLOWUP', ['call_job_id']),
    # ── tools del país ──
    'ACCOUNT_REQUESTED':       ('TOOL', ['lead_id', 'request_ref']),
    'ACCOUNT_CREATED':         ('TOOL', ['provider', 'lead_id']),   # una cuenta por lead y proveedor
    'ACCOUNT_ALREADY_EXISTS':  ('TOOL', ['provider', 'lead_id']),
    'ACCOUNT_FAILED':          ('TOOL', ['lead_id', 'request_ref']),
    'PAYMENT_LINK_REQUESTED':  ('TOOL', ['lead_id', 'request_ref']),
    'PAYMENT_LINK_CREATED':    ('TOOL', ['provider', 'order_ref']),
    'PAYMENT_LINK_FAILED':     ('TOOL', ['lead_id', 'request_ref']),
    'PAYMENT_CONFIRMED':       ('TOOL', ['provider', 'order_ref']),
    # ── grabaciones ──
    'RECORDING_ATTACHED':      ('RECORDING', ['call_job_id']),
    'RECORDING_SKIPPED_SHORT': ('RECORDING', ['call_job_id']),
    'RECORDING_MISSING':       ('RECORDING', ['call_job_id']),
}
DOMAINS = sorted({d for d, _ in EVENT_TYPES.values()})
ISSUE_TYPES = ['RESULT_CONFLICT', 'CRM_ACCOUNT_MISSING', 'CRM_STATUS_MISMATCH',
               'CALL_NEEDS_RECONCILIATION', 'FOLLOWUP_NEEDS_RECONCILIATION',
               'TECH_RETRY_REPEATED', 'ORPHAN_POSTCALL']


def _utc(ts=None):
    ts = ts or datetime.now(_tz.utc)
    if isinstance(ts, str):
        ts = datetime.fromisoformat(ts.replace('Z', '+00:00'))
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=_tz.utc)
    return ts.astimezone(_tz.utc).strftime('%Y-%m-%d %H:%M:%S')


def event_key(event_type, **parts):
    if event_type not in EVENT_TYPES:
        raise ValueError(f'VALIDATION_ERROR: event_type desconocido {event_type!r}')
    _, fields = EVENT_TYPES[event_type]
    missing = [f for f in fields if not parts.get(f)]
    if missing:
        raise ValueError(f'VALIDATION_ERROR: {event_type} requiere {missing} para su clave')
    return event_type + ':' + ':'.join(str(parts[f]) for f in fields)


def ensure_tables_sqlite(db):
    if db.driver == 'mysql':
        return
    cj.ensure_tables_sqlite(db)
    db.execute("""CREATE TABLE IF NOT EXISTS wf_events (
        id INTEGER PRIMARY KEY AUTOINCREMENT, event_key TEXT NOT NULL UNIQUE,
        event_type TEXT NOT NULL, event_domain TEXT NOT NULL, occurred_at DATETIME NOT NULL,
        call_job_id TEXT, lead_id TEXT, country_iso TEXT, route_key TEXT, provider TEXT,
        adapter_key TEXT, attempt INTEGER, conversation_id TEXT, provider_job_id TEXT,
        followup_id TEXT, result TEXT, duration_seconds INTEGER, amount REAL, currency TEXT,
        source_workflow TEXT, execution_id TEXT, metadata_json TEXT,
        created_at DATETIME DEFAULT (datetime('now')))""")
    for ix in ('occurred_at', 'event_type, occurred_at', 'country_iso, occurred_at',
               'route_key, occurred_at', 'provider, occurred_at', 'lead_id', 'call_job_id'):
        name = 'ix_ev_' + ix.replace(', ', '_')
        db.execute(f"CREATE INDEX IF NOT EXISTS {name} ON wf_events({ix})")
    db.execute("""CREATE TABLE IF NOT EXISTS wf_reconciliation_issues (
        id INTEGER PRIMARY KEY AUTOINCREMENT, issue_key TEXT NOT NULL UNIQUE,
        issue_type TEXT NOT NULL, entity_type TEXT NOT NULL, entity_id TEXT NOT NULL,
        lead_id TEXT, country_iso TEXT, severity TEXT NOT NULL DEFAULT 'WARN',
        state TEXT NOT NULL DEFAULT 'OPEN', detail_json TEXT, occurrences INTEGER NOT NULL DEFAULT 1,
        first_seen_at DATETIME DEFAULT (datetime('now')), last_seen_at DATETIME DEFAULT (datetime('now')),
        resolved_at DATETIME, resolved_by TEXT)""")


def _ins(db):
    return 'INSERT IGNORE' if db.driver == 'mysql' else 'INSERT OR IGNORE'


_COLS = ['call_job_id', 'lead_id', 'country_iso', 'route_key', 'provider', 'adapter_key', 'attempt',
         'conversation_id', 'provider_job_id', 'followup_id', 'result', 'duration_seconds',
         'amount', 'currency', 'source_workflow', 'execution_id']


def record_event(db, event_type, occurred_at=None, metadata=None, **fields):
    """Inserta el evento si su clave no existe. Devuelve (created, event_key).

    La GARANTÍA de unicidad es el UNIQUE(event_key) de la base. `created` es
    informativo (se decide por una lectura previa): bajo concurrencia, dos
    escritores simultáneos del mismo hecho pueden ver ambos created=True, pero
    en la tabla queda UNA fila. Ninguna métrica depende de `created`.

    Los campos de la clave que no son columnas (request_ref, order_ref) se
    guardan en metadata_json.
    """
    key = event_key(event_type, **fields)
    domain, keyf = EVENT_TYPES[event_type]
    existed = bool(db.one("SELECT id FROM wf_events WHERE event_key=§", (key,)))
    meta = dict(metadata or {})
    for f in keyf:
        if f not in _COLS:
            meta.setdefault(f, fields.get(f))
    vals = {c: fields.get(c) for c in _COLS}
    cols = ['event_key', 'event_type', 'event_domain', 'occurred_at'] + _COLS + ['metadata_json']
    params = [key, event_type, domain, _utc(occurred_at)] + [vals[c] for c in _COLS] + \
             [json.dumps(meta, ensure_ascii=False, default=str) if meta else None]
    db.execute(f"{_ins(db)} INTO wf_events ({','.join(cols)}) VALUES ({','.join(['§'] * len(cols))})",
               tuple(params))
    return (not existed), key


def record_call_result(db, call_job_id, result, duration_seconds=None, conversation_id=None,
                       sip_code=None, callback_at=None, occurred_at=None, source_workflow='WF9',
                       execution_id=None):
    """Lo que WF9 (o WF2 si el resultado es inmediato) escribe EN EL MOMENTO.

    1. hecho: wf_call_jobs.result/duration (primer escritor gana)
    2. traza: evento CALL_RESULT (clave por call_job_id)
    3. si llega un resultado distinto al ya guardado → issue RESULT_CONFLICT
    Devuelve el estado de record_result: RECORDED / DUPLICATE / CONFLICT / NOT_FOUND.
    """
    status = cj.record_result(db, call_job_id, result, duration_seconds, conversation_id,
                              sip_code, callback_at)
    job = cj.job_get(db, call_job_id)
    if status == 'NOT_FOUND':
        open_issue(db, 'ORPHAN_POSTCALL', 'call_job', call_job_id,
                   detail={'result': result, 'conversation_id': conversation_id})
        return status
    if status == 'CONFLICT':
        open_issue(db, 'RESULT_CONFLICT', 'call_job', call_job_id, lead_id=job.get('lead_id'),
                   country_iso=job.get('country_iso'),
                   detail={'stored': job.get('result'), 'received': result,
                           'received_duration': duration_seconds})
        return status
    record_event(db, 'CALL_RESULT', occurred_at=occurred_at, call_job_id=call_job_id,
                 lead_id=job['lead_id'], country_iso=job['country_iso'], route_key=job['route_key'],
                 provider=job['provider'], adapter_key=job.get('adapter_key'), attempt=job['attempt'],
                 conversation_id=job.get('conversation_id'), result=job['result'],
                 duration_seconds=job.get('duration_seconds'), source_workflow=source_workflow,
                 execution_id=execution_id, metadata={'sip_code': sip_code} if sip_code else None)
    return status


# ── reconciliación ────────────────────────────────────────────────────

def open_issue(db, issue_type, entity_type, entity_id, lead_id=None, country_iso=None,
               severity='WARN', detail=None):
    """Idempotente por issue_key: el mismo desajuste es UNA fila.

    Re-detectarlo suma `occurrences` y actualiza last_seen_at. `occurrences` es
    informativo: dos detecciones simultáneas del mismo issue nuevo pueden
    contarse como una. La unicidad del issue sí está garantizada (UNIQUE).
    """
    if issue_type not in ISSUE_TYPES:
        raise ValueError(f'issue_type desconocido: {issue_type!r}')
    key = f'{issue_type}:{entity_type}:{entity_id}'
    d = json.dumps(detail or {}, ensure_ascii=False, default=str)
    now = "UTC_TIMESTAMP()" if db.driver == 'mysql' else "datetime('now')"
    if db.one("SELECT id FROM wf_reconciliation_issues WHERE issue_key=§", (key,)):
        db.execute(f"UPDATE wf_reconciliation_issues SET occurrences=occurrences+1, "
                   f"last_seen_at={now}, detail_json=§ WHERE issue_key=§ AND state='OPEN'", (d, key))
        return key
    db.execute(f"{_ins(db)} INTO wf_reconciliation_issues (issue_key, issue_type, entity_type, "
               "entity_id, lead_id, country_iso, severity, detail_json, first_seen_at, last_seen_at) "
               f"VALUES (§,§,§,§,§,§,§,§,{now},{now})",
               (key, issue_type, entity_type, str(entity_id), lead_id, country_iso, severity, d))
    return key


def resolve_issue(db, issue_key, actor, state='RESOLVED'):
    now = "UTC_TIMESTAMP()" if db.driver == 'mysql' else "datetime('now')"
    _, rc = db.execute(f"UPDATE wf_reconciliation_issues SET state=§, resolved_at={now}, resolved_by=§ "
                       "WHERE issue_key=§ AND state='OPEN'", (state, actor, issue_key))
    return rc == 1


def reconcile_account(db, lead_id, crm_account_opened, provider='cashstudio'):
    """WF14 V2 (rol nuevo: reconciliación). Compara el evento local con el CRM.

    Nunca borra ni corrige datos locales: registra la diferencia."""
    local = db.one("SELECT event_key FROM wf_events WHERE event_type='ACCOUNT_CREATED' "
                   "AND lead_id=§ AND provider=§", (lead_id, provider))
    if local and not crm_account_opened:
        return open_issue(db, 'CRM_ACCOUNT_MISSING', 'lead', lead_id, lead_id=lead_id,
                          detail={'local_event': local['event_key'], 'crm_account_opened': False})
    return None
