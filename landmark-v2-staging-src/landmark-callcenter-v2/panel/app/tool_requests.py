"""
tool_requests.py — WF3 / WF7: claim idempotente de las tools de país
====================================================================

El problema real de v1
----------------------
ElevenLabs reintenta un tool-call si la respuesta tarda. El bot de WhatsApp
puede pedir lo mismo por otra vía. WF3 v1 se apoyaba en el 409 de CashStudio,
que llega DESPUÉS de haber pedido la cuenta; WF7 v1 en un `X-Idempotency-Key`
que el proveedor puede ignorar. En ambos casos, dos pedidos simultáneos podían
salir los dos.

V2: se reserva el pedido ANTES de llamar al proveedor
-----------------------------------------------------
`request_key` = `{TOOL_TYPE}:{lead_id}:{request_ref}`, DETERMINISTA y UNIQUE.
`request_ref` = conversation_id de la llamada que pidió la tool, o execution_id
si no hay conversación (mismo criterio que ANALYTICS_EVENT_CONTRACT · key_parts).

El ganador ejecuta. El perdedor NO vuelve a llamar al proveedor: lee la fila y
responde con lo que ya se registró. Dos pedidos DISTINTOS del mismo lead (dos
llamadas distintas) son dos claims distintos, y eso es correcto: es el 409 del
proveedor de cuentas —no este ledger— el que decide que el lead ya tiene cuenta.

Las 3 tools (`CREATE_ACCOUNT`, `CREATE_PAYMENT_LINK`, `CALLBACK`) usan el mismo
mecanismo: pertenecen al PAÍS, no al proveedor de voz.
"""

import secrets

TOOL_TYPES = ['CREATE_ACCOUNT', 'CREATE_PAYMENT_LINK', 'CALLBACK']
STATES = ['CLAIMED', 'SUCCEEDED', 'ALREADY_EXISTS', 'FAILED', 'NEEDS_RECONCILIATION']


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
    db.execute("""CREATE TABLE IF NOT EXISTS wf_tool_requests (
        id INTEGER PRIMARY KEY AUTOINCREMENT, request_key TEXT NOT NULL UNIQUE,
        tool_type TEXT NOT NULL, lead_id TEXT NOT NULL, country_iso TEXT,
        request_ref TEXT, call_job_id TEXT, conversation_id TEXT, route_key TEXT,
        tool_provider TEXT, mode TEXT, order_ref TEXT, amount REAL, currency TEXT,
        claim_token TEXT, state TEXT NOT NULL DEFAULT 'CLAIMED', result_ref TEXT,
        error_code TEXT, error_message TEXT, execution_id TEXT,
        claimed_at DATETIME DEFAULT (datetime('now')), completed_at DATETIME,
        updated_at DATETIME DEFAULT (datetime('now')))""")


def request_key(tool_type, lead_id, request_ref):
    if tool_type not in TOOL_TYPES:
        raise ValueError(f'VALIDATION_ERROR: tool_type desconocido {tool_type!r}')
    if not lead_id:
        raise ValueError('VALIDATION_ERROR: lead_id es obligatorio')
    if not request_ref:
        raise ValueError('VALIDATION_ERROR: request_ref es obligatorio '
                         '(conversation_id de la llamada, o execution_id)')
    return f'{tool_type}:{lead_id}:{request_ref}'


def claim(db, tool_type, lead_id, request_ref, claim_token=None, country_iso=None,
          call_job_id=None, conversation_id=None, route_key=None, tool_provider=None,
          mode=None, amount=None, currency=None, execution_id=None):
    """(won, row). won=True ⇒ esta ejecución llama al proveedor.
    won=False ⇒ NO llamar: devolver el resultado de `row`."""
    key = request_key(tool_type, lead_id, request_ref)
    claim_token = claim_token or secrets.token_hex(16)
    db.execute(
        f"{_ins_ignore(db)} INTO wf_tool_requests (request_key, tool_type, lead_id, country_iso, "
        "request_ref, call_job_id, conversation_id, route_key, tool_provider, mode, amount, "
        "currency, claim_token, execution_id, state) VALUES (§,§,§,§,§,§,§,§,§,§,§,§,§,§,'CLAIMED')",
        (key, tool_type, lead_id, country_iso, request_ref, call_job_id, conversation_id,
         route_key, tool_provider, mode, amount, currency, claim_token, execution_id))
    row = db.one("SELECT * FROM wf_tool_requests WHERE request_key=§", (key,))
    return (bool(row) and row.get('claim_token') == claim_token), row


def _finish(db, key, state, extra_sql='', extra_params=()):
    _, rc = db.execute(
        f"UPDATE wf_tool_requests SET state=§, completed_at={_now(db)}, updated_at={_now(db)} "
        f"{extra_sql} WHERE request_key=§ AND state='CLAIMED'",
        (state, *extra_params, key))
    return rc == 1


def mark_succeeded(db, key, result_ref=None, order_ref=None):
    """result_ref: id de la cuenta creada o del link de pago. order_ref: la orden
    del proveedor de pagos (out_trade_no) — es la clave del evento PAYMENT_*."""
    return _finish(db, key, 'SUCCEEDED',
                   ", result_ref=§, order_ref=COALESCE(§, order_ref)", (result_ref, order_ref))


def mark_already_exists(db, key, result_ref=None):
    """409 del proveedor de cuentas. NO es un fallo: el lead ya tenía cuenta."""
    return _finish(db, key, 'ALREADY_EXISTS', ", result_ref=§", (result_ref,))


def mark_failed(db, key, error_code, error_message=None):
    return _finish(db, key, 'FAILED', ", error_code=§, error_message=§",
                   (error_code, (error_message or '')[:255]))


def mark_needs_reconciliation(db, key, error_code='AMBIGUOUS', error_message=None):
    """El POST al proveedor pudo ejecutarse y no sabemos el resultado (timeout,
    502). JAMÁS se reintenta a ciegas: crear dos cuentas o dos links de pago es
    peor que dejarlo para revisión."""
    return _finish(db, key, 'NEEDS_RECONCILIATION', ", error_code=§, error_message=§",
                   (error_code, (error_message or '')[:255]))


def release_stale(db, claimed_minutes=15):
    """Un claim huérfano (n8n murió después de reservar) queda en revisión, no se
    reintenta solo."""
    _, n = db.execute(
        "UPDATE wf_tool_requests SET state='NEEDS_RECONCILIATION', error_code='STALE_CLAIM' "
        f"WHERE state='CLAIMED' AND claimed_at < {_minus_minutes(db, claimed_minutes)}")
    return n


def get(db, key):
    return db.one("SELECT * FROM wf_tool_requests WHERE request_key=§", (key,)) or None


def last_for_lead(db, tool_type, lead_id):
    return db.one("SELECT * FROM wf_tool_requests WHERE tool_type=§ AND lead_id=§ "
                  "ORDER BY claimed_at DESC LIMIT 1", (tool_type, lead_id)) or None
