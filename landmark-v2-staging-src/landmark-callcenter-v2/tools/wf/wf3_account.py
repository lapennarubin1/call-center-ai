"""TEMPLATE_WF3_ACCOUNT_CREATION_V2 — apertura de cuenta de trading.

Reemplaza a "INDIA + NEPAL WF3 — Account Creation".

Lo que YA NO hace, respecto de v1:
  · no tiene el mapa `{india: 'IND', nepal: 'NPL'}` en un nodo: el market sale
    de `country_tool_configs` (tool CREATE_ACCOUNT del PAÍS)
  · no tiene nodos huérfanos (en v1 `Lead ID Válido?` no recibía entrada, así
    que la validación no corría nunca)
  · no se apoya en el 409 del proveedor como única idempotencia: reserva el
    pedido ANTES de llamar
  · no escribe notas en español en el CRM
"""

VERSION = '2.0.0'
NAME = 'TEMPLATE_WF3_ACCOUNT_CREATION_V2'


def build(wfbuild, js):
    W = wfbuild.Workflow(
        NAME, VERSION,
        'TEMPLATE_DATA v2.2 · ANALYTICS_EVENT v2.2 · CRM_ENGLISH_RULE',
        'WF3 v1 — desactivar este workflow; v1 vuelve a atender su webhook (§12)',
        'Webhook de tool de ElevenLabs + sub-workflow desde el bot de WhatsApp.')
    CORE, NOTES = js['lmcore.js'], js['lmnotes.js']
    PANEL = '={{ $env.LM_PANEL_URL || "http://172.18.0.1:8080" }}'
    CRM = '={{ $env.LM_LEADSTUDIO_URL || "https://lead-studio-9gnl.onrender.com" }}'

    # ══ 00 TRIGGERS ═══════════════════════════════════════════════════
    W.sticky(0, '00 — TRIGGERS', f"""{W.meta_header}

Dos entradas, una sola tubería:
 · **tool de ElevenLabs** durante la llamada (`create-account-v2`)
 · **sub-workflow** desde el bot de WhatsApp

La respuesta al agente sale por `[TOOL] Respond To Agent`, siempre con un
mensaje de negocio legible **en inglés**, nunca un 500.""", color=4, height=400)
    hook = W.webhook('[TRIGGER] Account Tool Webhook', 'create-account-v2', 0)
    sub = W.exec_trigger('[TRIGGER] Called By Bot',
                         ['lead_id', 'full_name', 'phone', 'country_iso',
                          'conversation_id', 'whatsapp_number'], 0, branch=2)

    # ══ 01 PARSE ══════════════════════════════════════════════════════
    W.sticky(1, '01 — PARSE & VALIDATE', """`country_iso` es **obligatorio y
explícito**. No se deduce del prefijo del teléfono: esa inferencia es
exactamente lo que el estándar prohíbe (§1.2).

`lead_id` debe ser un UUID de LeadStudio. Sin un lead real no se puede crear una
cuenta: se responde al agente con un error de negocio en inglés.

`request_ref` = `conversation_id` de la llamada que pidió la tool, o el
`execution_id` si no hay conversación. Es lo que hace idempotente el pedido:
dos reintentos de ElevenLabs de la MISMA conversación son un solo pedido.""",
              color=6, height=420)
    parse = W.code('[CONFIG] Parse Tool Request', CORE + r'''
const raw = $input.first().json || {};
const b = raw.body || raw;

const leadId = String(b.lead_id || '').trim();
const iso = String(b.country_iso || b.country || '').trim().toUpperCase();
const phone = String(b.phone || '').replace(/[^0-9+]/g, '');
const fullName = String(b.full_name || '').trim();
const conversationId = String(b.conversation_id || '').trim() || null;
const waNumber = String(b.whatsapp_number || b.phone || '').replace(/[^0-9+]/g, '');

const errs = [];
const UUID = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i;
if (!leadId) errs.push('lead_id');
else if (!UUID.test(leadId)) errs.push('lead_id no es un UUID de LeadStudio');
if (!/^[A-Z]{2}$/.test(iso)) errs.push('country_iso (ISO-2 explicito)');
if (!fullName || fullName.length < 2) errs.push('full_name');
if (!phone || phone.replace(/\D/g, '').length < 6) errs.push('phone');

const requestRef = conversationId || String($execution.id);
return [{ json: {
  valid: errs.length === 0,
  error_detail: errs.length ? ('campos invalidos o ausentes: ' + errs.join(', ')) : null,
  lead_id: leadId, country_iso: iso, phone: phone, full_name: fullName,
  whatsapp_number: waNumber || phone,
  conversation_id: conversationId, request_ref: requestRef,
  request_key: 'CREATE_ACCOUNT:' + leadId + ':' + requestRef,
  execution_id: $execution.id
}}];
''', 1)
    W.link(hook, parse)
    W.link(sub, parse)
    p_if = W.if_('[CONFIG] Request Is Valid', '={{ $json.valid }}', 'true', True, 1,
                 offset=1, single=True)
    W.chain(parse, p_if)

    # ══ 02 COUNTRY TOOLS ══════════════════════════════════════════════
    W.sticky(2, '02 — COUNTRY TOOL CONFIG', """`GET /api/countries/{iso}/tools`.

Las tools pertenecen al **PAÍS**, no al proveedor de voz: `IN_PROVEEDOR1` e
`IN_STRINGEE` usan exactamente la misma configuración de India.

Se rechaza (con un mensaje de negocio en inglés, no un 500) si:
 · el país está **apagado** (`country_enabled = false`) — §7.3
 · el país no está **READY**
 · la tool `CREATE_ACCOUNT` no está habilitada
 · falta `market` → **CONFIG_ERROR**. No se inventa un market de CashStudio.

Dos modos: `CONFIG_ROUTER` (por la API de LeadStudio) y `CUSTOM_ENDPOINT`
(endpoint propio del país, con su `credential_ref`).""", color=3, height=460)
    tools = W.http('[CONFIG] Load Country Tools', 'GET',
                   PANEL + '/api/countries/{{ $json.country_iso }}/tools', 2,
                   credentials={'httpHeaderAuth': {'id': '__PANEL_TOKEN_CREDENTIAL__',
                                                   'name': 'Landmark Panel API'}},
                   never_error=True, full_response=True, timeout=10000, retry=2,
                   on_error='continueRegularOutput')
    W.link(p_if, tools, 0)

    tcheck = W.code('[CONFIG] Resolve Account Tool', CORE + NOTES + r'''
const rows = $input.all();
return rows.map((r, i) => {
  const ctx = $('[CONFIG] Parse Tool Request').itemMatching(i).json;
  const resp = r.json || {};
  const code = Number(resp.statusCode || 0);
  const body = resp.body || {};

  let err = null, errCode = null;
  if (code === 404) { errCode = 'CONFIG_ERROR'; err = 'country ' + ctx.country_iso + ' not configured'; }
  else if (code < 200 || code >= 300) { errCode = 'CONFIG_ERROR'; err = 'panel HTTP ' + code; }
  else if (!body.country_enabled) { errCode = 'COUNTRY_DISABLED'; err = ctx.country_iso + ' is disabled'; }
  else if (!body.country_ready) { errCode = 'CONFIG_ERROR'; err = ctx.country_iso + ' is not ready'; }

  const tool = (body.tools || {}).CREATE_ACCOUNT || null;
  if (!errCode && (!tool || !tool.enabled)) {
    errCode = 'TOOL_DISABLED'; err = 'CREATE_ACCOUNT not enabled for ' + ctx.country_iso;
  }
  // El market NO se inventa: si falta, es CONFIG_ERROR.
  if (!errCode && tool.mode === 'CONFIG_ROUTER' && !String(tool.market || '').trim()) {
    errCode = 'CONFIG_ERROR';
    err = 'CREATE_ACCOUNT for ' + ctx.country_iso + ' has no market configured';
  }
  if (!errCode && tool.mode === 'CUSTOM_ENDPOINT' && !String(tool.endpoint || '').trim()) {
    errCode = 'CONFIG_ERROR';
    err = 'CREATE_ACCOUNT for ' + ctx.country_iso + ' has no endpoint configured';
  }

  return { json: Object.assign({}, ctx, {
    tool_ok: !errCode, error_code: errCode,
    // El error que se GUARDA en el CRM va en inglés (CRM_ENGLISH_RULE).
    error_note: errCode ? lmErrorNote(errCode, err) : null,
    user_message: errCode ? lmErrorNote(errCode, null) : null,
    error_detail: err,
    tool: tool, tool_mode: tool ? tool.mode : null,
    tool_provider: tool ? (tool.provider_key || 'cashstudio') : null,
    market: tool ? (tool.market || null) : null,
    endpoint: tool ? (tool.endpoint || null) : null,
    http_method: tool ? (tool.http_method || 'POST') : 'POST',
    tool_config: tool ? (tool.config || {}) : {},
    country_timezone: body.timezone || 'UTC'
  })};
});
''', 2, offset=1)
    W.chain(tools, tcheck)
    t_if = W.if_('[CONFIG] Tool Is Available', '={{ $json.tool_ok }}', 'true', True, 2,
                 offset=2, single=True)
    W.chain(tcheck, t_if)

    # ══ 03 CLAIM ══════════════════════════════════════════════════════
    W.sticky(3, '03 — IDEMPOTENT CLAIM', """El pedido se **reserva antes** de llamar
al proveedor: `wf_tool_requests`, clave `CREATE_ACCOUNT:{lead_id}:{request_ref}`.

v1 dependía del 409 de CashStudio, que llega DESPUÉS de haber pedido la cuenta.
Con dos reintentos simultáneos del mismo tool-call, salían los dos.

Perder el claim **no es un error**: se devuelve el resultado ya registrado.""",
              color=3, height=360)
    claimtok = W.code('[TOOL] Build Claim Token', r'''
const crypto = require('crypto');
return $input.all().map(it => ({ json: Object.assign({}, it.json, {
  claim_token: crypto.randomBytes(16).toString('hex')
})}));
''', 3)
    W.link(t_if, claimtok, 0)
    claim = W.mysql('[DB] Claim Tool Request', (
        "INSERT IGNORE INTO wf_tool_requests\n"
        "  (request_key, tool_type, lead_id, country_iso, request_ref, conversation_id,\n"
        "   tool_provider, mode, claim_token, execution_id, state)\n"
        "VALUES (?, 'CREATE_ACCOUNT', ?, ?, ?, ?, ?, ?, ?, ?, 'CLAIMED')"),
        3, offset=1,
        replacements='={{ $json.request_key }},={{ $json.lead_id }},={{ $json.country_iso }},'
                     '={{ $json.request_ref }},={{ $json.conversation_id }},'
                     '={{ $json.tool_provider }},={{ $json.tool_mode }},'
                     '={{ $json.claim_token }},={{ $json.execution_id }}',
        retry=2, on_error='continueRegularOutput')
    W.chain(claimtok, claim)
    claim_read = W.mysql('[DB] Read Tool Claim', (
        "SELECT ? AS _k, claim_token, state, result_ref\n"
        "  FROM wf_tool_requests WHERE request_key = ?"),
        3, offset=2,
        replacements='={{ $(\'[TOOL] Build Claim Token\').item.json.request_key }},'
                     '={{ $(\'[TOOL] Build Claim Token\').item.json.request_key }}',
        retry=2, on_error='continueRegularOutput')
    W.chain(claim, claim_read)
    claim_chk = W.code('[TOOL] Check Tool Claim', CORE + r'''
return $input.all().map((r, i) => {
  const ctx = $('[TOOL] Build Claim Token').itemMatching(i).json;
  const row = r.json || {};
  const won = !!row.claim_token && row.claim_token === ctx.claim_token;
  if (!won) {
    console.log('[WF3] claim perdido para ' + ctx.request_key + ' (estado ' +
                row.state + '): no se vuelve a crear la cuenta');
  }
  return { json: Object.assign({}, ctx, {
    claim_won: won, existing_state: row.state || null, existing_ref: row.result_ref || null
  })};
});
''', 3, offset=3)
    W.chain(claim_read, claim_chk)
    claim_if = W.if_('[TOOL] Claim Won', '={{ $json.claim_won }}', 'true', True, 3,
                     offset=4, single=True)
    W.chain(claim_chk, claim_if)

    ev_req = W.mysql('[DB] Record Event Account Requested', (
        "INSERT IGNORE INTO wf_events\n"
        "  (event_key, event_type, event_domain, occurred_at, lead_id, country_iso,\n"
        "   conversation_id, provider, source_workflow, execution_id, metadata_json)\n"
        "VALUES (?, 'ACCOUNT_REQUESTED', 'TOOL', UTC_TIMESTAMP(), ?, ?, ?, ?, 'WF3', ?, ?)"),
        4, replacements='={{ "ACCOUNT_REQUESTED:" + $json.lead_id + ":" + $json.request_ref }},'
                        '={{ $json.lead_id }},={{ $json.country_iso }},'
                        '={{ $json.conversation_id }},={{ $json.tool_provider }},'
                        '={{ $json.execution_id }},'
                        '={{ JSON.stringify({request_ref: $json.request_ref, mode: $json.tool_mode, market: $json.market}) }}',
        on_error='continueRegularOutput', retry=2)
    W.link(claim_if, ev_req, 0)

    # ══ 05 PROVIDER ═══════════════════════════════════════════════════
    W.sticky(5, '05 — ACCOUNT PROVIDER', """Dos modos, elegidos por la configuración
del país. **Ninguna rama por país.**

`CONFIG_ROUTER` → `POST /api/leads/{id}/cashstudio-account` con el `market` de
la config.
`CUSTOM_ENDPOINT` → el endpoint del país, autenticado con su `credential_ref`
(la credential vive en n8n; acá solo viaja su nombre lógico).

Agregar Pakistán es dar de alta el país y su tool en el panel. Cero nodos
nuevos.""", color=5, height=400)
    tok = W.http('[CRM] Get Token', 'POST', CRM + '/api/auth/login', 5,
                 headers={'Content-Type': 'application/json'},
                 credentials={'httpCustomAuth': {'id': '__LEADSTUDIO_LOGIN_CREDENTIAL__',
                                                 'name': 'LeadStudio Login'}},
                 never_error=True, full_response=True, timeout=15000, retry=2,
                 on_error='continueRegularOutput')
    W.chain(ev_req, tok)
    tokm = W.code('[CRM] Attach Token', CORE + r'''
const resp = $input.first().json || {};
const token = (resp.body && resp.body.accessToken) || null;
return $('[TOOL] Claim Won').all().map(it => ({ json: Object.assign({}, it.json, {
  access_token: token, token_ok: !!token
})}));
''', 5, offset=1)
    W.chain(tok, tokm)
    mode_sw = W.switch('[TOOL] Account Mode Router', '={{ $json.tool_mode }}',
                       [('CONFIG_ROUTER', 'router'), ('CUSTOM_ENDPOINT', 'custom')],
                       5, offset=2)
    W.chain(tokm, mode_sw)

    router = W.http('[TOOL] Create Account Via Router', 'POST',
                    CRM + '/api/leads/{{ $json.lead_id }}/cashstudio-account', 6,
                    headers={'Content-Type': 'application/json',
                             'Authorization': '=Bearer {{ $json.access_token }}'},
                    body_json='={{ JSON.stringify({ market: $json.market }) }}',
                    never_error=True, full_response=True, timeout=20000,
                    on_error='continueRegularOutput')
    W.link(mode_sw, router, 0)

    custom = W.http('[TOOL] Create Account Via Custom Endpoint', 'POST',
                    '={{ $json.endpoint }}', 6, branch=2,
                    headers={'Content-Type': 'application/json'},
                    credentials={'httpHeaderAuth': {'id': '__COUNTRY_TOOL_CREDENTIAL__',
                                                    'name': 'Country Tool API'}},
                    body_json='={{ JSON.stringify(Object.assign({ lead_id: $json.lead_id, '
                              'full_name: $json.full_name, phone: $json.phone, '
                              'country: $json.country_iso, market: $json.market }, '
                              '$json.tool_config.extra_body || {})) }}',
                    never_error=True, full_response=True, timeout=20000,
                    on_error='continueRegularOutput')
    W.link(mode_sw, custom, 1)

    mode_err = W.code('[ERROR] Unsupported Tool Mode', CORE + NOTES + r'''
return $input.all().map(it => ({ json: Object.assign({}, it.json, {
  account_status: 'FAILED', error_code: 'CONFIG_ERROR',
  error_detail: 'modo de tool no soportado: ' + it.json.tool_mode,
  crm_note: lmErrorNote('CONFIG_ERROR', 'unsupported tool mode'),
  user_message: lmErrorNote('CONFIG_ERROR', null),
  username: null, password: null, account_id: null, portal_url: null
})}));
''', 6, branch=4)
    W.link(mode_sw, mode_err, 2)

    # ══ 07 VALIDATE RESPONSE ══════════════════════════════════════════
    W.sticky(7, '07 — VALIDATE RESPONSE', """Contrato de CashStudio, respuesta por
respuesta:

| HTTP | significado | qué se hace |
|---|---|---|
| 201 | cuenta creada | `ACCOUNT_CREATED` · se entregan credenciales |
| 409 `EMAIL_TAKEN` / `EMAIL_INVALID` | dato del lead | `ACCOUNT_FAILED` |
| 409 sin `code` | el lead **ya tenía** cuenta | `ACCOUNT_ALREADY_EXISTS` |
| 429 | rate limit (20/15 min) | falla y **no** reintenta: reintentar empeora |
| 502 / timeout | **ambiguo** | `NEEDS_RECONCILIATION`, **no** se reintenta |
| resto | error | `ACCOUNT_FAILED` |

El 409 sin `code` **no** es un fallo: el cliente ya tiene cuenta. La API entrega
la contraseña una sola vez, en el create original, así que **no se reenvían
credenciales** que no tenemos — y la nota del CRM lo dice, en inglés.

v1 reintentaba el 502 una vez con un nodo Wait; eso puede crear dos cuentas.""",
              color=2, height=520)
    valid = W.code('[TOOL] Validate Account Response', CORE + NOTES + r'''
const rows = $input.all();
return rows.map((r, i) => {
  let ctx;
  try { ctx = $('[TOOL] Account Mode Router').itemMatching(i).json; }
  catch (e) { ctx = $('[TOOL] Claim Won').all()[0].json; }
  const resp = r.json || {};
  const code = Number(resp.statusCode || 0);
  const body = resp.body || {};

  let status, errorCode = null, detail = null;
  let username = null, password = null, accountId = null, traderId = null, portal = null;

  if (code === 201 && body.account) {
    status = 'CREATED';
    username = body.account.userName || body.account.username || null;
    password = body.password || null;
    accountId = body.account.id || null;
    traderId = body.account.traderId || null;
    portal = body.account.portalUrl || (ctx.tool_config || {}).portal_url || null;
    if (body.account.market && ctx.market && body.account.market !== ctx.market) {
      console.log('[WF3] WARN el proveedor devolvio market ' + body.account.market +
                  ' y se pidio ' + ctx.market);
    }
  } else if (code === 409) {
    const c = (body.details && body.details.code) || '';
    if (c === 'EMAIL_TAKEN' || c === 'EMAIL_INVALID') {
      status = 'FAILED'; errorCode = 'VALIDATION_ERROR'; detail = c;
    } else {
      // Ya tiene cuenta. NO es un fallo y NO se reintenta.
      status = 'ALREADY_EXISTS'; detail = body.error || 'lead already has a trader account';
    }
  } else if (code === 429) {
    status = 'FAILED'; errorCode = 'RETRYABLE_ERROR';
    detail = 'rate limited (429) - retry manually later';
  } else if (code === 0 || code === 408 || code >= 500) {
    // AMBIGUO: el proveedor pudo haber creado la cuenta. Jamás se reintenta.
    status = 'AMBIGUOUS'; errorCode = 'AMBIGUOUS';
    detail = 'provider did not confirm (HTTP ' + code + ')';
  } else {
    status = 'FAILED'; errorCode = 'PROVIDER_ERROR';
    detail = (body.error || JSON.stringify(body)).slice(0, 200);
  }

  const note = status === 'CREATED'
    ? lmAccountNote({ status: 'CREATED', market: ctx.market, username: username, portal_url: portal })
    : status === 'ALREADY_EXISTS'
      ? lmAccountNote({ status: 'ALREADY_EXISTS', market: ctx.market })
      : status === 'AMBIGUOUS'
        ? lmErrorNote('AMBIGUOUS', detail)
        : lmAccountNote({ status: 'FAILED', market: ctx.market, error: detail });

  return { json: Object.assign({}, ctx, {
    account_status: status, http_status: code, error_code: errorCode, error_detail: detail,
    username: username, password: password, account_id: accountId, trader_id: traderId,
    portal_url: portal, crm_note: note,
    user_message: status === 'CREATED'
      ? 'Your trading account has been created. The login details have been sent to you.'
      : status === 'ALREADY_EXISTS'
        ? 'You already have a trading account with us.'
        : 'We could not open the account right now. Our team will follow up.'
  })};
});
''', 7)
    W.link(router, valid)
    W.link(custom, valid)
    W.link(mode_err, valid)

    st_sw = W.switch('[TOOL] Account Result Router', '={{ $json.account_status }}',
                     [('CREATED', 'created'), ('ALREADY_EXISTS', 'exists'),
                      ('AMBIGUOUS', 'ambiguous')], 7, offset=1, fallback='extra')
    W.chain(valid, st_sw)

    # ══ 08 PERSIST ════════════════════════════════════════════════════
    W.sticky(8, '08 — PERSIST & CRM', """Primero el evento **local** e idempotente
(`ACCOUNT_CREATED:{provider}:{lead_id}`: una cuenta por lead y proveedor, aunque
el pedido se repita), después LeadStudio.

La nota del CRM va **en inglés** y la genera `lmnotes.js`, el mismo catálogo que
usan los otros seis workflows. El nombre, el teléfono, el usuario y la URL del
portal **no** se traducen.""", color=3, height=380)
    ev_created = W.mysql('[DB] Record Event Account Created', (
        "INSERT IGNORE INTO wf_events\n"
        "  (event_key, event_type, event_domain, occurred_at, lead_id, country_iso,\n"
        "   conversation_id, provider, source_workflow, execution_id, metadata_json)\n"
        "VALUES (?, 'ACCOUNT_CREATED', 'TOOL', UTC_TIMESTAMP(), ?, ?, ?, ?, 'WF3', ?, ?)"),
        8, replacements='={{ "ACCOUNT_CREATED:" + $json.tool_provider + ":" + $json.lead_id }},'
                        '={{ $json.lead_id }},={{ $json.country_iso }},'
                        '={{ $json.conversation_id }},={{ $json.tool_provider }},'
                        '={{ $json.execution_id }},'
                        '={{ JSON.stringify({market: $json.market, account_id: $json.account_id, trader_id: $json.trader_id}) }}',
        on_error='continueRegularOutput', retry=2)
    W.link(st_sw, ev_created, 0)

    ev_exists = W.mysql('[DB] Record Event Account Exists', (
        "INSERT IGNORE INTO wf_events\n"
        "  (event_key, event_type, event_domain, occurred_at, lead_id, country_iso,\n"
        "   conversation_id, provider, source_workflow, execution_id)\n"
        "VALUES (?, 'ACCOUNT_ALREADY_EXISTS', 'TOOL', UTC_TIMESTAMP(), ?, ?, ?, ?, 'WF3', ?)"),
        8, branch=2,
        replacements='={{ "ACCOUNT_ALREADY_EXISTS:" + $json.tool_provider + ":" + $json.lead_id }},'
                     '={{ $json.lead_id }},={{ $json.country_iso }},'
                     '={{ $json.conversation_id }},={{ $json.tool_provider }},'
                     '={{ $json.execution_id }}',
        on_error='continueRegularOutput', retry=2)
    W.link(st_sw, ev_exists, 1)

    amb = W.mysql('[DB] Mark Tool Needs Reconciliation', (
        "UPDATE wf_tool_requests\n"
        "   SET state = 'NEEDS_RECONCILIATION', error_code = 'AMBIGUOUS',\n"
        "       error_message = ?, completed_at = UTC_TIMESTAMP()\n"
        " WHERE request_key = ? AND state = 'CLAIMED'"),
        8, branch=3,
        replacements='={{ $json.error_detail }},={{ $json.request_key }}',
        on_error='continueRegularOutput', retry=2)
    W.link(st_sw, amb, 2)
    amb_issue = W.mysql('[DB] Open Account Issue', (
        "INSERT INTO wf_reconciliation_issues\n"
        "  (issue_key, issue_type, entity_type, entity_id, lead_id, country_iso,\n"
        "   severity, state, detail_json, first_seen_at, last_seen_at)\n"
        "VALUES (?, 'CRM_ACCOUNT_MISSING', 'lead', ?, ?, ?, 'ERROR', 'OPEN', ?,\n"
        "        UTC_TIMESTAMP(), UTC_TIMESTAMP())\n"
        "ON DUPLICATE KEY UPDATE occurrences = occurrences + 1,\n"
        "        last_seen_at = UTC_TIMESTAMP(), detail_json = VALUES(detail_json)"),
        8, branch=3, offset=1,
        replacements='={{ "CRM_ACCOUNT_MISSING:lead:" + $json.lead_id }},={{ $json.lead_id }},'
                     '={{ $json.lead_id }},={{ $json.country_iso }},'
                     '={{ JSON.stringify({http: $json.http_status, detail: $json.error_detail, request_key: $json.request_key}) }}',
        on_error='continueRegularOutput', retry=2)
    W.chain(amb, amb_issue)

    ev_failed = W.mysql('[DB] Record Event Account Failed', (
        "INSERT IGNORE INTO wf_events\n"
        "  (event_key, event_type, event_domain, occurred_at, lead_id, country_iso,\n"
        "   conversation_id, provider, result, source_workflow, execution_id, metadata_json)\n"
        "VALUES (?, 'ACCOUNT_FAILED', 'TOOL', UTC_TIMESTAMP(), ?, ?, ?, ?, ?, 'WF3', ?, ?)"),
        8, branch=5,
        replacements='={{ "ACCOUNT_FAILED:" + $json.lead_id + ":" + $json.request_ref }},'
                     '={{ $json.lead_id }},={{ $json.country_iso }},'
                     '={{ $json.conversation_id }},={{ $json.tool_provider }},'
                     '={{ $json.error_code }},={{ $json.execution_id }},'
                     '={{ JSON.stringify({http: $json.http_status, detail: $json.error_detail}) }}',
        on_error='continueRegularOutput', retry=2)
    W.link(st_sw, ev_failed, 3)

    close = W.mysql('[DB] Close Tool Request', (
        "UPDATE wf_tool_requests\n"
        "   SET state = ?, result_ref = ?, error_code = ?, error_message = ?,\n"
        "       completed_at = UTC_TIMESTAMP()\n"
        " WHERE request_key = ? AND state = 'CLAIMED'"),
        9, replacements='={{ $json.account_status === "CREATED" ? "SUCCEEDED" '
                        ': ($json.account_status === "ALREADY_EXISTS" ? "ALREADY_EXISTS" : "FAILED") }},'
                        '={{ $json.account_id }},={{ $json.error_code }},'
                        '={{ $json.error_detail }},={{ $json.request_key }}',
        on_error='continueRegularOutput', retry=2)
    for n in (ev_created, ev_exists, ev_failed):
        W.link(n, close)
    W.link(amb_issue, close)

    note = W.http('[CRM] Post Account Note', 'POST',
                  CRM + '/api/leads/{{ $json.lead_id }}/followups', 9, offset=1,
                  headers={'Content-Type': 'application/json',
                           'Authorization': '=Bearer {{ $json.access_token }}'},
                  body_json='={{ JSON.stringify({ type: "NOTE", notes: '
                            '$json.crm_note.slice(0, 900) }) }}',
                  never_error=True, full_response=True, timeout=15000,
                  on_error='continueRegularOutput')
    stage_prep = W.code('[CRM] Prepare Note Context', CORE + r'''
// El nodo MySQL anterior reemplazó el json; se recompone desde el validador.
return $input.all().map((r, i) => ({
  json: $('[TOOL] Validate Account Response').itemMatching(i).json
}));
''', 9, offset=1)
    W.conns.pop('[DB] Close Tool Request', None)
    W.chain(close, stage_prep)
    W.conns['[CRM] Post Account Note'] = W.conns.get('[CRM] Post Account Note', {})
    W.link(stage_prep, note)

    stage = W.http('[CRM] Patch Lead Stage', 'PATCH',
                   CRM + '/api/leads/{{ $json.lead_id }}', 9, offset=3,
                   headers={'Content-Type': 'application/json',
                            'Authorization': '=Bearer {{ $json.access_token }}'},
                   body_json='={{ JSON.stringify({ stage: "INTERESTED" }) }}',
                   never_error=True, full_response=True, timeout=15000,
                   on_error='continueRegularOutput')
    stage_if = W.if_('[CRM] Account Was Opened',
                     '={{ $json.account_status === "CREATED" || $json.account_status === "ALREADY_EXISTS" }}',
                     'true', True, 9, offset=2, single=True)
    stage_ctx = W.code('[CRM] Restore Context After Note', r'''
return $input.all().map((r, i) => ({
  json: $('[TOOL] Validate Account Response').itemMatching(i).json
}));
''', 9, offset=2, branch=1)
    W.chain(note, stage_ctx)
    W.chain(stage_ctx, stage_if)
    W.link(stage_if, stage, 0)

    # ══ 10 NOTIFY ═════════════════════════════════════════════════════
    W.sticky(10, '10 — NOTIFY', """Las credenciales se entregan por los canales
configurados del país. **La contraseña nunca se loguea** ni se guarda en el
evento ni en la nota del CRM.

Telegram usa la credential `Landmark Telegram`; el chat sale de
`route_telegram_targets` (propósito `account`) expuesto en la config del país.
Si el país no tiene destino configurado, no se envía y se deja constancia —
no se cae el workflow.""", color=6, height=380)
    notify_if = W.if_('[TELEGRAM] Should Notify',
                      '={{ $json.account_status === "CREATED" && '
                      '!!($json.tool_config && $json.tool_config.telegram_chat_id) }}',
                      'true', True, 10, single=True)
    W.link(stage_if, notify_if, 1)
    W.link(stage, notify_if)
    tg = W.telegram('[TELEGRAM] Send Account Notification',
                    '={{ $json.tool_config.telegram_chat_id }}',
                    '=<b>New trading account</b>\n'
                    'Client: {{ $json.full_name }}\n'
                    'Lead: {{ $json.lead_id }}\n'
                    'Country: {{ $json.country_iso }}\n'
                    'Market: {{ $json.market }}\n'
                    'Username: {{ $json.username }}\n'
                    'Portal: {{ $json.portal_url }}',
                    10, offset=1)
    W.link(notify_if, tg, 0)

    # ══ 11 RESPOND ════════════════════════════════════════════════════
    W.sticky(11, '11 — RESPOND TO AGENT', """Siempre se responde al agente con un
mensaje de negocio legible **en inglés**, nunca con un 500 ni con un stack
trace. Un tool que devuelve 500 hace que el agente improvise en medio de la
llamada.""", color=4, height=320)
    respond_ctx = W.code('[TOOL] Build Agent Response', CORE + r'''
// La contraseña NO se loguea. Solo se devuelve al canal que la entrega.
return $input.all().map((r, i) => {
  let d;
  try { d = $('[TOOL] Validate Account Response').itemMatching(i).json; }
  catch (e) { d = r.json; }
  lmLog('WF3', d, { lead_id: d.lead_id, country: d.country_iso,
                    conversation_id: d.conversation_id,
                    result: d.account_status, error_code: d.error_code,
                    action: 'ACCOUNT' });
  return { json: {
    success: d.account_status === 'CREATED',
    already_exists: d.account_status === 'ALREADY_EXISTS',
    status: d.account_status,
    message_to_user: d.user_message,
    lead_id: d.lead_id, country: d.country_iso,
    account_id: d.account_id || null,
    username: d.username || null,
    portal_url: d.portal_url || null
  }};
});
''', 11)
    W.link(notify_if, respond_ctx, 1)
    W.link(tg, respond_ctx)

    refuse = W.code('[TOOL] Build Refusal Response', CORE + NOTES + r'''
// Rechazo de negocio (país apagado, tool deshabilitada, config incompleta,
// datos inválidos). Mensaje en inglés, sin filtrar detalles internos al agente.
return $input.all().map(it => {
  const d = it.json;
  const code = d.error_code || 'VALIDATION_ERROR';
  console.log('[WF3][exec=' + (d.execution_id || $execution.id) + '] action=refuse code=' +
              code + ' lead=' + (d.lead_id || '-') + ' country=' + (d.country_iso || '-') +
              ' detail=' + (d.error_detail || d.error_detail === '' ? d.error_detail : '-'));
  return { json: {
    success: false, status: 'REFUSED', error_code: code,
    message_to_user: d.user_message || lmErrorNote(code, null),
    lead_id: d.lead_id || null, country: d.country_iso || null
  }};
});
''', 11, branch=3)
    W.link(p_if, refuse, 1)
    W.link(t_if, refuse, 1)

    dup_resp = W.code('[TOOL] Build Duplicate Response', CORE + r'''
// El claim lo tiene otra ejecución del MISMO pedido: se responde con lo que ya
// quedó registrado en vez de crear una segunda cuenta.
return $input.all().map(it => {
  const d = it.json;
  console.log('[WF3] pedido duplicado ' + d.request_key + ' (estado ' +
              d.existing_state + '): no se crea otra cuenta');
  return { json: {
    success: d.existing_state === 'SUCCEEDED',
    already_exists: d.existing_state === 'ALREADY_EXISTS',
    status: 'DUPLICATE_REQUEST', duplicate: true,
    message_to_user: 'Your request is already being processed.',
    lead_id: d.lead_id, country: d.country_iso,
    account_id: d.existing_ref || null
  }};
});
''', 11, branch=5)
    W.link(claim_if, dup_resp, 1)

    out = W.respond('[TOOL] Respond To Agent', 12,
                    body='={{ JSON.stringify($json) }}')
    for n in (respond_ctx, refuse, dup_resp):
        W.link(n, out)
    return W
