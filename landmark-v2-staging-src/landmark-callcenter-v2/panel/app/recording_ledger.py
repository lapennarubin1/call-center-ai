"""
recording_ledger.py — WF10 V2: correlación e idempotencia de grabaciones
========================================================================

Qué cambia respecto de WF10 v1
------------------------------
v1 correlacionaba una grabación con su follow-up por `RIGHT(phone,10)` contra
`wf_call_followups`, tomando "el más reciente sin grabación". Con varios
intentos por lead, o con dos rutas activas en el mismo país, eso adjunta el
audio de una llamada al follow-up de otra — sin error visible.

V2 usa la cadena de identidad:

    call_job_id → conversation_id → followup_id → grabación

`wf_call_jobs` guarda las tres primeras y `wf_recording_ledger` las ata a la
grabación concreta. La correlación por teléfono queda como COMPATIBILIDAD
explícita (`correlation='LEGACY_PHONE'`), solo para grabaciones anteriores al
corte, y marcada como tal en la base.

Idempotencia
------------
`recording_ref` es la identidad de la grabación EN SU ORIGEN y es UNIQUE:
  · Stringee local → filename del worker (`stringee-<phone>-<ms>.wav`)
  · ElevenLabs     → conversation_id
Ganar el claim es la única autorización para subir y reenviar. Se gana por
relectura del token propio, nunca por affectedRows (N8N_TEMPLATE_STANDARD §6.1).
"""

import secrets

STATES = ['CLAIMED', 'UPLOADED', 'SENT', 'SKIPPED_SHORT', 'ORPHAN', 'FAILED']
CORRELATIONS = ['CALL_JOB', 'CONVERSATION', 'FOLLOWUP', 'LEGACY_PHONE', 'NONE']
SOURCES = ['STRINGEE_WORKER', 'ELEVENLABS_API']


def _ins_ignore(db):
    return 'INSERT IGNORE' if db.driver == 'mysql' else 'INSERT OR IGNORE'


def _now(db):
    return 'UTC_TIMESTAMP()' if db.driver == 'mysql' else "datetime('now')"


def _minus_minutes(db, m):
    return (f'DATE_SUB(UTC_TIMESTAMP(), INTERVAL {int(m)} MINUTE)' if db.driver == 'mysql'
            else f"datetime('now', '-{int(m)} minutes')")


def ensure_tables_sqlite(db):
    """Solo tests/demo. En MariaDB la tabla la crea la migración 002."""
    if db.driver == 'mysql':
        return
    db.execute("""CREATE TABLE IF NOT EXISTS wf_recording_ledger (
        id INTEGER PRIMARY KEY AUTOINCREMENT, recording_ref TEXT NOT NULL UNIQUE,
        source TEXT NOT NULL, call_job_id TEXT, conversation_id TEXT, followup_id TEXT,
        lead_id TEXT, route_key TEXT, country_iso TEXT, provider TEXT,
        duration_seconds INTEGER, size_bytes INTEGER, correlation TEXT,
        state TEXT NOT NULL DEFAULT 'CLAIMED', claim_token TEXT,
        crm_uploaded INTEGER NOT NULL DEFAULT 0, telegram_sent INTEGER NOT NULL DEFAULT 0,
        error_code TEXT, error_message TEXT, execution_id TEXT,
        claimed_at DATETIME DEFAULT (datetime('now')), completed_at DATETIME,
        updated_at DATETIME DEFAULT (datetime('now')))""")


# ── correlación ───────────────────────────────────────────────────────

def correlate(db, conversation_id=None, call_job_id=None, lead_id=None, phone=None,
              allow_legacy_phone=False):
    """Resuelve la llamada a la que pertenece una grabación.

    Orden de preferencia (el primero que resuelve, gana):
      1. call_job_id     — el proveedor lo propagó: identidad directa
      2. conversation_id — ElevenLabs lo devuelve siempre que hubo conversación
      3. lead_id         — la ÚNICA llamada en vuelo o la última completada
      4. phone (últimos 10) — SOLO si allow_legacy_phone: compatibilidad

    Devuelve (job|None, correlation). `correlation` queda guardado en el ledger:
    una grabación adjuntada por teléfono es auditable como tal.
    """
    if call_job_id:
        job = db.one("SELECT * FROM wf_call_jobs WHERE call_job_id=§", (call_job_id,))
        if job:
            return job, 'CALL_JOB'
    if conversation_id:
        job = db.one("SELECT * FROM wf_call_jobs WHERE conversation_id=§", (conversation_id,))
        if job:
            return job, 'CONVERSATION'
    if lead_id:
        job = db.one("SELECT * FROM wf_call_jobs WHERE inflight_lead=§", (lead_id,))
        if job:
            return job, 'FOLLOWUP' if job.get('followup_id') else 'CALL_JOB'
        job = db.one("SELECT * FROM wf_call_jobs WHERE lead_id=§ AND result IS NOT NULL "
                     "ORDER BY completed_at DESC LIMIT 1", (lead_id,))
        if job:
            return job, 'FOLLOWUP' if job.get('followup_id') else 'CALL_JOB'
    if phone and allow_legacy_phone:
        last10 = ''.join(ch for ch in str(phone) if ch.isdigit())[-10:]
        if last10:
            job = db.one(
                "SELECT j.* FROM wf_call_jobs j WHERE j.lead_id IN "
                "(SELECT lead_id FROM wf_call_jobs WHERE lead_id LIKE §) LIMIT 0", (last10,))
            # La búsqueda real por teléfono vive en el workflow (la tabla de leads
            # es del CRM, no de V2). Acá se deja la marca de compatibilidad para
            # que el ledger registre CÓMO se correlacionó.
            if job:
                return job, 'LEGACY_PHONE'
    return None, 'NONE'


# ── claim ─────────────────────────────────────────────────────────────

def claim_recording(db, recording_ref, source, claim_token=None, call_job_id=None,
                    conversation_id=None, followup_id=None, lead_id=None, route_key=None,
                    country_iso=None, provider=None, duration_seconds=None, size_bytes=None,
                    correlation=None, execution_id=None):
    """True = esta ejecución es la dueña de la grabación y debe procesarla.
    False = ya la tomó otra (o otra corrida anterior): NO subir, NO reenviar."""
    if not recording_ref:
        raise ValueError('recording_ref es obligatorio (clave de idempotencia)')
    if source not in SOURCES:
        raise ValueError(f'source desconocido: {source!r}')
    claim_token = claim_token or secrets.token_hex(16)
    db.execute(
        f"{_ins_ignore(db)} INTO wf_recording_ledger (recording_ref, source, claim_token, "
        "call_job_id, conversation_id, followup_id, lead_id, route_key, country_iso, provider, "
        "duration_seconds, size_bytes, correlation, execution_id, state) "
        "VALUES (§,§,§,§,§,§,§,§,§,§,§,§,§,§,'CLAIMED')",
        (recording_ref, source, claim_token, call_job_id, conversation_id, followup_id,
         lead_id, route_key, country_iso, provider,
         int(duration_seconds) if duration_seconds is not None else None,
         int(size_bytes) if size_bytes is not None else None,
         correlation or 'NONE', execution_id))
    row = db.one("SELECT claim_token FROM wf_recording_ledger WHERE recording_ref=§",
                 (recording_ref,))
    return bool(row) and row.get('claim_token') == claim_token


def _finish(db, recording_ref, state, extra_sql='', extra_params=()):
    _, rc = db.execute(
        f"UPDATE wf_recording_ledger SET state=§, completed_at={_now(db)}, "
        f"updated_at={_now(db)} {extra_sql} WHERE recording_ref=§ AND state='CLAIMED'",
        (state, *extra_params, recording_ref))
    return rc == 1


def mark_uploaded(db, recording_ref, followup_id=None):
    """Subida al CRM confirmada. No es terminal: falta Telegram si la ruta lo pide."""
    _, rc = db.execute(
        f"UPDATE wf_recording_ledger SET crm_uploaded=1, state='UPLOADED', "
        f"followup_id=COALESCE(§, followup_id), updated_at={_now(db)} "
        "WHERE recording_ref=§ AND state IN ('CLAIMED','UPLOADED')",
        (followup_id, recording_ref))
    return rc == 1


def mark_sent(db, recording_ref, telegram=True):
    """Terminal: la grabación llegó a todos los destinos que la ruta pedía."""
    _, rc = db.execute(
        f"UPDATE wf_recording_ledger SET telegram_sent=§, state='SENT', "
        f"completed_at={_now(db)}, updated_at={_now(db)} "
        "WHERE recording_ref=§ AND state IN ('CLAIMED','UPLOADED')",
        (1 if telegram else 0, recording_ref))
    return rc == 1


def mark_skipped_short(db, recording_ref, duration_seconds, min_secs):
    """Por debajo del mínimo de la ruta. Terminal y contable: el panel muestra
    cuántas grabaciones se descartan por cortas."""
    return _finish(db, recording_ref, 'SKIPPED_SHORT',
                   ", duration_seconds=§, error_code='BELOW_MIN', error_message=§",
                   (int(duration_seconds) if duration_seconds is not None else None,
                    f'duration {duration_seconds}s < route minimum {min_secs}s'))


def mark_orphan(db, recording_ref, detail=None):
    """No se pudo atar a ninguna llamada. NO se sube al CRM: adjuntar un audio al
    follow-up equivocado es peor que no adjuntarlo."""
    return _finish(db, recording_ref, 'ORPHAN',
                   ", error_code='NO_CORRELATION', error_message=§", ((detail or '')[:255],))


def mark_failed(db, recording_ref, error_code, error_message=None):
    return _finish(db, recording_ref, 'FAILED',
                   ", error_code=§, error_message=§",
                   (error_code, (error_message or '')[:255]))


def release_stale(db, claimed_minutes=30):
    """Una ejecución que murió con el claim tomado dejaría la grabación bloqueada
    para siempre. Tras el umbral se marca FAILED para que el operador la vea;
    NO se reenvía sola (podría duplicar un envío que sí ocurrió)."""
    _, n = db.execute(
        "UPDATE wf_recording_ledger SET state='FAILED', error_code='STALE_CLAIM' "
        f"WHERE state='CLAIMED' AND claimed_at < {_minus_minutes(db, claimed_minutes)}")
    return n


def get(db, recording_ref):
    return db.one("SELECT * FROM wf_recording_ledger WHERE recording_ref=§",
                  (recording_ref,)) or None


def pending_for_route(db, route_key, limit=200):
    return db.q("SELECT * FROM wf_recording_ledger WHERE route_key=§ AND state='CLAIMED' "
                "ORDER BY claimed_at LIMIT " + str(int(limit)), (route_key,))
