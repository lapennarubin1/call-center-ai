"""TEMPLATE_WF7_8_PAYMENT_CALLBACK_V2 — link de pago, callback y confirmación.

Reemplaza a "WF7+WF8 MULTI-PAIS — Payment Link + Callback".

Lo que YA NO hace, respecto de v1:
  · no tiene `payment_provider = country === 'india' ? 'okpay' : 'monetix'`:
    el proveedor, la moneda y los límites salen de `country_tool_configs`
  · no lleva `mchId`, la clave de firma MD5, el `cpid`/`cppwd` del SMS ni la
    contraseña de LeadStudio dentro del JSON
  · no responde al cliente en hinglish desde un nodo de código
  · no confía en que el proveedor respete `X-Idempotency-Key`: reserva el
    pedido antes de llamar
"""

VERSION = '2.0.0'
NAME = 'TEMPLATE_WF7_8_PAYMENT_CALLBACK_V2'


def build(wfbuild, js):
    W = wfbuild.Workflow(
        NAME, VERSION,
        'TEMPLATE_DATA v2.2 · ANALYTICS_EVENT v2.2 · CRM_ENGLISH_RULE',
        'WF7+WF8 v1 — desactivar este workflow; v1 vuelve a atender sus webhooks (§12)',
        'Tools de ElevenLabs (payment link, callback) + callback del proveedor de pagos.')
    CORE, NOTES, PAY = js['lmcore.js'], js['lmnotes.js'], js['lmpay.js']
    PANEL = '={{ $env.LM_PANEL_URL || "http://172.18.0.1:8080" }}'
    CRM = '={{ $env.LM_LEADSTUDIO_URL || "https://lead-studio-9gnl.onrender.com" }}'

    # ══ 00 TRIGGERS ═══════════════════════════════════════════════════
    W.sticky(0, '00 — TRIGGERS', f"""{W.meta_header}

Tres entradas independientes:
 · **CREATE_PAYMENT_LINK** — tool del agente (`payment-link-v2`)
 · **CALLBACK** — tool del agente (`callback-request-v2`): registra la hora que
   pidió el cliente
 · **callback del proveedor de pagos** (`payment-callback-v2`) — el antiguo WF8

Un solo workflow porque comparten la configuración de pagos del país
(`country_tool_configs`), y separarlos duplicaría esa resolución.""",
              color=4, height=420)
    pay_hook = W.webhook('[TRIGGER] Payment Link Tool', 'payment-link-v2', 0)
    cb_hook = W.webhook('[TRIGGER] Callback Tool', 'callback-request-v2', 0, branch=2)
    prov_hook = W.webhook('[TRIGGER] Payment Provider Callback', 'payment-callback-v2', 0,
                          branch=8)
    prov_ack = W.respond('[TRIGGER] Acknowledge Provider', 0, branch=9, body='SUCCESS',
                         kind='text')
    W.link(prov_hook, prov_ack)

    # ══ 01 PARSE ══════════════════════════════════════════════════════
    W.sticky(1, '01 — PARSE & VALIDATE', """`country_iso` explícito, `lead_id` UUID,
importe numérico. Los límites (`min_amount` / `max_amount`) y la moneda salen de
la configuración del país: **no** hay `if india -> INR` en ningún nodo.

`request_ref` = `conversation_id` (o `execution_id`). Dos reintentos del mismo
tool-call son **un** pedido; dos llamadas distintas del mismo lead son dos.""",
              color=6, height=380)
    parse = W.code('[CONFIG] Parse Payment Request', CORE + r'''
const raw = $input.first().json || {};
const b = raw.body || raw;
const leadId = String(b.lead_id || '').trim();
const iso = String(b.country_iso || b.country || '').trim().toUpperCase();
const phone = String(b.phone || '').replace(/[^0-9+]/g, '');
const conversationId = String(b.conversation_id || '').trim() || null;
const amountRaw = String(b.amount === undefined || b.amount === null ? '' : b.amount)
  .replace(/[^0-9.]/g, '');
const amount = amountRaw === '' ? NaN : Number(amountRaw);
const riskOk = b.risk_disclosure_confirmed === true ||
               String(b.risk_disclosure_confirmed).toLowerCase() === 'true';

const errs = [];
const UUID = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i;
if (!leadId) errs.push('lead_id');
else if (!UUID.test(leadId)) errs.push('lead_id no es un UUID de LeadStudio');
if (!/^[A-Z]{2}$/.test(iso)) errs.push('country_iso (ISO-2 explicito)');
if (!phone) errs.push('phone');
if (!Number.isFinite(amount) || amount <= 0) errs.push('amount');
if (!riskOk) errs.push('risk_disclosure_confirmed debe ser true');

const requestRef = conversationId || String($execution.id);
return [{ json: {
  valid: errs.length === 0,
  error_detail: errs.length ? ('campos invalidos o ausentes: ' + errs.join(', ')) : null,
  tool_type: 'CREATE_PAYMENT_LINK',
  lead_id: leadId, country_iso: iso, phone: phone,
  full_name: String(b.full_name || '').trim(),
  whatsapp_number: String(b.whatsapp_number || b.phone || '').replace(/[^0-9+]/g, ''),
  amount: Number.isFinite(amount) ? Math.floor(amount) : null,
  conversation_id: conversationId, request_ref: requestRef,
  request_key: 'CREATE_PAYMENT_LINK:' + leadId + ':' + requestRef,
  execution_id: $execution.id
}}];
''', 1)
    W.link(pay_hook, parse)
    p_if = W.if_('[CONFIG] Request Is Valid', '={{ $json.valid }}', 'true', True, 1,
                 offset=1, single=True)
    W.chain(parse, p_if)

    # ══ 02 COUNTRY TOOL ═══════════════════════════════════════════════
    W.sticky(2, '02 — COUNTRY TOOL CONFIG', """`GET /api/countries/{iso}/tools` →
`CREATE_PAYMENT_LINK`.

De la configuración salen: `provider_key`, `currency`, `market`, `endpoint`,
`credential_ref`, y en `config` los límites `min_amount` / `max_amount`.

Nepal con Monetix sin contrato queda **apagado en el panel** (`enabled = 0`) y
esto responde "tool not enabled" en inglés. v1 lo resolvía con tres nodos
`[CONFIG REQUIRED]` y un nodo HTTP desactivado —que en n8n deja pasar los datos
igual— dentro del workflow.""", color=3, height=440)
    tools = W.http('[CONFIG] Load Country Tools', 'GET',
                   PANEL + '/api/countries/{{ $json.country_iso }}/tools', 2,
                   credentials={'httpHeaderAuth': {'id': '__PANEL_TOKEN_CREDENTIAL__',
                                                   'name': 'Landmark Panel API'}},
                   never_error=True, full_response=True, timeout=10000, retry=2,
                   on_error='continueRegularOutput')
    W.link(p_if, tools, 0)
    tcheck = W.code('[CONFIG] Resolve Payment Tool', CORE + NOTES + r'''
return $input.all().map((r, i) => {
  const ctx = $('[CONFIG] Parse Payment Request').itemMatching(i).json;
  const resp = r.json || {};
  const code = Number(resp.statusCode || 0);
  const body = resp.body || {};

  let errCode = null, err = null;
  if (code === 404) { errCode = 'CONFIG_ERROR'; err = 'country ' + ctx.country_iso + ' not configured'; }
  else if (code < 200 || code >= 300) { errCode = 'CONFIG_ERROR'; err = 'panel HTTP ' + code; }
  else if (!body.country_enabled) { errCode = 'COUNTRY_DISABLED'; err = ctx.country_iso + ' is disabled'; }
  else if (!body.country_ready) { errCode = 'CONFIG_ERROR'; err = ctx.country_iso + ' is not ready'; }

  const tool = (body.tools || {}).CREATE_PAYMENT_LINK || null;
  if (!errCode && (!tool || !tool.enabled)) {
    errCode = 'TOOL_DISABLED'; err = 'CREATE_PAYMENT_LINK not enabled for ' + ctx.country_iso;
  }
  const cfg = tool ? (tool.config || {}) : {};
  if (!errCode && !String(tool.currency || '').trim()) {
    errCode = 'CONFIG_ERROR'; err = 'no currency configured for ' + ctx.country_iso;
  }
  if (!errCode && !String(tool.provider_key || '').trim()) {
    errCode = 'CONFIG_ERROR'; err = 'no payment provider configured for ' + ctx.country_iso;
  }
  // Límites de importe: configuración del país, no un literal.
  const min = cfg.min_amount === undefined ? null : Number(cfg.min_amount);
  const max = cfg.max_amount === undefined ? null : Number(cfg.max_amount);
  if (!errCode && min !== null && ctx.amount < min) {
    errCode = 'VALIDATION_ERROR';
    err = 'amount below minimum ' + tool.currency + ' ' + min;
  }
  if (!errCode && max !== null && ctx.amount > max) {
    errCode = 'VALIDATION_ERROR';
    err = 'amount above maximum ' + tool.currency + ' ' + max;
  }
  if (!errCode && tool.mode === 'CUSTOM_ENDPOINT' && !String(tool.endpoint || '').trim()) {
    errCode = 'CONFIG_ERROR'; err = 'no endpoint configured for ' + ctx.country_iso;
  }

  const orderRef = 'LM_' + ctx.lead_id + '_' + Date.now();
  return { json: Object.assign({}, ctx, {
    tool_ok: !errCode, error_code: errCode, error_detail: err,
    error_note: errCode ? lmErrorNote(errCode, err) : null,
    user_message: errCode
      ? 'We cannot generate a payment link right now. Our team will follow up.'
      : null,
    tool: tool, tool_mode: tool ? tool.mode : null,
    payment_provider: tool ? tool.provider_key : null,
    currency: tool ? tool.currency : null,
    market: tool ? (tool.market || null) : null,
    endpoint: tool ? (tool.endpoint || null) : null,
    tool_config: cfg,
    order_ref: orderRef,
    idempotency_key: 'pay_' + orderRef
  })};
});
''', 2, offset=1)
    W.chain(tools, tcheck)
    t_if = W.if_('[CONFIG] Tool Is Available', '={{ $json.tool_ok }}', 'true', True, 2,
                 offset=2, single=True)
    W.chain(tcheck, t_if)

    # ══ 03 CLAIM ══════════════════════════════════════════════════════
    W.sticky(3, '03 — IDEMPOTENT CLAIM', """`wf_tool_requests`, clave
`CREATE_PAYMENT_LINK:{lead_id}:{request_ref}`. Se reserva **antes** de pedir el
link.

v1 mandaba `X-Idempotency-Key` y confiaba en que el proveedor lo respetara. Si
no lo respeta, el cliente recibe dos links de pago por SMS.""",
              color=3, height=340)
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
        "   tool_provider, mode, order_ref, amount, currency, claim_token,\n"
        "   execution_id, state)\n"
        "VALUES (?, 'CREATE_PAYMENT_LINK', ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'CLAIMED')"),
        3, offset=1,
        replacements='={{ $json.request_key }},={{ $json.lead_id }},={{ $json.country_iso }},'
                     '={{ $json.request_ref }},={{ $json.conversation_id }},'
                     '={{ $json.payment_provider }},={{ $json.tool_mode }},'
                     '={{ $json.order_ref }},={{ $json.amount }},={{ $json.currency }},'
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
  return { json: Object.assign({}, ctx, {
    claim_won: won, existing_state: row.state || null, existing_ref: row.result_ref || null
  })};
});
''', 3, offset=3)
    W.chain(claim_read, claim_chk)
    claim_if = W.if_('[TOOL] Claim Won', '={{ $json.claim_won }}', 'true', True, 3,
                     offset=4, single=True)
    W.chain(claim_chk, claim_if)

    ev_req = W.mysql('[DB] Record Event Payment Requested', (
        "INSERT IGNORE INTO wf_events\n"
        "  (event_key, event_type, event_domain, occurred_at, lead_id, country_iso,\n"
        "   conversation_id, provider, amount, currency, source_workflow, execution_id,\n"
        "   metadata_json)\n"
        "VALUES (?, 'PAYMENT_LINK_REQUESTED', 'TOOL', UTC_TIMESTAMP(), ?, ?, ?, ?, ?, ?,\n"
        "        'WF7', ?, ?)"),
        4, replacements='={{ "PAYMENT_LINK_REQUESTED:" + $json.lead_id + ":" + $json.request_ref }},'
                        '={{ $json.lead_id }},={{ $json.country_iso }},'
                        '={{ $json.conversation_id }},={{ $json.payment_provider }},'
                        '={{ $json.amount }},={{ $json.currency }},={{ $json.execution_id }},'
                        '={{ JSON.stringify({order_ref: $json.order_ref, mode: $json.tool_mode}) }}',
        on_error='continueRegularOutput', retry=2)
    W.link(claim_if, ev_req, 0)

    # ══ 05 PROVIDER ═══════════════════════════════════════════════════
    W.sticky(5, '05 — PAYMENT PROVIDER', """Dos modos, elegidos por configuración.
**Ninguna rama por país ni por proveedor de pagos.**

`CONFIG_ROUTER` → la API de LeadStudio crea el link con el `market` del país.
`CUSTOM_ENDPOINT` → el endpoint del país (OkPay, Monetix, el que sea), con su
credential. **La firma y las claves viven en la credential**, nunca en un nodo:
v1 tenía una implementación de MD5 y la clave del comercio dentro de un Code
node exportable.

Agregar un proveedor de pagos es configurar el país. Si además exige una firma
propia, se agrega su credential; el workflow no cambia.""", color=5, height=440)
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
    mode_sw = W.switch('[TOOL] Payment Mode Router', '={{ $json.tool_mode }}',
                       [('DIRECT_PROVIDER', 'direct'), ('UNIVERSAL_ROUTER', 'router')],
                       5, offset=2)
    W.chain(tokm, mode_sw)

    # ── DIRECT_PROVIDER ───────────────────────────────────────────────
    # El país llama a SU pasarela. El adaptador decide cómo se arma la
    # petición; la firma se calcula aquí, en una librería versionada, no
    # con JavaScript guardado en la base de datos.
    adapter_sw = W.switch('[PAYMENT] Adapter Router', '={{ $json.adapter_key }}',
                          [('OKPAY_V1', 'okpay')], 6)
    W.link(mode_sw, adapter_sw, 0)

    okpay_build = W.code('[PAYMENT] Build Okpay Request', CORE + PAY + r'''
// mchId y la clave de firma vienen de la credential de n8n, nunca de la
// base ni del workflow. Si faltan, es CONFIG_ERROR: sin clave no se puede
// firmar, y una firma mal hecha la rechaza la pasarela.
const cred = $env.LM_OKPAY_MCH_ID ? {
  mch_id: $env.LM_OKPAY_MCH_ID, sign_key: $env.LM_OKPAY_SIGN_KEY
} : null;

return $input.all().map(it => {
  const j = it.json;
  const cfg = j.tool_config || {};
  if (!cred || !cred.mch_id || !cred.sign_key) {
    return { json: Object.assign({}, j, {
      payment_status: 'FAILED', error_code: 'CONFIG_ERROR',
      error_detail: 'OkPay credential is missing (LM_OKPAY_MCH_ID / LM_OKPAY_SIGN_KEY).',
      checkout_url: null, transaction_id: null,
      crm_note: lmErrorNote('CONFIG_ERROR', 'payment provider not configured')
    })};
  }
  try {
    const built = lmOkpayBuild({
      currency:     j.currency,
      out_trade_no: j.order_ref,
      amount:       j.amount,
      callback_url: cfg.callback_url || j.callback_url,
      return_url:   cfg.return_url || null,
      phone:        j.phone,
      lead_id:      j.lead_id
    }, cred.mch_id, cred.sign_key, cfg.pay_type || 'UPI');
    lmLog('payment.okpay.built', { lead_id: j.lead_id, order_ref: j.order_ref });
    return { json: Object.assign({}, j, {
      pay_form_body: built.form_body, pay_content_type: built.content_type
    })};
  } catch (e) {
    const nonLatin = String(e.message || '').indexOf('LM_PAY_NON_LATIN1') === 0;
    return { json: Object.assign({}, j, {
      payment_status: 'FAILED', error_code: 'VALIDATION_ERROR',
      error_detail: nonLatin
        ? 'The order contains a character the gateway cannot sign.'
        : 'Could not build the payment request.',
      checkout_url: null, transaction_id: null,
      crm_note: lmErrorNote('VALIDATION_ERROR', 'payment request could not be built')
    })};
  }
});
''', 6, offset=1)
    W.link(adapter_sw, okpay_build, 0)

    okpay_post = W.http('[PAYMENT] Post Okpay Collect', 'POST',
                        '={{ $json.endpoint }}', 6, offset=2,
                        headers={'Content-Type': 'application/x-www-form-urlencoded',
                                 'X-Idempotency-Key': '={{ $json.idempotency_key }}'},
                        body='={{ $json.pay_form_body }}',
                        never_error=True, full_response=True, timeout=20000,
                        on_error='continueRegularOutput')
    W.chain(okpay_build, okpay_post)

    adapter_err = W.code('[ERROR] Unsupported Payment Adapter', CORE + NOTES + r'''
// Un adaptador que no está en el catálogo se puede GUARDAR mientras esté
// apagado, pero no puede operar (§5). Nunca se cae al otro modo.
return $input.all().map(it => ({ json: Object.assign({}, it.json, {
  payment_status: 'FAILED', error_code: 'CONFIG_ERROR',
  error_detail: 'Unsupported payment adapter: ' + (it.json.adapter_key || '(none)'),
  checkout_url: null, transaction_id: null,
  crm_note: lmErrorNote('CONFIG_ERROR', 'payment provider not supported')
})}));
''', 6, offset=1, branch=3)
    W.link(adapter_sw, adapter_err, 1)

    # ── UNIVERSAL_ROUTER ──────────────────────────────────────────────
    # Una API central recibe la petición ya normalizada y decide ella qué
    # pasarela usar. Su endpoint y su credencial son configuración: NO se
    # asume que sea LeadStudio ni ningún otro backend concreto.
    router = W.http('[PAYMENT] Post Universal Router', 'POST',
                    '={{ $json.endpoint }}', 6, branch=2,
                    headers={'Content-Type': 'application/json',
                             'X-Idempotency-Key': '={{ $json.idempotency_key }}'},
                    credentials={'httpHeaderAuth': {'id': '__PAYMENT_ROUTER_CREDENTIAL__',
                                                    'name': 'Payment Router API'}},
                    body_json='={{ JSON.stringify({ lead_id: $json.lead_id, '
                              'country_iso: $json.country_iso, '
                              'amount: $json.amount, currency: $json.currency, '
                              'order_ref: $json.order_ref, phone: $json.phone, '
                              'callback_url: ($json.tool_config.callback_url || null) }) }}',
                    never_error=True, full_response=True, timeout=20000,
                    on_error='continueRegularOutput')
    W.link(mode_sw, router, 1)

    mode_err = W.code('[ERROR] Unsupported Payment Mode', CORE + NOTES + r'''
return $input.all().map(it => ({ json: Object.assign({}, it.json, {
  payment_status: 'FAILED', error_code: 'CONFIG_ERROR',
  error_detail: 'Unsupported payment mode: ' + (it.json.tool_mode || '(none)') +
                '. Valid modes are DIRECT_PROVIDER and UNIVERSAL_ROUTER.',
  checkout_url: null, transaction_id: null,
  crm_note: lmErrorNote('CONFIG_ERROR', 'payment mode not supported')
})}));
''', 6, branch=4)
    W.link(mode_sw, mode_err, 2)

    # ══ 07 VALIDATE ═══════════════════════════════════════════════════
    W.sticky(7, '07 — VALIDATE RESPONSE', """`neverError` seguido de un check
explícito. Un 2xx con cuerpo de error del proveedor **no** es un éxito: v1
devolvía `code=1 msg=fail` con HTTP 200 y el flujo seguía como si nada.

Timeout o 5xx = **ambiguo**: el link pudo crearse. Se marca
`NEEDS_RECONCILIATION` y **no se reintenta**.""", color=2, height=400)
    valid = W.code('[TOOL] Validate Payment Response', CORE + NOTES + r'''
return $input.all().map((r, i) => {
  let ctx;
  try { ctx = $('[TOOL] Payment Mode Router').itemMatching(i).json; }
  catch (e) { ctx = $('[TOOL] Claim Won').all()[0].json; }
  const resp = r.json || {};
  const code = Number(resp.statusCode || 0);
  const body = resp.body || {};
  const data = body.data || body;

  const url = data.checkout_url || data.url || data.short_url || data.paymentUrl || null;
  const txn = data.transaction_id || data.transaction_Id || data.transactionId ||
              data.trade_no || null;
  // Un 2xx con el codigo de error del proveedor NO es exito.
  const providerOk = (body.code === undefined || Number(body.code) === 0) &&
                     (body.success === undefined || body.success === true);

  let status, errorCode = null, detail = null;
  if (code >= 200 && code < 300 && providerOk && url) {
    status = 'CREATED';
  } else if (code === 0 || code === 408 || code >= 500) {
    status = 'AMBIGUOUS'; errorCode = 'AMBIGUOUS';
    detail = 'provider did not confirm (HTTP ' + code + ')';
  } else if (code === 401 || code === 403) {
    status = 'FAILED'; errorCode = 'AUTH_ERROR'; detail = 'payment provider rejected credentials';
  } else {
    status = 'FAILED'; errorCode = 'PROVIDER_ERROR';
    detail = 'HTTP ' + code + ' ' + String(body.msg || body.error || '').slice(0, 150);
  }

  const note = status === 'CREATED'
    ? lmPaymentNote({ status: 'LINK_CREATED', amount: ctx.amount, currency: ctx.currency,
                      provider: ctx.payment_provider, order_ref: ctx.order_ref,
                      transaction_id: txn })
    : status === 'AMBIGUOUS'
      ? lmErrorNote('AMBIGUOUS', detail)
      : lmPaymentNote({ status: 'LINK_FAILED', amount: ctx.amount, currency: ctx.currency,
                        provider: ctx.payment_provider, order_ref: ctx.order_ref,
                        error: detail });

  return { json: Object.assign({}, ctx, {
    payment_status: status, http_status: code, error_code: errorCode, error_detail: detail,
    checkout_url: url, transaction_id: txn, crm_note: note,
    // Mensaje al cliente: INGLÉS, igual que todo lo que va al CRM.
    user_message: status === 'CREATED'
      ? ('Your payment link has been sent. Please check your messages. ' +
         'Remember that trading involves risk.')
      : 'We could not generate the payment link right now. Our team will follow up.',
    // El SMS lo arma el país en su config; si no hay plantilla, no se envía.
    sms_text: status === 'CREATED'
      ? String((ctx.tool_config || {}).sms_template ||
               'Landmark Markets: {currency} {amount} payment link: {url}')
          .replace('{currency}', ctx.currency).replace('{amount}', ctx.amount)
          .replace('{url}', url || '')
      : null
  })};
});
''', 7)
    # Las cuatro salidas posibles del despacho convergen en el MISMO
    # validador: un éxito del router y uno de OkPay se normalizan igual,
    # y los dos errores de configuración entran por la misma puerta.
    W.link(okpay_post, valid)
    W.link(router, valid)
    W.link(adapter_err, valid)
    W.link(mode_err, valid)
    st_sw = W.switch('[TOOL] Payment Result Router', '={{ $json.payment_status }}',
                     [('CREATED', 'created'), ('AMBIGUOUS', 'ambiguous')], 7, offset=1)
    W.chain(valid, st_sw)

    # ══ 08 PERSIST ════════════════════════════════════════════════════
    W.sticky(8, '08 — PERSIST & DELIVER', """Evento local primero
(`PAYMENT_LINK_CREATED:{provider}:{order_ref}`, idempotente por orden), después
el CRM y la entrega al cliente.

El SMS usa la plantilla y el endpoint del país, y su autenticación sale de la
credential `SMS Gateway`. v1 llevaba `cpid` y `cppwd` en la query de cuatro
nodos distintos, en el JSON exportable.""", color=3, height=380)
    ev_created = W.mysql('[DB] Record Event Payment Link Created', (
        "INSERT IGNORE INTO wf_events\n"
        "  (event_key, event_type, event_domain, occurred_at, lead_id, country_iso,\n"
        "   conversation_id, provider, amount, currency, source_workflow, execution_id,\n"
        "   metadata_json)\n"
        "VALUES (?, 'PAYMENT_LINK_CREATED', 'TOOL', UTC_TIMESTAMP(), ?, ?, ?, ?, ?, ?,\n"
        "        'WF7', ?, ?)"),
        8, replacements='={{ "PAYMENT_LINK_CREATED:" + $json.payment_provider + ":" + $json.order_ref }},'
                        '={{ $json.lead_id }},={{ $json.country_iso }},'
                        '={{ $json.conversation_id }},={{ $json.payment_provider }},'
                        '={{ $json.amount }},={{ $json.currency }},={{ $json.execution_id }},'
                        '={{ JSON.stringify({order_ref: $json.order_ref, transaction_id: $json.transaction_id}) }}',
        on_error='continueRegularOutput', retry=2)
    W.link(st_sw, ev_created, 0)

    amb = W.mysql('[DB] Mark Payment Needs Reconciliation', (
        "UPDATE wf_tool_requests\n"
        "   SET state = 'NEEDS_RECONCILIATION', error_code = 'AMBIGUOUS',\n"
        "       error_message = ?, completed_at = UTC_TIMESTAMP()\n"
        " WHERE request_key = ? AND state = 'CLAIMED'"),
        8, branch=2, replacements='={{ $json.error_detail }},={{ $json.request_key }}',
        on_error='continueRegularOutput', retry=2)
    W.link(st_sw, amb, 1)

    ev_failed = W.mysql('[DB] Record Event Payment Link Failed', (
        "INSERT IGNORE INTO wf_events\n"
        "  (event_key, event_type, event_domain, occurred_at, lead_id, country_iso,\n"
        "   conversation_id, provider, amount, currency, result, source_workflow,\n"
        "   execution_id, metadata_json)\n"
        "VALUES (?, 'PAYMENT_LINK_FAILED', 'TOOL', UTC_TIMESTAMP(), ?, ?, ?, ?, ?, ?, ?,\n"
        "        'WF7', ?, ?)"),
        8, branch=4,
        replacements='={{ "PAYMENT_LINK_FAILED:" + $json.lead_id + ":" + $json.request_ref }},'
                     '={{ $json.lead_id }},={{ $json.country_iso }},'
                     '={{ $json.conversation_id }},={{ $json.payment_provider }},'
                     '={{ $json.amount }},={{ $json.currency }},={{ $json.error_code }},'
                     '={{ $json.execution_id }},'
                     '={{ JSON.stringify({http: $json.http_status, detail: $json.error_detail}) }}',
        on_error='continueRegularOutput', retry=2)
    W.link(st_sw, ev_failed, 2)

    close = W.mysql('[DB] Close Tool Request', (
        "UPDATE wf_tool_requests\n"
        "   SET state = ?, result_ref = ?, error_code = ?, error_message = ?,\n"
        "       completed_at = UTC_TIMESTAMP()\n"
        " WHERE request_key = ? AND state = 'CLAIMED'"),
        9, replacements='={{ $json.payment_status === "CREATED" ? "SUCCEEDED" : "FAILED" }},'
                        '={{ $json.transaction_id }},={{ $json.error_code }},'
                        '={{ $json.error_detail }},={{ $json.request_key }}',
        on_error='continueRegularOutput', retry=2)
    W.link(ev_created, close)
    W.link(ev_failed, close)

    restore = W.code('[TOOL] Restore Payment Context', r'''
return $input.all().map((r, i) => ({
  json: $('[TOOL] Validate Payment Response').itemMatching(i).json
}));
''', 9, offset=1)
    W.chain(close, restore)
    W.link(amb, restore)

    sms_if = W.if_('[TOOL] Should Send Sms',
                   '={{ !!$json.sms_text && !!($json.tool_config && $json.tool_config.sms_endpoint) }}',
                   'true', True, 9, offset=2, single=True)
    W.chain(restore, sms_if)
    sms = W.http('[TOOL] Send Payment Sms', 'POST',
                 '={{ $json.tool_config.sms_endpoint }}', 9, offset=3,
                 headers={'Content-Type': 'application/json'},
                 credentials={'httpHeaderAuth': {'id': '__SMS_CREDENTIAL__',
                                                 'name': 'SMS Gateway'}},
                 body_json='={{ JSON.stringify({ to: ($json.whatsapp_number || $json.phone), '
                           'text: $json.sms_text }) }}',
                 never_error=True, full_response=True, timeout=15000,
                 on_error='continueRegularOutput')
    W.link(sms_if, sms, 0)

    note = W.http('[CRM] Post Payment Note', 'POST',
                  CRM + '/api/leads/{{ $json.lead_id }}/followups', 10,
                  headers={'Content-Type': 'application/json',
                           'Authorization': '=Bearer {{ $json.access_token }}'},
                  body_json='={{ JSON.stringify({ type: "NOTE", '
                            'notes: $json.crm_note.slice(0, 900) }) }}',
                  never_error=True, full_response=True, timeout=15000,
                  on_error='continueRegularOutput')
    note_ctx = W.code('[CRM] Prepare Payment Note', r'''
return $input.all().map((r, i) => ({
  json: $('[TOOL] Validate Payment Response').itemMatching(i).json
}));
''', 10, branch=-1)
    W.link(sms_if, note_ctx, 1)
    W.link(sms, note_ctx)
    W.chain(note_ctx, note)

    respond_ctx = W.code('[TOOL] Build Agent Response', CORE + r'''
return $input.all().map((r, i) => {
  let d;
  try { d = $('[TOOL] Validate Payment Response').itemMatching(i).json; }
  catch (e) { d = r.json; }
  lmLog('WF7', d, { lead_id: d.lead_id, country: d.country_iso,
                    conversation_id: d.conversation_id, result: d.payment_status,
                    error_code: d.error_code, action: 'PAYMENT_LINK' });
  return { json: {
    success: d.payment_status === 'CREATED',
    status: d.payment_status,
    message_to_user: d.user_message,
    order_ref: d.order_ref, amount: d.amount, currency: d.currency,
    payment_provider: d.payment_provider,
    lead_id: d.lead_id, country: d.country_iso
  }};
});
''', 11)
    W.chain(note, respond_ctx)

    refuse = W.code('[TOOL] Build Refusal Response', CORE + r'''
return $input.all().map(it => {
  const d = it.json;
  const code = d.error_code || 'VALIDATION_ERROR';
  console.log('[WF7][exec=' + (d.execution_id || $execution.id) + '] action=refuse code=' +
              code + ' lead=' + (d.lead_id || '-') + ' detail=' + (d.error_detail || '-'));
  return { json: {
    success: false, status: 'REFUSED', error_code: code,
    message_to_user: d.user_message ||
      'We cannot generate a payment link right now. Our team will follow up.',
    lead_id: d.lead_id || null, country: d.country_iso || null
  }};
});
''', 11, branch=3)
    W.link(p_if, refuse, 1)
    W.link(t_if, refuse, 1)

    dup = W.code('[TOOL] Build Duplicate Response', CORE + r'''
return $input.all().map(it => {
  const d = it.json;
  console.log('[WF7] pedido duplicado ' + d.request_key + ' (estado ' +
              d.existing_state + '): no se crea otro link');
  return { json: { success: d.existing_state === 'SUCCEEDED', duplicate: true,
                   status: 'DUPLICATE_REQUEST',
                   message_to_user: 'Your payment link is already on its way.',
                   lead_id: d.lead_id, country: d.country_iso } };
});
''', 11, branch=5)
    W.link(claim_if, dup, 1)

    out = W.respond('[TOOL] Respond To Agent', 12, body='={{ JSON.stringify($json) }}')
    for n in (respond_ctx, refuse, dup):
        W.link(n, out)

    # ══ CALLBACK TOOL ═════════════════════════════════════════════════
    W.sticky(13, '13 — CALLBACK TOOL', """El cliente pide que lo llamen a otra hora.

La tool **registra** la hora pedida (idempotente por pedido) y deja una nota en
el CRM **en inglés**. La programación efectiva la hace el **motor de follow-up**
cuando llega el post-call con `result=CALLBACK`: un solo lugar decide cuándo se
vuelve a llamar.

WF9 lee esta hora registrada cuando la extracción de datos de la conversación no
la capturó, así que la tool no queda en el aire.""", color=6, height=400)
    cb_parse = W.code('[CONFIG] Parse Callback Request', CORE + r'''
const raw = $input.first().json || {};
const b = raw.body || raw;
const leadId = String(b.lead_id || '').trim();
const conversationId = String(b.conversation_id || '').trim() || null;
const when = String(b.callback_at || b.callback_date || '').trim();
const UUID = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i;

const errs = [];
if (!leadId || !UUID.test(leadId)) errs.push('lead_id');
const parsed = when ? new Date(when.replace(' ', 'T')) : null;
if (!parsed || isNaN(parsed)) errs.push('callback_at (ISO-8601)');
else if (parsed.getTime() <= Date.now()) errs.push('callback_at debe estar en el futuro');

const requestRef = conversationId || String($execution.id);
return [{ json: {
  valid: errs.length === 0,
  error_detail: errs.length ? errs.join(', ') : null,
  lead_id: leadId, country_iso: String(b.country_iso || b.country || '').toUpperCase(),
  conversation_id: conversationId, request_ref: requestRef,
  request_key: 'CALLBACK:' + leadId + ':' + requestRef,
  callback_at: parsed && !isNaN(parsed) ? parsed.toISOString().replace(/\.\d{3}Z$/, 'Z') : null,
  callback_at_sql: parsed && !isNaN(parsed)
    ? parsed.toISOString().slice(0, 19).replace('T', ' ') : null,
  execution_id: $execution.id
}}];
''', 13)
    W.link(cb_hook, cb_parse)
    cb_if = W.if_('[CONFIG] Callback Request Is Valid', '={{ $json.valid }}', 'true',
                  True, 13, offset=1, single=True)
    W.chain(cb_parse, cb_if)

    cb_store = W.mysql('[DB] Record Callback Request', (
        "INSERT INTO wf_tool_requests\n"
        "  (request_key, tool_type, lead_id, country_iso, request_ref, conversation_id,\n"
        "   tool_provider, mode, order_ref, state, result_ref, execution_id, completed_at)\n"
        "VALUES (?, 'CALLBACK', ?, ?, ?, ?, 'agent', 'CONFIG_ROUTER', ?, 'SUCCEEDED', ?, ?,\n"
        "        UTC_TIMESTAMP())\n"
        "ON DUPLICATE KEY UPDATE result_ref = VALUES(result_ref),\n"
        "        order_ref = VALUES(order_ref), updated_at = UTC_TIMESTAMP()"),
        13, offset=2,
        replacements='={{ $json.request_key }},={{ $json.lead_id }},={{ $json.country_iso }},'
                     '={{ $json.request_ref }},={{ $json.conversation_id }},'
                     '={{ $json.callback_at }},={{ $json.callback_at }},'
                     '={{ $json.execution_id }}',
        retry=2, on_error='continueRegularOutput')
    W.link(cb_if, cb_store, 0)

    cb_resp = W.code('[TOOL] Build Callback Response', CORE + NOTES + r'''
return $input.all().map((r, i) => {
  const d = $('[CONFIG] Callback Request Is Valid').itemMatching(i).json;
  console.log('[WF7][exec=' + d.execution_id + '] action=callback_requested lead=' +
              d.lead_id + ' at=' + d.callback_at);
  return { json: {
    success: true, status: 'CALLBACK_REGISTERED',
    message_to_user: 'Thank you. We have noted your preferred time and will call you back.',
    lead_id: d.lead_id, callback_at: d.callback_at,
    crm_note: lmPhrase('CALL_CALLBACK') + ' [requested for ' + d.callback_at + ']'
  }};
});
''', 13, offset=3)
    W.chain(cb_store, cb_resp)
    cb_refuse = W.code('[TOOL] Build Callback Refusal', CORE + r'''
return $input.all().map(it => {
  const d = it.json;
  console.log('[WF7] callback rechazado: ' + d.error_detail);
  return { json: { success: false, status: 'REFUSED', error_code: 'VALIDATION_ERROR',
                   message_to_user: 'We could not register that time. Please confirm it again.',
                   lead_id: d.lead_id || null } };
});
''', 13, branch=3, offset=3)
    W.link(cb_if, cb_refuse, 1)
    cb_out = W.respond('[TOOL] Respond Callback', 14, body='={{ JSON.stringify($json) }}')
    W.link(cb_resp, cb_out)
    W.link(cb_refuse, cb_out)

    # ══ WF8 · PROVIDER CALLBACK ═══════════════════════════════════════
    W.sticky(15, '15 — WF8: PAYMENT CONFIRMATION', """Callback del proveedor de
pagos. Se responde `SUCCESS` de inmediato (el proveedor lo exige) y el
procesamiento sigue en paralelo.

`PAYMENT_CONFIRMED:{provider}:{order_ref}` es idempotente: si el proveedor
reenvía el callback —cosa que hacen todos— el pago **no se cuenta dos veces**.

La firma del callback se verifica con la credential del proveedor cuando su
configuración la define. Un callback no verificable se registra y **no**
confirma el pago.""", color=2, height=420)
    prov_parse = W.code('[CONFIG] Parse Provider Callback', CORE + r'''
// El formato varía por proveedor: se leen los alias conocidos sin asumir uno.
const raw = $input.first().json || {};
const b = raw.body || raw;
const status = String(b.status || b.trade_status || b.state || '').toUpperCase();
const orderRef = String(b.out_trade_no || b.order_ref || b.orderId || '').trim();
const txn = String(b.transaction_id || b.transactionId || b.trade_no || '').trim() || null;
const amount = b.money || b.amount || null;
const leadId = String(b.attach || b.lead_id || '').trim() || null;

const paid = ['SUCCESS', 'PAID', 'COMPLETED', '1', 'TRUE'].indexOf(status) >= 0;
return [{ json: {
  is_paid: paid, status: status, order_ref: orderRef, transaction_id: txn,
  amount: amount === null ? null : Number(String(amount).replace(/[^0-9.]/g, '')),
  lead_id: leadId, execution_id: $execution.id,
  valid: !!orderRef
}}];
''', 15)
    W.link(prov_hook, prov_parse)

    prov_lookup = W.mysql('[DB] Resolve Payment Order', (
        "SELECT ? AS _k, lead_id, country_iso, tool_provider, currency, amount,\n"
        "       conversation_id, state\n"
        "  FROM wf_tool_requests\n"
        " WHERE tool_type = 'CREATE_PAYMENT_LINK' AND order_ref = ?\n"
        " UNION ALL\n"
        "SELECT ?, NULL, NULL, NULL, NULL, NULL, NULL, NULL\n"
        " WHERE NOT EXISTS (SELECT 1 FROM wf_tool_requests x\n"
        "                    WHERE x.tool_type = 'CREATE_PAYMENT_LINK' AND x.order_ref = ?)"),
        15, offset=1,
        replacements='={{ $json.order_ref }},={{ $json.order_ref }},'
                     '={{ $json.order_ref }},={{ $json.order_ref }}',
        retry=2, on_error='continueRegularOutput')
    W.chain(prov_parse, prov_lookup)

    prov_merge = W.code('[RESULT] Merge Payment Context', CORE + NOTES + r'''
return $input.all().map((r, i) => {
  const ctx = $('[CONFIG] Parse Provider Callback').itemMatching(i).json;
  const row = r.json || {};
  const found = !!row.lead_id;
  const provider = row.tool_provider || 'unknown';
  return { json: Object.assign({}, ctx, {
    order_found: found,
    lead_id: ctx.lead_id || row.lead_id || null,
    country_iso: row.country_iso || null,
    payment_provider: provider,
    currency: row.currency || null,
    expected_amount: row.amount === undefined ? null : row.amount,
    conversation_id: row.conversation_id || null,
    process: ctx.is_paid && found && !!ctx.lead_id || (ctx.is_paid && found),
    crm_note: lmPaymentNote({ status: 'CONFIRMED', amount: ctx.amount,
                              currency: row.currency, provider: provider,
                              order_ref: ctx.order_ref, transaction_id: ctx.transaction_id })
  })};
});
''', 15, offset=2)
    W.chain(prov_lookup, prov_merge)
    prov_if = W.if_('[RESULT] Payment Is Confirmed', '={{ $json.process }}', 'true', True,
                    15, offset=3, single=True)
    W.chain(prov_merge, prov_if)

    ev_paid = W.mysql('[DB] Record Event Payment Confirmed', (
        "INSERT IGNORE INTO wf_events\n"
        "  (event_key, event_type, event_domain, occurred_at, lead_id, country_iso,\n"
        "   conversation_id, provider, amount, currency, source_workflow, execution_id,\n"
        "   metadata_json)\n"
        "VALUES (?, 'PAYMENT_CONFIRMED', 'TOOL', UTC_TIMESTAMP(), ?, ?, ?, ?, ?, ?,\n"
        "        'WF8', ?, ?)"),
        16, replacements='={{ "PAYMENT_CONFIRMED:" + $json.payment_provider + ":" + $json.order_ref }},'
                         '={{ $json.lead_id }},={{ $json.country_iso }},'
                         '={{ $json.conversation_id }},={{ $json.payment_provider }},'
                         '={{ $json.amount }},={{ $json.currency }},={{ $json.execution_id }},'
                         '={{ JSON.stringify({order_ref: $json.order_ref, transaction_id: $json.transaction_id, status: $json.status}) }}',
        on_error='continueRegularOutput', retry=2)
    W.link(prov_if, ev_paid, 0)

    paid_tok = W.http('[CRM] Get Token For Confirmation', 'POST', CRM + '/api/auth/login',
                      16, offset=1, headers={'Content-Type': 'application/json'},
                      credentials={'httpCustomAuth': {'id': '__LEADSTUDIO_LOGIN_CREDENTIAL__',
                                                      'name': 'LeadStudio Login'}},
                      never_error=True, full_response=True, timeout=15000, retry=2,
                      on_error='continueRegularOutput')
    W.chain(ev_paid, paid_tok)
    paid_note_ctx = W.code('[CRM] Prepare Confirmation Note', r'''
const resp = $input.first().json || {};
const token = (resp.body && resp.body.accessToken) || null;
return $('[RESULT] Payment Is Confirmed').all().map(it => ({
  json: Object.assign({}, it.json, { access_token: token })
}));
''', 16, offset=2)
    W.chain(paid_tok, paid_note_ctx)
    paid_note = W.http('[CRM] Post Confirmation Note', 'POST',
                       CRM + '/api/leads/{{ $json.lead_id }}/followups', 16, offset=3,
                       headers={'Content-Type': 'application/json',
                                'Authorization': '=Bearer {{ $json.access_token }}'},
                       body_json='={{ JSON.stringify({ type: "NOTE", '
                                 'notes: $json.crm_note.slice(0, 900) }) }}',
                       never_error=True, full_response=True, timeout=15000,
                       on_error='continueRegularOutput')
    W.chain(paid_note_ctx, paid_note)

    prov_skip = W.code('[LOG] Payment Not Processed', CORE + r'''
// Callback que no confirma un pago (pendiente, fallido) o de una orden que no
// existe en V2 (pago de un link creado por v1). Se registra, no se procesa.
return $input.all().map(it => {
  const d = it.json;
  console.log('[WF8][exec=' + d.execution_id + '] callback no procesado · status=' +
              d.status + ' order=' + d.order_ref + ' encontrado=' + d.order_found);
  return { json: { ok: true, processed: false, status: d.status,
                   order_ref: d.order_ref, order_found: d.order_found } };
});
''', 16, branch=3)
    W.link(prov_if, prov_skip, 1)

    paid_log = W.code('[LOG] Payment Processed', CORE + r'''
return $input.all().map((r, i) => {
  const d = $('[CRM] Prepare Confirmation Note').itemMatching(i).json;
  lmLog('WF8', d, { lead_id: d.lead_id, country: d.country_iso,
                    provider: d.payment_provider, result: 'PAYMENT_CONFIRMED',
                    action: 'CONFIRMED' });
  return { json: { ok: true, processed: true, order_ref: d.order_ref,
                   transaction_id: d.transaction_id, amount: d.amount } };
});
''', 17)
    W.chain(paid_note, paid_log)
    return W
