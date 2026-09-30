"""TEMPLATE_WF14_RECONCILIATION_ANALYTICS_V2 — reconciliación y sincronización.

Reemplaza a "WF14 - CRM Sync (LeadStudio API -> MySQL) + Stringee Call Log".

CAMBIO DE ROL (el más importante del suite):
  v1 era la fuente de analytics: el panel dependía de que WF14 hubiera corrido.
  V2 **no** alimenta el dashboard. Los KPIs salen de `wf_call_jobs` y
  `wf_events`, que los workflows escriben en el momento en que las cosas pasan.

  WF14 ahora: (a) reconcilia estados locales que quedaron a medias,
  (b) compara lo local contra el CRM y abre issues, (c) conserva la
  sincronización legacy `crm_leads` / `stringee_calls` que otras pantallas usan.

  **Nunca borra ni corrige datos locales.** Una diferencia abre un issue.
"""

VERSION = '2.0.0'
NAME = 'TEMPLATE_WF14_RECONCILIATION_ANALYTICS_V2'


def build(wfbuild, js):
    W = wfbuild.Workflow(
        NAME, VERSION,
        'ANALYTICS_EVENT v2.2 · TEMPLATE_DATA v2.2',
        'WF14 v1 — desactivar este workflow; v1 vuelve a sincronizar (§12)',
        'Schedule cada 5 min.')
    CORE, NOTES = js['lmcore.js'], js['lmnotes.js']
    PANEL = '={{ $env.LM_PANEL_URL || "http://172.18.0.1:8080" }}'
    CRM = '={{ $env.LM_LEADSTUDIO_URL || "https://lead-studio-9gnl.onrender.com" }}'

    # ══ 00 TRIGGER ════════════════════════════════════════════════════
    W.sticky(0, '00 — TRIGGER', f"""{W.meta_header}

Cada 5 minutos. **El dashboard no espera a este workflow**: si WF14 no corre en
una hora, los KPIs del panel siguen siendo correctos porque salen de
`wf_call_jobs` y `wf_events`. Lo que se acumula es el trabajo de
reconciliación, que es exactamente lo que debe acumularse.""",
              color=4, height=360)
    trig = W.schedule('[TRIGGER] Every Five Minutes', 5, 0)

    setts = W.http('[CONFIG] Load Settings', 'GET', PANEL + '/api/settings', 1,
                   credentials={'httpHeaderAuth': {'id': '__PANEL_TOKEN_CREDENTIAL__',
                                                   'name': 'Landmark Panel API'}},
                   never_error=True, full_response=True, timeout=10000, retry=2,
                   on_error='continueRegularOutput')
    W.chain(trig, setts)

    W.sticky(1, '01 — LOAD CONFIG', """Los umbrales de reconciliación son
**configuración** (`wf_settings`), no literales: cuántos minutos hace falta que
un `DISPATCHING` lleve parado para considerarlo perdido depende de la latencia
real de cada proveedor.

Si `/api/settings` no responde, se usan los defaults documentados (los mismos
que siembra la migración) y se deja constancia en el log.""",
              color=3, height=360)
    cfg = W.code('[CONFIG] Resolve Thresholds', CORE + r'''
const resp = $input.first().json || {};
const code = Number(resp.statusCode || 0);
const s = ((resp.body || {}).settings) || {};
const defaults = { reconcile_dispatching_minutes: 5, reconcile_unknown_minutes: 5,
                   reconcile_dispatched_minutes: 60, reconcile_claimed_minutes: 10,
                   reconcile_ledger_minutes: 15, wf14_leadstudio_page_size: 200 };
if (code < 200 || code >= 300) {
  console.log('[WF14] WARN /api/settings HTTP ' + code + ': usando defaults documentados');
}
const cfg = Object.assign({}, defaults, s);
return [{ json: Object.assign({}, cfg, { execution_id: $execution.id,
                                         settings_from_api: code >= 200 && code < 300 }) }];
''', 1, offset=1)
    W.chain(setts, cfg)

    # ══ 02 RECONCILE LOCAL STATE ══════════════════════════════════════
    W.sticky(2, '02 — RECONCILE CALL JOBS', """Reclasifica lo que quedó a medias.
**Nunca re-despacha.**

| estado viejo | qué pasó | qué se hace |
|---|---|---|
| `CLAIMED` sin despachar | no salió llamada | → `RELEASED` (se re-reclama tras el backoff) |
| `DISPATCHING` parado | **pudo** salir | → `NEEDS_RECONCILIATION` |
| `UNKNOWN` | pudo salir | → `NEEDS_RECONCILIATION` |
| `DISPATCHED` sin post-call | salió, no llegó el resultado | → `NEEDS_RECONCILIATION` |

La diferencia entre `UNKNOWN` y `NEEDS_RECONCILIATION` importa: `UNKNOWN` es lo
que acaba de reportar el adapter; `NEEDS_RECONCILIATION` es lo que ya nadie va a
resolver solo. **Ninguno de los dos se vuelve a llamar.**""",
              color=2, height=460)
    r1 = W.mysql('[DB] Reconcile Stale Claimed', (
        "UPDATE wf_call_jobs\n"
        "   SET state = 'RELEASED', error_class = 'TECHNICAL', error_code = 'STALE_CLAIM',\n"
        "       tech_retry_count = tech_retry_count + 1,\n"
        "       next_tech_retry_at = DATE_ADD(UTC_TIMESTAMP(), INTERVAL\n"
        "         LEAST(POW(2, tech_retry_count), 60) MINUTE),\n"
        "       updated_at = UTC_TIMESTAMP()\n"
        " WHERE state = 'CLAIMED'\n"
        "   AND claimed_at < DATE_SUB(UTC_TIMESTAMP(), INTERVAL ? MINUTE)"),
        2, replacements='={{ $json.reconcile_claimed_minutes }}',
        retry=2, on_error='continueRegularOutput')
    W.chain(cfg, r1)
    r2 = W.mysql('[DB] Reconcile Stale Dispatching', (
        "UPDATE wf_call_jobs\n"
        "   SET state = 'NEEDS_RECONCILIATION', error_class = 'AMBIGUOUS',\n"
        "       error_code = 'STALE_DISPATCHING', updated_at = UTC_TIMESTAMP()\n"
        " WHERE state = 'DISPATCHING'\n"
        "   AND updated_at < DATE_SUB(UTC_TIMESTAMP(), INTERVAL ? MINUTE)"),
        2, offset=1,
        replacements='={{ $(\'[CONFIG] Resolve Thresholds\').item.json.reconcile_dispatching_minutes }}',
        retry=2, on_error='continueRegularOutput')
    W.chain(r1, r2)
    r3 = W.mysql('[DB] Reconcile Unknown', (
        "UPDATE wf_call_jobs\n"
        "   SET state = 'NEEDS_RECONCILIATION', updated_at = UTC_TIMESTAMP()\n"
        " WHERE state = 'UNKNOWN'\n"
        "   AND updated_at < DATE_SUB(UTC_TIMESTAMP(), INTERVAL ? MINUTE)"),
        2, offset=2,
        replacements='={{ $(\'[CONFIG] Resolve Thresholds\').item.json.reconcile_unknown_minutes }}',
        retry=2, on_error='continueRegularOutput')
    W.chain(r2, r3)
    r4 = W.mysql('[DB] Reconcile Dispatched Without Postcall', (
        "UPDATE wf_call_jobs\n"
        "   SET state = 'NEEDS_RECONCILIATION', error_code = 'NO_POSTCALL',\n"
        "       error_class = 'AMBIGUOUS', updated_at = UTC_TIMESTAMP()\n"
        " WHERE state = 'DISPATCHED'\n"
        "   AND dispatched_at < DATE_SUB(UTC_TIMESTAMP(), INTERVAL ? MINUTE)"),
        2, offset=3,
        replacements='={{ $(\'[CONFIG] Resolve Thresholds\').item.json.reconcile_dispatched_minutes }}',
        retry=2, on_error='continueRegularOutput')
    W.chain(r3, r4)

    W.sticky(3, '03 — RECONCILE LEDGERS', """Los tres ledgers de idempotencia:
conversaciones, pedidos de tool y grabaciones.

Un claim huérfano (n8n murió con la reserva tomada) **no se reintenta solo**:
pasa a revisión. Reintentar un `POST /followups`, una creación de cuenta o un
link de pago que pudo haberse ejecutado es peor que dejarlo para un humano.""",
              color=2, height=380)
    r5 = W.mysql('[DB] Reconcile Conversation Ledger', (
        "UPDATE wf_conversation_ledger\n"
        "   SET state = 'NEEDS_RECONCILIATION', error_code = 'STALE_CLAIM'\n"
        " WHERE state = 'CLAIMED' AND followup_id IS NULL\n"
        "   AND claimed_at < DATE_SUB(UTC_TIMESTAMP(), INTERVAL ? MINUTE)"),
        3, replacements='={{ $(\'[CONFIG] Resolve Thresholds\').item.json.reconcile_ledger_minutes }}',
        retry=2, on_error='continueRegularOutput')
    W.chain(r4, r5)
    r6 = W.mysql('[DB] Reconcile Tool Requests', (
        "UPDATE wf_tool_requests\n"
        "   SET state = 'NEEDS_RECONCILIATION', error_code = 'STALE_CLAIM',\n"
        "       updated_at = UTC_TIMESTAMP()\n"
        " WHERE state = 'CLAIMED'\n"
        "   AND claimed_at < DATE_SUB(UTC_TIMESTAMP(), INTERVAL ? MINUTE)"),
        3, offset=1,
        replacements='={{ $(\'[CONFIG] Resolve Thresholds\').item.json.reconcile_ledger_minutes }}',
        retry=2, on_error='continueRegularOutput')
    W.chain(r5, r6)
    r7 = W.mysql('[DB] Reconcile Recording Ledger', (
        "UPDATE wf_recording_ledger\n"
        "   SET state = 'FAILED', error_code = 'STALE_CLAIM', updated_at = UTC_TIMESTAMP()\n"
        " WHERE state = 'CLAIMED'\n"
        "   AND claimed_at < DATE_SUB(UTC_TIMESTAMP(), INTERVAL 30 MINUTE)"),
        3, offset=2, retry=2, on_error='continueRegularOutput')
    W.chain(r6, r7)

    # ══ 04 OPEN ISSUES ════════════════════════════════════════════════
    W.sticky(4, '04 — OPEN ISSUES', """Todo lo que quedó en `NEEDS_RECONCILIATION`
se convierte en un issue con `issue_key` **determinista**: volver a detectarlo
suma `occurrences`, no crea filas nuevas. Así el panel muestra "3 llamadas a
revisar", no "3.000 alertas repetidas".

También se abre `TECH_RETRY_REPEATED` cuando un lead acumula reintentos técnicos
contra el mismo proveedor: eso no es un problema del lead, es un problema del
proveedor, y conviene verlo antes de que consuma la ventana de llamadas.""",
              color=2, height=400)
    i1 = W.mysql('[DB] Open Call Reconciliation Issues', (
        "INSERT INTO wf_reconciliation_issues\n"
        "  (issue_key, issue_type, entity_type, entity_id, lead_id, country_iso,\n"
        "   severity, state, detail_json, first_seen_at, last_seen_at)\n"
        "SELECT CONCAT('CALL_NEEDS_RECONCILIATION:call_job:', j.call_job_id),\n"
        "       'CALL_NEEDS_RECONCILIATION', 'call_job', j.call_job_id, j.lead_id,\n"
        "       j.country_iso, 'WARN', 'OPEN',\n"
        "       JSON_OBJECT('state', j.state, 'error_code', j.error_code,\n"
        "                   'route_key', j.route_key, 'attempt', j.attempt,\n"
        "                   'provider', j.provider),\n"
        "       UTC_TIMESTAMP(), UTC_TIMESTAMP()\n"
        "  FROM wf_call_jobs j\n"
        " WHERE j.state = 'NEEDS_RECONCILIATION'\n"
        "   AND j.updated_at >= DATE_SUB(UTC_TIMESTAMP(), INTERVAL 1 DAY)\n"
        "ON DUPLICATE KEY UPDATE occurrences = occurrences + 1,\n"
        "        last_seen_at = UTC_TIMESTAMP()"),
        4, retry=2, on_error='continueRegularOutput')
    W.chain(r7, i1)
    i2 = W.mysql('[DB] Open Followup Reconciliation Issues', (
        "INSERT INTO wf_reconciliation_issues\n"
        "  (issue_key, issue_type, entity_type, entity_id, lead_id, country_iso,\n"
        "   severity, state, detail_json, first_seen_at, last_seen_at)\n"
        "SELECT CONCAT('FOLLOWUP_NEEDS_RECONCILIATION:conversation:', l.conversation_id),\n"
        "       'FOLLOWUP_NEEDS_RECONCILIATION', 'conversation', l.conversation_id,\n"
        "       l.lead_id, NULL, 'ERROR', 'OPEN',\n"
        "       JSON_OBJECT('state', l.state, 'error_code', l.error_code,\n"
        "                   'route_key', l.route_key, 'call_job_id', l.call_job_id),\n"
        "       UTC_TIMESTAMP(), UTC_TIMESTAMP()\n"
        "  FROM wf_conversation_ledger l\n"
        " WHERE l.state = 'NEEDS_RECONCILIATION'\n"
        "   AND l.updated_at >= DATE_SUB(UTC_TIMESTAMP(), INTERVAL 1 DAY)\n"
        "ON DUPLICATE KEY UPDATE occurrences = occurrences + 1,\n"
        "        last_seen_at = UTC_TIMESTAMP()"),
        4, offset=1, retry=2, on_error='continueRegularOutput')
    W.chain(i1, i2)
    i3 = W.mysql('[DB] Open Tech Retry Issues', (
        "INSERT INTO wf_reconciliation_issues\n"
        "  (issue_key, issue_type, entity_type, entity_id, lead_id, country_iso,\n"
        "   severity, state, detail_json, first_seen_at, last_seen_at)\n"
        "SELECT CONCAT('TECH_RETRY_REPEATED:route:', j.route_key),\n"
        "       'TECH_RETRY_REPEATED', 'route', j.route_key, NULL, j.country_iso,\n"
        "       'WARN', 'OPEN',\n"
        "       JSON_OBJECT('released_jobs', COUNT(*), 'provider', j.provider,\n"
        "                   'max_retries', MAX(j.tech_retry_count)),\n"
        "       UTC_TIMESTAMP(), UTC_TIMESTAMP()\n"
        "  FROM wf_call_jobs j\n"
        " WHERE j.state = 'RELEASED'\n"
        "   AND j.updated_at >= DATE_SUB(UTC_TIMESTAMP(), INTERVAL 1 HOUR)\n"
        " GROUP BY j.route_key, j.country_iso, j.provider\n"
        "HAVING COUNT(*) >= 5\n"
        "ON DUPLICATE KEY UPDATE occurrences = occurrences + 1,\n"
        "        last_seen_at = UTC_TIMESTAMP(), detail_json = VALUES(detail_json)"),
        4, offset=2, retry=2, on_error='continueRegularOutput')
    W.chain(i2, i3)

    # ══ 05 CRM SYNC (legacy) ══════════════════════════════════════════
    W.sticky(5, '05 — CRM SYNC (LEGACY)', """Se conserva la sincronización
`LeadStudio -> crm_leads` de v1 porque otras pantallas del panel la usan y
porque la reconciliación necesita el lado del CRM para comparar.

Usa `GET /api/leads` (reporting, sin cupo), **nunca** `/queue`: la cola es para
llamar, y consumirla acá le robaría leads a WF2.

Diferencia clave con v1: esto **ya no alimenta el dashboard del call center**.
Es una copia para comparar, no la fuente de la verdad.""",
              color=6, height=420)
    login = W.http('[CRM] Get Token', 'POST', CRM + '/api/auth/login', 5,
                   headers={'Content-Type': 'application/json'},
                   credentials={'httpCustomAuth': {'id': '__LEADSTUDIO_LOGIN_CREDENTIAL__',
                                                   'name': 'LeadStudio Login'}},
                   never_error=True, full_response=True, timeout=20000, retry=2,
                   on_error='continueRegularOutput')
    W.chain(i3, login)
    tok = W.code('[CRM] Check Token', CORE + r'''
const resp = $input.first().json || {};
const token = (resp.body && resp.body.accessToken) || null;
const cfg = $('[CONFIG] Resolve Thresholds').first().json;
if (!token) console.log('[WF14] AUTH_ERROR: sin token, se omite la sincronizacion del CRM');
return [{ json: Object.assign({}, cfg, { access_token: token, token_ok: !!token }) }];
''', 5, offset=1)
    W.chain(login, tok)
    tok_if = W.if_('[CRM] Token Is Valid', '={{ $json.token_ok }}', 'true', True, 5,
                   offset=2, single=True)
    W.chain(tok, tok_if)

    fetch = W.code('[CRM] Fetch Leads Snapshot', CORE + r'''
// Barrido por estado. Paginado explícito; sin agotar la cola de llamadas.
const cfg = $json;
const STATUSES = ['NOT_CONTACTED', 'ATTEMPTING', 'NO_ANSWER', 'CONTACTED',
                  'SNOOZED', 'UNREACHABLE', 'CLOSED'];
const pageSize = Number(cfg.wf14_leadstudio_page_size || 200);
const base = ($env.LM_LEADSTUDIO_URL || 'https://lead-studio-9gnl.onrender.com') +
             '/api/leads';
const seen = new Map();
for (const status of STATUSES) {
  let offset = 0, guard = 0;
  while (guard++ < 50) {
    let res;
    try {
      res = await this.helpers.httpRequest({ method: 'GET',
        url: base + '?limit=' + pageSize + '&offset=' + offset +
             '&status=' + encodeURIComponent(status),
        headers: { Authorization: 'Bearer ' + cfg.access_token },
        json: true, timeout: 30000 });
    } catch (e) {
      console.log('[WF14] ' + status + ' offset ' + offset + ': ' + e.message);
      break;
    }
    const leads = res.leads || [];
    for (const l of leads) { if (l.id) seen.set(String(l.id), l); }
    const total = Number(res.total || 0);
    offset += pageSize;
    if (!leads.length || offset >= total) break;
  }
}
console.log('[WF14] snapshot del CRM: ' + seen.size + ' lead(s) unicos');
if (!seen.size) return [];

function sqlDate(v) {
  if (!v) return null;
  const d = new Date(v);
  return isNaN(d) ? null : d.toISOString().slice(0, 19).replace('T', ' ');
}
const rows = [...seen.values()].map(l => ({
  lead_id: String(l.id).slice(0, 64),
  full_name: String(l.name || '').trim().slice(0, 160) || null,
  phone: String(l.phone || '').replace(/[^0-9+]/g, '').slice(0, 32) || null,
  language: String(l.language || '').trim().slice(0, 16) || null,
  status: String(l.status || '').trim().toUpperCase().slice(0, 48) || null,
  stage: String(l.stage || '').trim().toUpperCase().slice(0, 48) || null,
  call_attempts: Number(l.attempts || 0) || 0,
  do_not_call: l.doNotCall ? 1 : 0,
  last_contacted_at: sqlDate(l.lastContactedAt),
  next_follow_up_at: sqlDate(l.nextFollowUpAt),
  account_opened: (l.accountOpened === true ||
                   String(l.stage || '').toUpperCase() === 'INTERESTED') ? 1 : 0
}));
// Lotes de 500: ni una sentencia por fila ni un INSERT gigante.
const BATCH = 500, out = [];
for (let i = 0; i < rows.length; i += BATCH) {
  out.push({ json: { batch: rows.slice(i, i + BATCH), batch_num: out.length + 1,
                     batch_size: Math.min(BATCH, rows.length - i),
                     execution_id: cfg.execution_id } });
}
return out;
''', 6)
    W.link(tok_if, fetch, 0)

    W.sticky(6, '06 — UPSERT crm_leads', """`INSERT ... ON DUPLICATE KEY UPDATE` en
lotes de 500. El país sale del prefijo real del teléfono (autoritativo) y no del
campo `country` del lead, que puede venir nulo.

El `provider` de `crm_leads` **ya no** se deduce de "si aparece en
stringee_calls": en V2 la ruta y el proveedor de cada llamada están en
`wf_call_jobs`. Se conserva la columna para no romper las pantallas viejas, pero
la verdad está en el job.""", color=3, height=400)
    upsert = W.mysql('[DB] Upsert Crm Leads', (
        "INSERT INTO crm_leads\n"
        "  (lead_id, full_name, phone, country, country_code, language, status, stage,\n"
        "   call_attempts, do_not_call, last_contacted_at, next_follow_up_at, provider,\n"
        "   synced_at)\n"
        "SELECT j.lead_id, j.full_name, j.phone,\n"
        "       CASE WHEN j.phone LIKE '+977%' OR j.phone LIKE '977%' THEN 'nepal'\n"
        "            WHEN j.phone LIKE '+52%'  OR j.phone LIKE '52%'  THEN 'mexico'\n"
        "            WHEN j.phone LIKE '+91%'  OR j.phone LIKE '91%'  THEN 'india'\n"
        "            WHEN j.phone LIKE '+57%'  OR j.phone LIKE '57%'  THEN 'colombia'\n"
        "            WHEN j.phone LIKE '+58%'  OR j.phone LIKE '58%'  THEN 'venezuela'\n"
        "            ELSE 'unknown' END,\n"
        "       CASE WHEN j.phone LIKE '+977%' OR j.phone LIKE '977%' THEN '+977'\n"
        "            WHEN j.phone LIKE '+52%'  OR j.phone LIKE '52%'  THEN '+52'\n"
        "            WHEN j.phone LIKE '+91%'  OR j.phone LIKE '91%'  THEN '+91'\n"
        "            WHEN j.phone LIKE '+57%'  OR j.phone LIKE '57%'  THEN '+57'\n"
        "            WHEN j.phone LIKE '+58%'  OR j.phone LIKE '58%'  THEN '+58'\n"
        "            ELSE '' END,\n"
        "       j.language, j.status, j.stage, j.call_attempts, j.do_not_call,\n"
        "       j.last_contacted_at, j.next_follow_up_at,\n"
        "       (SELECT c.provider FROM wf_call_jobs c WHERE c.lead_id = j.lead_id\n"
        "         ORDER BY c.created_at DESC LIMIT 1),\n"
        "       UTC_TIMESTAMP()\n"
        "  FROM JSON_TABLE(?, '$[*]' COLUMNS (\n"
        "        lead_id VARCHAR(64) PATH '$.lead_id',\n"
        "        full_name VARCHAR(160) PATH '$.full_name',\n"
        "        phone VARCHAR(32) PATH '$.phone',\n"
        "        language VARCHAR(16) PATH '$.language',\n"
        "        status VARCHAR(48) PATH '$.status',\n"
        "        stage VARCHAR(48) PATH '$.stage',\n"
        "        call_attempts INT PATH '$.call_attempts',\n"
        "        do_not_call INT PATH '$.do_not_call',\n"
        "        last_contacted_at DATETIME PATH '$.last_contacted_at',\n"
        "        next_follow_up_at DATETIME PATH '$.next_follow_up_at')) AS j\n"
        "ON DUPLICATE KEY UPDATE\n"
        "  full_name = VALUES(full_name), phone = VALUES(phone),\n"
        "  country = VALUES(country), country_code = VALUES(country_code),\n"
        "  language = VALUES(language), status = VALUES(status), stage = VALUES(stage),\n"
        "  call_attempts = VALUES(call_attempts), do_not_call = VALUES(do_not_call),\n"
        "  last_contacted_at = VALUES(last_contacted_at),\n"
        "  next_follow_up_at = VALUES(next_follow_up_at),\n"
        "  provider = COALESCE(VALUES(provider), provider),\n"
        "  synced_at = VALUES(synced_at)"),
        6, offset=1, replacements='={{ JSON.stringify($json.batch) }}',
        retry=2, on_error='continueRegularOutput')
    W.chain(fetch, upsert)

    # ══ 07 COMPARE ════════════════════════════════════════════════════
    W.sticky(7, '07 — COMPARE LOCAL vs CRM', """Dos comparaciones, ambas **solo de
lectura** sobre los datos locales:

 · **cuenta local sin reflejo en el CRM** — hay `ACCOUNT_CREATED` en
   `wf_events` y el CRM no marca la cuenta → `CRM_ACCOUNT_MISSING`
 · **estado divergente** — el lead quedó `ATTEMPTING` en el CRM pero
   localmente la llamada terminó hace rato → `CRM_STATUS_MISMATCH`

Un issue **no corrige** nada. Se abre para que una persona decida. Corregir
automáticamente el CRM desde acá es cómo se pierden datos sin que nadie lo
note.""", color=2, height=420)
    c1 = W.mysql('[DB] Compare Accounts', (
        "INSERT INTO wf_reconciliation_issues\n"
        "  (issue_key, issue_type, entity_type, entity_id, lead_id, country_iso,\n"
        "   severity, state, detail_json, first_seen_at, last_seen_at)\n"
        "SELECT CONCAT('CRM_ACCOUNT_MISSING:lead:', e.lead_id), 'CRM_ACCOUNT_MISSING',\n"
        "       'lead', e.lead_id, e.lead_id, e.country_iso, 'WARN', 'OPEN',\n"
        "       JSON_OBJECT('local_event', e.event_key, 'provider', e.provider,\n"
        "                   'crm_stage', c.stage, 'crm_account_opened', 0),\n"
        "       UTC_TIMESTAMP(), UTC_TIMESTAMP()\n"
        "  FROM wf_events e\n"
        "  JOIN crm_leads c ON c.lead_id = e.lead_id\n"
        " WHERE e.event_type = 'ACCOUNT_CREATED'\n"
        "   AND e.occurred_at >= DATE_SUB(UTC_TIMESTAMP(), INTERVAL 7 DAY)\n"
        "   AND COALESCE(c.stage, '') <> 'INTERESTED'\n"
        "ON DUPLICATE KEY UPDATE occurrences = occurrences + 1,\n"
        "        last_seen_at = UTC_TIMESTAMP(), detail_json = VALUES(detail_json)"),
        7, retry=2, on_error='continueRegularOutput')
    W.chain(upsert, c1)
    c2 = W.mysql('[DB] Compare Lead Status', (
        "INSERT INTO wf_reconciliation_issues\n"
        "  (issue_key, issue_type, entity_type, entity_id, lead_id, country_iso,\n"
        "   severity, state, detail_json, first_seen_at, last_seen_at)\n"
        "SELECT CONCAT('CRM_STATUS_MISMATCH:lead:', c.lead_id), 'CRM_STATUS_MISMATCH',\n"
        "       'lead', c.lead_id, c.lead_id, j.country_iso, 'WARN', 'OPEN',\n"
        "       JSON_OBJECT('crm_status', c.status, 'local_state', j.state,\n"
        "                   'local_result', j.result, 'call_job_id', j.call_job_id),\n"
        "       UTC_TIMESTAMP(), UTC_TIMESTAMP()\n"
        "  FROM crm_leads c\n"
        "  JOIN wf_call_jobs j ON j.lead_id = c.lead_id\n"
        " WHERE c.status = 'ATTEMPTING' AND j.state = 'COMPLETED'\n"
        "   AND j.completed_at < DATE_SUB(UTC_TIMESTAMP(), INTERVAL 2 HOUR)\n"
        "   AND j.completed_at >= DATE_SUB(UTC_TIMESTAMP(), INTERVAL 2 DAY)\n"
        "ON DUPLICATE KEY UPDATE occurrences = occurrences + 1,\n"
        "        last_seen_at = UTC_TIMESTAMP(), detail_json = VALUES(detail_json)"),
        7, offset=1, retry=2, on_error='continueRegularOutput')
    W.chain(c1, c2)

    # ══ 08 PROVIDER RECONCILE ═════════════════════════════════════════
    W.sticky(8, '08 — PROVIDER RECONCILE', """Se conserva el volcado del call-log
del worker Stringee a `stringee_calls` (volumen y duración reales del
proveedor), para poder contrastar lo que facturó el proveedor contra lo que
registró el suite.

`from_start_time` va en **segundos** (Stringee rechaza milisegundos). Ese
detalle costó una tarde en v1 y queda anotado acá.""", color=6, height=360)
    st_get = W.http('[PROVIDER] Fetch Stringee Call Log', 'GET',
                    '={{ $env.LM_STRINGEE_WORKER_URL || "http://172.18.0.1:8091" }}/call-log',
                    8, query={'limit': '200',
                              'from_start_time': '={{ Math.floor((Date.now() - 20*60*1000) / 1000) }}'},
                    never_error=True, full_response=True, timeout=30000, retry=2,
                    on_error='continueRegularOutput')
    W.chain(c2, st_get)
    st_norm = W.code('[PROVIDER] Normalize Call Log', CORE + r'''
const resp = $input.first().json || {};
const code = Number(resp.statusCode || 0);
const body = resp.body || {};
const calls = (body.data && body.data.calls) || body.calls || [];
if (code < 200 || code >= 300) {
  console.log('[WF14] call-log del worker HTTP ' + code + ': se omite');
  return [];
}
if (!calls.length) { console.log('[WF14] sin llamadas nuevas en el call-log'); return []; }
const rows = calls.map(c => {
  const to = String(c.to_number || '').replace(/[^0-9]/g, '');
  return { call_id: String(c.id || ''), phone: to,
           answered: Number(c.answer_time) > 0 ? 1 : 0,
           duration_secs: Number(c.answer_duration || 0),
           start_time: Number(c.start_time || 0),
           answer_time: Number(c.answer_time || 0),
           stop_time: Number(c.stop_time || 0) };
}).filter(r => r.call_id);
console.log('[WF14] call-log: ' + rows.length + ' llamada(s)');
return [{ json: { batch: rows, batch_size: rows.length } }];
''', 8, offset=1)
    W.chain(st_get, st_norm)
    st_up = W.mysql('[DB] Upsert Stringee Calls', (
        "INSERT INTO stringee_calls\n"
        "  (call_id, lead_id, phone, country, answered, duration_secs, start_time,\n"
        "   answer_time, stop_time)\n"
        "SELECT t.call_id,\n"
        "       (SELECT c.lead_id FROM crm_leads c\n"
        "         WHERE RIGHT(c.phone, 10) = RIGHT(t.phone, 10) LIMIT 1),\n"
        "       t.phone,\n"
        "       CASE WHEN t.phone LIKE '977%' THEN 'nepal'\n"
        "            WHEN t.phone LIKE '52%'  THEN 'mexico'\n"
        "            WHEN t.phone LIKE '91%'  THEN 'india'\n"
        "            ELSE 'unknown' END,\n"
        "       t.answered, t.duration_secs, t.start_time, t.answer_time, t.stop_time\n"
        "  FROM JSON_TABLE(?, '$[*]' COLUMNS (\n"
        "        call_id VARCHAR(64) PATH '$.call_id',\n"
        "        phone VARCHAR(32) PATH '$.phone',\n"
        "        answered INT PATH '$.answered',\n"
        "        duration_secs INT PATH '$.duration_secs',\n"
        "        start_time BIGINT PATH '$.start_time',\n"
        "        answer_time BIGINT PATH '$.answer_time',\n"
        "        stop_time BIGINT PATH '$.stop_time')) AS t\n"
        "ON DUPLICATE KEY UPDATE\n"
        "  lead_id = COALESCE(VALUES(lead_id), lead_id), country = VALUES(country),\n"
        "  answered = VALUES(answered), duration_secs = VALUES(duration_secs),\n"
        "  answer_time = VALUES(answer_time), stop_time = VALUES(stop_time)"),
        8, offset=2, replacements='={{ JSON.stringify($json.batch) }}',
        retry=2, on_error='continueRegularOutput')
    W.chain(st_norm, st_up)

    # ══ 09 SUMMARY ════════════════════════════════════════════════════
    W.sticky(9, '09 — SUMMARY', """Resumen de la corrida y registro en
`panel_sync_log` (compatibilidad con las pantallas existentes).

Lo que **no** hay acá: ningún cálculo de KPI. Los números del panel salen de
`wf_call_jobs` y `wf_events`, en tiempo real, sin pasar por este workflow.""",
              color=4, height=340)
    summ = W.mysql('[DB] Load Reconciliation Counters', (
        "SELECT\n"
        "  (SELECT COUNT(*) FROM wf_call_jobs WHERE state = 'NEEDS_RECONCILIATION')\n"
        "     AS jobs_needing_reconciliation,\n"
        "  (SELECT COUNT(*) FROM wf_call_jobs WHERE state = 'RELEASED') AS jobs_released,\n"
        "  (SELECT COUNT(*) FROM wf_conversation_ledger\n"
        "    WHERE state = 'NEEDS_RECONCILIATION') AS ledger_needing_reconciliation,\n"
        "  (SELECT COUNT(*) FROM wf_tool_requests\n"
        "    WHERE state = 'NEEDS_RECONCILIATION') AS tools_needing_reconciliation,\n"
        "  (SELECT COUNT(*) FROM wf_recording_ledger WHERE state = 'ORPHAN')\n"
        "     AS recordings_orphan,\n"
        "  (SELECT COUNT(*) FROM wf_reconciliation_issues WHERE state = 'OPEN')\n"
        "     AS issues_open"),
        9, retry=2, on_error='continueRegularOutput')
    W.chain(st_up, summ)
    log = W.code('[LOG] Reconciliation Summary', CORE + r'''
const c = $input.first().json || {};
const s = { workflow: 'WF14', execution_id: $execution.id,
            jobs_needing_reconciliation: Number(c.jobs_needing_reconciliation || 0),
            jobs_released: Number(c.jobs_released || 0),
            ledger_needing_reconciliation: Number(c.ledger_needing_reconciliation || 0),
            tools_needing_reconciliation: Number(c.tools_needing_reconciliation || 0),
            recordings_orphan: Number(c.recordings_orphan || 0),
            issues_open: Number(c.issues_open || 0) };
console.log('[WF14][exec=' + $execution.id + '] ' + JSON.stringify(s));
if (s.issues_open > 0) {
  console.log('[WF14] hay ' + s.issues_open + ' issue(s) abiertos: /callcenter/issues');
}
return [{ json: s }];
''', 9, offset=1)
    W.chain(summ, log)
    sync_log = W.mysql('[DB] Record Sync Log', (
        "INSERT INTO panel_sync_log (source, rows_in, status, synced_at)\n"
        "VALUES ('crm-v2', ?, 'ok', UTC_TIMESTAMP())"),
        9, offset=2, replacements='={{ $json.issues_open }}',
        retry=2, on_error='continueRegularOutput')
    W.chain(log, sync_log)

    skip = W.code('[LOG] Crm Sync Skipped', CORE + r'''
// Sin token no se sincroniza el CRM, pero la reconciliación LOCAL de las
// secciones 02-04 ya corrió: eso es lo que no puede dejar de hacerse.
console.log('[WF14][exec=' + $execution.id + '] sincronizacion del CRM omitida ' +
            '(AUTH_ERROR); la reconciliacion local si se ejecuto');
return [{ json: { ok: true, crm_sync: false, reason: 'AUTH_ERROR' } }];
''', 6, branch=3)
    W.link(tok_if, skip, 1)
    return W
