"""TEMPLATE_WF9_POST_CALL_HANDLER_V2 — resultado final de la llamada.

Reemplaza a "INDIA MULTI-PAIS WF9 — Post-Call Handler".

Lo que YA NO hace, respecto de v1:
  · no identifica el proveedor por `phone.startsWith('+')` — eso era una
    inferencia que se rompe en cuanto un proveedor cambia el formato
  · no deduplica con `$getWorkflowStaticData` (no sobrevive un restart ni se
    comparte entre workers): usa `wf_conversation_ledger`
  · no mapea `FAILED -> NO_ANSWER` de forma global
  · no calcula follow-up: normaliza y llama al motor
  · no descarta en silencio los post-call sin lead_id: abre un issue
"""

VERSION = '2.0.0'
NAME = 'TEMPLATE_WF9_POST_CALL_HANDLER_V2'


def build(wfbuild, js):
    W = wfbuild.Workflow(
        NAME, VERSION,
        'TEMPLATE_DATA v2.2 · FOLLOWUP_ENGINE v2.2 · ANALYTICS_EVENT v2.2',
        'WF9 v1 — desactivar este workflow; v1 vuelve a atender su webhook (§12)',
        'Webhook de ElevenLabs + callback del worker Stringee + polling de respaldo.')
    CORE, ENG, NOTES = js['lmcore.js'], js['lmengine.js'], js['lmnotes.js']
    PANEL = '={{ $env.LM_PANEL_URL || "http://172.18.0.1:8080" }}'
    CRM = '={{ $env.LM_LEADSTUDIO_URL || "https://lead-studio-9gnl.onrender.com" }}'

    # ══ 00 TRIGGERS ═══════════════════════════════════════════════════
    W.sticky(0, '00 — TRIGGERS', f"""{W.meta_header}

Tres entradas, **una sola tubería**:

1. **Webhook de ElevenLabs** (`elevenlabs-postcall-v2`) — la vía normal, con
   `Raw Body` activado para poder verificar la firma HMAC.
2. **Callback del worker Stringee** (`stringee-callback-v2`) — el worker ya
   sabe reenviar el estado final (`sendCallback`), incluido
   `elevenlabs_conversation_id`. Es la vía rápida para Stringee.
3. **Polling de respaldo**, cada 2 min, ventana configurable
   (`postcall_polling_window_minutes`). Cubre los webhooks perdidos.

Que el webhook y el polling vean la misma conversación es **normal**: el ledger
de conversaciones lo resuelve y el segundo termina con `ALREADY_CLAIMED`.""",
              color=4, height=480)
    hook = W.webhook('[TRIGGER] Post Call Webhook', 'elevenlabs-postcall-v2', 0,
                     raw_body=True)
    ack = W.respond('[TRIGGER] Acknowledge Webhook', 0, branch=-2, body='OK', kind='text')
    W.link(hook, ack)

    st_hook = W.webhook('[TRIGGER] Stringee Worker Callback', 'stringee-callback-v2', 0,
                        branch=2)
    st_ack = W.respond('[TRIGGER] Acknowledge Worker', 0, branch=3, body='={ "ok": true }')
    W.link(st_hook, st_ack)

    poll = W.schedule('[TRIGGER] Polling Safety Net', 2, 0, branch=5)

    # ══ 01 LOAD CONFIG ════════════════════════════════════════════════
    W.sticky(1, '01 — LOAD CONFIG', """`GET /api/routes/active?all=1` devuelve
**todas** las rutas no archivadas con su `elevenlabs_agent_id`. El polling
consulta exactamente esos agentes: la lista de agentes **no** está hardcodeada
(v1 tenía dos IDs pegados en el código).

`GET /api/settings` aporta la ventana de polling y la ruta de compatibilidad
para post-calls de v1 sin `route_key`.""", color=3, height=360)
    cfg = W.http('[CONFIG] Load All Routes', 'GET', PANEL + '/api/routes/active?all=1', 1,
                 branch=5,
                 credentials={'httpHeaderAuth': {'id': '__PANEL_TOKEN_CREDENTIAL__',
                                                 'name': 'Landmark Panel API'}},
                 never_error=True, full_response=True, timeout=10000, retry=2,
                 on_error='continueRegularOutput')
    W.link(poll, cfg)
    setts = W.http('[CONFIG] Load Settings', 'GET', PANEL + '/api/settings', 1,
                   branch=5, offset=1,
                   credentials={'httpHeaderAuth': {'id': '__PANEL_TOKEN_CREDENTIAL__',
                                                   'name': 'Landmark Panel API'}},
                   never_error=True, full_response=True, timeout=10000, retry=2,
                   on_error='continueRegularOutput')
    W.chain(cfg, setts)

    # ══ 02 FETCH (polling) ════════════════════════════════════════════
    W.sticky(2, '02 — POLLING FETCH', """Trae las conversaciones de los agentes
configurados en la ventana reciente. **Sin `$getWorkflowStaticData`**: la
deduplicación es del ledger, que sí sobrevive a un restart y sí se comparte
entre workers.

La API key sale de la credential `ElevenLabs API`, nunca del nodo.""",
              color=3, height=320)
    fetch = W.code('[CONFIG] Fetch Recent Conversations', CORE + r'''
// Red de seguridad: recupera las conversaciones recientes de TODOS los agentes
// configurados en las rutas. Ni un agent_id hardcodeado.
const cfgResp = $('[CONFIG] Load All Routes').first().json || {};
const setResp = $input.first().json || {};
const routes = ((cfgResp.body || {}).routes) || [];
const settings = ((setResp.body || {}).settings) || {};
const windowMin = Number(settings.postcall_polling_window_minutes || 20);

const agents = [...new Set(routes.map(r => r.elevenlabs_agent_id).filter(Boolean))];
if (!agents.length) {
  console.log('[WF9] polling: ninguna ruta define elevenlabs_agent_id');
  return [];
}
const key = $env.ELEVENLABS_API_KEY || '';
if (!key) {
  console.log('[WF9] polling: falta ELEVENLABS_API_KEY en el entorno de n8n; ' +
              'el webhook sigue funcionando');
  return [];
}
const after = Math.floor(Date.now() / 1000) - windowMin * 60;
const base = 'https://api.elevenlabs.io/v1/convai/conversations';
const byId = new Map();
for (const agentId of agents) {
  let cursor = '', page = 0;
  do {
    let url = base + '?page_size=100&summary_mode=include&call_start_after_unix=' + after +
              '&agent_id=' + encodeURIComponent(agentId);
    if (cursor) url += '&cursor=' + encodeURIComponent(cursor);
    let res;
    try {
      res = await this.helpers.httpRequest({ method: 'GET', url,
        headers: { 'xi-api-key': key }, json: true, timeout: 30000 });
    } catch (e) {
      console.log('[WF9] polling agente ' + agentId + ': ' + e.message);
      break;
    }
    for (const c of (res.conversations || [])) {
      if (c.conversation_id) byId.set(c.conversation_id, c);
    }
    cursor = res.has_more ? (res.next_cursor || '') : '';
    page++;
  } while (cursor && page < 30);
}
const ids = [...byId.keys()];
console.log('[WF9] polling: ' + ids.length + ' conversacion(es) en los ultimos ' +
            windowMin + ' min');
const out = [];
for (const id of ids) {
  try {
    const det = await this.helpers.httpRequest({ method: 'GET', url: base + '/' + id,
      headers: { 'xi-api-key': key }, json: true, timeout: 20000 });
    out.push({ json: { source: 'polling', payload: det } });
  } catch (e) {
    console.log('[WF9] polling detalle ' + id + ': ' + e.message);
  }
}
return out;
''', 2, branch=5)
    W.chain(setts, fetch)

    # ══ 03 VERIFY + PARSE ═════════════════════════════════════════════
    W.sticky(3, '03 — VERIFY & PARSE', """**HMAC**: formato `t=<ts>,v0=<firma>` de
ElevenLabs, sobre `timestamp + "." + rawBody`. El secreto sale de
`ELEVENLABS_WEBHOOK_SECRET` (entorno de n8n), nunca del JSON.

Una firma inválida **rechaza** el evento. v1 logueaba el desajuste y procesaba
igual, que es lo mismo que no verificar.

**Correlación explícita, cero inferencia**: `call_job_id` y `route_key` vienen
en `dynamic_variables` porque WF2 los puso ahí. Si el proveedor no los propaga
(hoy el worker Stringee solo reenvía `lead_id`), se resuelve por la **única
llamada en vuelo del lead**.

Prohibido `phone.startsWith('+')` para adivinar el proveedor.""",
              color=6, height=480)
    verify = W.code('[CONFIG] Verify And Parse Webhook', CORE + r'''
// Verificación HMAC + parseo del post-call de ElevenLabs.
const crypto = require('crypto');
const item = $input.first();
const raw = item.json || {};

// Raw body (necesario para que la firma sea verificable).
let rawStr = '';
try {
  if (item.binary && item.binary.data && item.binary.data.data) {
    rawStr = Buffer.from(item.binary.data.data, 'base64').toString('utf8');
  }
} catch (e) { rawStr = ''; }

let body = raw.body || raw;
if (rawStr) { try { body = JSON.parse(rawStr); } catch (e) { /* queda el parseado */ } }

const secret = $env.ELEVENLABS_WEBHOOK_SECRET || '';
const headers = raw.headers || {};
const sigHeader = String(headers['elevenlabs-signature'] ||
                         headers['ElevenLabs-Signature'] || '');

let verified = false, authError = null;
if (!secret) {
  authError = 'ELEVENLABS_WEBHOOK_SECRET no configurado en n8n';
} else if (!sigHeader) {
  authError = 'webhook sin cabecera de firma';
} else if (!rawStr) {
  authError = 'sin Raw Body: la firma no es verificable (activar "Raw Body" en el nodo Webhook)';
} else {
  const parts = {};
  for (const p of sigHeader.split(',')) {
    const [k, v] = p.split('=');
    parts[k] = v;
  }
  const ts = parts['t'] || '';
  const got = parts['v0'] || parts['v1'] || '';
  if (!ts || Math.abs(Date.now() / 1000 - Number(ts)) > 1800) {
    authError = 'timestamp fuera de ventana (posible replay)';
  } else {
    const expected = crypto.createHmac('sha256', secret).update(ts + '.' + rawStr).digest('hex');
    const a = Buffer.from(got), b = Buffer.from(expected);
    verified = a.length === b.length && crypto.timingSafeEqual(a, b);
    if (!verified) authError = 'firma HMAC no coincide';
  }
}

if (!verified) {
  // Se RECHAZA. v1 procesaba igual, lo que dejaba el webhook abierto.
  console.log('[WF9] AUTH_ERROR webhook rechazado: ' + authError);
  return [{ json: { accepted: false, error_code: 'AUTH_ERROR', error_detail: authError } }];
}
return [{ json: { accepted: true, source: 'webhook', payload: body.data || body } }];
''', 3)
    W.link(hook, verify)

    auth_if = W.if_('[CONFIG] Webhook Accepted', '={{ $json.accepted }}', 'true', True,
                    3, offset=1, single=True)
    W.chain(verify, auth_if)
    auth_rej = W.code('[ERROR] Webhook Rejected', CORE + r'''
const d = $json;
console.log('[WF9][exec=' + $execution.id + '] action=reject code=AUTH_ERROR detail=' +
            d.error_detail);
return [{ json: { ok: false, error: true, error_code: 'AUTH_ERROR',
                  error_detail: d.error_detail } }];
''', 3, branch=2, offset=1)
    W.link(auth_if, auth_rej, 1)

    st_parse = W.code('[CONFIG] Parse Worker Callback', CORE + r'''
// El worker Stringee POSTea su job completo (publicJob): trae job_id, lead_id,
// final_status, answered, sip_code, elevenlabs_conversation_id y los timestamps.
// Es evidencia TELEFÓNICA real, no una inferencia por formato de teléfono.
const raw = $input.first().json || {};
const b = raw.body || raw;
const token = $env.LM_WORKER_CALLBACK_TOKEN || '';
const auth = String((raw.headers || {}).authorization || '');
if (token && auth !== ('Bearer ' + token)) {
  console.log('[WF9] AUTH_ERROR callback del worker con token invalido');
  return [{ json: { accepted: false, error_code: 'AUTH_ERROR',
                    error_detail: 'token del worker invalido' } }];
}
if (!b.job_id && !b.lead_id) {
  return [{ json: { accepted: false, error_code: 'VALIDATION_ERROR',
                    error_detail: 'callback sin job_id ni lead_id' } }];
}
const answeredAt = Number(b.answered_at ? Date.parse(b.answered_at) : 0);
const finishedAt = Number(b.finished_at ? Date.parse(b.finished_at) : 0);
const duration = (answeredAt && finishedAt && finishedAt > answeredAt)
  ? Math.round((finishedAt - answeredAt) / 1000) : 0;

return [{ json: { accepted: true, source: 'worker_callback', payload: null,
  worker: {
    lead_id: String(b.lead_id || ''),
    provider_job_id: String(b.job_id || ''),
    provider_call_id: b.stringee_call_id || null,
    conversation_id: b.elevenlabs_conversation_id || null,
    telephony_status: String(b.final_status || b.telephony_status || ''),
    answered: b.answered === true,
    sip_code: b.sip_code === null || b.sip_code === undefined ? null : String(b.sip_code),
    duration_seconds: duration
  } }}];
''', 3, branch=4)
    W.link(st_hook, st_parse)
    st_if = W.if_('[CONFIG] Worker Callback Accepted', '={{ $json.accepted }}', 'true',
                  True, 3, branch=4, offset=1, single=True)
    W.chain(st_parse, st_if)
    st_rej = W.code('[ERROR] Worker Callback Rejected', CORE + r'''
const d = $json;
console.log('[WF9][exec=' + $execution.id + '] action=reject code=' + d.error_code +
            ' detail=' + d.error_detail);
return [{ json: { ok: false, error: true, error_code: d.error_code,
                  error_detail: d.error_detail } }];
''', 3, branch=6, offset=1)
    W.link(st_if, st_rej, 1)

    # ══ 04 NORMALIZE ══════════════════════════════════════════════════
    W.sticky(4, '04 — NORMALIZE CALL RESULT', """Clasificación por **evidencia**,
no por defecto:

 · `duration = 0` y sin transcripción → `NO_ANSWER`
 · terminación de buzón o `< 8s` sin éxito → `VOICEMAIL`
 · `callback_requested` → `CALLBACK`
 · `call_successful = success` → `ANSWERED`
 · `failure` **con** código SIP → `FAILED` + `sip_code` (la política reclasifica)
 · `failure` **sin** evidencia → `UNKNOWN`, **no** `NO_ANSWER`

v1 mapeaba `FAILED -> NO_ANSWER` globalmente, así que un fallo técnico consumía
un intento de negocio y disparaba un reintento comercial.

`UNKNOWN` sí llega al motor: la política decide (por defecto `NONE`, no
reintenta ni cierra). Lo que **nunca** llega al motor es un error técnico de
DESPACHO, que WF2 resuelve como `RELEASED`.""", color=6, height=500)
    norm = W.code('[RESULT] Normalize Call Result', CORE + r'''
// De payload crudo (webhook, polling o callback del worker) al CALL RESULT
// CONTRACT. Este nodo NO decide follow-up: solo describe qué pasó.
const out = [];
for (const it of $input.all()) {
  const d = it.json || {};

  // ── rama worker Stringee ────────────────────────────────────────────
  if (d.source === 'worker_callback') {
    const w = d.worker || {};
    const st = String(w.telephony_status || '').toUpperCase();
    let result;
    if (w.answered && w.duration_seconds > 0) result = 'ANSWERED';
    else if (st === 'NO_ANSWER') result = 'NO_ANSWER';
    else if (st === 'BUSY') result = 'BUSY';
    else if (st === 'REJECTED' || st === 'UNAVAILABLE') result = 'FAILED';
    else if (st === 'FAILED') result = 'FAILED';
    else result = 'UNKNOWN';
    out.push({ json: {
      source: 'worker_callback', lead_id: w.lead_id || null,
      call_job_id: null, route_key: null, country_iso: null, provider: null,
      adapter_key: null, attempt: null,
      conversation_id: w.conversation_id || null,
      provider_job_id: w.provider_job_id || null,
      sip_code: w.sip_code, result: result,
      duration_seconds: Number(w.duration_seconds || 0),
      callback_requested: false, callback_at: null, summary: null,
      execution_id: $execution.id
    }});
    continue;
  }

  // ── rama ElevenLabs (webhook o polling) ─────────────────────────────
  const p = d.payload || {};
  const meta = p.metadata || {};
  const analysis = p.analysis || {};
  const dyn = (p.conversation_initiation_client_data &&
               p.conversation_initiation_client_data.dynamic_variables) ||
              meta.dynamic_variables || {};
  const dc = analysis.data_collection_results || analysis.data_collection || {};

  const conversationId = String(p.conversation_id || p.conversationId || '') || null;
  const duration = Number(meta.call_duration_secs || p.duration_secs || 0);
  const success = String(analysis.call_successful || '').toLowerCase();
  const endedBy = String(meta.termination_reason || '').toLowerCase();
  const summary = analysis.transcript_summary || '';
  const hasTranscript = Array.isArray(p.transcript) ? p.transcript.length > 0
                        : !!(p.transcript && String(p.transcript).length);

  const callbackReq = ['true', 'yes'].indexOf(
    String((dc.callback_requested || {}).value || '').toLowerCase()) >= 0;
  const callbackDate = String((dc.callback_date || {}).value || '') || null;
  const dncFlag = ['true', 'yes'].indexOf(
    String((dc.do_not_call || {}).value || '').toLowerCase()) >= 0;
  const wrongNumber = ['true', 'yes'].indexOf(
    String((dc.wrong_number || {}).value || '').toLowerCase()) >= 0;

  // sip_code SOLO si el proveedor lo reporta. Nunca inventado.
  const sipRaw = meta.sip_code || meta.sip_status_code ||
                 (meta.termination_reason_details && meta.termination_reason_details.sip_code) || null;
  const sipCode = sipRaw ? String(sipRaw) : null;

  const isVoicemail = endedBy.indexOf('voicemail') >= 0 ||
                      String(summary).toLowerCase().indexOf('voicemail') >= 0 ||
                      (duration > 0 && duration < 8 && success !== 'success');

  let result;
  if (dncFlag) result = 'DNC';
  else if (wrongNumber) result = 'WRONG_NUMBER';
  else if (callbackReq) result = 'CALLBACK';
  else if (duration === 0 && !hasTranscript) result = 'NO_ANSWER';
  else if (isVoicemail) result = 'VOICEMAIL';
  else if (success === 'success') result = 'ANSWERED';
  else if (success === 'failure' || success === 'failed') {
    // FAILED solo si hay evidencia de qué falló. Sin evidencia: UNKNOWN.
    result = sipCode ? 'FAILED' : (duration > 0 ? 'ANSWERED' : 'UNKNOWN');
  } else if (duration > 0 || hasTranscript) result = 'ANSWERED';
  else result = 'UNKNOWN';

  out.push({ json: {
    source: d.source || 'webhook',
    // Correlación EXPLÍCITA: viene de dynamic_variables porque WF2 la puso.
    call_job_id: dyn.call_job_id || null,
    route_key: dyn.route_key || null,
    country_iso: dyn.country_iso || null,
    provider: dyn.provider || null,
    adapter_key: dyn.adapter_key || null,
    attempt: dyn.attempt ? Number(dyn.attempt) : null,
    lead_id: dyn.lead_id || null,
    conversation_id: conversationId,
    provider_job_id: null,
    sip_code: sipCode,
    result: result,
    duration_seconds: duration,
    callback_requested: callbackReq,
    callback_at: callbackDate,
    summary: String(summary).slice(0, 400),
    // ElevenLabs no garantiza el idioma del resumen: el agente habla en el
    // idioma del pais. Se marca como NO garantizado, asi que lmCallNote lo
    // descarta y el CRM recibe su texto canonico. El original viaja igual
    // en el evento local, que es donde queda la evidencia.
    summary_language: (analysis.language || conv.language || null),
    summary_is_english: false,
    execution_id: $execution.id
  }});
}
return out;
''', 4)
    W.link(auth_if, norm, 0)
    W.link(st_if, norm, 0)
    W.link(fetch, norm)

    # ══ 05 CORRELATE ══════════════════════════════════════════════════
    W.sticky(5, '05 — RESOLVE CALL JOB', """Orden de correlación, del más fuerte al
más débil:

1. `call_job_id` de `dynamic_variables` — identidad directa
2. `conversation_id` ya guardado en el job (lo escribió WF2 al despachar)
3. `provider_job_id` — el `job_id` del worker Stringee
4. **la única llamada en vuelo del lead** — `UNIQUE(inflight_lead)` garantiza
   que no hay ambigüedad. Esto es lo que hace que el worker actual funcione sin
   ningún cambio (PV-3 **resuelto**: el worker sí reenvía `lead_id`).

Sin ninguna de las cuatro ⇒ issue `ORPHAN_POSTCALL` (lo abre el motor) y no se
crea follow-up.

La consulta usa subconsultas escalares: **siempre devuelve exactamente una
fila por item**, así el emparejamiento con el item de entrada no se desalinea.""",
              color=3, height=500)
    corr = W.mysql('[DB] Resolve Call Job', (
        "SELECT ? AS _k,\n"
        "  COALESCE(\n"
        "    (SELECT j.call_job_id FROM wf_call_jobs j WHERE j.call_job_id = ?),\n"
        "    (SELECT j.call_job_id FROM wf_call_jobs j WHERE ? <> '' AND j.conversation_id = ?\n"
        "       ORDER BY j.created_at DESC LIMIT 1),\n"
        "    (SELECT j.call_job_id FROM wf_call_jobs j WHERE ? <> '' AND j.provider_job_id = ?\n"
        "       ORDER BY j.created_at DESC LIMIT 1),\n"
        "    (SELECT j.call_job_id FROM wf_call_jobs j WHERE ? <> '' AND j.inflight_lead = ?)\n"
        "  ) AS resolved_call_job_id,\n"
        "  (SELECT t.order_ref FROM wf_tool_requests t\n"
        "    WHERE t.tool_type = 'CALLBACK' AND t.state = 'SUCCEEDED'\n"
        "      AND ((? <> '' AND t.conversation_id = ?) OR (? <> '' AND t.lead_id = ?))\n"
        "    ORDER BY t.claimed_at DESC LIMIT 1) AS requested_callback_at"),
        5, replacements='={{ $json.conversation_id || $json.call_job_id || $json.lead_id || "" }},'
                        '={{ $json.call_job_id || "" }},'
                        '={{ $json.conversation_id || "" }},={{ $json.conversation_id || "" }},'
                        '={{ $json.provider_job_id || "" }},={{ $json.provider_job_id || "" }},'
                        '={{ $json.lead_id || "" }},={{ $json.lead_id || "" }},'
                        '={{ $json.conversation_id || "" }},={{ $json.conversation_id || "" }},'
                        '={{ $json.lead_id || "" }},={{ $json.lead_id || "" }}',
        retry=2, on_error='continueRegularOutput')
    W.chain(norm, corr)

    keep_cb = W.code('[RESULT] Keep Callback Request', r'''
// La hora que el cliente pidió por la tool CALLBACK viaja junto al item para
// que el motor la use si la extracción de datos de la conversación no la trajo.
return $input.all().map((r, i) => ({ json: Object.assign({}, r.json, {
  _requested_callback_at: (r.json || {}).requested_callback_at || null
})}));
''', 5, offset=1, branch=2)

    load_job = W.mysql('[DB] Load Job Context', (
        "SELECT ? AS _k, j.call_job_id, j.lead_id, j.route_key, j.country_iso,\n"
        "       j.provider, j.adapter_key, j.attempt, j.state, j.result,\n"
        "       j.conversation_id, j.followup_id\n"
        "  FROM wf_call_jobs j WHERE j.call_job_id = ?\n"
        " UNION ALL\n"
        "SELECT ?, NULL, NULL, NULL, NULL, NULL, NULL, NULL, NULL, NULL, NULL, NULL\n"
        " WHERE NOT EXISTS (SELECT 1 FROM wf_call_jobs x WHERE x.call_job_id = ?)"),
        5, offset=2,
        replacements='={{ $json.resolved_call_job_id || "" }},'
                     '={{ $json.resolved_call_job_id || "" }},'
                     '={{ $json.resolved_call_job_id || "" }},'
                     '={{ $json.resolved_call_job_id || "" }}',
        retry=2, on_error='continueRegularOutput')
    W.chain(corr, keep_cb)
    W.chain(keep_cb, load_job)

    merge = W.code('[RESULT] Merge Job Context', CORE + r'''
// El job local MANDA sobre lo que dice el post-call: es el hecho registrado en
// el momento del despacho. El post-call aporta el RESULTADO, no la identidad.
const rows = $input.all();
return rows.map((r, i) => {
  const ctx = $('[RESULT] Normalize Call Result').itemMatching(i).json;
  const job = r.json || {};
  const found = !!job.call_job_id;
  return { json: Object.assign({}, ctx, {
    job_found: found,
    call_job_id: found ? job.call_job_id : (ctx.call_job_id || null),
    lead_id: found ? job.lead_id : ctx.lead_id,
    route_key: found ? job.route_key : ctx.route_key,
    country_iso: found ? job.country_iso : ctx.country_iso,
    provider: found ? job.provider : ctx.provider,
    adapter_key: found ? job.adapter_key : ctx.adapter_key,
    attempt: found ? Number(job.attempt) : ctx.attempt,
    job_state: found ? job.state : null,
    job_result: found ? job.result : null,
    requested_callback_at: (() => {
      try { return $('[RESULT] Keep Callback Request').itemMatching(i).json._requested_callback_at; }
      catch (e) { return null; }
    })(),
    correlation: found
      ? (ctx.call_job_id ? 'CALL_JOB'
        : ctx.conversation_id ? 'CONVERSATION'
        : ctx.provider_job_id ? 'PROVIDER_JOB' : 'INFLIGHT_LEAD')
      : 'NONE'
  })};
});
''', 5, offset=2)
    W.chain(load_job, merge)

    # ══ 06 RECORDING CORRELATION HANDOFF ══════════════════════════════
    W.sticky(6, '06 — RECORDING HANDOFF', """Se ata el `conversation_id` al job
**antes** de llamar al motor. Así WF10 puede correlacionar la grabación por la
cadena `call_job_id → conversation_id` aunque esta ejecución termine en
`ALREADY_CLAIMED` (webhook y polling a la vez).

Sin esto, la grabación de una llamada Stringee no tendría cómo encontrar su
follow-up salvo por teléfono — que es justo lo que V2 deja de usar.""",
              color=3, height=340)
    link_conv = W.mysql('[DB] Link Conversation To Job', (
        "UPDATE wf_call_jobs\n"
        "   SET conversation_id = COALESCE(conversation_id, ?),\n"
        "       provider_job_id = COALESCE(provider_job_id, ?),\n"
        "       updated_at = UTC_TIMESTAMP()\n"
        " WHERE call_job_id = ? AND ? <> ''"),
        6, replacements='={{ $json.conversation_id || null }},'
                        '={{ $json.provider_job_id || null }},'
                        '={{ $json.call_job_id || "" }},={{ $json.call_job_id || "" }}',
        retry=2, on_error='continueRegularOutput')
    W.chain(merge, link_conv)

    ready = W.code('[RESULT] Build Engine Input', CORE + r'''
// CALL RESULT CONTRACT completo. Si falta el job, igual se envía: el motor
// abre el issue ORPHAN_POSTCALL (un único lugar donde se decide eso).
const rows = $input.all();
return rows.map((r, i) => {
  const d = $('[RESULT] Merge Job Context').itemMatching(i).json;
  lmLog('WF9', d, { call_job_id: d.call_job_id, route_key: d.route_key,
                    country: d.country_iso, provider: d.provider,
                    lead_id: d.lead_id, attempt: d.attempt,
                    conversation_id: d.conversation_id, result: d.result,
                    action: 'NORMALIZED[' + d.correlation + ']' });
  return { json: {
    call_job_id: d.call_job_id, lead_id: d.lead_id, route_key: d.route_key,
    country_iso: d.country_iso, provider: d.provider, adapter_key: d.adapter_key,
    attempt: d.attempt, conversation_id: d.conversation_id,
    provider_job_id: d.provider_job_id, sip_code: d.sip_code,
    result: d.result, call_status: d.result,
    duration_seconds: Number(d.duration_seconds || 0),
    callback_requested: !!d.callback_requested || !!d.requested_callback_at,
    // La hora pedida por la tool CALLBACK sirve de respaldo si la extracción
    // de datos de la conversación no la capturó.
    callback_at: d.callback_at || d.requested_callback_at || null,
    notes: null, summary: d.summary,
    summary_language: d.summary_language || null,
    summary_is_english: !!d.summary_is_english,
    source: d.source,
    execution_id: d.execution_id, access_token: null
  }};
});
''', 6, offset=1)
    W.chain(link_conv, ready)

    has_route = W.if_('[RESULT] Route Key Present',
                      '={{ !!$json.route_key }}', 'true', True, 6, offset=2, single=True)
    W.chain(ready, has_route)

    legacy = W.code('[CONFIG] Apply Legacy Compat Route', CORE + r'''
// Post-call de una llamada lanzada por un workflow v1: no tiene route_key.
// Se usa la ruta de compatibilidad CONFIGURADA (wf_settings.legacy_compat_route_key).
// Vacía = no se procesa (fail-closed). Nunca se adivina la ruta por el prefijo.
const settings = (() => {
  try { return ($('[CONFIG] Load Settings').first().json.body || {}).settings || {}; }
  catch (e) { return {}; }
})();
const compat = String(settings.legacy_compat_route_key || '').trim();
return $input.all().map(it => {
  const d = it.json;
  if (!compat) {
    console.log('[WF9] LEGACY_NO_ROUTE lead=' + d.lead_id + ' conv=' + d.conversation_id +
                ' — sin legacy_compat_route_key configurada: no se procesa');
    return { json: Object.assign({}, d, { legacy_ok: false,
                                          error_code: 'VALIDATION_ERROR',
                                          error_detail: 'LEGACY_NO_ROUTE' }) };
  }
  console.log('[WF9] LEGACY_NO_ROUTE lead=' + d.lead_id + ' -> ruta de compatibilidad ' + compat);
  return { json: Object.assign({}, d, { route_key: compat, legacy_ok: true }) };
});
''', 6, branch=3, offset=2)
    W.link(has_route, legacy, 1)
    legacy_if = W.if_('[CONFIG] Legacy Route Resolved', '={{ $json.legacy_ok }}', 'true',
                      True, 6, branch=3, offset=3, single=True)
    W.chain(legacy, legacy_if)
    legacy_drop = W.code('[LOG] Legacy Without Route', CORE + r'''
// Se descarta CON MOTIVO. v1 descartaba en silencio: ese era el bug por el que
// "WF9 no funcionaba para Stringee".
return $input.all().map(it => {
  const d = it.json;
  console.log('[WF9][exec=' + d.execution_id + '] action=drop code=LEGACY_NO_ROUTE lead=' +
              d.lead_id + ' conv=' + (d.conversation_id || '-'));
  return { json: { ok: false, skipped: true, reason: 'LEGACY_NO_ROUTE',
                   lead_id: d.lead_id, conversation_id: d.conversation_id } };
});
''', 6, branch=5, offset=3)
    W.link(legacy_if, legacy_drop, 1)

    # ══ 07 ENGINE ═════════════════════════════════════════════════════
    W.sticky(7, '07 — FOLLOW-UP ENGINE', """WF9 **no** decide el seguimiento: pasa el
CALL RESULT al motor y el motor hace todo lo demás —registrar el hecho, reclamar
la idempotencia, resolver la política, crear el follow-up y emitir los eventos.

v1 tenía la lógica repartida: WF2 programaba `scheduleNextAt` para Asterisk y
WF9 lo programaba para Stringee con reglas distintas. Dos motores que no
coincidían.""", color=5, height=340)
    engine = W.exec_wf('[ENGINE] Call Followup Engine', 'TEMPLATE_FOLLOWUP_ENGINE_V2', 7)
    W.link(has_route, engine, 0)
    W.link(legacy_if, engine, 0)

    done = W.code('[LOG] Post Call Processed', CORE + r'''
// Resumen por item: qué decidió el motor para cada post-call.
const rows = $input.all();
let processed = 0, skipped = 0, errors = 0;
const out = rows.map(r => {
  const d = r.json || {};
  if (d.error) errors++;
  else if (d.skipped) skipped++;
  else processed++;
  return { json: d };
});
console.log('[WF9][exec=' + $execution.id + '] post-calls: ' + processed +
            ' procesados · ' + skipped + ' omitidos (duplicados/ya reclamados) · ' +
            errors + ' con error');
return out;
''', 7, offset=1)
    W.chain(engine, done)
    return W
