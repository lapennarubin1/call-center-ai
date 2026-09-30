"""
call_jobs.py — Referencia ejecutable de la ejecución de llamadas V2.2
====================================================================

Los templates n8n ejecutarán exactamente estas sentencias. Este módulo las fija
para probarlas con concurrencia real en MariaDB.

Dos contadores que NO se mezclan
--------------------------------
  attempt           intento de NEGOCIO. Lo ve la política de follow-up
                    (1 → +2h, 2 → +3h, … 9 → CLOSE). Solo lo consume una llamada
                    que realmente pudo salir.
  tech_retry_count  reintentos TÉCNICOS del mismo intento de negocio: 401/403,
                    credencial inválida, endpoint caído ANTES de enviar, error de
                    config, PATCH ATTEMPTING fallido. No es NO_ANSWER. No consume
                    intento. Se reintenta con backoff exponencial (tope 60 min)
                    para que un proveedor caído no genere un bucle.

Máquina de estados
------------------
    CLAIMED ─► DISPATCHING ─► DISPATCHED ─► COMPLETED   (post-call con resultado)
       │            │   └────────────────► COMPLETED   (resultado inmediato: SIP 603…)
       │            ├──► UNKNOWN ─► NEEDS_RECONCILIATION   (pudo salir: JAMÁS re-dispatch)
       └────────────┴──► RELEASED ─► (backoff) ─► CLAIMED   (técnico: no salió nada)
    FAILED  terminal (p. ej. dato inválido detectado antes de enviar)

Una llamada en vuelo por lead
-----------------------------
  wf_call_jobs.inflight_lead (columna generada, UNIQUE) vale lead_id mientras el
  job está en CLAIMED/DISPATCHING/DISPATCHED/UNKNOWN/NEEDS_RECONCILIATION.
    · nunca dos llamadas simultáneas al mismo lead, desde ninguna ruta ni intento
    · WF9 puede resolver lead_id → call_job_id sin ambigüedad aunque el proveedor
      no devuelva call_job_id (ver STRINGEE_WORKER_AUDIT_V2_2.md)

Ganador de un claim: INSERT IGNORE + relectura del token propio. Nunca affectedRows.
Tiempo: todo UTC (UTC_TIMESTAMP() en MariaDB, datetime('now') en sqlite).
"""

import secrets
from datetime import datetime, timezone as _tz

JOB_STATES = ['CLAIMED', 'DISPATCHING', 'DISPATCHED', 'UNKNOWN', 'RELEASED',
              'FAILED', 'COMPLETED', 'NEEDS_RECONCILIATION']
INFLIGHT_STATES = ['CLAIMED', 'DISPATCHING', 'DISPATCHED', 'UNKNOWN', 'NEEDS_RECONCILIATION']
FINAL_RESULTS = ['ANSWERED', 'NO_ANSWER', 'BUSY', 'FAILED', 'VOICEMAIL', 'CALLBACK',
                 'WRONG_NUMBER', 'DNC', 'UNKNOWN']
TECH_BACKOFF_MAX_MIN = 60


def make_call_job_id(route_key, now=None):
    now = now or datetime.now(_tz.utc)
    return f"{route_key}-{now.strftime('%Y%m%dT%H%M%SZ')}-{secrets.token_hex(3)}"


def tech_backoff_minutes(n_failures):
    """1, 2, 4, 8, 16, 32, 60, 60… minutos."""
    return min(2 ** max(0, int(n_failures) - 1), TECH_BACKOFF_MAX_MIN)


def _ins_ignore(db):
    return 'INSERT IGNORE' if db.driver == 'mysql' else 'INSERT OR IGNORE'


def _now(db):
    return 'UTC_TIMESTAMP()' if db.driver == 'mysql' else "datetime('now')"


def _plus_minutes(db, m):
    return (f'DATE_ADD(UTC_TIMESTAMP(), INTERVAL {int(m)} MINUTE)' if db.driver == 'mysql'
            else f"datetime('now', '+{int(m)} minutes')")


def _minus_minutes(db, m):
    return (f'DATE_SUB(UTC_TIMESTAMP(), INTERVAL {int(m)} MINUTE)' if db.driver == 'mysql'
            else f"datetime('now', '-{int(m)} minutes')")


def ensure_tables_sqlite(db):
    """Solo tests/demo. En MariaDB las crea la migración."""
    if db.driver == 'mysql':
        return
    inflight = "','".join(INFLIGHT_STATES)
    db.execute(f"""CREATE TABLE IF NOT EXISTS wf_call_jobs (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        call_job_id TEXT NOT NULL UNIQUE, lead_id TEXT NOT NULL,
        route_id INTEGER NOT NULL, route_key TEXT NOT NULL, country_iso TEXT NOT NULL,
        provider TEXT NOT NULL, adapter_key TEXT, attempt INTEGER NOT NULL,
        execution_id TEXT, state TEXT NOT NULL DEFAULT 'CLAIMED',
        tech_retry_count INTEGER NOT NULL DEFAULT 0, next_tech_retry_at DATETIME,
        error_class TEXT, conversation_id TEXT, provider_job_id TEXT, provider_call_id TEXT,
        dispatch_http_status INTEGER, sip_code TEXT, result TEXT, duration_seconds INTEGER,
        callback_at DATETIME, followup_id TEXT, error_code TEXT, error_message TEXT,
        created_at DATETIME DEFAULT (datetime('now')), claimed_at DATETIME DEFAULT (datetime('now')),
        dispatched_at DATETIME, completed_at DATETIME, updated_at DATETIME DEFAULT (datetime('now')),
        inflight_lead TEXT GENERATED ALWAYS AS
            (CASE WHEN state IN ('{inflight}') THEN lead_id ELSE NULL END) STORED,
        UNIQUE (lead_id, attempt))""")
    db.execute("CREATE UNIQUE INDEX IF NOT EXISTS uq_inflight_lead ON wf_call_jobs(inflight_lead)")
    db.execute("""CREATE TABLE IF NOT EXISTS wf_conversation_ledger (
        conversation_id TEXT PRIMARY KEY, call_job_id TEXT, route_key TEXT, lead_id TEXT,
        provider TEXT, attempt INTEGER, source TEXT NOT NULL, claim_token TEXT,
        state TEXT NOT NULL DEFAULT 'CLAIMED', result TEXT, followup_id TEXT,
        error_code TEXT, error_detail TEXT, execution_id TEXT,
        claimed_at DATETIME DEFAULT (datetime('now')), completed_at DATETIME,
        updated_at DATETIME DEFAULT (datetime('now')))""")


# ── claim ─────────────────────────────────────────────────────────────

def next_attempt(db, lead_id, crm_attempts):
    """Intento de negocio a usar.

    Si hay un intento LIBERADO por error técnico, se retoma ese mismo número (no
    se salta: no se consume). Si no, el mayor entre el contador del CRM y lo
    registrado localmente, +1. Así, si LeadStudio no incrementa `attempts`
    (PENDING_VERIFICATION PV-1), el UNIQUE(lead_id, attempt) no bloquea al lead."""
    rel = db.one("SELECT MIN(attempt) AS a FROM wf_call_jobs WHERE lead_id=§ AND state='RELEASED'",
                 (lead_id,))
    if rel and rel.get('a') is not None:
        return int(rel['a'])
    row = db.one("SELECT MAX(attempt) AS m FROM wf_call_jobs WHERE lead_id=§", (lead_id,))
    return max(int(crm_attempts or 0), int((row or {}).get('m') or 0)) + 1


def claim_job(db, call_job_id, lead_id, route_id, route_key, country_iso, provider,
              attempt, execution_id=None, adapter_key=None):
    """True = ganó. False = SKIP (otro lo tiene, el lead ya tiene una llamada en
    vuelo, o el intento liberado sigue en backoff)."""
    db.execute(
        f"{_ins_ignore(db)} INTO wf_call_jobs (call_job_id, lead_id, route_id, route_key, "
        "country_iso, provider, adapter_key, attempt, execution_id, state, created_at, claimed_at) "
        f"VALUES (§,§,§,§,§,§,§,§,§,'CLAIMED',{_now(db)},{_now(db)})",
        (call_job_id, lead_id, route_id, route_key, country_iso, provider, adapter_key,
         int(attempt), execution_id))
    if _owner(db, lead_id, attempt) == call_job_id:
        return True
    # re-claim de un intento liberado por error TÉCNICO, respetando el backoff
    db.execute(
        "UPDATE wf_call_jobs SET call_job_id=§, route_id=§, route_key=§, country_iso=§, provider=§, "
        f"adapter_key=§, execution_id=§, state='CLAIMED', claimed_at={_now(db)} "
        "WHERE lead_id=§ AND attempt=§ AND state='RELEASED' "
        f"AND (next_tech_retry_at IS NULL OR next_tech_retry_at <= {_now(db)})",
        (call_job_id, route_id, route_key, country_iso, provider, adapter_key, execution_id,
         lead_id, int(attempt)))
    return _owner(db, lead_id, attempt) == call_job_id


def _owner(db, lead_id, attempt):
    row = db.one("SELECT call_job_id FROM wf_call_jobs WHERE lead_id=§ AND attempt=§",
                 (lead_id, int(attempt)))
    return row.get('call_job_id') if row else None


def _transition(db, call_job_id, from_states, to_state, extra_sql='', extra_params=()):
    ph = ','.join(['§'] * len(from_states))
    _, rc = db.execute(
        f"UPDATE wf_call_jobs SET state=§, updated_at={_now(db)} {extra_sql} "
        f"WHERE call_job_id=§ AND state IN ({ph})",
        (to_state, *extra_params, call_job_id, *from_states))
    return rc == 1


# ── despacho ──────────────────────────────────────────────────────────

def mark_dispatching(db, call_job_id):
    """ANTES del HTTP al proveedor. False → NO despachar."""
    return _transition(db, call_job_id, ['CLAIMED'], 'DISPATCHING')


def mark_dispatched(db, call_job_id, conversation_id=None, provider_job_id=None,
                    provider_call_id=None, http_status=None):
    """dispatch_outcome = ACCEPTED."""
    return _transition(
        db, call_job_id, ['DISPATCHING'], 'DISPATCHED',
        f", conversation_id=§, provider_job_id=§, provider_call_id=§, dispatch_http_status=§, "
        f"dispatched_at={_now(db)}",
        (conversation_id, provider_job_id, provider_call_id, http_status))


def mark_released(db, call_job_id, error_code, error_message=None, http_status=None):
    """dispatch_outcome = TECHNICAL_ERROR: la llamada NO salió. No consume el
    intento de negocio; programa el reintento técnico con backoff."""
    row = db.one("SELECT tech_retry_count FROM wf_call_jobs WHERE call_job_id=§", (call_job_id,))
    if not row:
        return False
    n = int(row['tech_retry_count'] or 0) + 1
    return _transition(
        db, call_job_id, ['CLAIMED', 'DISPATCHING'], 'RELEASED',
        f", error_class='TECHNICAL', error_code=§, error_message=§, dispatch_http_status=§, "
        f"tech_retry_count=§, next_tech_retry_at={_plus_minutes(db, tech_backoff_minutes(n))}",
        (error_code, (error_message or '')[:255], http_status, n))


def mark_unknown(db, call_job_id, error_message=None):
    """dispatch_outcome = UNKNOWN: la llamada PUDO salir. Jamás re-dispatch."""
    return _transition(db, call_job_id, ['DISPATCHING'], 'UNKNOWN',
                       ", error_class='AMBIGUOUS', error_code='DISPATCH_UNKNOWN', error_message=§",
                       ((error_message or '')[:255],))


def mark_failed(db, call_job_id, error_code, error_message=None):
    """Terminal sin reintento (dato inválido detectado antes de enviar)."""
    return _transition(db, call_job_id, ['CLAIMED', 'DISPATCHING'], 'FAILED',
                       ", error_class='PERMANENT', error_code=§, error_message=§",
                       (error_code, (error_message or '')[:255]))


def record_result(db, call_job_id, result, duration_seconds=None, conversation_id=None,
                  sip_code=None, callback_at=None):
    """Resultado FINAL en el hecho de la llamada. Primer escritor gana.

    Fuente de las métricas de llamadas: el mismo resultado recibido dos veces no
    suma minutos dos veces. Resuelve UNKNOWN / NEEDS_RECONCILIATION si el
    post-call llega tarde.
    Devuelve RECORDED · DUPLICATE (mismo resultado) · CONFLICT (otro) · NOT_FOUND.
    """
    if result not in FINAL_RESULTS:
        raise ValueError(f'resultado final inválido: {result!r}')
    _, rc = db.execute(
        f"UPDATE wf_call_jobs SET state='COMPLETED', result=§, duration_seconds=§, "
        f"conversation_id=COALESCE(conversation_id, §), sip_code=COALESCE(sip_code, §), "
        f"callback_at=§, completed_at={_now(db)}, dispatched_at=COALESCE(dispatched_at, {_now(db)}), "
        f"updated_at={_now(db)} "
        "WHERE call_job_id=§ AND result IS NULL "
        "AND state IN ('DISPATCHING','DISPATCHED','UNKNOWN','NEEDS_RECONCILIATION')",
        (result, int(duration_seconds) if duration_seconds is not None else None,
         conversation_id, sip_code, callback_at, call_job_id))
    if rc == 1:
        return 'RECORDED'
    row = job_get(db, call_job_id)
    if not row:
        return 'NOT_FOUND'
    return 'DUPLICATE' if row.get('result') == result else 'CONFLICT'


def attach_followup(db, call_job_id, followup_id):
    _, rc = db.execute("UPDATE wf_call_jobs SET followup_id=§ WHERE call_job_id=§ AND followup_id IS NULL",
                       (followup_id, call_job_id))
    return rc == 1


def find_inflight_job(db, lead_id):
    """lead_id → la ÚNICA llamada en vuelo (UNIQUE inflight_lead)."""
    return db.one("SELECT * FROM wf_call_jobs WHERE inflight_lead=§", (lead_id,)) or None


def reconcile_stale_jobs(db, dispatching_minutes=5, dispatched_minutes=60, claimed_minutes=10):
    """Reclasifica. NUNCA re-despacha."""
    out = {}
    _, out['dispatching'] = db.execute(
        "UPDATE wf_call_jobs SET state='NEEDS_RECONCILIATION', error_code='STALE_DISPATCHING', "
        f"error_class='AMBIGUOUS' WHERE state='DISPATCHING' AND updated_at < {_minus_minutes(db, dispatching_minutes)}")
    _, out['unknown'] = db.execute(
        "UPDATE wf_call_jobs SET state='NEEDS_RECONCILIATION' "
        f"WHERE state='UNKNOWN' AND updated_at < {_minus_minutes(db, dispatching_minutes)}")
    _, out['dispatched'] = db.execute(
        "UPDATE wf_call_jobs SET state='NEEDS_RECONCILIATION', error_code='NO_POSTCALL' "
        f"WHERE state='DISPATCHED' AND dispatched_at < {_minus_minutes(db, dispatched_minutes)}")
    _, out['claimed'] = db.execute(
        "UPDATE wf_call_jobs SET state='RELEASED', error_class='TECHNICAL', error_code='STALE_CLAIM', "
        "tech_retry_count=tech_retry_count+1 "
        f"WHERE state='CLAIMED' AND claimed_at < {_minus_minutes(db, claimed_minutes)}")
    return out


def job_get(db, call_job_id):
    return db.one("SELECT * FROM wf_call_jobs WHERE call_job_id=§", (call_job_id,)) or None


# ── ledger de conversaciones (igual que V2.1) ─────────────────────────

def claim_conversation(db, conversation_id, source, claim_token=None, route_key=None,
                       lead_id=None, provider=None, attempt=None, call_job_id=None,
                       execution_id=None):
    if not conversation_id:
        raise ValueError('conversation_id es obligatorio (clave de idempotencia)')
    claim_token = claim_token or secrets.token_hex(16)
    db.execute(
        f"{_ins_ignore(db)} INTO wf_conversation_ledger (conversation_id, source, claim_token, "
        "route_key, lead_id, provider, attempt, call_job_id, execution_id, state) "
        "VALUES (§,§,§,§,§,§,§,§,§,'CLAIMED')",
        (conversation_id, source, claim_token, route_key, lead_id, provider, attempt,
         call_job_id, execution_id))
    row = db.one("SELECT claim_token FROM wf_conversation_ledger WHERE conversation_id=§",
                 (conversation_id,))
    return bool(row) and row.get('claim_token') == claim_token


def complete_conversation(db, conversation_id, followup_id, result=None):
    _, rc = db.execute(
        f"UPDATE wf_conversation_ledger SET state='PROCESSED', followup_id=§, result=§, "
        f"completed_at={_now(db)} WHERE conversation_id=§ AND state='CLAIMED'",
        (followup_id, result, conversation_id))
    return rc == 1


def fail_conversation(db, conversation_id, error_code, error_detail=None, followup_may_exist=False):
    state = 'NEEDS_RECONCILIATION' if followup_may_exist else 'FAILED'
    _, rc = db.execute(
        "UPDATE wf_conversation_ledger SET state=§, error_code=§, error_detail=§ "
        "WHERE conversation_id=§ AND state='CLAIMED'",
        (state, error_code, (error_detail or '')[:255], conversation_id))
    return rc == 1


def reconcile_stale_conversations(db, claimed_minutes=15):
    _, n = db.execute(
        "UPDATE wf_conversation_ledger SET state='NEEDS_RECONCILIATION', error_code='STALE_CLAIM' "
        f"WHERE state='CLAIMED' AND followup_id IS NULL AND claimed_at < {_minus_minutes(db, claimed_minutes)}")
    return n


def conversation_get(db, conversation_id):
    return db.one("SELECT * FROM wf_conversation_ledger WHERE conversation_id=§", (conversation_id,)) or None
