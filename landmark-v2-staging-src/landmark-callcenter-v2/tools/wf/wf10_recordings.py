"""TEMPLATE_WF10_RECORDINGS_V2 — grabaciones al CRM y a Telegram.

Reemplaza a "WF10 — Recordings to LeadStudio API + Telegram".

Lo que YA NO hace, respecto de v1:
  · no correlaciona por `RIGHT(phone,10)` contra el follow-up "más reciente sin
    grabación": con varios intentos por lead eso adjunta el audio de una llamada
    al follow-up de otra
  · no tiene los chat_id de Telegram ni el mapa de países en los nodos
  · no filtra por "solo las de hoy" (perdía las de medianoche)
  · no manda a Telegram y DESPUÉS registra: reserva antes, envía después
"""

VERSION = '2.0.0'
NAME = 'TEMPLATE_WF10_RECORDINGS_V2'


def build(wfbuild, js):
    W = wfbuild.Workflow(
        NAME, VERSION,
        'TEMPLATE_DATA v2.2 · ANALYTICS_EVENT v2.2 · CRM_ENGLISH_RULE',
        'WF10 v1 — desactivar este workflow; v1 sigue con su propio ledger (§12)',
        'Schedule cada 5 min.')
    CORE, NOTES = js['lmcore.js'], js['lmnotes.js']
    PANEL = '={{ $env.LM_PANEL_URL || "http://172.18.0.1:8080" }}'
    CRM = '={{ $env.LM_LEADSTUDIO_URL || "https://lead-studio-9gnl.onrender.com" }}'

    # ══ 00 TRIGGER ════════════════════════════════════════════════════
    W.sticky(0, '00 — TRIGGER', f"""{W.meta_header}

Cada 5 minutos. La ventana de recogida es **configuración por ruta**
(`recording_lookback_hours`, 48 h por defecto), no "las de hoy": v1 comparaba
contra la fecha UTC del día y perdía sistemáticamente las grabaciones de las
llamadas de medianoche.""", color=4, height=360)
    trig = W.schedule('[TRIGGER] Every Five Minutes', 5, 0)

    # ══ 01 CONFIG ═════════════════════════════════════════════════════
    W.sticky(1, '01 — LOAD CONFIG', """`GET /api/routes/active?include_archived=1`.

**Incluye archivadas a propósito**: una grabación puede llegar después de que la
ruta se archivó, y su configuración (mínimo de duración, destinos de Telegram)
tiene que seguir resolviendo. Por eso `route_key` nunca se reutiliza.

Por ruta: `recording_enabled`, `recording_min_secs`, `recording_upload_crm`,
`recording_telegram`, `recording_source`, `recording_lookback_hours` y los
chats de Telegram con propósito `recording`.""", color=3, height=420)
    cfg = W.http('[CONFIG] Load Routes With Recordings', 'GET',
                 PANEL + '/api/routes/active?include_archived=1', 1,
                 credentials={'httpHeaderAuth': {'id': '__PANEL_TOKEN_CREDENTIAL__',
                                                 'name': 'Landmark Panel API'}},
                 never_error=True, full_response=True, timeout=10000, retry=2,
                 on_error='continueRegularOutput')
    W.chain(trig, cfg)
    setts = W.http('[CONFIG] Load Settings', 'GET', PANEL + '/api/settings', 1, offset=1,
                   credentials={'httpHeaderAuth': {'id': '__PANEL_TOKEN_CREDENTIAL__',
                                                   'name': 'Landmark Panel API'}},
                   never_error=True, full_response=True, timeout=10000, retry=2,
                   on_error='continueRegularOutput')
    W.chain(cfg, setts)

    cfg_chk = W.code('[CONFIG] Check Recording Config', CORE + r'''
const rResp = $('[CONFIG] Load Routes With Recordings').first().json || {};
const sResp = $input.first().json || {};
const code = Number(rResp.statusCode || 0);
const routes = ((rResp.body || {}).routes) || [];
const settings = ((sResp.body || {}).settings) || {};
if (code < 200 || code >= 300) {
  console.log('[WF10] CONFIG_ERROR: el panel no respondio (HTTP ' + code +
              '): no se procesa ninguna grabacion');
  return [{ json: { config_ok: false, error_code: 'CONFIG_ERROR', routes: [] } }];
}
const enabled = routes.filter(r => r.recording && r.recording.enabled);
console.log('[WF10] rutas con grabacion activa: ' + enabled.length + '/' + routes.length);
return [{ json: {
  config_ok: enabled.length > 0, routes: enabled, all_routes: routes,
  default_min_secs: Number(settings.recording_default_min_secs || 60),
  error_code: enabled.length ? null : 'NO_RECORDING_ROUTES',
  execution_id: $execution.id
}}];
''', 1, offset=2)
    W.chain(setts, cfg_chk)
    cfg_if = W.if_('[CONFIG] Config Is Usable', '={{ $json.config_ok }}', 'true', True,
                   1, offset=3, single=True)
    W.chain(cfg_chk, cfg_if)
    cfg_err = W.code('[LOG] Nothing To Process', CORE + r'''
const d = $json;
console.log('[WF10][exec=' + $execution.id + '] fin sin procesar · code=' + d.error_code);
return [{ json: { ok: true, processed: 0, reason: d.error_code } }];
''', 1, branch=2, offset=3)
    W.link(cfg_if, cfg_err, 1)

    # ══ 02 COLLECT ════════════════════════════════════════════════════
    W.sticky(2, '02 — COLLECT RECORDINGS', """Dos orígenes, elegidos por
`recording_source` de la ruta — **no** por un `if stringee`:

 · `STRINGEE_WORKER` → `GET {endpoint}/recordings` y
   `GET /recordings/{filename}` del worker local
 · `ELEVENLABS_API` → las llamadas COMPLETADAS con `conversation_id` que aún no
   están en el ledger, vía `GET /v1/convai/conversations/{id}/audio`

La duración se lee del **header del WAV** (`byteRate` en el offset 28), no del
tamaño en bytes: el tamaño depende del sample rate y daba mal. Si el header no
se puede leer, la grabación **no se descarta**: se deja pasar marcada —
descartar por no poder medir perdía conversaciones reales en silencio.""",
              color=6, height=460)
    collect = W.code('[RECORDING] Collect Pending', CORE + r'''
// Arma la lista de grabaciones a procesar. Un solo nodo hace las llamadas HTTP
// de listado/descarga (igual que v1) para no multiplicar nodos por origen.
const cfg = $json;
const out = [];

function wavDurationSecs(b64, sizeBytes) {
  // Header WAV canónico: byteRate en el offset 28 (uint32 LE).
  try {
    const head = Buffer.from(b64.slice(0, 200), 'base64');
    if (head.length < 44) return null;
    if (head.toString('ascii', 0, 4) !== 'RIFF') return null;
    const byteRate = head.readUInt32LE(28);
    if (!byteRate) return null;
    const dataBytes = Math.max(0, (sizeBytes || head.length) - 44);
    return Math.round(dataBytes / byteRate);
  } catch (e) { return null; }
}

for (const route of cfg.routes) {
  const rec = route.recording || {};
  const source = route.recording_source ||
    (route.adapter_key === 'STRINGEE_WORKER' ? 'STRINGEE_WORKER' : 'ELEVENLABS_API');
  const lookbackH = Number(route.recording_lookback_hours || 48);
  const since = Date.now() - lookbackH * 3600 * 1000;
  const minSecs = Number(rec.min_secs === undefined ? cfg.default_min_secs : rec.min_secs);

  if (source === 'STRINGEE_WORKER') {
    const base = route.provider_endpoint || 'http://172.18.0.1:8091';
    let list;
    try {
      list = await this.helpers.httpRequest({ method: 'GET', url: base + '/recordings',
                                              json: true, timeout: 30000 });
    } catch (e) {
      console.log('[WF10][' + route.route_key + '] worker no responde: ' + e.message);
      continue;
    }
    for (const r of (list.recordings || [])) {
      if (Number(r.timestamp_ms || 0) < since) continue;
      let dl;
      try {
        dl = await this.helpers.httpRequest({ method: 'GET',
          url: base + '/recordings/' + encodeURIComponent(r.filename),
          json: true, timeout: 60000 });
      } catch (e) {
        console.log('[WF10][' + route.route_key + '] descarga ' + r.filename + ': ' + e.message);
        continue;
      }
      if (!dl.ok || !dl.audio_base64) continue;
      const secs = wavDurationSecs(dl.audio_base64, r.size_bytes);
      out.push({
        json: {
          recording_ref: r.filename, source: 'STRINGEE_WORKER',
          filename: r.filename, phone: String(r.phone || ''),
          recorded_at: new Date(Number(r.timestamp_ms || Date.now())).toISOString(),
          size_bytes: Number(r.size_bytes || 0),
          duration_seconds: secs === null ? 0 : secs,
          duration_unknown: secs === null,
          min_secs: minSecs,
          route_key: route.route_key, route_id: route.route_id, country_iso: route.iso,
          provider: route.provider, adapter_key: route.adapter_key,
          upload_crm: !!rec.upload_crm, send_telegram: !!rec.telegram,
          telegram_chats: (route.telegram || {}).recording || [],
          conversation_id: null, call_job_id: null,
          worker_endpoint: base,
          execution_id: cfg.execution_id
        },
        binary: { data: { data: dl.audio_base64, mimeType: 'audio/wav',
                          fileName: r.filename } }
      });
    }
  } else {
    // ELEVENLABS_API: el listado de candidatos lo resuelve el nodo siguiente
    // contra la base (llamadas completadas sin grabacion registrada). Acá solo
    // se deja la ruta preparada.
    out.push({ json: {
      recording_ref: null, source: 'ELEVENLABS_API', pending_lookup: true,
      min_secs: minSecs, lookback_hours: lookbackH,
      route_key: route.route_key, route_id: route.route_id, country_iso: route.iso,
      provider: route.provider, adapter_key: route.adapter_key,
      upload_crm: !!rec.upload_crm, send_telegram: !!rec.telegram,
      telegram_chats: (route.telegram || {}).recording || [],
      execution_id: cfg.execution_id
    }});
  }
}
console.log('[WF10][exec=' + cfg.execution_id + '] candidatas: ' + out.length);
return out;
''', 2)
    W.link(cfg_if, collect, 0)

    split = W.if_('[RECORDING] Needs Conversation Lookup',
                  '={{ $json.pending_lookup === true }}', 'true', True, 2, offset=1,
                  single=True)
    W.chain(collect, split)

    el_lookup = W.mysql('[DB] Find Calls Without Recording', (
        "SELECT j.call_job_id, j.conversation_id, j.lead_id, j.route_key, j.country_iso,\n"
        "       j.provider, j.adapter_key, j.followup_id, j.duration_seconds\n"
        "  FROM wf_call_jobs j\n"
        "  LEFT JOIN wf_recording_ledger l ON l.call_job_id = j.call_job_id\n"
        " WHERE j.route_key = ? AND j.state = 'COMPLETED'\n"
        "   AND j.conversation_id IS NOT NULL\n"
        "   AND j.completed_at >= DATE_SUB(UTC_TIMESTAMP(), INTERVAL ? HOUR)\n"
        "   AND l.id IS NULL\n"
        " ORDER BY j.completed_at DESC LIMIT 100"),
        2, branch=2, offset=1,
        replacements='={{ $json.route_key }},={{ $json.lookback_hours }}',
        always_output=True, retry=2, on_error='continueRegularOutput')
    W.link(split, el_lookup, 0)

    el_fetch = W.code('[RECORDING] Fetch ElevenLabs Audio', CORE + r'''
// Descarga el audio de la conversación. La cadena de correlación ya está
// resuelta por construcción: la fila viene de wf_call_jobs.
const key = $env.ELEVENLABS_API_KEY || '';
const out = [];
for (const it of $input.all()) {
  const row = it.json || {};
  if (!row.conversation_id) continue;
  const route = (() => {
    try {
      return $('[RECORDING] Needs Conversation Lookup').all()
        .map(x => x.json).find(x => x.route_key === row.route_key) || {};
    } catch (e) { return {}; }
  })();
  if (!key) {
    console.log('[WF10] falta ELEVENLABS_API_KEY: no se descargan grabaciones SIP');
    break;
  }
  let audio;
  try {
    const res = await this.helpers.httpRequest({
      method: 'GET',
      url: 'https://api.elevenlabs.io/v1/convai/conversations/' +
           encodeURIComponent(row.conversation_id) + '/audio',
      headers: { 'xi-api-key': key }, encoding: 'arraybuffer', returnFullResponse: true,
      timeout: 60000 });
    audio = Buffer.from(res.body).toString('base64');
  } catch (e) {
    console.log('[WF10] audio de ' + row.conversation_id + ': ' + e.message);
    continue;
  }
  out.push({
    json: {
      recording_ref: row.conversation_id, source: 'ELEVENLABS_API',
      filename: row.conversation_id + '.mp3',
      phone: null, recorded_at: new Date().toISOString(),
      size_bytes: Math.round(audio.length * 3 / 4),
      duration_seconds: Number(row.duration_seconds || 0), duration_unknown: false,
      min_secs: Number(route.min_secs || 60),
      route_key: row.route_key, route_id: route.route_id || null,
      country_iso: row.country_iso, provider: row.provider,
      adapter_key: row.adapter_key,
      upload_crm: !!route.upload_crm, send_telegram: !!route.send_telegram,
      telegram_chats: route.telegram_chats || [],
      call_job_id: row.call_job_id, conversation_id: row.conversation_id,
      lead_id: row.lead_id, followup_id: row.followup_id,
      execution_id: $execution.id
    },
    binary: { data: { data: audio, mimeType: 'audio/mpeg',
                      fileName: row.conversation_id + '.mp3' } }
  });
}
console.log('[WF10] grabaciones SIP descargadas: ' + out.length);
return out;
''', 2, branch=2, offset=2)
    W.chain(el_lookup, el_fetch)

    # ══ 03 CORRELATE ══════════════════════════════════════════════════
    W.sticky(3, '03 — CORRELATE', """Cadena preferida:

    call_job_id → conversation_id → followup_id → grabación

Para las del worker Stringee, que no traen ningún id, se resuelve por la llamada
del lead: el worker nombra el archivo `stringee-<phone>-<ms>.wav`, y ese
teléfono se cruza contra las llamadas de **esa ruta** en la ventana de tiempo
del archivo.

Eso se marca en el ledger como `LEGACY_PHONE`: es **compatibilidad explícita y
auditable**, no el mecanismo principal. `correlation` queda guardado en cada
fila, así que se puede medir cuántas grabaciones siguen dependiendo del
teléfono.

Sin correlación ⇒ `ORPHAN`: **no se sube al CRM**. Adjuntar un audio al
follow-up equivocado es peor que no adjuntarlo.""", color=3, height=480)
    corr = W.mysql('[DB] Correlate Recording', (
        "SELECT ? AS _k,\n"
        "  COALESCE(\n"
        "    (SELECT j.call_job_id FROM wf_call_jobs j WHERE ? <> '' AND j.call_job_id = ?),\n"
        "    (SELECT j.call_job_id FROM wf_call_jobs j WHERE ? <> '' AND j.conversation_id = ?\n"
        "       ORDER BY j.created_at DESC LIMIT 1),\n"
        "    (SELECT j.call_job_id FROM wf_call_jobs j\n"
        "      WHERE ? <> '' AND j.route_key = ?\n"
        "        AND j.lead_id IN (SELECT c.lead_id FROM crm_leads c\n"
        "                           WHERE RIGHT(c.phone, 10) = RIGHT(?, 10))\n"
        "        AND j.created_at <= ? AND j.created_at >= DATE_SUB(?, INTERVAL 2 HOUR)\n"
        "      ORDER BY j.created_at DESC LIMIT 1)\n"
        "  ) AS matched_call_job_id"),
        3, replacements='={{ $json.recording_ref }},'
                        '={{ $json.call_job_id || "" }},={{ $json.call_job_id || "" }},'
                        '={{ $json.conversation_id || "" }},={{ $json.conversation_id || "" }},'
                        '={{ $json.phone || "" }},={{ $json.route_key }},'
                        '={{ $json.phone || "" }},'
                        '={{ $json.recorded_at.replace("T"," ").replace("Z","").slice(0,19) }},'
                        '={{ $json.recorded_at.replace("T"," ").replace("Z","").slice(0,19) }}',
        always_output=True, retry=2, on_error='continueRegularOutput')
    W.link(split, corr, 1)
    W.link(el_fetch, corr)

    corr_load = W.mysql('[DB] Load Recording Job', (
        "SELECT ? AS _k, j.call_job_id, j.lead_id, j.route_key, j.country_iso,\n"
        "       j.provider, j.adapter_key, j.conversation_id, j.followup_id,\n"
        "       j.duration_seconds\n"
        "  FROM wf_call_jobs j WHERE j.call_job_id = ?\n"
        " UNION ALL\n"
        "SELECT ?, NULL, NULL, NULL, NULL, NULL, NULL, NULL, NULL, NULL\n"
        " WHERE NOT EXISTS (SELECT 1 FROM wf_call_jobs x WHERE x.call_job_id = ?)"),
        3, offset=1,
        replacements='={{ $json._k }},={{ $json.matched_call_job_id || "" }},'
                     '={{ $json._k }},={{ $json.matched_call_job_id || "" }}',
        retry=2, on_error='continueRegularOutput')
    W.chain(corr, corr_load)

    corr_merge = W.code('[RECORDING] Merge Correlation', CORE + r'''
// Reconstruye el item (incluido el binario) y anota CÓMO se correlacionó.
const rows = $input.all();
const out = [];
for (let i = 0; i < rows.length; i++) {
  const job = rows[i].json || {};
  let src;
  try { src = $('[DB] Correlate Recording').itemMatching(i); }
  catch (e) { src = null; }
  const key = job._k;
  // el item original (con el binario) está en el nodo que lo produjo
  let orig = null;
  for (const nodeName of ['[RECORDING] Fetch ElevenLabs Audio',
                          '[RECORDING] Needs Conversation Lookup']) {
    try {
      const cand = $(nodeName).all().find(x => x.json.recording_ref === key);
      if (cand) { orig = cand; break; }
    } catch (e) { /* el nodo puede no haber corrido en esta rama */ }
  }
  if (!orig) continue;
  const d = orig.json;

  const matched = !!job.call_job_id;
  const correlation = !matched ? 'NONE'
    : (d.call_job_id ? 'CALL_JOB'
      : d.conversation_id ? 'CONVERSATION'
      : job.followup_id ? 'FOLLOWUP' : 'LEGACY_PHONE');

  const duration = (d.duration_seconds && d.duration_seconds > 0)
    ? Number(d.duration_seconds)
    : Number(job.duration_seconds || 0);

  out.push({
    json: Object.assign({}, d, {
      call_job_id: job.call_job_id || null,
      lead_id: job.lead_id || d.lead_id || null,
      conversation_id: job.conversation_id || d.conversation_id || null,
      followup_id: job.followup_id || d.followup_id || null,
      country_iso: job.country_iso || d.country_iso,
      provider: job.provider || d.provider,
      duration_seconds: duration,
      correlated: matched,
      correlation: correlation,
      below_min: duration > 0 && duration < Number(d.min_secs || 60)
    }),
    binary: orig.binary
  });
}
console.log('[WF10] correlacionadas: ' +
            out.filter(o => o.json.correlated).length + '/' + out.length +
            ' (legacy por telefono: ' +
            out.filter(o => o.json.correlation === 'LEGACY_PHONE').length + ')');
return out;
''', 3, offset=2)
    W.chain(corr_load, corr_merge)

    # ══ 04 CLAIM ══════════════════════════════════════════════════════
    W.sticky(4, '04 — CLAIM & FILTER', """`wf_recording_ledger`, clave
`recording_ref` (el filename del worker o el `conversation_id`). UNIQUE: la
misma grabación **no se sube ni se reenvía dos veces**, aunque dos corridas se
solapen o el workflow se re-ejecute.

Ganar el claim es la **única** autorización para subir y reenviar. v1 mandaba a
Telegram primero y registraba después, así que un fallo intermedio reenviaba
todo el historial en la corrida siguiente.

Filtro de duración: `recording_min_secs` **de la ruta** (60 s por defecto,
configurable). Las descartadas se cuentan: `RECORDING_SKIPPED_SHORT`.""",
              color=3, height=440)
    claimtok = W.code('[RECORDING] Build Claim Token', r'''
const crypto = require('crypto');
return $input.all().map(it => ({
  json: Object.assign({}, it.json, { claim_token: crypto.randomBytes(16).toString('hex') }),
  binary: it.binary
}));
''', 4)
    W.chain(corr_merge, claimtok)
    claim = W.mysql('[DB] Claim Recording', (
        "INSERT IGNORE INTO wf_recording_ledger\n"
        "  (recording_ref, source, call_job_id, conversation_id, followup_id, lead_id,\n"
        "   route_key, country_iso, provider, duration_seconds, size_bytes, correlation,\n"
        "   claim_token, execution_id, state)\n"
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'CLAIMED')"),
        4, offset=1,
        replacements='={{ $json.recording_ref }},={{ $json.source }},'
                     '={{ $json.call_job_id }},={{ $json.conversation_id }},'
                     '={{ $json.followup_id }},={{ $json.lead_id }},={{ $json.route_key }},'
                     '={{ $json.country_iso }},={{ $json.provider }},'
                     '={{ $json.duration_seconds }},={{ $json.size_bytes }},'
                     '={{ $json.correlation }},={{ $json.claim_token }},'
                     '={{ $json.execution_id }}',
        retry=2, on_error='continueRegularOutput')
    W.chain(claimtok, claim)
    claim_read = W.mysql('[DB] Read Recording Claim', (
        "SELECT ? AS _k, claim_token, state\n"
        "  FROM wf_recording_ledger WHERE recording_ref = ?"),
        4, offset=2,
        replacements='={{ $(\'[RECORDING] Build Claim Token\').item.json.recording_ref }},'
                     '={{ $(\'[RECORDING] Build Claim Token\').item.json.recording_ref }}',
        retry=2, on_error='continueRegularOutput')
    W.chain(claim, claim_read)
    claim_chk = W.code('[RECORDING] Check Recording Claim', CORE + r'''
const rows = $input.all();
const out = [];
for (let i = 0; i < rows.length; i++) {
  const row = rows[i].json || {};
  const orig = $('[RECORDING] Build Claim Token').itemMatching(i);
  const ctx = orig.json;
  const won = !!row.claim_token && row.claim_token === ctx.claim_token;
  if (!won) continue;                        // ya procesada: no se repite
  out.push({ json: Object.assign({}, ctx, { claim_won: true }), binary: orig.binary });
}
console.log('[WF10] grabaciones reclamadas en esta corrida: ' + out.length +
            '/' + rows.length);
return out;
''', 4, offset=3)
    W.chain(claim_read, claim_chk)

    filt = W.switch('[RECORDING] Filter Router',
                    '={{ !$json.correlated ? "ORPHAN" : ($json.below_min ? "SHORT" : "OK") }}',
                    [('OK', 'ok'), ('SHORT', 'short'), ('ORPHAN', 'orphan')], 4, offset=4,
                    fallback='none')
    W.chain(claim_chk, filt)

    short = W.mysql('[DB] Mark Recording Short', (
        "UPDATE wf_recording_ledger\n"
        "   SET state = 'SKIPPED_SHORT', error_code = 'BELOW_MIN', error_message = ?,\n"
        "       completed_at = UTC_TIMESTAMP(), updated_at = UTC_TIMESTAMP()\n"
        " WHERE recording_ref = ? AND state = 'CLAIMED'"),
        5, branch=3,
        replacements='={{ "duration " + $json.duration_seconds + "s < route minimum " + $json.min_secs + "s" }},'
                     '={{ $json.recording_ref }}',
        on_error='continueRegularOutput', retry=2)
    W.link(filt, short, 1)
    short_ev = W.mysql('[DB] Record Event Recording Short', (
        "INSERT IGNORE INTO wf_events\n"
        "  (event_key, event_type, event_domain, occurred_at, call_job_id, lead_id,\n"
        "   country_iso, route_key, provider, duration_seconds, source_workflow,\n"
        "   execution_id)\n"
        "VALUES (?, 'RECORDING_SKIPPED_SHORT', 'RECORDING', UTC_TIMESTAMP(), ?, ?, ?, ?, ?,\n"
        "        ?, 'WF10', ?)"),
        5, branch=3, offset=1,
        replacements='={{ "RECORDING_SKIPPED_SHORT:" + ($(\'[RECORDING] Filter Router\').item.json.call_job_id || $(\'[RECORDING] Filter Router\').item.json.recording_ref) }},'
                     '={{ $(\'[RECORDING] Filter Router\').item.json.call_job_id }},'
                     '={{ $(\'[RECORDING] Filter Router\').item.json.lead_id }},'
                     '={{ $(\'[RECORDING] Filter Router\').item.json.country_iso }},'
                     '={{ $(\'[RECORDING] Filter Router\').item.json.route_key }},'
                     '={{ $(\'[RECORDING] Filter Router\').item.json.provider }},'
                     '={{ $(\'[RECORDING] Filter Router\').item.json.duration_seconds }},'
                     '={{ $(\'[RECORDING] Filter Router\').item.json.execution_id }}',
        on_error='continueRegularOutput', retry=2)
    W.chain(short, short_ev)

    orph = W.mysql('[DB] Mark Recording Orphan', (
        "UPDATE wf_recording_ledger\n"
        "   SET state = 'ORPHAN', error_code = 'NO_CORRELATION',\n"
        "       error_message = 'no matching call job',\n"
        "       completed_at = UTC_TIMESTAMP(), updated_at = UTC_TIMESTAMP()\n"
        " WHERE recording_ref = ? AND state = 'CLAIMED'"),
        5, branch=5, replacements='={{ $json.recording_ref }}',
        on_error='continueRegularOutput', retry=2)
    W.link(filt, orph, 2)
    orph_log = W.code('[LOG] Recording Orphan', CORE + r'''
// Sin llamada a la que pertenecer: NO se sube al CRM. Queda contabilizada para
// que el panel muestre cuántas grabaciones quedan sin correlacionar.
return $input.all().map((r, i) => {
  const d = $('[RECORDING] Filter Router').itemMatching(i).json;
  console.log('[WF10] ORPHAN ref=' + d.recording_ref + ' route=' + d.route_key +
              ' (no se sube al CRM)');
  return { json: { ok: false, recording_ref: d.recording_ref, state: 'ORPHAN' } };
});
''', 5, branch=5, offset=1)
    W.chain(orph, orph_log)

    none_log = W.code('[LOG] Recording Unroutable', CORE + r'''
return $input.all().map(it => {
  console.log('[WF10] item sin clasificar: ' + it.json.recording_ref);
  return { json: { ok: false, recording_ref: it.json.recording_ref, state: 'UNCLASSIFIED' } };
});
''', 5, branch=7)
    W.link(filt, none_log, 3)

    # ══ 06 UPLOAD ═════════════════════════════════════════════════════
    W.sticky(6, '06 — UPLOAD TO CRM', """`PUT /api/calls/recording/{followup_id}`
con el audio como `multipart/form-data`.

Solo se sube si la ruta tiene `recording_upload_crm` y la llamada **ya tiene**
`followup_id`. Sin follow-up todavía (el post-call llegó después que la
grabación), la grabación queda `CLAIMED` y la recoge la corrida siguiente:
no se pierde y no se sube a ciegas.""", color=2, height=380)
    up_if = W.if_('[RECORDING] Should Upload To Crm',
                  '={{ $json.upload_crm === true && !!$json.followup_id }}', 'true', True,
                  6, single=True)
    W.link(filt, up_if, 0)

    up_tok = W.http('[CRM] Get Token', 'POST', CRM + '/api/auth/login', 6, offset=1,
                    headers={'Content-Type': 'application/json'},
                    credentials={'httpCustomAuth': {'id': '__LEADSTUDIO_LOGIN_CREDENTIAL__',
                                                    'name': 'LeadStudio Login'}},
                    never_error=True, full_response=True, timeout=15000, retry=2,
                    on_error='continueRegularOutput')
    W.link(up_if, up_tok, 0)
    up_ctx = W.code('[CRM] Attach Upload Token', r'''
const resp = $input.first().json || {};
const token = (resp.body && resp.body.accessToken) || null;
return $('[RECORDING] Should Upload To Crm').all().map(it => ({
  json: Object.assign({}, it.json, { access_token: token }), binary: it.binary
}));
''', 6, offset=2)
    W.chain(up_tok, up_ctx)

    upload = W.http('[CRM] Upload Recording', 'PUT',
                    CRM + '/api/calls/recording/{{ $json.followup_id }}', 6, offset=3,
                    headers={'Authorization': '=Bearer {{ $json.access_token }}'},
                    content_type='multipart-form-data',
                    body_params=[{'parameterType': 'formBinaryData', 'name': 'file',
                                  'inputDataFieldName': 'data'}],
                    never_error=True, full_response=True, timeout=60000,
                    on_error='continueRegularOutput')
    W.chain(up_ctx, upload)

    up_chk = W.code('[CRM] Check Upload', CORE + r'''
const rows = $input.all();
return rows.map((r, i) => {
  const orig = $('[CRM] Attach Upload Token').itemMatching(i);
  const d = orig.json;
  const code = Number((r.json || {}).statusCode || 0);
  const ok = code >= 200 && code < 300;
  if (!ok) console.log('[WF10] upload al CRM fallo · ref=' + d.recording_ref +
                       ' HTTP ' + code);
  return { json: Object.assign({}, d, { upload_ok: ok, upload_http_status: code }),
           binary: orig.binary };
});
''', 6, offset=4)
    W.chain(upload, up_chk)
    up_mark = W.mysql('[DB] Mark Recording Uploaded', (
        "UPDATE wf_recording_ledger\n"
        "   SET crm_uploaded = ?, state = 'UPLOADED', updated_at = UTC_TIMESTAMP()\n"
        " WHERE recording_ref = ? AND state IN ('CLAIMED','UPLOADED')"),
        6, offset=5,
        replacements='={{ $json.upload_ok ? 1 : 0 }},={{ $json.recording_ref }}',
        on_error='continueRegularOutput', retry=2)
    W.chain(up_chk, up_mark)

    # ══ 07 TELEGRAM ═══════════════════════════════════════════════════
    W.sticky(7, '07 — TELEGRAM', """Destinos por **ruta** y propósito `recording`
(`route_telegram_targets`). v1 tenía cuatro nodos de Telegram con los chat_id y
el mapa de países escritos a mano, y dos de ellos apuntaban al mismo grupo por
un copy-paste.

El caption lleva duración real (leída del WAV), país, ruta y proveedor. v1
mostraba `— s` porque el caption leía un campo que no existía.""",
              color=6, height=380)
    tg_ctx = W.code('[TELEGRAM] Prepare Recording Message', CORE + NOTES + r'''
// Un item por (grabación × chat de destino). Sin chats configurados, no se
// envía nada y el workflow sigue.
const out = [];
const rows = $input.all();
for (let i = 0; i < rows.length; i++) {
  let orig;
  try { orig = $('[CRM] Check Upload').itemMatching(i); }
  catch (e) { orig = rows[i]; }
  const d = orig.json;
  if (!d.send_telegram) continue;
  const chats = d.telegram_chats || [];
  if (!chats.length) {
    console.log('[WF10] ruta ' + d.route_key + ' pide Telegram y no tiene chat destino');
    continue;
  }
  const caption = [
    d.country_iso + ' · ' + d.route_key + ' · ' + (d.provider || ''),
    d.duration_seconds + 's' + (d.duration_unknown ? ' (estimada)' : ''),
    d.recorded_at
  ].join(' — ');
  for (const chat of chats) {
    out.push({ json: Object.assign({}, d, { telegram_chat_id: chat, caption: caption }),
               binary: orig.binary });
  }
}
return out;
''', 7)
    W.link(up_if, tg_ctx, 1)
    W.link(up_mark, tg_ctx)

    tg = W.telegram('[TELEGRAM] Send Recording', '={{ $json.telegram_chat_id }}',
                    '={{ $json.caption }}', 7, offset=1, audio=True,
                    extra={'onError': 'continueRegularOutput'})
    W.chain(tg_ctx, tg)

    # ══ 08 FINALIZE ═══════════════════════════════════════════════════
    W.sticky(8, '08 — FINALIZE', """Se cierra el ledger y se emite
`RECORDING_ATTACHED`, con clave por `call_job_id`: una grabación por llamada,
contada una sola vez.

La nota del CRM, si la ruta la pide, va **en inglés**.""", color=3, height=320)
    fin = W.mysql('[DB] Mark Recording Sent', (
        "UPDATE wf_recording_ledger\n"
        "   SET telegram_sent = 1, state = 'SENT', completed_at = UTC_TIMESTAMP(),\n"
        "       updated_at = UTC_TIMESTAMP()\n"
        " WHERE recording_ref = ? AND state IN ('CLAIMED','UPLOADED')"),
        8, replacements='={{ $(\'[TELEGRAM] Prepare Recording Message\').item.json.recording_ref }}',
        on_error='continueRegularOutput', retry=2)
    W.chain(tg, fin)

    fin_ev = W.mysql('[DB] Record Event Recording Attached', (
        "INSERT IGNORE INTO wf_events\n"
        "  (event_key, event_type, event_domain, occurred_at, call_job_id, lead_id,\n"
        "   country_iso, route_key, provider, conversation_id, followup_id,\n"
        "   duration_seconds, source_workflow, execution_id, metadata_json)\n"
        "VALUES (?, 'RECORDING_ATTACHED', 'RECORDING', UTC_TIMESTAMP(), ?, ?, ?, ?, ?, ?, ?,\n"
        "        ?, 'WF10', ?, ?)"),
        8, offset=1,
        replacements='={{ "RECORDING_ATTACHED:" + ($(\'[TELEGRAM] Prepare Recording Message\').item.json.call_job_id || $(\'[TELEGRAM] Prepare Recording Message\').item.json.recording_ref) }},'
                     '={{ $(\'[TELEGRAM] Prepare Recording Message\').item.json.call_job_id }},'
                     '={{ $(\'[TELEGRAM] Prepare Recording Message\').item.json.lead_id }},'
                     '={{ $(\'[TELEGRAM] Prepare Recording Message\').item.json.country_iso }},'
                     '={{ $(\'[TELEGRAM] Prepare Recording Message\').item.json.route_key }},'
                     '={{ $(\'[TELEGRAM] Prepare Recording Message\').item.json.provider }},'
                     '={{ $(\'[TELEGRAM] Prepare Recording Message\').item.json.conversation_id }},'
                     '={{ $(\'[TELEGRAM] Prepare Recording Message\').item.json.followup_id }},'
                     '={{ $(\'[TELEGRAM] Prepare Recording Message\').item.json.duration_seconds }},'
                     '={{ $(\'[TELEGRAM] Prepare Recording Message\').item.json.execution_id }},'
                     '={{ JSON.stringify({correlation: $(\'[TELEGRAM] Prepare Recording Message\').item.json.correlation, source: $(\'[TELEGRAM] Prepare Recording Message\').item.json.source}) }}',
        on_error='continueRegularOutput', retry=2)
    W.chain(fin, fin_ev)

    summary = W.code('[LOG] Recording Summary', CORE + r'''
function count(n) { try { return $(n).all().length; } catch (e) { return 0; } }
const s = { workflow: 'WF10', execution_id: $execution.id,
            attached: count('[DB] Record Event Recording Attached'),
            below_min: count('[DB] Record Event Recording Short'),
            orphan: count('[LOG] Recording Orphan') };
console.log('[WF10][exec=' + $execution.id + '] ' + JSON.stringify(s));
return [{ json: s }];
''', 9)
    W.chain(fin_ev, summary)
    W.link(short_ev, summary)
    W.link(orph_log, summary)
    W.link(none_log, summary)
    return W
