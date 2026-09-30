"""TEMPLATE_FOLLOWUP_ENGINE_V2 — el ÚNICO motor de follow-up del sistema.

Contrato: contracts/FOLLOWUP_ENGINE_CONTRACT_V2_2.json
Oráculo ejecutable: panel/app/followup_engine.py  (paridad verificada en tests)

No conoce proveedores ni países: recibe un CALL RESULT normalizado y decide.
Lo llaman WF2 (solo resultados FINAL sin conversación) y WF9 (todo post-call).
"""

VERSION = '2.0.0'
NAME = 'TEMPLATE_FOLLOWUP_ENGINE_V2'

INPUT_FIELDS = ['call_job_id', 'lead_id', 'route_key', 'country_iso', 'provider',
                'adapter_key', 'attempt', 'conversation_id', 'provider_job_id',
                'sip_code', 'result', 'call_status', 'duration_seconds',
                'callback_requested', 'callback_at', 'notes', 'summary',
                'summary_language', 'summary_is_english', 'source',
                'execution_id', 'access_token']


def build(wfbuild, js):
    W = wfbuild.Workflow(
        NAME, VERSION,
        'FOLLOWUP_ENGINE v2.2 · TEMPLATE_DATA v2.2 · ANALYTICS_EVENT v2.2',
        'ninguno: en v1 la política vivía repartida entre WF2 y WF9 (§12)',
        'Sub-workflow. Lo invocan WF2 y WF9 con Execute Workflow.')
    CORE, ENG, NOTES = js['lmcore.js'], js['lmengine.js'], js['lmnotes.js']

    # ── 00 TRIGGERS ───────────────────────────────────────────────────
    W.sticky(0, '00 — TRIGGER', f"""{W.meta_header}

**Sub-workflow.** No tiene trigger propio: lo invocan
`TEMPLATE_WF2_CALL_DISPATCHER_V2` (solo cuando el dispatch YA es el resultado
final, p. ej. SIP 603) y `TEMPLATE_WF9_POST_CALL_HANDLER_V2` (todo post-call).

Entra **un CALL RESULT normalizado**, nunca la respuesta cruda de un proveedor.
`access_token` es opcional: si el llamador ya hizo login, se reusa y este
workflow no vuelve a autenticarse (un login por ciclo, no por llamada).

NUNCA se invoca con `result=DISPATCHED`: el motor lo rechaza con
VALIDATION_ERROR.""", color=4, height=420)
    t = W.exec_trigger('[TRIGGER] Call Result Input', INPUT_FIELDS, 0)

    # ── 01 VALIDATE ───────────────────────────────────────────────────
    W.sticky(1, '01 — VALIDATE INPUT', """Contrato completo, resultado FINAL,
`attempt >= 1`, y al menos uno de `conversation_id` / `call_job_id`.

`DISPATCHED` **no es un resultado final**: si llega, es un bug del llamador y
se corta acá con VALIDATION_ERROR en vez de crear un follow-up inventado.

Un error TÉCNICO de dispatch (AUTH_ERROR, CONFIG_ERROR, 401/403) **nunca**
debería llegar hasta acá: lo maneja WF2 como RELEASED.""", color=6, height=360)
    val = W.code('[ENGINE] Validate Input', CORE + ENG + r'''
// Valida el CALL RESULT CONTRACT antes de tocar nada.
const items = $input.all();
const out = [];
for (const it of items) {
  const d = it.json || {};
  const errs = [];
  const result = String(d.result || '').toUpperCase();
  const attempt = Number(d.attempt);

  if (!d.lead_id) errs.push('lead_id');
  if (!d.route_key) errs.push('route_key');
  if (!Number.isInteger(attempt) || attempt < 1) errs.push('attempt (entero >= 1)');
  if (!result) errs.push('result');
  if (!d.execution_id) errs.push('execution_id');
  if (!d.conversation_id && !d.call_job_id) errs.push('conversation_id o call_job_id');

  if (result === 'DISPATCHED') {
    errs.push('result=DISPATCHED no es final: el motor no se llama con el');
  } else if (result && LM_FINAL_RESULTS.indexOf(result) < 0) {
    errs.push('result desconocido: ' + result);
  }

  // clave de idempotencia: conversation_id si existe, si no call_job_id
  const idemKey = d.conversation_id || d.call_job_id;
  const idemKind = d.conversation_id ? 'conversation_id' : 'call_job_id';

  out.push({ json: Object.assign({}, d, {
    result: result,
    attempt: attempt,
    valid: errs.length === 0,
    error_code: errs.length ? 'VALIDATION_ERROR' : null,
    error_detail: errs.length ? ('campos invalidos o ausentes: ' + errs.join(', ')) : null,
    idempotency_key: idemKey,
    idempotency_kind: idemKind,
    sip_code: (d.sip_code === undefined || d.sip_code === '') ? null : d.sip_code,
    duration_seconds: (d.duration_seconds === undefined || d.duration_seconds === null)
                      ? null : Number(d.duration_seconds)
  })});
}
return out;
''', 1)
    val_if = W.if_('[ENGINE] Input Is Valid', '={{ $json.valid }}', 'true', True, 1,
                   offset=1, single=True)
    W.chain(t, val, val_if)

    err_val = W.code('[ERROR] Validation Error', CORE + r'''
// El item no cumple el contrato. Se descarta CON MOTIVO (nunca en silencio:
// v1 tiraba los post-call sin lead_id sin dejar rastro) y no se crea followup.
const d = $json;
lmLog('ENGINE', d, { lead_id: d.lead_id, route_key: d.route_key, attempt: d.attempt,
                     call_job_id: d.call_job_id, conversation_id: d.conversation_id,
                     error_code: 'VALIDATION_ERROR', action: 'REJECT' });
return [{ json: { ok: false, skipped: true, error: true, error_code: 'VALIDATION_ERROR',
                  error_detail: d.error_detail, lead_id: d.lead_id || null,
                  call_job_id: d.call_job_id || null } }];
''', 1, branch=2, offset=1)

    # ── 02 RESOLVE JOB ────────────────────────────────────────────────
    W.sticky(2, '02 — RESOLVE JOB', """`call_job_id` de la entrada; si el
proveedor no lo propagó, se resuelve por la **única llamada en vuelo del lead**
(`wf_call_jobs.inflight_lead` es UNIQUE, así que no hay ambigüedad posible).

Ese UNIQUE es lo que permite que el worker Stringee —que hoy solo reenvía
`lead_id`— funcione sin cambios (PV-3).

Sin job ⇒ issue `ORPHAN_POSTCALL` y fin. No se crea un follow-up huérfano.""",
              color=3, height=380)
    jobq = W.mysql('[DB] Resolve Call Job', (
        "SELECT * FROM wf_call_jobs\n"
        " WHERE (? <> '' AND call_job_id = ?)\n"
        "    OR (? = '' AND inflight_lead = ?)\n"
        " LIMIT 1"),
        2, replacements='={{ $json.call_job_id || "" }},={{ $json.call_job_id || "" }},'
                        '={{ $json.call_job_id || "" }},={{ $json.lead_id }}',
        always_output=True, on_error='continueRegularOutput', retry=2)
    W.link(val_if, jobq, 0)
    W.link(val_if, err_val, 1)

    jobmerge = W.code('[ENGINE] Merge Job Context', CORE + r'''
// El nodo MySQL reemplaza el json del item por la fila. Hay que recomponer el
// contexto del CALL RESULT (que quedó en el nodo de validación) con el job.
const rows = $input.all();
const out = [];
for (let i = 0; i < rows.length; i++) {
  const job = rows[i].json && rows[i].json.call_job_id ? rows[i].json : null;
  const ctx = $('[ENGINE] Validate Input').all()[Math.min(i, $('[ENGINE] Validate Input').all().length - 1)].json;
  out.push({ json: Object.assign({}, ctx, {
    job_found: !!job,
    // Los datos del JOB mandan sobre los del post-call: el job es el hecho local.
    call_job_id: job ? job.call_job_id : ctx.call_job_id,
    route_key: job ? job.route_key : ctx.route_key,
    country_iso: job ? job.country_iso : ctx.country_iso,
    provider: job ? job.provider : ctx.provider,
    adapter_key: job ? job.adapter_key : ctx.adapter_key,
    job_attempt: job ? Number(job.attempt) : ctx.attempt,
    job_state: job ? job.state : null,
    job_result: job ? job.result : null,
    job_followup_id: job ? job.followup_id : null
  })});
}
return out;
''', 2, offset=1)
    job_if = W.if_('[ENGINE] Job Was Found', '={{ $json.job_found }}', 'true', True, 2,
                   offset=2, single=True)
    W.chain(jobq, jobmerge, job_if)

    orphan = W.mysql('[DB] Open Orphan Issue', (
        "INSERT INTO wf_reconciliation_issues\n"
        "  (issue_key, issue_type, entity_type, entity_id, lead_id, country_iso,\n"
        "   severity, state, detail_json, first_seen_at, last_seen_at)\n"
        "VALUES (?, 'ORPHAN_POSTCALL', 'lead', ?, ?, ?, 'WARN', 'OPEN', ?,\n"
        "        UTC_TIMESTAMP(), UTC_TIMESTAMP())\n"
        "ON DUPLICATE KEY UPDATE occurrences = occurrences + 1,\n"
        "        last_seen_at = UTC_TIMESTAMP(), detail_json = VALUES(detail_json)"),
        2, branch=2, offset=2,
        replacements='={{ "ORPHAN_POSTCALL:lead:" + $json.lead_id }},={{ $json.lead_id }},'
                     '={{ $json.lead_id }},={{ $json.country_iso || null }},'
                     '={{ JSON.stringify({result: $json.result, conversation_id: $json.conversation_id, source: $json.source}) }}',
        on_error='continueRegularOutput', retry=2)
    orphan_log = W.code('[LOG] Orphan Post Call', CORE + r'''
// Llegó un resultado de una llamada que V2 no registró: post-call de una
// llamada lanzada por v1, o un lead cuyo job ya se cerró. NO se crea followup
// (no sabemos a qué intento pertenece) y queda como issue para revisión.
const d = $json;
lmLog('ENGINE', d, { lead_id: d.lead_id, route_key: d.route_key, result: d.result,
                     conversation_id: d.conversation_id, error_code: 'ORPHAN_POSTCALL',
                     action: 'ISSUE' });
return [{ json: { ok: false, skipped: true, reason: 'ORPHAN_POSTCALL',
                  lead_id: d.lead_id, conversation_id: d.conversation_id || null } }];
''', 2, branch=2, offset=3)
    W.link(job_if, orphan, 1)
    W.chain(orphan, orphan_log)

    # ── 03 RECORD RESULT (local antes que el CRM) ─────────────────────
    W.sticky(3, '03 — RECORD RESULT (LOCAL FIRST)', """El hecho de la llamada se
escribe **antes** de llamar al CRM: el panel ve el resultado en segundos aunque
LeadStudio esté caído (ANALYTICS_ARCHITECTURE §1).

`UPDATE ... WHERE result IS NULL` ⇒ **el primer escritor gana**. El mismo
resultado recibido dos veces (webhook + polling) no suma minutos dos veces.

  RECORDED   sigue el flujo
  DUPLICATE  fin sin error (otro proceso ya lo registró)
  CONFLICT   issue `RESULT_CONFLICT` y fin: NO se crea un segundo follow-up""",
              color=3, height=400)
    rec = W.mysql('[DB] Record Call Result', (
        "UPDATE wf_call_jobs\n"
        "   SET state = 'COMPLETED', result = ?, duration_seconds = ?,\n"
        "       conversation_id = COALESCE(conversation_id, ?),\n"
        "       sip_code = COALESCE(sip_code, ?), callback_at = ?,\n"
        "       completed_at = UTC_TIMESTAMP(),\n"
        "       dispatched_at = COALESCE(dispatched_at, UTC_TIMESTAMP()),\n"
        "       updated_at = UTC_TIMESTAMP()\n"
        " WHERE call_job_id = ? AND result IS NULL\n"
        "   AND state IN ('DISPATCHING','DISPATCHED','UNKNOWN','NEEDS_RECONCILIATION')"),
        3, replacements='={{ $json.result }},={{ $json.duration_seconds }},'
                        '={{ $json.conversation_id || null }},={{ $json.sip_code || null }},'
                        '={{ $json.callback_at ? $json.callback_at.replace("T"," ").replace("Z","").slice(0,19) : null }},'
                        '={{ $json.call_job_id }}',
        on_error='continueRegularOutput', retry=2)
    W.link(job_if, rec, 0)

    reread = W.mysql('[DB] Read Job After Record', (
        "SELECT call_job_id, lead_id, route_key, country_iso, provider, adapter_key,\n"
        "       attempt, state, result, duration_seconds, conversation_id, sip_code,\n"
        "       followup_id\n"
        "  FROM wf_call_jobs WHERE call_job_id = ?"),
        3, offset=1, replacements='={{ $(\'[ENGINE] Merge Job Context\').item.json.call_job_id }}',
        always_output=True, on_error='continueRegularOutput', retry=2)
    W.chain(rec, reread)

    classify = W.code('[ENGINE] Classify Record Status', CORE + r'''
// Se decide por RELECTURA de la fila, nunca por affectedRows: el driver mysql2
// que usa n8n cambia el significado de ROW_COUNT segun el flag FOUND_ROWS
// (N8N_TEMPLATE_STANDARD §6.1).
const rows = $input.all();
const ctxAll = $('[ENGINE] Merge Job Context').all();
const out = [];
for (let i = 0; i < rows.length; i++) {
  const job = rows[i].json || {};
  const ctx = ctxAll[Math.min(i, ctxAll.length - 1)].json;
  let status;
  if (!job.call_job_id) status = 'NOT_FOUND';
  else if (job.result === ctx.result && job.completed_at !== null) status = 'RECORDED';
  else if (job.result === ctx.result) status = 'RECORDED';
  else if (job.result) status = 'CONFLICT';
  else status = 'NOT_FOUND';

  // Si ya tenia followup_id, el resultado ya estaba registrado por otro proceso.
  if (status === 'RECORDED' && job.followup_id) status = 'DUPLICATE';

  out.push({ json: Object.assign({}, ctx, {
    record_status: status,
    stored_result: job.result || null,
    job_duration_seconds: job.duration_seconds === undefined ? null : job.duration_seconds,
    job_conversation_id: job.conversation_id || ctx.conversation_id || null,
    job_sip_code: job.sip_code || ctx.sip_code || null,
    existing_followup_id: job.followup_id || null
  })});
}
return out;
''', 3, offset=2)
    W.chain(reread, classify)

    sw = W.switch('[ENGINE] Record Status Router', '={{ $json.record_status }}',
                  [('RECORDED', 'recorded'), ('DUPLICATE', 'duplicate'),
                   ('CONFLICT', 'conflict')], 3, offset=3)
    W.chain(classify, sw)

    ev_result = W.mysql('[DB] Record Event Call Result', (
        "INSERT IGNORE INTO wf_events\n"
        "  (event_key, event_type, event_domain, occurred_at, call_job_id, lead_id,\n"
        "   country_iso, route_key, provider, adapter_key, attempt, conversation_id,\n"
        "   result, duration_seconds, source_workflow, execution_id, metadata_json)\n"
        "VALUES (?, 'CALL_RESULT', 'CALL', UTC_TIMESTAMP(), ?, ?, ?, ?, ?, ?, ?, ?, ?, ?,\n"
        "        'ENGINE', ?, ?)"),
        4, replacements='={{ "CALL_RESULT:" + $json.call_job_id }},={{ $json.call_job_id }},'
                        '={{ $json.lead_id }},={{ $json.country_iso }},={{ $json.route_key }},'
                        '={{ $json.provider }},={{ $json.adapter_key || null }},'
                        '={{ $json.job_attempt }},={{ $json.job_conversation_id }},'
                        '={{ $json.result }},={{ $json.job_duration_seconds }},'
                        '={{ $json.execution_id }},'
                        '={{ JSON.stringify({sip_code: $json.job_sip_code, source: $json.source}) }}',
        on_error='continueRegularOutput', retry=2)
    W.link(sw, ev_result, 0)

    dup_log = W.code('[LOG] Duplicate Result', CORE + r'''
// Camino NORMAL cuando webhook y polling observan la misma conversación.
// No es un error y no se reporta como tal.
const d = $json;
lmLog('ENGINE', d, { call_job_id: d.call_job_id, lead_id: d.lead_id,
                     route_key: d.route_key, result: d.result, action: 'DUPLICATE' });
return [{ json: { ok: true, skipped: true, reason: 'DUPLICATE_RESULT',
                  call_job_id: d.call_job_id, followup_id: d.existing_followup_id } }];
''', 4, branch=2)
    W.link(sw, dup_log, 1)

    conflict = W.mysql('[DB] Open Result Conflict Issue', (
        "INSERT INTO wf_reconciliation_issues\n"
        "  (issue_key, issue_type, entity_type, entity_id, lead_id, country_iso,\n"
        "   severity, state, detail_json, first_seen_at, last_seen_at)\n"
        "VALUES (?, 'RESULT_CONFLICT', 'call_job', ?, ?, ?, 'ERROR', 'OPEN', ?,\n"
        "        UTC_TIMESTAMP(), UTC_TIMESTAMP())\n"
        "ON DUPLICATE KEY UPDATE occurrences = occurrences + 1,\n"
        "        last_seen_at = UTC_TIMESTAMP(), detail_json = VALUES(detail_json)"),
        4, branch=3,
        replacements='={{ "RESULT_CONFLICT:call_job:" + $json.call_job_id }},'
                     '={{ $json.call_job_id }},={{ $json.lead_id }},={{ $json.country_iso }},'
                     '={{ JSON.stringify({stored: $json.stored_result, received: $json.result, received_duration: $json.duration_seconds}) }}',
        on_error='continueRegularOutput', retry=2)
    conflict_log = W.code('[LOG] Result Conflict', CORE + r'''
// Dos fuentes dicen resultados distintos para la MISMA llamada. No se elige
// una: se conserva la primera (el hecho ya escrito) y se abre un issue.
// Crear un segundo follow-up acá duplicaría el seguimiento del lead.
const d = $json;
lmLog('ENGINE', d, { call_job_id: d.call_job_id, lead_id: d.lead_id,
                     result: d.result, error_code: 'RESULT_CONFLICT', action: 'ISSUE' });
return [{ json: { ok: false, skipped: true, reason: 'RESULT_CONFLICT',
                  call_job_id: d.call_job_id, stored_result: d.stored_result,
                  received_result: d.result } }];
''', 4, branch=3, offset=1)
    W.link(sw, conflict, 2)
    W.chain(conflict, conflict_log)

    notfound_log = W.code('[LOG] Record Not Applicable', CORE + r'''
// Fallback del Switch: el job existe pero no estaba en un estado que admita
// resultado (p. ej. RELEASED o FAILED). No se inventa un follow-up.
const d = $json;
lmLog('ENGINE', d, { call_job_id: d.call_job_id, lead_id: d.lead_id,
                     result: d.result, error_code: 'RESULT_NOT_APPLICABLE', action: 'SKIP' });
return [{ json: { ok: false, skipped: true, reason: 'RESULT_NOT_APPLICABLE',
                  call_job_id: d.call_job_id, record_status: d.record_status } }];
''', 4, branch=4)
    W.link(sw, notfound_log, 3)

    # ── 05 IDEMPOTENCY CLAIM ──────────────────────────────────────────
    W.sticky(5, '05 — IDEMPOTENCY CLAIM', """`wf_conversation_ledger`, clave
`conversation_id`; si no hay conversación (resultado inmediato SIP), la clave es
`call_job_id`.

Se gana por **relectura del token propio** tras un `INSERT IGNORE`, nunca por
`affectedRows`.

**Perder el claim no es un error**: es el camino normal cuando el webhook y el
polling llegan juntos. Devuelve `{skipped: true, reason: ALREADY_CLAIMED}`.

Nunca `$getWorkflowStaticData`: no sobrevive un restart ni se comparte entre
workers.""", color=3, height=420)
    claim_prep = W.code('[ENGINE] Build Claim Token', r'''
// Token propio de ESTA ejecución. Se compara tras el INSERT IGNORE para saber
// si ganamos el claim (patrón del estándar §6.1).
const crypto = require('crypto');
return $input.all().map(it => ({ json: Object.assign({}, it.json, {
  claim_token: crypto.randomBytes(16).toString('hex')
})}));
''', 5)
    W.chain(ev_result, claim_prep)

    claim = W.mysql('[DB] Claim Idempotency', (
        "INSERT IGNORE INTO wf_conversation_ledger\n"
        "  (conversation_id, call_job_id, route_key, lead_id, provider, attempt,\n"
        "   source, claim_token, state, execution_id)\n"
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'CLAIMED', ?)"),
        5, offset=1,
        replacements='={{ $json.idempotency_key }},={{ $json.call_job_id }},'
                     '={{ $json.route_key }},={{ $json.lead_id }},={{ $json.provider }},'
                     '={{ $json.job_attempt }},={{ $json.source || "dispatch" }},'
                     '={{ $json.claim_token }},={{ $json.execution_id }}',
        on_error='continueRegularOutput', retry=2)
    claim_read = W.mysql('[DB] Read Claim Owner', (
        "SELECT claim_token, state, followup_id FROM wf_conversation_ledger\n"
        " WHERE conversation_id = ?"),
        5, offset=2,
        replacements='={{ $(\'[ENGINE] Build Claim Token\').item.json.idempotency_key }}',
        always_output=True, on_error='continueRegularOutput', retry=2)
    W.chain(claim, claim_read)
    W.chain(claim_prep, claim)

    claim_check = W.code('[ENGINE] Check Claim Owner', CORE + r'''
const rows = $input.all();
const ctxAll = $('[ENGINE] Build Claim Token').all();
return rows.map((r, i) => {
  const ctx = ctxAll[Math.min(i, ctxAll.length - 1)].json;
  const row = r.json || {};
  return { json: Object.assign({}, ctx, {
    claim_won: !!row.claim_token && row.claim_token === ctx.claim_token,
    ledger_state: row.state || null,
    ledger_followup_id: row.followup_id || null
  })};
});
''', 5, offset=3)
    claim_if = W.if_('[ENGINE] Claim Won', '={{ $json.claim_won }}', 'true', True, 5,
                     offset=4, single=True)
    W.chain(claim_read, claim_check, claim_if)

    lost = W.code('[LOG] Claim Already Taken', CORE + r'''
// Otro proceso ya está creando el follow-up de esta conversación. Terminar
// SIN error es lo correcto (FOLLOWUP_ENGINE_CONTRACT · output_when_lost_claim).
const d = $json;
lmLog('ENGINE', d, { call_job_id: d.call_job_id, conversation_id: d.conversation_id,
                     lead_id: d.lead_id, action: 'ALREADY_CLAIMED' });
return [{ json: { ok: true, skipped: true, reason: 'ALREADY_CLAIMED',
                  conversation_id: d.conversation_id || null,
                  call_job_id: d.call_job_id,
                  followup_id: d.ledger_followup_id } }];
''', 5, branch=2, offset=4)
    W.link(claim_if, lost, 1)

    # ── 06 RESOLVE ROUTE ──────────────────────────────────────────────
    W.sticky(6, '06 — RESOLVE ROUTE', """`GET /api/routes/by-key/{route_key}` —
resuelve la ruta **aunque esté apagada o archivada**. Un post-call llega minutos
u horas después: para entonces la ruta pudo apagarse, y la política que aplica
sigue siendo la de esa ruta.

Sin ruta ⇒ `CONFIG_ERROR`. No se adivina una política por defecto: un lead
seguido con la política equivocada es peor que un lead sin seguimiento.

Red interna o HTTPS (§7.2). El token va en la credential `Landmark Panel API`.""",
              color=3, height=400)
    route = W.http('[CONFIG] Resolve Route By Key',
                   'GET', '={{ $env.LM_PANEL_URL || "http://172.18.0.1:8080" }}'
                          '/api/routes/by-key/{{ $json.route_key }}', 6,
                   credentials={'httpHeaderAuth': {'id': '__PANEL_TOKEN_CREDENTIAL__',
                                                   'name': 'Landmark Panel API'}},
                   never_error=True, full_response=True, timeout=10000, retry=2,
                   on_error='continueRegularOutput')
    W.link(claim_if, route, 0)

    route_check = W.code('[CONFIG] Check Route Response', CORE + r'''
const rows = $input.all();
const ctxAll = $('[ENGINE] Check Claim Owner').all();
return rows.map((r, i) => {
  const ctx = ctxAll[Math.min(i, ctxAll.length - 1)].json;
  const resp = r.json || {};
  const code = resp.statusCode || 0;
  const body = resp.body || {};
  const ok = code >= 200 && code < 300 && body && body.route_key;
  return { json: Object.assign({}, ctx, {
    route_ok: !!ok,
    route: ok ? body : null,
    route_http_status: code,
    policy: ok ? (body.followup_policy || null) : null,
    timezone: ok ? (body.timezone || 'UTC') : 'UTC',
    route_error: ok ? null : ('no se pudo resolver la ruta ' + ctx.route_key +
                              ' (HTTP ' + code + ')')
  })};
});
''', 6, offset=1)
    route_if = W.if_('[CONFIG] Route Resolved', '={{ $json.route_ok }}', 'true', True, 6,
                     offset=2, single=True)
    W.chain(route, route_check, route_if)

    route_err = W.mysql('[DB] Fail Ledger Config Error', (
        "UPDATE wf_conversation_ledger\n"
        "   SET state = 'FAILED', error_code = 'CONFIG_ERROR', error_detail = ?\n"
        " WHERE conversation_id = ? AND state = 'CLAIMED'"),
        6, branch=2, offset=2,
        replacements='={{ $json.route_error }},={{ $json.idempotency_key }}',
        on_error='continueRegularOutput', retry=2)
    route_err_log = W.code('[ERROR] Config Error Route', CORE + r'''
// CONFIG_ERROR: se libera el claim marcándolo FAILED (no PROCESSED) para que el
// operador lo vea, y NO se crea follow-up. El estándar §9 dice que un
// CONFIG_ERROR aborta la rama; acá la rama es una sola llamada.
const d = $json;
lmLog('ENGINE', d, { call_job_id: d.call_job_id, route_key: d.route_key,
                     lead_id: d.lead_id, error_code: 'CONFIG_ERROR', action: 'ABORT' });
return [{ json: { ok: false, error: true, error_code: 'CONFIG_ERROR',
                  error_detail: d.route_error, call_job_id: d.call_job_id } }];
''', 6, branch=2, offset=3)
    W.link(route_if, route_err, 1)
    W.chain(route_err, route_err_log)

    # ── 07 DECIDE ─────────────────────────────────────────────────────
    W.sticky(7, '07 — RESOLVE POLICY & SCHEDULE', """Reglas explícitas
`(result, attempt) -> action`, con `"*"` de comodín y `unmatched_action` de red
de seguridad. **Sin módulo, sin `% 3`** (v1 calculaba `posInCycle` y `cycle` con
aritmética modular, que es lo que hacía imposible cambiar la política sin tocar
código).

Reclasificación SIP: que 603 signifique "no contestó" lo decide
`policy.no_answer_sip_codes`, **no el adapter**.

`schedule_next_at` se calcula en el **huso del país**: `+Nd` y `+Nbd` conservan
la hora de reloj local y respetan DST.

Este nodo es un port 1:1 de `app/followup_engine.py`; la paridad se verifica en
`tests/test_engine_parity_v2.py` (6.930 casos).""", color=5, height=460)
    decide = W.code('[ENGINE] Resolve Policy And Schedule', CORE + ENG + NOTES + r'''
const out = [];
for (const it of $input.all()) {
  const d = it.json;
  const now = new Date();
  try {
    const decision = lmResolve(d.policy, Number(d.job_attempt), d.result, now,
                               d.timezone || 'UTC', d.callback_at, d.job_sip_code);
    // La nota que va al CRM SIEMPRE en inglés (CRM_ENGLISH_RULE).
    const note = lmCallNote({
      result: decision.result, attempt: d.job_attempt, route_key: d.route_key,
      provider: d.provider, sip_code: decision.sip_code,
      duration_seconds: d.job_duration_seconds, action: decision.action,
      schedule_next_at: decision.schedule_next_at,
      // El resumen SOLO entra si la fuente garantiza que es ingles.
      // lmCallNote lo descarta si no; el original queda como evidencia
      // local en wf_events.metadata_json. Ver CRM_ENGLISH_RULE.
      summary: d.summary || null,
      summary_language: d.summary_language || null,
      summary_is_english: !!d.summary_is_english
    });
    const closeReason = decision.action === 'CLOSE'
      ? (decision.effective_result === 'WRONG_NUMBER' ? 'WRONG_NUMBER'
        : decision.effective_result === 'DNC' ? 'DNC' : 'MAX_ATTEMPTS')
      : null;
    out.push({ json: Object.assign({}, d, {
      decision: decision,
      crm_notes: note,
      crm_outcome: ({ ANSWERED: 'CONNECTED', CALLBACK: 'CALLBACK',
                      WRONG_NUMBER: 'WRONG_NUMBER', DNC: 'WRONG_NUMBER' })[decision.result]
                   || 'NO_ANSWER',
      crm_call_status: ({ ANSWERED: 'ANSWERED', CALLBACK: 'ANSWERED',
                          NO_ANSWER: 'NO_ANSWER', BUSY: 'NO_ANSWER',
                          VOICEMAIL: 'NO_ANSWER' })[decision.result] || 'FAILED',
      close_reason: closeReason,
      policy_ok: true, policy_error: null
    })});
  } catch (e) {
    // Una política inválida es CONFIG_ERROR, no un follow-up inventado.
    out.push({ json: Object.assign({}, d, {
      policy_ok: false, policy_error: e.message, decision: null
    })});
  }
}
return out;
''', 7)
    W.link(route_if, decide, 0)
    pol_if = W.if_('[ENGINE] Policy Applied', '={{ $json.policy_ok }}', 'true', True, 7,
                   offset=1, single=True)
    W.chain(decide, pol_if)
    pol_err = W.code('[ERROR] Policy Error', CORE + r'''
const d = $json;
lmLog('ENGINE', d, { call_job_id: d.call_job_id, route_key: d.route_key,
                     error_code: 'CONFIG_ERROR', action: 'ABORT' });
return [{ json: { ok: false, error: true, error_code: 'CONFIG_ERROR',
                  error_detail: 'policy invalida: ' + d.policy_error,
                  call_job_id: d.call_job_id } }];
''', 7, branch=2, offset=1)
    W.link(pol_if, pol_err, 1)

    # ── 08 CRM ────────────────────────────────────────────────────────
    W.sticky(8, '08 — CRM: CREATE FOLLOWUP', """El motor es **el único** que hace
`POST /leads/{id}/followups` en todo el suite.

`notes` va en **inglés** (CRM_ENGLISH_RULE), sin excepciones. El resumen del
agente **sólo** se adjunta si la fuente garantiza que viene en inglés
(`summary_language='en'`). Si no, NO entra: el original se guarda como
evidencia local en `wf_events.metadata_json` y la nota lleva su texto canónico.
No se traduce por nuestra cuenta — inventar una traducción dentro de n8n sin un
servicio configurado sería peor que omitirla.

Si el POST se envió y la respuesta no llega (timeout, 5xx), el follow-up
**pudo haberse creado**: se marca `NEEDS_RECONCILIATION` y **no se reintenta**.
Reintentar a ciegas duplica el seguimiento del lead.

El login usa la credential `LeadStudio Login` (Custom Auth). Si el llamador ya
pasó `access_token`, no se vuelve a autenticar.""", color=2, height=440)
    tok_if = W.if_('[CRM] Token Already Provided',
                   '={{ !!$json.access_token }}', 'true', True, 8, single=True)
    W.link(pol_if, tok_if, 0)

    login = W.http('[CRM] Get Token', 'POST',
                   '={{ $env.LM_LEADSTUDIO_URL || "https://lead-studio-9gnl.onrender.com" }}'
                   '/api/auth/login', 8, branch=1, offset=1,
                   headers={'Content-Type': 'application/json'},
                   credentials={'httpCustomAuth': {'id': '__LEADSTUDIO_LOGIN_CREDENTIAL__',
                                                   'name': 'LeadStudio Login'}},
                   never_error=True, full_response=True, timeout=15000, retry=2,
                   on_error='continueRegularOutput')
    W.link(tok_if, login, 1)

    tok_merge = W.code('[CRM] Resolve Access Token', CORE + r'''
// Un solo login por ejecución. El token nunca se loguea.
const ctxAll = $('[ENGINE] Policy Applied').all();
const rows = $input.all();
let token = null;
try {
  const r = rows[0] && rows[0].json ? rows[0].json : {};
  token = (r.body && r.body.accessToken) || r.accessToken || null;
} catch (e) { token = null; }
return ctxAll.map(it => {
  const d = it.json;
  const t = d.access_token || token;
  return { json: Object.assign({}, d, { access_token: t, token_ok: !!t }) };
});
''', 8, branch=1, offset=2)
    W.link(login, tok_merge)

    tok_pass = W.code('[CRM] Use Provided Token', r'''
// El llamador (WF9) ya hizo login en este ciclo: se reusa su token.
return $input.all().map(it => ({ json: Object.assign({}, it.json, { token_ok: true }) }));
''', 8, offset=2)
    W.link(tok_if, tok_pass, 0)

    tok_check = W.if_('[CRM] Token Available', '={{ $json.token_ok }}', 'true', True,
                      8, offset=3, single=True)
    W.link(tok_pass, tok_check)
    W.link(tok_merge, tok_check)

    auth_err = W.mysql('[DB] Fail Ledger Auth Error', (
        "UPDATE wf_conversation_ledger\n"
        "   SET state = 'FAILED', error_code = 'AUTH_ERROR',\n"
        "       error_detail = 'LeadStudio login failed'\n"
        " WHERE conversation_id = ? AND state = 'CLAIMED'"),
        8, branch=3, offset=3, replacements='={{ $json.idempotency_key }}',
        on_error='continueRegularOutput', retry=2)
    auth_log = W.code('[ERROR] Auth Error', CORE + r'''
// AUTH_ERROR: no se pudo autenticar contra LeadStudio. NO se crea follow-up y
// el claim queda FAILED para que el operador lo vea. El resultado de la llamada
// YA está guardado localmente (sección 03): no se pierde nada.
const d = $json;
lmLog('ENGINE', d, { call_job_id: d.call_job_id, route_key: d.route_key,
                     error_code: 'AUTH_ERROR', action: 'ABORT' });
return [{ json: { ok: false, error: true, error_code: 'AUTH_ERROR',
                  call_job_id: d.call_job_id } }];
''', 8, branch=3, offset=4)
    W.link(tok_check, auth_err, 1)
    W.chain(auth_err, auth_log)

    crm = W.http('[CRM] Create Followup', 'POST',
                 '={{ $env.LM_LEADSTUDIO_URL || "https://lead-studio-9gnl.onrender.com" }}'
                 '/api/leads/{{ $json.lead_id }}/followups', 9,
                 headers={'Content-Type': 'application/json',
                          'Authorization': '=Bearer {{ $json.access_token }}'},
                 body_json='={{ JSON.stringify($json.crm_body) }}',
                 never_error=True, full_response=True, timeout=20000,
                 on_error='continueRegularOutput')
    body_build = W.code('[CRM] Build Followup Body', NOTES + r'''
// Cuerpo EXACTO del contrato de LeadStudio. Los enums técnicos (type, outcome,
// callStatus) son los de la API; el texto humano (`notes`) va en inglés.
// PENDING_VERIFICATION PV-11: el enum de outcome/callStatus no está confirmado
// por documentación; estos valores salen de WF9 v1, que funciona en producción.
return $input.all().map(it => {
  const d = it.json;
  const body = {
    type: 'CALL',
    outcome: d.crm_outcome,
    callStatus: d.crm_call_status,
    durationSeconds: Number(d.job_duration_seconds || 0),
    notes: String(d.crm_notes || '').slice(0, 900)
  };
  if (d.decision && d.decision.schedule_next_at) {
    body.scheduleNextAt = d.decision.schedule_next_at;
  }
  return { json: Object.assign({}, d, { crm_body: body }) };
});
''', 8, offset=4)
    W.link(tok_check, body_build, 0)
    W.chain(body_build, crm)

    crm_check = W.code('[CRM] Check Followup Response', CORE + r'''
// neverError=true SIEMPRE va seguido de un check explícito (§9). En v1 un 404 y
// un 200 se procesaban igual porque faltaba este nodo.
const rows = $input.all();
const ctxAll = $('[CRM] Build Followup Body').all();
return rows.map((r, i) => {
  const ctx = ctxAll[Math.min(i, ctxAll.length - 1)].json;
  const resp = r.json || {};
  const code = Number(resp.statusCode || 0);
  const body = resp.body || {};
  const fid = (body.followUp && body.followUp.id) || body.id ||
              (body.data && body.data.id) || null;
  const ok = code >= 200 && code < 300 && !!fid;

  // AMBIGUO: el POST salió y no sabemos si se ejecutó. NUNCA se reintenta.
  const ambiguous = !ok && (code === 0 || code >= 500 || code === 408 || code === 429);

  return { json: Object.assign({}, ctx, {
    crm_ok: ok, followup_id: fid, crm_http_status: code,
    crm_ambiguous: ambiguous,
    crm_error: ok ? null : ('HTTP ' + code + ' ' + JSON.stringify(body).slice(0, 180))
  })};
});
''', 9, offset=1)
    crm_if = W.if_('[CRM] Followup Created', '={{ $json.crm_ok }}', 'true', True, 9,
                   offset=2, single=True)
    W.chain(crm, crm_check, crm_if)

    amb_if = W.if_('[ENGINE] CRM Failure Is Ambiguous', '={{ $json.crm_ambiguous }}',
                   'true', True, 9, branch=2, offset=2, single=True)
    W.link(crm_if, amb_if, 1)

    amb_ledger = W.mysql('[DB] Ledger Needs Reconciliation', (
        "UPDATE wf_conversation_ledger\n"
        "   SET state = 'NEEDS_RECONCILIATION', error_code = 'CRM_ERROR', error_detail = ?\n"
        " WHERE conversation_id = ? AND state = 'CLAIMED'"),
        9, branch=2, offset=3,
        replacements='={{ $json.crm_error }},={{ $json.idempotency_key }}',
        on_error='continueRegularOutput', retry=2)
    amb_issue = W.mysql('[DB] Open Followup Reconciliation Issue', (
        "INSERT INTO wf_reconciliation_issues\n"
        "  (issue_key, issue_type, entity_type, entity_id, lead_id, country_iso,\n"
        "   severity, state, detail_json, first_seen_at, last_seen_at)\n"
        "VALUES (?, 'FOLLOWUP_NEEDS_RECONCILIATION', 'call_job', ?, ?, ?, 'ERROR',\n"
        "        'OPEN', ?, UTC_TIMESTAMP(), UTC_TIMESTAMP())\n"
        "ON DUPLICATE KEY UPDATE occurrences = occurrences + 1,\n"
        "        last_seen_at = UTC_TIMESTAMP(), detail_json = VALUES(detail_json)"),
        9, branch=2, offset=4,
        replacements='={{ "FOLLOWUP_NEEDS_RECONCILIATION:call_job:" + $json.call_job_id }},'
                     '={{ $json.call_job_id }},={{ $json.lead_id }},={{ $json.country_iso }},'
                     '={{ JSON.stringify({http: $json.crm_http_status, error: $json.crm_error, action: $json.decision ? $json.decision.action : null}) }}',
        on_error='continueRegularOutput', retry=2)
    amb_log = W.code('[LOG] Needs Reconciliation', CORE + r'''
// El POST /followups pudo haberse ejecutado. Se marca para revisión y NO se
// reintenta: un reintento a ciegas crearía un segundo follow-up y el lead
// quedaría con dos seguimientos activos.
const d = $json;
lmLog('ENGINE', d, { call_job_id: d.call_job_id, lead_id: d.lead_id,
                     route_key: d.route_key, error_code: 'CRM_ERROR',
                     action: 'NEEDS_RECONCILIATION' });
return [{ json: { ok: false, needs_reconciliation: true, error_code: 'CRM_ERROR',
                  call_job_id: d.call_job_id, error_detail: d.crm_error } }];
''', 9, branch=2, offset=5)
    W.link(amb_if, amb_ledger, 0)
    W.chain(amb_ledger, amb_issue, amb_log)

    def_ledger = W.mysql('[DB] Fail Ledger CRM Error', (
        "UPDATE wf_conversation_ledger\n"
        "   SET state = 'FAILED', error_code = 'CRM_ERROR', error_detail = ?\n"
        " WHERE conversation_id = ? AND state = 'CLAIMED'"),
        9, branch=4, offset=3,
        replacements='={{ $json.crm_error }},={{ $json.idempotency_key }}',
        on_error='continueRegularOutput', retry=2)
    def_log = W.code('[ERROR] CRM Rejected Followup', CORE + r'''
// Rechazo DEFINITIVO (4xx de negocio): el follow-up no se creó y no se va a
// crear reintentando. El resultado de la llamada ya está guardado localmente.
const d = $json;
lmLog('ENGINE', d, { call_job_id: d.call_job_id, lead_id: d.lead_id,
                     error_code: 'CRM_ERROR', action: 'REJECTED' });
return [{ json: { ok: false, error: true, error_code: 'CRM_ERROR',
                  error_detail: d.crm_error, call_job_id: d.call_job_id } }];
''', 9, branch=4, offset=4)
    W.link(amb_if, def_ledger, 1)
    W.chain(def_ledger, def_log)

    # ── 10 PERSIST ────────────────────────────────────────────────────
    W.sticky(10, '10 — PERSIST', """Se cierra el claim con el `followup_id`, se
ata al job y se escriben los eventos de negocio.

`event_key` **determinista** (`FOLLOWUP_CREATED:{call_job_id}`): el mismo hecho
escrito dos veces choca con el UNIQUE y no se cuenta dos veces. Nunca un UUID
aleatorio — eso anularía la idempotencia.

`LEAD_CLOSED` lleva el motivo en `result`: MAX_ATTEMPTS · WRONG_NUMBER · DNC.""",
              color=3, height=360)
    persist = W.mysql('[DB] Complete Ledger', (
        "UPDATE wf_conversation_ledger\n"
        "   SET state = 'PROCESSED', followup_id = ?, result = ?,\n"
        "       completed_at = UTC_TIMESTAMP()\n"
        " WHERE conversation_id = ? AND state = 'CLAIMED'"),
        10, replacements='={{ $json.followup_id }},={{ $json.result }},'
                         '={{ $json.idempotency_key }}',
        on_error='continueRegularOutput', retry=2)
    W.link(crm_if, persist, 0)

    attach = W.mysql('[DB] Attach Followup To Job', (
        "UPDATE wf_call_jobs SET followup_id = ?, updated_at = UTC_TIMESTAMP()\n"
        " WHERE call_job_id = ? AND followup_id IS NULL"),
        10, offset=1,
        replacements='={{ $(\'[CRM] Check Followup Response\').item.json.followup_id }},'
                     '={{ $(\'[CRM] Check Followup Response\').item.json.call_job_id }}',
        on_error='continueRegularOutput', retry=2)
    W.chain(persist, attach)

    ev_fu = W.mysql('[DB] Record Event Followup Created', (
        "INSERT IGNORE INTO wf_events\n"
        "  (event_key, event_type, event_domain, occurred_at, call_job_id, lead_id,\n"
        "   country_iso, route_key, provider, adapter_key, attempt, conversation_id,\n"
        "   followup_id, result, source_workflow, execution_id, metadata_json)\n"
        "VALUES (?, 'FOLLOWUP_CREATED', 'FOLLOWUP', UTC_TIMESTAMP(), ?, ?, ?, ?, ?, ?, ?,\n"
        "        ?, ?, ?, 'ENGINE', ?, ?)"),
        10, offset=2,
        replacements='={{ "FOLLOWUP_CREATED:" + $(\'[CRM] Check Followup Response\').item.json.call_job_id }},'
                     '={{ $(\'[CRM] Check Followup Response\').item.json.call_job_id }},'
                     '={{ $(\'[CRM] Check Followup Response\').item.json.lead_id }},'
                     '={{ $(\'[CRM] Check Followup Response\').item.json.country_iso }},'
                     '={{ $(\'[CRM] Check Followup Response\').item.json.route_key }},'
                     '={{ $(\'[CRM] Check Followup Response\').item.json.provider }},'
                     '={{ $(\'[CRM] Check Followup Response\').item.json.adapter_key || null }},'
                     '={{ $(\'[CRM] Check Followup Response\').item.json.job_attempt }},'
                     '={{ $(\'[CRM] Check Followup Response\').item.json.job_conversation_id }},'
                     '={{ $(\'[CRM] Check Followup Response\').item.json.followup_id }},'
                     '={{ $(\'[CRM] Check Followup Response\').item.json.decision.action }},'
                     '={{ $(\'[CRM] Check Followup Response\').item.json.execution_id }},'
                     '={{ JSON.stringify($(\'[CRM] Check Followup Response\').item.json.decision) }}',
        on_error='continueRegularOutput', retry=2)
    W.chain(attach, ev_fu)

    post = W.code('[ENGINE] Decide Post Events', r'''
// Qué eventos adicionales corresponden según la acción decidida.
return $input.all().map((it, i) => {
  const d = $('[CRM] Check Followup Response').all()[i].json;
  const a = d.decision ? d.decision.action : 'NONE';
  return { json: Object.assign({}, d, {
    needs_callback_event: a === 'CALLBACK',
    needs_closed_event: a === 'CLOSE',
    needs_crm_patch: !!(d.decision && d.decision.crm_status_hint)
  })};
});
''', 10, offset=3)
    W.chain(ev_fu, post)

    cb_if = W.if_('[ENGINE] Callback Scheduled', '={{ $json.needs_callback_event }}',
                  'true', True, 11, single=True)
    W.chain(post, cb_if)
    ev_cb = W.mysql('[DB] Record Event Callback Scheduled', (
        "INSERT IGNORE INTO wf_events\n"
        "  (event_key, event_type, event_domain, occurred_at, call_job_id, lead_id,\n"
        "   country_iso, route_key, provider, attempt, followup_id, result,\n"
        "   source_workflow, execution_id)\n"
        "VALUES (?, 'CALLBACK_SCHEDULED', 'FOLLOWUP', UTC_TIMESTAMP(), ?, ?, ?, ?, ?, ?,\n"
        "        ?, ?, 'ENGINE', ?)"),
        11, offset=1,
        replacements='={{ "CALLBACK_SCHEDULED:" + $json.call_job_id }},={{ $json.call_job_id }},'
                     '={{ $json.lead_id }},={{ $json.country_iso }},={{ $json.route_key }},'
                     '={{ $json.provider }},={{ $json.job_attempt }},={{ $json.followup_id }},'
                     '={{ $json.decision.schedule_next_at }},={{ $json.execution_id }}',
        on_error='continueRegularOutput', retry=2)
    W.link(cb_if, ev_cb, 0)

    close_if = W.if_('[ENGINE] Lead Closed', '={{ $json.needs_closed_event }}',
                     'true', True, 12, single=True)
    W.link(cb_if, close_if, 1)
    W.link(ev_cb, close_if)
    ev_close = W.mysql('[DB] Record Event Lead Closed', (
        "INSERT IGNORE INTO wf_events\n"
        "  (event_key, event_type, event_domain, occurred_at, call_job_id, lead_id,\n"
        "   country_iso, route_key, provider, attempt, followup_id, result,\n"
        "   source_workflow, execution_id)\n"
        "VALUES (?, 'LEAD_CLOSED', 'FOLLOWUP', UTC_TIMESTAMP(), ?, ?, ?, ?, ?, ?, ?, ?,\n"
        "        'ENGINE', ?)"),
        12, offset=1,
        replacements='={{ "LEAD_CLOSED:" + $json.call_job_id }},={{ $json.call_job_id }},'
                     '={{ $json.lead_id }},={{ $json.country_iso }},={{ $json.route_key }},'
                     '={{ $json.provider }},={{ $json.job_attempt }},={{ $json.followup_id }},'
                     '={{ $json.close_reason }},={{ $json.execution_id }}',
        on_error='continueRegularOutput', retry=2)
    W.link(close_if, ev_close, 0)

    # ── 12 CRM PATCH ──────────────────────────────────────────────────
    W.sticky(13, '13 — CRM PATCH (OPCIONAL)', """`PATCH /api/leads/{id}` con el
`crm_status_hint` de la decisión.

**PENDING_VERIFICATION PV-1**: no está confirmado si LeadStudio mueve `status` y
`attempts` solo al recibir el follow-up. Si lo hace, este paso sobra y se apaga
poniendo `crm_patch_enabled=false` en el nodo `[CRM] Should Patch Lead` — sin
tocar el resto del motor.

El fallo de este PATCH **no** invalida el follow-up: ya se creó y ya se
registró localmente.""", color=6, height=360)
    patch_if = W.if_('[CRM] Should Patch Lead',
                     '={{ $json.needs_crm_patch && ($json.crm_patch_enabled !== false) }}',
                     'true', True, 13, single=True)
    W.link(close_if, patch_if, 1)
    W.link(ev_close, patch_if)

    patch = W.http('[CRM] Patch Lead Status', 'PATCH',
                   '={{ $env.LM_LEADSTUDIO_URL || "https://lead-studio-9gnl.onrender.com" }}'
                   '/api/leads/{{ $json.lead_id }}', 13, offset=1,
                   headers={'Content-Type': 'application/json',
                            'Authorization': '=Bearer {{ $json.access_token }}'},
                   body_json='={{ JSON.stringify({ status: $json.decision.crm_status_hint }) }}',
                   never_error=True, full_response=True, timeout=15000,
                   on_error='continueRegularOutput')
    W.link(patch_if, patch, 0)

    # ── 13 OUTPUT ─────────────────────────────────────────────────────
    W.sticky(14, '14 — OUTPUT', """Salida normalizada del
`FOLLOWUP_ENGINE_CONTRACT`. Es lo que ven WF2 y WF9.

Una línea de log por decisión, con todos los campos de correlación del §10 y
**sin** teléfono completo ni tokens.""", color=4, height=300)
    out = W.code('[LOG] Emit Decision', CORE + r'''
// Salida del contrato. Se emite igual si el PATCH opcional falló: el follow-up
// ya existe y el hecho local ya está escrito.
const ctxAll = $('[ENGINE] Decide Post Events').all();
return ctxAll.map(it => {
  const d = it.json;
  lmLog('ENGINE', d, {
    call_job_id: d.call_job_id, route_key: d.route_key, country: d.country_iso,
    provider: d.provider, adapter_key: d.adapter_key, lead_id: d.lead_id,
    attempt: d.job_attempt, conversation_id: d.job_conversation_id,
    followup_id: d.followup_id, result: d.result,
    action: d.decision ? d.decision.action : null
  });
  return { json: {
    ok: true,
    call_job_id: d.call_job_id,
    conversation_id: d.job_conversation_id || null,
    lead_id: d.lead_id,
    route_key: d.route_key,
    attempt: d.job_attempt,
    decision: d.decision,
    followup_id: d.followup_id,
    idempotency: { key: d.idempotency_kind, claimed: true, ledger_state: 'PROCESSED' },
    error: null
  }};
});
''', 14)
    W.link(patch_if, out, 1)
    W.link(patch, out)
    return W
