"""TEMPLATE_WF2_CALL_DISPATCHER_V2 — despacho de llamadas.

Reemplaza a "WF2 UNIFICADO — Asterisk + Stringee".

Lo que YA NO hace, respecto de v1:
  · no evalúa interruptores: `/api/routes/active` ya devuelve solo lo que debe llamar
  · no ramifica por país (`if india`, `if nepal`): ramifica por `adapter_key`
  · no calcula follow-up (2h/3h/días hábiles): eso es del motor, y solo para
    resultados FINALES sin conversación
  · no usa BATCH_SIZE=6 / BATCH_SIZE=1 / batchSize=5 como capacidad de negocio
  · no trata un 401 como "no contestó"

Contratos: TEMPLATE_DATA v2.2 · PROVIDER_ADAPTER v2.2 · FOLLOWUP_ENGINE v2.2 ·
ANALYTICS_EVENT v2.2
"""

VERSION = '2.0.0'
NAME = 'TEMPLATE_WF2_CALL_DISPATCHER_V2'


def build(wfbuild, js):
    W = wfbuild.Workflow(
        NAME, VERSION,
        'TEMPLATE_DATA v2.2 · PROVIDER_ADAPTER v2.2 · FOLLOWUP_ENGINE v2.2 · ANALYTICS_EVENT v2.2',
        'WF2 UNIFICADO (v1) — apagar rutas desde el panel, luego desactivar este workflow (§12)',
        'Schedule cada 1 min + webhook manual para pruebas.')
    CORE, ENG, NOTES = js['lmcore.js'], js['lmengine.js'], js['lmnotes.js']
    PANEL = '={{ $env.LM_PANEL_URL || "http://172.18.0.1:8080" }}'
    CRM = '={{ $env.LM_LEADSTUDIO_URL || "https://lead-studio-9gnl.onrender.com" }}'

    # ══ 00 TRIGGERS ═══════════════════════════════════════════════════
    W.sticky(0, '00 — TRIGGERS', f"""{W.meta_header}

Corre **cada minuto**. Cada corrida despacha, como mucho, la capacidad vigente
de cada ruta activa en ese instante.

El webhook `dispatch-now-v2` existe para probar una corrida a mano sin esperar
al schedule. **No está desactivado**: en n8n un nodo `disabled` deja pasar los
datos igual, así que apagar cosas desde el JSON es un no-fix (§2.3). Lo que se
apaga, se apaga desde el panel.

Config: `GET /api/routes/active` · `GET /api/settings` (red interna).""",
              color=4, height=440)
    trig = W.schedule('[TRIGGER] Every Minute', 1, 0)
    hook = W.webhook('[TRIGGER] Manual Dispatch', 'dispatch-now-v2', 0, branch=1,
                     respond='lastNode')

    # ══ 01 LOAD CONFIG ════════════════════════════════════════════════
    W.sticky(1, '01 — LOAD CONFIG', """`GET /api/routes/active` devuelve **solo**
las rutas que deben llamar ahora: país ON ∧ proveedor ON ∧ ruta ON ∧ no
archivadas ∧ READY ∧ dentro del horario, con `capacity_now` ya resuelta.

**Los tres interruptores no se evalúan acá.** Un template que lea `countries`,
`voice_providers` o `call_routes` de la base viola el estándar (§1).

**Fail-closed**: si el panel no responde 200, o responde 0 rutas, el ciclo
termina sin llamar a nadie. Nunca hay un "modo degradado" que llame igual.""",
              color=3, height=420)
    routes = W.http('[CONFIG] Load Active Routes', 'GET', PANEL + '/api/routes/active', 1,
                    credentials={'httpHeaderAuth': {'id': '__PANEL_TOKEN_CREDENTIAL__',
                                                    'name': 'Landmark Panel API'}},
                    never_error=True, full_response=True, timeout=10000, retry=2,
                    on_error='continueRegularOutput')
    W.link(trig, routes)
    W.link(hook, routes)

    settings = W.http('[CONFIG] Load Settings', 'GET', PANEL + '/api/settings', 1, offset=1,
                      credentials={'httpHeaderAuth': {'id': '__PANEL_TOKEN_CREDENTIAL__',
                                                      'name': 'Landmark Panel API'}},
                      never_error=True, full_response=True, timeout=10000, retry=2,
                      on_error='continueRegularOutput')
    W.chain(routes, settings)

    cfg = W.code('[CONFIG] Check Config Response', CORE + r'''
// neverError=true SIEMPRE seguido de un check explícito (§9).
const routesResp = $('[CONFIG] Load Active Routes').first().json || {};
const setResp = $input.first().json || {};
const rcode = Number(routesResp.statusCode || 0);
const rbody = routesResp.body || {};
const routes = Array.isArray(rbody.routes) ? rbody.routes : [];

// Los settings son opcionales: si el endpoint falla se usan los defaults
// DOCUMENTADOS (iguales al seed de la migración) y se deja constancia.
const scode = Number(setResp.statusCode || 0);
const settings = (scode >= 200 && scode < 300 && setResp.body && setResp.body.settings)
  ? setResp.body.settings : null;
const defaults = { tech_retry_max: 8, tech_retry_backoff_cap_minutes: 60 };
const S = Object.assign({}, defaults, settings || {});
if (!settings) {
  console.log('[WF2] WARN /api/settings no disponible (HTTP ' + scode +
              '): usando defaults documentados');
}

// ── CERROJO DEL MODO DE OPERACION ──────────────────────────────────
// Segunda linea de defensa. El panel ya devuelve cero rutas invocables
// cuando el modo no lo permite, pero WF2 lo comprueba TAMBIEN aqui y
// termina antes de pedir un solo lead. Dos sistemas llamando a la vez
// significa que el mismo cliente recibe dos llamadas: merece dos
// cerrojos, no uno.
//
// FALLA CERRADO. WF2 exige una autorizacion EXPLICITA:
//
//     operating_mode === 'V2_PRIMARY'  Y  dispatch_allowed === true
//
// Cualquier otra cosa —ausente, invalida, ambigua, o la respuesta de un
// panel anterior que no conoce el campo— significa NO LLAMAR.
//
// La version anterior asumia V2_PRIMARY cuando el campo faltaba, "por
// compatibilidad". Eso convertia una respuesta incompleta en permiso
// para marcar telefonos de clientes. Un panel que no sabe decir en que
// modo esta no es una fuente de configuracion valida para WF2.
const operatingMode = rbody.operating_mode || null;
const dispatchAllowed = rbody.dispatch_allowed;
const modeOk = operatingMode === 'V2_PRIMARY' && dispatchAllowed === true;

if (!modeOk) {
  const motivo = !operatingMode
    ? 'the panel did not report operating_mode (panel too old, or an ' +
      'incomplete response). WF2 requires an explicit V2_PRIMARY.'
    : (dispatchAllowed !== true
        ? 'the panel did not explicitly allow dispatch (dispatch_allowed=' +
          JSON.stringify(dispatchAllowed) + ').'
        : 'operating_mode is ' + operatingMode + ', not V2_PRIMARY.');
  console.log('[WF2] despacho BLOQUEADO: ' + motivo + ' ' +
              (rbody.blocked_reason || '') +
              ' El ciclo termina sin llamar a nadie.');
  return [{ json: {
    config_ok: false,
    routes_http_status: rcode,
    route_count: 0,
    routes: [],
    settings: S,
    settings_from_api: !!settings,
    operating_mode: operatingMode || 'UNKNOWN',
    dispatch_allowed: false,
    error_code: (!operatingMode || rbody.error_code === 'OPERATING_MODE_UNKNOWN')
      ? 'OPERATING_MODE_UNKNOWN' : 'OPERATING_MODE_BLOCKED',
    error_detail: rbody.blocked_reason || motivo,
    execution_id: $execution.id
  }}];
}

const ok = rcode >= 200 && rcode < 300 && routes.length > 0;
if (!ok) {
  console.log('[WF2] sin rutas activas (HTTP ' + rcode + ', ' + routes.length +
              ' rutas): el ciclo termina sin llamar a nadie');
}
return [{ json: {
  config_ok: ok,
  routes_http_status: rcode,
  route_count: routes.length,
  routes: routes,
  settings: S,
  settings_from_api: !!settings,
  operating_mode: operatingMode,
  dispatch_allowed: true,
  error_code: ok ? null : (rcode >= 200 && rcode < 300 ? 'NO_ACTIVE_ROUTES' : 'CONFIG_ERROR'),
  execution_id: $execution.id
}}];
''', 1, offset=2)
    W.chain(settings, cfg)

    cfg_if = W.if_('[CONFIG] Config Is Usable', '={{ $json.config_ok }}', 'true', True,
                   1, offset=3, single=True)
    W.chain(cfg, cfg_if)
    cfg_err = W.code('[ERROR] Config Error Abort', CORE + r'''
// CONFIG_ERROR aborta el ciclo: NADIE llama (§9). Que no haya rutas activas no
// es un error — es el resultado correcto de apagar un país o estar fuera de
// horario — pero termina igual sin despachar.
const d = $json;
console.log('[WF2][exec=' + d.execution_id + '] action=abort code=' + d.error_code +
            ' http=' + d.routes_http_status + ' routes=' + d.route_count);
return [{ json: { ok: false, dispatched: 0, error_code: d.error_code,
                  route_count: d.route_count } }];
''', 1, branch=2, offset=3)
    W.link(cfg_if, cfg_err, 1)

    # ══ 02-04 COUNTRIES · ROUTES · CAPACITY ═══════════════════════════
    W.sticky(2, '02/03/04 — COUNTRIES · ROUTES · CAPACITY', """Las rutas activas se
agrupan por país. La **demanda del país** es la suma de la capacidad vigente de
sus rutas:

    India:  IN_PROVEEDOR1 = 5 · IN_STRINGEE = 3  →  demanda 8

Se pide **una sola cola por país** con `limit = demanda`, y los leads se
reparten entre las rutas por `priority`. v1 pedía una cola por rama, así que dos
ramas del mismo país leían el mismo pool y podían tomar el mismo lead.

`capacity_now` ya viene calculada por el panel (franja horaria vigente en el
huso del país, con DST). **Capacidad de negocio ≠ batching HTTP de n8n**: acá no
hay ningún BATCH_SIZE.

⚠️ **PV-13** (documentado en PENDING_VERIFICATION): esto asume que
`GET /api/leads/queue` es una LECTURA y no una reserva. Si resultara que reserva
leads, el reparto sigue siendo correcto —el claim atómico es por lead— pero
convendría revisar el `limit`.""", color=4, height=480)
    plan = W.code('[ROUTE] Build Country Plan', CORE + r'''
// Agrupa rutas por país y calcula la demanda efectiva. Sin un solo "if India":
// el país es un dato, no una rama.
const d = $json;
const byCountry = {};
for (const r of d.routes) {
  const cap = Number(r.capacity_now || 0);
  if (cap <= 0) continue;                       // ruta sin capacidad en esta franja
  const iso = r.iso;
  if (!byCountry[iso]) {
    byCountry[iso] = { iso: iso, country: r.country, timezone: r.timezone,
                       dial_prefix: r.dial_prefix,
                       national_number_len: r.national_number_len || null,
                       language: r.language, demand: 0, routes: [] };
  }
  byCountry[iso].demand += cap;
  byCountry[iso].routes.push({
    route_id: r.route_id, route_key: r.route_key, provider: r.provider,
    adapter_key: r.adapter_key, provider_endpoint: r.provider_endpoint || null,
    priority: Number(r.priority || 100), capacity: cap,
    caller_id: r.caller_id || null,
    elevenlabs_agent_id: r.elevenlabs_agent_id || null,
    elevenlabs_phone_number_id: r.elevenlabs_phone_number_id || null,
    language: r.language, timezone: r.timezone, iso: r.iso,
    country: r.country, dial_prefix: r.dial_prefix
  });
}
const out = [];
for (const iso of Object.keys(byCountry)) {
  const c = byCountry[iso];
  c.routes.sort((a, b) => a.priority - b.priority || a.route_key.localeCompare(b.route_key));
  console.log('[WF2][exec=' + d.execution_id + '][' + iso + '] demanda=' + c.demand +
              ' rutas=' + c.routes.map(r => r.route_key + ':' + r.capacity).join(','));
  out.push({ json: Object.assign({}, c, { settings: d.settings,
                                          execution_id: d.execution_id }) });
}
return out;
''', 2)
    W.link(cfg_if, plan, 0)

    # ══ 05 FETCH ELIGIBLE LEADS ═══════════════════════════════════════
    W.sticky(5, '05 — FETCH ELIGIBLE LEADS', """Un login por ciclo (credential
`LeadStudio Login`, Custom Auth: el usuario y la contraseña **no** están en este
JSON).

Dos lecturas por país:
 · **reactivación** — leads `NO_ANSWER` cuyo `nextFollowUpAt` ya venció vuelven
   a `NOT_CONTACTED` (es lo que hacía v1, pero genérico: sin un par de nodos por
   país)
 · **cola** — `status=NOT_CONTACTED`, `limit` = demanda del país

`doNotCall` y un `status` distinto de `NOT_CONTACTED` excluyen el lead.""",
              color=6, height=420)
    login = W.http('[CRM] Get Token', 'POST', CRM + '/api/auth/login', 5,
                   headers={'Content-Type': 'application/json'},
                   credentials={'httpCustomAuth': {'id': '__LEADSTUDIO_LOGIN_CREDENTIAL__',
                                                   'name': 'LeadStudio Login'}},
                   never_error=True, full_response=True, timeout=15000, retry=2,
                   on_error='continueRegularOutput')
    W.chain(plan, login)

    tok = W.code('[CRM] Check Token', CORE + r'''
const resp = $input.first().json || {};
const code = Number(resp.statusCode || 0);
const token = (resp.body && resp.body.accessToken) || null;
const plans = $('[ROUTE] Build Country Plan').all();
if (!token) {
  console.log('[WF2] AUTH_ERROR: login a LeadStudio fallo (HTTP ' + code + ')');
}
return plans.map(p => ({ json: Object.assign({}, p.json, {
  access_token: token, token_ok: !!token, token_http_status: code
})}));
''', 5, offset=1)
    W.chain(login, tok)
    tok_if = W.if_('[CRM] Token Is Valid', '={{ $json.token_ok }}', 'true', True, 5,
                   offset=2, single=True)
    W.chain(tok, tok_if)
    tok_err = W.code('[ERROR] Auth Error Abort', CORE + r'''
// AUTH_ERROR aborta la rama y alerta (§9). No se llama a nadie sin poder
// registrar el intento en el CRM.
const d = $json;
console.log('[WF2][exec=' + d.execution_id + '] action=abort code=AUTH_ERROR http=' +
            d.token_http_status);
return [{ json: { ok: false, error_code: 'AUTH_ERROR', country: d.iso } }];
''', 5, branch=3, offset=2)
    W.link(tok_if, tok_err, 1)

    # reactivación de vencidos (genérica por país)
    react_get = W.http('[LEADS] Fetch Due Follow Ups', 'GET', CRM + '/api/leads/queue', 6,
                       branch=2,
                       query={'limit': '={{ Math.min(200, $json.demand * 10) }}',
                              'country': '={{ $json.iso }}', 'status': 'NO_ANSWER'},
                       headers={'Authorization': '=Bearer {{ $json.access_token }}'},
                       never_error=True, full_response=True, timeout=15000, retry=2,
                       on_error='continueRegularOutput')
    W.link(tok_if, react_get, 0)
    react_pick = W.code('[LEADS] Select Due For Reactivation', CORE + r'''
// Un lead NO_ANSWER con nextFollowUpAt vencido vuelve a estar disponible.
// Genérico: el país es un parámetro, no una rama (v1 tenía 2 nodos por país).
const ctx = $('[CRM] Token Is Valid').item.json;
const resp = $json || {};
const code = Number(resp.statusCode || 0);
const leads = (resp.body && Array.isArray(resp.body.leads)) ? resp.body.leads : [];
if (code < 200 || code >= 300) {
  console.log('[WF2][' + ctx.iso + '] WARN cola NO_ANSWER HTTP ' + code + ': sin reactivar');
  return [];
}
const now = Date.now();
const due = leads.filter(l => l.nextFollowUpAt && new Date(l.nextFollowUpAt).getTime() <= now
                              && !l.doNotCall);
console.log('[WF2][exec=' + ctx.execution_id + '][' + ctx.iso + '] reactivables=' +
            due.length + '/' + leads.length);
return due.map(l => ({ json: { lead_id: l.id, iso: ctx.iso,
                               access_token: ctx.access_token,
                               execution_id: ctx.execution_id } }));
''', 6, branch=2, offset=1)
    W.chain(react_get, react_pick)
    react_patch = W.http('[CRM] Reactivate Lead', 'PATCH',
                         CRM + '/api/leads/{{ $json.lead_id }}', 6, branch=2, offset=2,
                         headers={'Content-Type': 'application/json',
                                  'Authorization': '=Bearer {{ $json.access_token }}'},
                         body_json='={{ JSON.stringify({ status: "NOT_CONTACTED" }) }}',
                         never_error=True, full_response=True, timeout=15000,
                         on_error='continueRegularOutput')
    W.chain(react_pick, react_patch)
    react_log = W.code('[LOG] Reactivated', CORE + r'''
const n = $input.all().length;
console.log('[WF2] leads reactivados en este ciclo: ' + n);
return [{ json: { reactivated: n } }];
''', 6, branch=2, offset=3)
    W.chain(react_patch, react_log)

    # cola normal
    queue = W.http('[LEADS] Fetch Country Queue', 'GET', CRM + '/api/leads/queue', 6,
                   query={'limit': '={{ $json.demand }}', 'country': '={{ $json.iso }}',
                          'status': 'NOT_CONTACTED'},
                   headers={'Authorization': '=Bearer {{ $json.access_token }}'},
                   never_error=True, full_response=True, timeout=15000, retry=2,
                   on_error='continueRegularOutput')
    W.link(tok_if, queue, 0)

    # ══ 06 NORMALIZE + 08 ALLOCATION ══════════════════════════════════
    W.sticky(7, '06/08 — NORMALIZE & ROUTE ALLOCATION', """Se normaliza el teléfono a
**E.164 con `+`** usando el `dial_prefix` **del país** (dato de la ruta, no un
literal). El adapter que lo necesite sin `+` lo quita él mismo, en su
`Build Request`.

Reparto: los leads se asignan a las rutas del país por `priority`, hasta llenar
la `capacity` de cada una. La ruta que sobra capacidad no roba leads a la
siguiente.

Exclusiones (una por línea, todas con motivo logueado):
 · `doNotCall`
 · `status != NOT_CONTACTED`
 · teléfono que no forma un E.164 válido para el prefijo del país
 · país sin ruta con capacidad (no debería llegar acá)""", color=6, height=440)
    alloc = W.code('[LEADS] Normalize And Allocate', CORE + r'''
const ctx = $('[CRM] Token Is Valid').item.json;
const resp = $json || {};
const code = Number(resp.statusCode || 0);
const leads = (resp.body && Array.isArray(resp.body.leads)) ? resp.body.leads : [];
if (code < 200 || code >= 300) {
  console.log('[WF2][' + ctx.iso + '] ERROR cola HTTP ' + code + ': sin despachar');
  return [];
}

function toE164(raw, prefix, natLen) {
  let p = String(raw || '').replace(/[^0-9+]/g, '');
  if (!p) return '';
  if (p.startsWith('+')) return p;
  const pd = String(prefix || '').replace('+', '');
  if (pd && p.startsWith(pd) && p.length > pd.length + 5) return '+' + p;
  if (natLen && p.length === Number(natLen)) return prefix + p;
  if (!pd) return '+' + p;
  return prefix + p;
}

const skipped = [];
const eligible = [];
for (const l of leads) {
  const status = String(l.status || '').trim().toUpperCase();
  if (!l.id) { skipped.push('sin id'); continue; }
  if (l.doNotCall) { skipped.push(l.id + ':doNotCall'); continue; }
  if (status !== 'NOT_CONTACTED') { skipped.push(l.id + ':status=' + status); continue; }
  const phone = toE164(l.phone, ctx.dial_prefix, ctx.national_number_len);
  if (!/^\+[1-9]\d{7,14}$/.test(phone)) { skipped.push(l.id + ':phone'); continue; }
  eligible.push({ lead_id: String(l.id), full_name: String(l.name || '').trim() || 'Customer',
                  phone: phone, language: String(l.language || ctx.language || 'en').toLowerCase(),
                  crm_attempts: Number(l.attempts || 0) });
}

// Reparto por prioridad, respetando la capacidad de CADA ruta.
const out = [];
let i = 0;
for (const r of ctx.routes) {
  for (let k = 0; k < r.capacity && i < eligible.length; k++, i++) {
    const lead = eligible[i];
    out.push({ json: Object.assign({}, lead, {
      route_id: r.route_id, route_key: r.route_key, country_iso: r.iso,
      country: r.country, provider: r.provider, adapter_key: r.adapter_key,
      provider_endpoint: r.provider_endpoint,
      elevenlabs_agent_id: r.elevenlabs_agent_id,
      elevenlabs_phone_number_id: r.elevenlabs_phone_number_id,
      caller_id: r.caller_id, timezone: r.timezone,
      access_token: ctx.access_token, execution_id: ctx.execution_id,
      settings: ctx.settings
    })});
  }
}
console.log('[WF2][exec=' + ctx.execution_id + '][' + ctx.iso + '] elegibles=' +
            eligible.length + ' asignados=' + out.length + ' descartados=' +
            skipped.length + ' ' + JSON.stringify(skipped).slice(0, 300));
return out;
''', 7)
    W.chain(queue, alloc)

    # ══ 07 ATOMIC CLAIM ═══════════════════════════════════════════════
    W.sticky(8, '07 — ATOMIC CLAIM', """`UNIQUE(lead_id, attempt)` decide, de forma
atómica, quién despacha. El ganador se confirma **releyendo el `call_job_id`
propio**, nunca por `affectedRows` (el driver `mysql2` cambia su significado
según `FOUND_ROWS`).

`attempt` = `next_attempt()`: si hay un intento `RELEASED` por error técnico se
retoma **ese mismo número** (un 401 no consume un intento de negocio); si no,
`max(CRM, local) + 1`.

`UNIQUE(inflight_lead)` garantiza además **una sola llamada en vuelo por lead**,
en cualquier intento y desde cualquier ruta. Perder el claim es un **SKIP**
normal, no un error.

Un `call_job_id` aleatorio por sí solo NO alcanza: la unicidad la da el índice.""",
              color=3, height=460)
    nexta = W.mysql('[DB] Resolve Next Attempt', (
        "SELECT ? AS _lead,\n"
        "  (SELECT MIN(attempt) FROM wf_call_jobs\n"
        "     WHERE lead_id = ? AND state = 'RELEASED'\n"
        "       AND (next_tech_retry_at IS NULL OR next_tech_retry_at <= UTC_TIMESTAMP())\n"
        "  ) AS released_attempt,\n"
        "  (SELECT MIN(attempt) FROM wf_call_jobs\n"
        "     WHERE lead_id = ? AND state = 'RELEASED') AS released_any,\n"
        "  (SELECT MAX(attempt) FROM wf_call_jobs WHERE lead_id = ?) AS max_attempt,\n"
        "  (SELECT COUNT(*) FROM wf_call_jobs WHERE inflight_lead = ?) AS inflight,\n"
        "  (SELECT MAX(tech_retry_count) FROM wf_call_jobs\n"
        "     WHERE lead_id = ? AND state = 'RELEASED') AS tech_retries"),
        8, replacements='={{ $json.lead_id }},={{ $json.lead_id }},={{ $json.lead_id }},'
                        '={{ $json.lead_id }},={{ $json.lead_id }},={{ $json.lead_id }}',
        retry=2, on_error='continueRegularOutput')
    W.chain(alloc, nexta)

    mkclaim = W.code('[CLAIM] Build Call Job', CORE + r'''
// call_job_id = {route_key}-{UTC compacto}-{6 hex}. Es la PK lógica del
// despacho y viaja en dynamic_variables hasta el post-call.
const crypto = require('crypto');
const rows = $input.all();
const out = [];
for (let i = 0; i < rows.length; i++) {
  const r = rows[i].json || {};
  const lead = $('[LEADS] Normalize And Allocate').itemMatching(i).json;
  const S = lead.settings || {};
  const techMax = Number(S.tech_retry_max || 8);

  const released = r.released_attempt;
  const releasedAny = r.released_any;
  const maxAttempt = Number(r.max_attempt || 0);
  const inflight = Number(r.inflight || 0);
  const techRetries = Number(r.tech_retries || 0);

  // Un lead con una llamada en vuelo (incluye NEEDS_RECONCILIATION) no se
  // vuelve a llamar. El UNIQUE lo impediría igual; acá se evita el intento.
  if (inflight > 0) {
    console.log('[WF2][exec=' + lead.execution_id + '][' + lead.route_key +
                '] skip lead=' + lead.lead_id + ' motivo=IN_FLIGHT');
    continue;
  }
  // Intento liberado que sigue en backoff: todavía no toca.
  if (released === null && releasedAny !== null && releasedAny !== undefined) {
    console.log('[WF2][exec=' + lead.execution_id + '][' + lead.route_key +
                '] skip lead=' + lead.lead_id + ' motivo=TECH_BACKOFF');
    continue;
  }
  // Tope de reintentos técnicos: no se insiste para siempre contra un
  // proveedor caído; queda para reconciliación.
  if (released !== null && released !== undefined && techRetries >= techMax) {
    console.log('[WF2][exec=' + lead.execution_id + '][' + lead.route_key +
                '] skip lead=' + lead.lead_id + ' motivo=TECH_RETRY_EXHAUSTED');
    continue;
  }

  const attempt = (released !== null && released !== undefined)
    ? Number(released)
    : Math.max(Number(lead.crm_attempts || 0), maxAttempt) + 1;

  const stamp = new Date().toISOString().replace(/[-:]/g, '').replace(/\.\d{3}Z$/, 'Z');
  const callJobId = lead.route_key + '-' + stamp + '-' + crypto.randomBytes(3).toString('hex');

  out.push({ json: Object.assign({}, lead, {
    attempt: attempt,
    call_job_id: callJobId,
    is_tech_reclaim: (released !== null && released !== undefined)
  })});
}
return out;
''', 8, offset=1)
    W.chain(nexta, mkclaim)

    ins = W.mysql('[DB] Insert Claim', (
        "INSERT IGNORE INTO wf_call_jobs\n"
        "  (call_job_id, lead_id, route_id, route_key, country_iso, provider,\n"
        "   adapter_key, attempt, execution_id, state, created_at, claimed_at)\n"
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'CLAIMED', UTC_TIMESTAMP(), UTC_TIMESTAMP())"),
        8, offset=2,
        replacements='={{ $json.call_job_id }},={{ $json.lead_id }},={{ $json.route_id }},'
                     '={{ $json.route_key }},={{ $json.country_iso }},={{ $json.provider }},'
                     '={{ $json.adapter_key }},={{ $json.attempt }},={{ $json.execution_id }}',
        retry=2, on_error='continueRegularOutput')
    W.chain(mkclaim, ins)

    reclaim = W.mysql('[DB] Reclaim Released Attempt', (
        "UPDATE wf_call_jobs\n"
        "   SET call_job_id = ?, route_id = ?, route_key = ?, country_iso = ?,\n"
        "       provider = ?, adapter_key = ?, execution_id = ?, state = 'CLAIMED',\n"
        "       claimed_at = UTC_TIMESTAMP(), updated_at = UTC_TIMESTAMP()\n"
        " WHERE lead_id = ? AND attempt = ? AND state = 'RELEASED'\n"
        "   AND (next_tech_retry_at IS NULL OR next_tech_retry_at <= UTC_TIMESTAMP())"),
        8, offset=3,
        replacements='={{ $json.call_job_id }},={{ $json.route_id }},={{ $json.route_key }},'
                     '={{ $json.country_iso }},={{ $json.provider }},={{ $json.adapter_key }},'
                     '={{ $json.execution_id }},={{ $json.lead_id }},={{ $json.attempt }}',
        retry=2, on_error='continueRegularOutput')
    W.chain(ins, reclaim)

    owner = W.mysql('[DB] Read Claim Owner', (
        "SELECT ? AS _lead, ? AS _attempt,\n"
        "  (SELECT call_job_id FROM wf_call_jobs\n"
        "     WHERE lead_id = ? AND attempt = ?) AS owner_call_job_id"),
        8, offset=4,
        replacements='={{ $(\'[CLAIM] Build Call Job\').item.json.lead_id }},'
                     '={{ $(\'[CLAIM] Build Call Job\').item.json.attempt }},'
                     '={{ $(\'[CLAIM] Build Call Job\').item.json.lead_id }},'
                     '={{ $(\'[CLAIM] Build Call Job\').item.json.attempt }}',
        retry=2, on_error='continueRegularOutput')
    W.chain(reclaim, owner)

    won = W.code('[CLAIM] Check Claim Owner', CORE + r'''
// Relectura del token propio: el dueño de (lead_id, attempt) es quien despacha.
const rows = $input.all();
return rows.map((r, i) => {
  const ctx = $('[CLAIM] Build Call Job').itemMatching(i).json;
  const owner = (r.json || {}).owner_call_job_id || null;
  const winner = owner === ctx.call_job_id;
  if (!winner) {
    console.log('[WF2][exec=' + ctx.execution_id + '][' + ctx.route_key +
                '] skip lead=' + ctx.lead_id + ' attempt=' + ctx.attempt +
                ' motivo=CLAIM_LOST owner=' + owner);
  }
  return { json: Object.assign({}, ctx, { claim_won: winner, claim_owner: owner }) };
});
''', 8, offset=5)
    W.chain(owner, won)
    won_if = W.if_('[CLAIM] Claim Won', '={{ $json.claim_won }}', 'true', True, 8,
                   offset=6, single=True)
    W.chain(won, won_if)
    skip_log = W.code('[LOG] Claim Skipped', CORE + r'''
// Perder el claim NO es un error: otra ruta del mismo país (o la corrida
// anterior) ya tomó ese lead. Es exactamente lo que evita la llamada doble.
const n = $input.all().length;
console.log('[WF2] claims perdidos en este ciclo: ' + n + ' (comportamiento esperado)');
return [{ json: { skipped_claims: n } }];
''', 8, branch=3, offset=6)
    W.link(won_if, skip_log, 1)

    ev_claim = W.mysql('[DB] Record Event Call Claimed', (
        "INSERT IGNORE INTO wf_events\n"
        "  (event_key, event_type, event_domain, occurred_at, call_job_id, lead_id,\n"
        "   country_iso, route_key, provider, adapter_key, attempt,\n"
        "   source_workflow, execution_id)\n"
        "VALUES (?, 'CALL_CLAIMED', 'CALL', UTC_TIMESTAMP(), ?, ?, ?, ?, ?, ?, ?,\n"
        "        'WF2', ?)"),
        9, replacements='={{ "CALL_CLAIMED:" + $json.call_job_id }},={{ $json.call_job_id }},'
                        '={{ $json.lead_id }},={{ $json.country_iso }},={{ $json.route_key }},'
                        '={{ $json.provider }},={{ $json.adapter_key }},={{ $json.attempt }},'
                        '={{ $json.execution_id }}',
        on_error='continueRegularOutput', retry=2)
    W.link(won_if, ev_claim, 0)

    # ══ 13 CRM ATTEMPTING ═════════════════════════════════════════════
    W.sticky(9, '13 — CRM: ATTEMPTING', """Se marca el intento en LeadStudio
**antes** de llamar, lo más temprano que es seguro: si el CRM no puede registrar
el intento, no se llama.

Si el PATCH falla, es un **error TÉCNICO**: el job va a `RELEASED`, no consume
el intento de negocio, y se reintenta con backoff. No es un NO_ANSWER.""",
              color=2, height=360)
    attempting = W.http('[CRM] Mark Attempting', 'PATCH',
                        CRM + '/api/leads/{{ $json.lead_id }}', 9, offset=1,
                        headers={'Content-Type': 'application/json',
                                 'Authorization': '=Bearer {{ $json.access_token }}'},
                        body_json='={{ JSON.stringify({ status: "ATTEMPTING", '
                                  'attempts: $json.attempt }) }}',
                        never_error=True, full_response=True, timeout=15000,
                        on_error='continueRegularOutput')
    W.chain(ev_claim, attempting)

    att_check = W.code('[CRM] Check Attempting', CORE + r'''
const rows = $input.all();
return rows.map((r, i) => {
  const ctx = $('[CLAIM] Check Claim Owner').itemMatching(i).json;
  const code = Number((r.json || {}).statusCode || 0);
  const ok = code >= 200 && code < 300;
  return { json: Object.assign({}, ctx, {
    attempting_ok: ok, attempting_http_status: code,
    // Marcar el intento es parte del contrato con el CRM: si falla, no llamamos.
    tech_error_code: ok ? null : (code === 401 || code === 403 ? 'AUTH_ERROR' : 'CRM_ERROR')
  })};
});
''', 9, offset=2)
    W.chain(attempting, att_check)
    att_if = W.if_('[CRM] Attempting Recorded', '={{ $json.attempting_ok }}', 'true',
                   True, 9, offset=3, single=True)
    W.chain(att_check, att_if)

    # ══ DISPATCHING antes del HTTP ════════════════════════════════════
    W.sticky(10, '09 — ADAPTER ROUTING', """`DISPATCHING` se escribe **SIEMPRE antes**
del HTTP al proveedor. Si n8n se cae entre el dispatch y la normalización, el
job queda en `DISPATCHING` y el reconciliador lo manda a
`NEEDS_RECONCILIATION`: **no se vuelve a llamar**.

El Switch ramifica por **`adapter_key`**, nunca por proveedor ni por país. Un
futuro PROVEEDOR2 con `ELEVENLABS_SIP` reutiliza la misma rama: cero código
nuevo.

La salida *fallback* (adapter desconocido) va a `CONFIG_ERROR`. No debería
ocurrir —el panel no deja activar un adapter fuera del catálogo— pero la rama
existe: ninguna salida de Switch queda sin conectar.""", color=5, height=440)
    dispatching = W.mysql('[DB] Mark Dispatching', (
        "UPDATE wf_call_jobs SET state = 'DISPATCHING', updated_at = UTC_TIMESTAMP()\n"
        " WHERE call_job_id = ? AND state = 'CLAIMED'"),
        10, replacements='={{ $json.call_job_id }}', retry=2,
        on_error='continueRegularOutput')
    W.link(att_if, dispatching, 0)

    disp_ctx = W.code('[ROUTE] Prepare Dispatch Request', CORE + r'''
// dispatch_request del PROVIDER_ADAPTER_CONTRACT. Todo sale de la ruta y del
// lead; el adapter no aporta ningún dato de negocio.
const rows = $input.all();
return rows.map((r, i) => {
  const d = $('[CRM] Attempting Recorded').itemMatching(i).json;
  return { json: Object.assign({}, d, {
    dynamic_variables: {
      lead_id: String(d.lead_id),
      full_name: String(d.full_name),
      phone: String(d.phone),
      country: String(d.country || ''),
      country_iso: String(d.country_iso),
      language: String(d.language),
      attempt: String(d.attempt),
      call_attempts: String(d.attempt),
      call_job_id: String(d.call_job_id),
      route_key: String(d.route_key),
      route_id: String(d.route_id),
      provider: String(d.provider),
      adapter_key: String(d.adapter_key),
      is_followup: 'false'
    }
  })};
});
''', 10, offset=1)
    W.chain(dispatching, disp_ctx)

    adapter_sw = W.switch('[ROUTE] Resolve Adapter', '={{ $json.adapter_key }}',
                          [('ELEVENLABS_SIP', 'sip'), ('STRINGEE_WORKER', 'stringee')],
                          10, offset=2)
    W.chain(disp_ctx, adapter_sw)

    # ══ 10 ELEVENLABS SIP ═════════════════════════════════════════════
    W.sticky(11, '10 — ADAPTER: ELEVENLABS_SIP', """Los cuatro pasos obligatorios del
`PROVIDER_ADAPTER_CONTRACT`: Build Request → Dispatch → (respuesta cruda) →
Normalize Dispatch.

Este adapter **no** decide reintentos, **no** programa follow-ups, **no**
interpreta qué SIP significa "no contestó" (eso es política de la ruta) y **no**
toca `wf_call_jobs`.

Devuelve `dispatch_outcome`: ACCEPTED · FINAL · TECHNICAL_ERROR · UNKNOWN.
Ante la duda entre TECHNICAL_ERROR y UNKNOWN → **UNKNOWN**: es preferible
reconciliar a mano que llamar dos veces.

La API key vive en la credential `ElevenLabs API`, nunca en el nodo.""",
              color=5, height=460)
    sip_build = W.code('[ELEVENLABS_SIP] Build Request', r'''
// INPUT NORMALIZATION: dispatch_request -> body de ElevenLabs.
return $input.all().map(it => {
  const d = it.json;
  return { json: Object.assign({}, d, {
    provider_body: {
      agent_id: d.elevenlabs_agent_id,
      agent_phone_number_id: d.elevenlabs_phone_number_id,
      to_number: d.phone,                      // E.164 CON '+' para SIP
      conversation_initiation_client_data: {
        conversation_config_override: { agent: { language: d.language } },
        dynamic_variables: d.dynamic_variables
      }
    }
  })};
});
''', 11)
    W.link(adapter_sw, sip_build, 0)
    sip_disp = W.http('[ELEVENLABS_SIP] Dispatch', 'POST',
                      'https://api.elevenlabs.io/v1/convai/sip-trunk/outbound-call', 11,
                      offset=1,
                      headers={'Content-Type': 'application/json'},
                      credentials={'httpHeaderAuth': {'id': '__ELEVENLABS_CREDENTIAL__',
                                                      'name': 'ElevenLabs API'}},
                      body_json='={{ JSON.stringify($json.provider_body) }}',
                      never_error=True, full_response=True, timeout=45000,
                      on_error='continueRegularOutput')
    W.chain(sip_build, sip_disp)
    sip_norm = W.code('[ELEVENLABS_SIP] Normalize Dispatch', CORE + r'''
// RESULT NORMALIZATION: respuesta cruda -> dispatch_result.
// La tabla de clasificación es la de PROVIDER_ADAPTER_CONTRACT ·
// technical_classification. El adapter NO decide política.
const rows = $input.all();
return rows.map((r, i) => {
  const d = $('[ELEVENLABS_SIP] Build Request').itemMatching(i).json;
  const resp = r.json || {};
  const code = Number(resp.statusCode || 0);
  const body = resp.body || {};
  const conv = body.conversation_id || null;
  const msg = String(body.message || body.detail || '');

  // SIP crudo. Qué significa 603 lo decide la POLÍTICA, no este nodo.
  const sipMatch = msg.match(/SIP\s*(\d{3})/i) || msg.match(/\b([4-6]\d{2})\b/);
  const sipCode = sipMatch ? sipMatch[1] : null;

  let outcome, errorCode = null, errorClass = null, finalResult = null;
  if (code >= 200 && code < 300 && body.success === true && conv) {
    outcome = 'ACCEPTED';
  } else if (code === 401 || code === 403) {
    outcome = 'TECHNICAL_ERROR'; errorCode = 'AUTH_ERROR'; errorClass = 'TECHNICAL';
  } else if (code === 0) {
    // timeout o conexión cortada: la llamada PUDO salir
    outcome = 'UNKNOWN'; errorCode = 'DISPATCH_UNKNOWN'; errorClass = 'AMBIGUOUS';
  } else if (code >= 500) {
    outcome = 'UNKNOWN'; errorCode = 'DISPATCH_UNKNOWN'; errorClass = 'AMBIGUOUS';
  } else if (sipCode && !conv) {
    // Resultado FINAL: hubo intento telefónico y no habrá post-call.
    outcome = 'FINAL'; finalResult = 'FAILED'; errorCode = 'SIP_' + sipCode;
  } else if (code >= 400 && code < 500) {
    // 4xx sin SIP y sin conversación: config o datos. No salió llamada.
    outcome = 'TECHNICAL_ERROR'; errorCode = 'CONFIG_ERROR'; errorClass = 'TECHNICAL';
  } else {
    outcome = 'UNKNOWN'; errorCode = 'DISPATCH_UNKNOWN'; errorClass = 'AMBIGUOUS';
  }

  return { json: Object.assign({}, d, {
    dispatch_outcome: outcome,
    conversation_id: conv,
    provider_job_id: null,
    provider_call_id: body.callSid || null,
    sip_code: sipCode,
    dispatch_http_status: code,
    final_result: finalResult,
    error_code: errorCode,
    error_class: errorClass,
    error_detail: msg.slice(0, 200),
    raw_excerpt: JSON.stringify(body).slice(0, 200)
  })};
});
''', 11, offset=2)
    W.chain(sip_disp, sip_norm)

    # ══ 11 STRINGEE WORKER ════════════════════════════════════════════
    W.sticky(12, '11 — ADAPTER: STRINGEE_WORKER', """Mismos cuatro pasos. El worker
recibe el teléfono **sin `+`** (así lo exige hoy) y devuelve `job_id`.

`job_id` significa **DISPATCHED**, no ANSWERED ni NO_ANSWER. El resultado real
llega después por el post-call de ElevenLabs y lo procesa WF9. v1 marcaba
`callStatus: ANSWERED` con `DISPATCHED`, que es falso.

`callback_url` apunta al webhook de WF9: el worker ya sabe reenviar el estado
final (`sendCallback`), incluido `elevenlabs_conversation_id`.

HTTP 429 `worker_capacity_reached` es **TECHNICAL_ERROR**: el worker está lleno,
la llamada no salió y el intento de negocio **no** se consume.""",
              color=5, height=460)
    st_build = W.code('[STRINGEE_WORKER] Build Request', r'''
// INPUT NORMALIZATION. El worker exige el teléfono SIN '+'; quitarlo es
// responsabilidad del adapter, no del dispatcher.
return $input.all().map(it => {
  const d = it.json;
  return { json: Object.assign({}, d, {
    provider_body: {
      lead_id: d.lead_id,
      phone: String(d.phone).replace(/^\+/, ''),
      full_name: d.full_name,
      country: d.country || d.country_iso,
      language: d.language,
      is_followup: 'false',
      call_attempts: Number(d.attempt),
      from_number: d.caller_id,
      agent_id: d.elevenlabs_agent_id,
      // El worker reenvía el estado final acá cuando termina la llamada.
      callback_url: (process.env.LM_WF9_CALLBACK_URL || '')
    }
  })};
});
''', 12)
    W.link(adapter_sw, st_build, 1)
    st_disp = W.http('[STRINGEE_WORKER] Dispatch Worker', 'POST',
                     '={{ $json.provider_endpoint || "http://172.18.0.1:8091" }}/call', 12,
                     offset=1, headers={'Content-Type': 'application/json'},
                     body_json='={{ JSON.stringify($json.provider_body) }}',
                     never_error=True, full_response=True, timeout=45000,
                     on_error='continueRegularOutput')
    W.chain(st_build, st_disp)
    st_norm = W.code('[STRINGEE_WORKER] Normalize Dispatch', CORE + r'''
// RESULT NORMALIZATION. El worker SOLO confirma que la llamada se disparó.
// Nunca produce un resultado FINAL: por eso este adapter no emite 'FINAL'.
const rows = $input.all();
return rows.map((r, i) => {
  const d = $('[STRINGEE_WORKER] Build Request').itemMatching(i).json;
  const resp = r.json || {};
  const code = Number(resp.statusCode || 0);
  const body = resp.body || {};
  const jobId = body.job_id || body.jobId || null;

  let outcome, errorCode = null, errorClass = null;
  if (code >= 200 && code < 300 && body.ok === true && jobId) {
    outcome = 'ACCEPTED';
  } else if (code === 401 || code === 403) {
    outcome = 'TECHNICAL_ERROR'; errorCode = 'AUTH_ERROR'; errorClass = 'TECHNICAL';
  } else if (code === 429) {
    // worker_capacity_reached: no salió llamada, no consume intento.
    outcome = 'TECHNICAL_ERROR'; errorCode = 'PROVIDER_BUSY'; errorClass = 'TECHNICAL';
  } else if (code === 400) {
    // missing_lead_id / invalid_phone: dato inválido detectado por el worker.
    outcome = 'TECHNICAL_ERROR'; errorCode = 'VALIDATION_ERROR'; errorClass = 'TECHNICAL';
  } else if (code === 503) {
    outcome = 'TECHNICAL_ERROR'; errorCode = 'PROVIDER_ERROR'; errorClass = 'TECHNICAL';
  } else if (code === 0 || code >= 500) {
    outcome = 'UNKNOWN'; errorCode = 'DISPATCH_UNKNOWN'; errorClass = 'AMBIGUOUS';
  } else {
    outcome = 'UNKNOWN'; errorCode = 'DISPATCH_UNKNOWN'; errorClass = 'AMBIGUOUS';
  }

  return { json: Object.assign({}, d, {
    dispatch_outcome: outcome,
    conversation_id: null,                 // llega en el post-call, no acá
    provider_job_id: jobId,
    provider_call_id: body.stringee_call_id || null,
    sip_code: null,
    dispatch_http_status: code,
    final_result: null,
    error_code: errorCode,
    error_class: errorClass,
    error_detail: String(body.error || '').slice(0, 200),
    raw_excerpt: JSON.stringify(body).slice(0, 200)
  })};
});
''', 12, offset=2)
    W.chain(st_disp, st_norm)

    adapter_err = W.code('[ERROR] Unsupported Adapter', CORE + r'''
// El panel no deja activar un adapter fuera del catálogo, así que esto no
// debería ocurrir nunca. Si ocurre es CONFIG_ERROR: no se llama, se registra.
return $input.all().map(it => ({ json: Object.assign({}, it.json, {
  dispatch_outcome: 'TECHNICAL_ERROR',
  error_code: 'CONFIG_ERROR',
  error_class: 'TECHNICAL',
  error_detail: 'adapter_key sin implementacion en WF2: ' + it.json.adapter_key,
  dispatch_http_status: null, conversation_id: null, provider_job_id: null,
  provider_call_id: null, sip_code: null, final_result: null, raw_excerpt: null
})}));
''', 12, branch=4)
    W.link(adapter_sw, adapter_err, 2)

    # ══ 12 NORMALIZE DISPATCH (común) ═════════════════════════════════
    W.sticky(13, '12 — DISPATCH OUTCOME ROUTER', """Las tres ramas de adapter
convergen acá con el **mismo** `dispatch_result`. A partir de este punto el
dispatcher no sabe —ni necesita saber— qué proveedor fue.

| outcome | qué hace el dispatcher |
|---|---|
| `ACCEPTED` | job → DISPATCHED · evento · **fin de la rama** (sin motor) |
| `FINAL` | registra el resultado y **llama al motor** con clave `call_job_id` |
| `TECHNICAL_ERROR` | job → RELEASED + backoff · **no consume intento** · no llega al motor |
| `UNKNOWN` | job → UNKNOWN → NEEDS_RECONCILIATION · **jamás re-dispatch** |""",
              color=5, height=420)
    outcome_sw = W.switch('[RESULT] Dispatch Outcome Router', '={{ $json.dispatch_outcome }}',
                          [('ACCEPTED', 'accepted'), ('FINAL', 'final'),
                           ('TECHNICAL_ERROR', 'technical'), ('UNKNOWN', 'unknown')],
                          13, fallback='none')
    W.link(sip_norm, outcome_sw)
    W.link(st_norm, outcome_sw)
    W.link(adapter_err, outcome_sw)

    # ── ACCEPTED ──────────────────────────────────────────────────────
    acc = W.mysql('[DB] Mark Dispatched', (
        "UPDATE wf_call_jobs\n"
        "   SET state = 'DISPATCHED', conversation_id = ?, provider_job_id = ?,\n"
        "       provider_call_id = ?, dispatch_http_status = ?,\n"
        "       dispatched_at = UTC_TIMESTAMP(), updated_at = UTC_TIMESTAMP()\n"
        " WHERE call_job_id = ? AND state = 'DISPATCHING'"),
        14, replacements='={{ $json.conversation_id }},={{ $json.provider_job_id }},'
                         '={{ $json.provider_call_id }},={{ $json.dispatch_http_status }},'
                         '={{ $json.call_job_id }}',
        retry=2, on_error='continueRegularOutput')
    W.link(outcome_sw, acc, 0)
    acc_ev = W.mysql('[DB] Record Event Call Dispatched', (
        "INSERT IGNORE INTO wf_events\n"
        "  (event_key, event_type, event_domain, occurred_at, call_job_id, lead_id,\n"
        "   country_iso, route_key, provider, adapter_key, attempt, conversation_id,\n"
        "   provider_job_id, source_workflow, execution_id)\n"
        "VALUES (?, 'CALL_DISPATCHED', 'CALL', UTC_TIMESTAMP(), ?, ?, ?, ?, ?, ?, ?, ?, ?,\n"
        "        'WF2', ?)"),
        14, offset=1,
        replacements='={{ "CALL_DISPATCHED:" + $(\'[RESULT] Dispatch Outcome Router\').item.json.call_job_id }},'
                     '={{ $(\'[RESULT] Dispatch Outcome Router\').item.json.call_job_id }},'
                     '={{ $(\'[RESULT] Dispatch Outcome Router\').item.json.lead_id }},'
                     '={{ $(\'[RESULT] Dispatch Outcome Router\').item.json.country_iso }},'
                     '={{ $(\'[RESULT] Dispatch Outcome Router\').item.json.route_key }},'
                     '={{ $(\'[RESULT] Dispatch Outcome Router\').item.json.provider }},'
                     '={{ $(\'[RESULT] Dispatch Outcome Router\').item.json.adapter_key }},'
                     '={{ $(\'[RESULT] Dispatch Outcome Router\').item.json.attempt }},'
                     '={{ $(\'[RESULT] Dispatch Outcome Router\').item.json.conversation_id }},'
                     '={{ $(\'[RESULT] Dispatch Outcome Router\').item.json.provider_job_id }},'
                     '={{ $(\'[RESULT] Dispatch Outcome Router\').item.json.execution_id }}',
        on_error='continueRegularOutput', retry=2)
    W.chain(acc, acc_ev)
    acc_log = W.code('[LOG] Dispatched', CORE + r'''
// FIN DE LA RAMA. No se crea follow-up: el resultado llega por post-call (WF9).
// v1 creaba acá un followup "CONNECTED duration=0", que falseaba las métricas.
return $input.all().map((r, i) => {
  const d = $('[RESULT] Dispatch Outcome Router').itemMatching(i).json;
  lmLog('WF2', d, { call_job_id: d.call_job_id, route_key: d.route_key,
                    country: d.country_iso, provider: d.provider,
                    adapter_key: d.adapter_key, lead_id: d.lead_id,
                    attempt: d.attempt, conversation_id: d.conversation_id,
                    action: 'DISPATCHED' });
  return { json: { ok: true, call_job_id: d.call_job_id, state: 'DISPATCHED',
                   conversation_id: d.conversation_id,
                   provider_job_id: d.provider_job_id } };
});
''', 14, offset=2)
    W.chain(acc_ev, acc_log)

    # ── FINAL ─────────────────────────────────────────────────────────
    W.sticky(15, '15 — FINAL RESULT HANDOFF', """El dispatch **ya es** el resultado
(SIP 603/408/486…) y no va a llegar ningún post-call. Si WF2 no registra, el
intento se pierde.

Se escribe el hecho local y **después** se invoca el motor con el CALL RESULT
normalizado, con clave de idempotencia `call_job_id` (no hay `conversation_id`).

WF2 **no** decide si 603 es "no contestó": manda `result=FAILED` + `sip_code` y
la política de la ruta reclasifica.""", color=3, height=400)
    fin_rec = W.mysql('[DB] Record Immediate Result', (
        "UPDATE wf_call_jobs\n"
        "   SET state = 'COMPLETED', result = ?, duration_seconds = 0,\n"
        "       sip_code = ?, dispatch_http_status = ?,\n"
        "       dispatched_at = COALESCE(dispatched_at, UTC_TIMESTAMP()),\n"
        "       completed_at = UTC_TIMESTAMP(), updated_at = UTC_TIMESTAMP()\n"
        " WHERE call_job_id = ? AND result IS NULL AND state = 'DISPATCHING'"),
        15, replacements='={{ $json.final_result }},={{ $json.sip_code }},'
                         '={{ $json.dispatch_http_status }},={{ $json.call_job_id }}',
        retry=2, on_error='continueRegularOutput')
    W.link(outcome_sw, fin_rec, 1)
    fin_call = W.exec_wf('[ENGINE] Call Followup Engine', 'TEMPLATE_FOLLOWUP_ENGINE_V2',
                         15, offset=1)
    fin_prep = W.code('[RESULT] Build Call Result', CORE + r'''
// CALL RESULT CONTRACT: lo único que el motor acepta.
return $input.all().map((r, i) => {
  const d = $('[RESULT] Dispatch Outcome Router').itemMatching(i).json;
  return { json: {
    call_job_id: d.call_job_id, lead_id: d.lead_id, route_key: d.route_key,
    country_iso: d.country_iso, provider: d.provider, adapter_key: d.adapter_key,
    attempt: Number(d.attempt), conversation_id: null,
    provider_job_id: d.provider_job_id, sip_code: d.sip_code,
    result: d.final_result || 'FAILED', call_status: 'FAILED',
    duration_seconds: 0, callback_requested: false, callback_at: null,
    notes: null, summary: null, source: 'dispatch',
    execution_id: d.execution_id, access_token: d.access_token
  }};
});
''', 15, offset=1, mode=None)
    # el nodo de preparación va ANTES del Execute Workflow
    W.conns.pop('[DB] Record Immediate Result', None)
    W.chain(fin_rec, fin_prep)
    W.chain(fin_prep, fin_call)
    fin_log = W.code('[LOG] Final Result Handed Off', CORE + r'''
return $input.all().map(r => {
  const d = r.json || {};
  console.log('[WF2] resultado FINAL entregado al motor · job=' +
              (d.call_job_id || '-') + ' action=' +
              (d.decision ? d.decision.action : '-'));
  return { json: { ok: true, handed_off: true, call_job_id: d.call_job_id || null,
                   decision: d.decision || null } };
});
''', 15, offset=2)
    W.chain(fin_call, fin_log)

    # ── TECHNICAL_ERROR ───────────────────────────────────────────────
    W.sticky(16, '16 — ERROR HANDLING', """**Técnico ≠ negocio.** Un 401, un 403, un
worker lleno o una config faltante **no son un NO_ANSWER** y no consumen un
intento de negocio: el job va a `RELEASED` con backoff 1, 2, 4 … 60 min y el
**mismo** número de intento se vuelve a reclamar cuando el backoff vence.

`AUTH_ERROR` y `CONFIG_ERROR` además cortan la rama de ese proveedor en el ciclo
y quedan visibles en el panel.

Agotado `tech_retry_max`, el job queda para reconciliación: no se insiste
indefinidamente contra un proveedor caído.

| código | reintento | acción |
|---|---|---|
| `CONFIG_ERROR` | no | aborta, nadie llama |
| `AUTH_ERROR` | no | aborta la rama, alerta |
| `PROVIDER_BUSY` | sí, backoff | capacidad del worker |
| `DISPATCH_UNKNOWN` | **nunca** | → NEEDS_RECONCILIATION |""", color=2, height=520)
    tech = W.mysql('[DB] Mark Released', (
        "UPDATE wf_call_jobs\n"
        "   SET state = 'RELEASED', error_class = 'TECHNICAL', error_code = ?,\n"
        "       error_message = ?, dispatch_http_status = ?,\n"
        "       tech_retry_count = tech_retry_count + 1,\n"
        "       next_tech_retry_at = DATE_ADD(UTC_TIMESTAMP(), INTERVAL\n"
        "         LEAST(POW(2, tech_retry_count), ?) MINUTE),\n"
        "       updated_at = UTC_TIMESTAMP()\n"
        " WHERE call_job_id = ? AND state IN ('CLAIMED','DISPATCHING')"),
        16, replacements='={{ $json.error_code }},={{ $json.error_detail }},'
                         '={{ $json.dispatch_http_status }},'
                         '={{ ($json.settings && $json.settings.tech_retry_backoff_cap_minutes) || 60 }},'
                         '={{ $json.call_job_id }}',
        retry=2, on_error='continueRegularOutput')
    W.link(outcome_sw, tech, 2)
    tech_ev = W.mysql('[DB] Record Event Tech Failed', (
        "INSERT IGNORE INTO wf_events\n"
        "  (event_key, event_type, event_domain, occurred_at, call_job_id, lead_id,\n"
        "   country_iso, route_key, provider, adapter_key, attempt, result,\n"
        "   source_workflow, execution_id, metadata_json)\n"
        "VALUES (?, 'CALL_TECH_FAILED', 'CALL', UTC_TIMESTAMP(), ?, ?, ?, ?, ?, ?, ?, ?,\n"
        "        'WF2', ?, ?)"),
        16, offset=1,
        replacements='={{ "CALL_TECH_FAILED:" + $(\'[RESULT] Dispatch Outcome Router\').item.json.call_job_id }},'
                     '={{ $(\'[RESULT] Dispatch Outcome Router\').item.json.call_job_id }},'
                     '={{ $(\'[RESULT] Dispatch Outcome Router\').item.json.lead_id }},'
                     '={{ $(\'[RESULT] Dispatch Outcome Router\').item.json.country_iso }},'
                     '={{ $(\'[RESULT] Dispatch Outcome Router\').item.json.route_key }},'
                     '={{ $(\'[RESULT] Dispatch Outcome Router\').item.json.provider }},'
                     '={{ $(\'[RESULT] Dispatch Outcome Router\').item.json.adapter_key }},'
                     '={{ $(\'[RESULT] Dispatch Outcome Router\').item.json.attempt }},'
                     '={{ $(\'[RESULT] Dispatch Outcome Router\').item.json.error_code }},'
                     '={{ $(\'[RESULT] Dispatch Outcome Router\').item.json.execution_id }},'
                     '={{ JSON.stringify({http: $(\'[RESULT] Dispatch Outcome Router\').item.json.dispatch_http_status, detail: $(\'[RESULT] Dispatch Outcome Router\').item.json.error_detail}) }}',
        on_error='continueRegularOutput', retry=2)
    W.chain(tech, tech_ev)
    tech_log = W.code('[LOG] Technical Failure', CORE + r'''
// NO llega al motor y NO se registra como NO_ANSWER. El intento de negocio
// queda intacto para cuando el problema técnico se resuelva.
return $input.all().map((r, i) => {
  const d = $('[RESULT] Dispatch Outcome Router').itemMatching(i).json;
  lmLog('WF2', d, { call_job_id: d.call_job_id, route_key: d.route_key,
                    provider: d.provider, lead_id: d.lead_id, attempt: d.attempt,
                    error_code: d.error_code, action: 'RELEASED' });
  return { json: { ok: false, call_job_id: d.call_job_id, state: 'RELEASED',
                   error_code: d.error_code, consumes_attempt: false } };
});
''', 16, offset=2)
    W.chain(tech_ev, tech_log)

    # ── UNKNOWN ───────────────────────────────────────────────────────
    unk = W.mysql('[DB] Mark Unknown', (
        "UPDATE wf_call_jobs\n"
        "   SET state = 'UNKNOWN', error_class = 'AMBIGUOUS',\n"
        "       error_code = 'DISPATCH_UNKNOWN', error_message = ?,\n"
        "       dispatch_http_status = ?, updated_at = UTC_TIMESTAMP()\n"
        " WHERE call_job_id = ? AND state = 'DISPATCHING'"),
        16, branch=3,
        replacements='={{ $json.error_detail }},={{ $json.dispatch_http_status }},'
                     '={{ $json.call_job_id }}',
        retry=2, on_error='continueRegularOutput')
    W.link(outcome_sw, unk, 3)
    unk_ev = W.mysql('[DB] Record Event Call Unknown', (
        "INSERT IGNORE INTO wf_events\n"
        "  (event_key, event_type, event_domain, occurred_at, call_job_id, lead_id,\n"
        "   country_iso, route_key, provider, adapter_key, attempt,\n"
        "   source_workflow, execution_id, metadata_json)\n"
        "VALUES (?, 'CALL_UNKNOWN', 'CALL', UTC_TIMESTAMP(), ?, ?, ?, ?, ?, ?, ?,\n"
        "        'WF2', ?, ?)"),
        16, branch=3, offset=1,
        replacements='={{ "CALL_UNKNOWN:" + $(\'[RESULT] Dispatch Outcome Router\').item.json.call_job_id }},'
                     '={{ $(\'[RESULT] Dispatch Outcome Router\').item.json.call_job_id }},'
                     '={{ $(\'[RESULT] Dispatch Outcome Router\').item.json.lead_id }},'
                     '={{ $(\'[RESULT] Dispatch Outcome Router\').item.json.country_iso }},'
                     '={{ $(\'[RESULT] Dispatch Outcome Router\').item.json.route_key }},'
                     '={{ $(\'[RESULT] Dispatch Outcome Router\').item.json.provider }},'
                     '={{ $(\'[RESULT] Dispatch Outcome Router\').item.json.adapter_key }},'
                     '={{ $(\'[RESULT] Dispatch Outcome Router\').item.json.attempt }},'
                     '={{ $(\'[RESULT] Dispatch Outcome Router\').item.json.execution_id }},'
                     '={{ JSON.stringify({http: $(\'[RESULT] Dispatch Outcome Router\').item.json.dispatch_http_status, detail: $(\'[RESULT] Dispatch Outcome Router\').item.json.error_detail}) }}',
        on_error='continueRegularOutput', retry=2)
    W.chain(unk, unk_ev)
    unk_log = W.code('[LOG] Dispatch Unknown', CORE + r'''
// No podemos PROBAR que la llamada no se creó. JAMÁS se re-despacha: el
// reconciliador lo pasa a NEEDS_RECONCILIATION y, si el post-call llega, el
// resultado se resuelve solo.
return $input.all().map((r, i) => {
  const d = $('[RESULT] Dispatch Outcome Router').itemMatching(i).json;
  lmLog('WF2', d, { call_job_id: d.call_job_id, route_key: d.route_key,
                    provider: d.provider, lead_id: d.lead_id, attempt: d.attempt,
                    error_code: 'DISPATCH_UNKNOWN', action: 'UNKNOWN' });
  return { json: { ok: false, call_job_id: d.call_job_id, state: 'UNKNOWN',
                   redispatch: false } };
});
''', 16, branch=3, offset=2)
    W.chain(unk_ev, unk_log)

    none_log = W.code('[LOG] Unroutable Outcome', CORE + r'''
// Fallback del Switch: un dispatch_outcome que no existe en el contrato.
// Se trata como UNKNOWN (conservador) y se deja constancia.
return $input.all().map(it => {
  const d = it.json;
  console.log('[WF2] dispatch_outcome fuera de contrato: ' + d.dispatch_outcome +
              ' job=' + d.call_job_id);
  return { json: { ok: false, call_job_id: d.call_job_id,
                   error_code: 'VALIDATION_ERROR',
                   dispatch_outcome: d.dispatch_outcome } };
});
''', 16, branch=5)
    W.link(outcome_sw, none_log, 4)

    # ── CRM ATTEMPTING falló → RELEASED (técnico) ────────────────────
    att_fail = W.mysql('[DB] Release On CRM Error', (
        "UPDATE wf_call_jobs\n"
        "   SET state = 'RELEASED', error_class = 'TECHNICAL', error_code = ?,\n"
        "       error_message = 'PATCH ATTEMPTING failed',\n"
        "       tech_retry_count = tech_retry_count + 1,\n"
        "       next_tech_retry_at = DATE_ADD(UTC_TIMESTAMP(), INTERVAL\n"
        "         LEAST(POW(2, tech_retry_count), 60) MINUTE),\n"
        "       updated_at = UTC_TIMESTAMP()\n"
        " WHERE call_job_id = ? AND state = 'CLAIMED'"),
        16, branch=7,
        replacements='={{ $json.tech_error_code }},={{ $json.call_job_id }}',
        retry=2, on_error='continueRegularOutput')
    W.link(att_if, att_fail, 1)
    att_fail_log = W.code('[LOG] Attempting Failed', CORE + r'''
// No se pudo marcar ATTEMPTING en el CRM: NO se llama. Error TÉCNICO, no
// consume intento de negocio.
return $input.all().map((r, i) => {
  const d = $('[CRM] Check Attempting').itemMatching(i).json;
  lmLog('WF2', d, { call_job_id: d.call_job_id, route_key: d.route_key,
                    lead_id: d.lead_id, attempt: d.attempt,
                    error_code: d.tech_error_code, action: 'RELEASED' });
  return { json: { ok: false, call_job_id: d.call_job_id, state: 'RELEASED',
                   error_code: d.tech_error_code, consumes_attempt: false } };
});
''', 16, branch=7, offset=1)
    W.chain(att_fail, att_fail_log)

    # ══ 17 AUDIT / METRICS ════════════════════════════════════════════
    W.sticky(17, '17 — AUDIT / METRICS', """Resumen del ciclo con los campos de
correlación del §10: workflow, execution_id, call_job_id, route_key, país,
proveedor, adapter, attempt, conversation_id, resultado y código de error.

**Nunca** teléfono completo, tokens ni `accessToken`.

Las métricas del panel NO salen de este log: salen de `wf_call_jobs` y
`wf_events`, que ya se escribieron en cada transición.""", color=4, height=340)
    audit = W.code('[LOG] Cycle Summary', CORE + r'''
// Una sola línea por ciclo, para leer en Executions sin abrir cada nodo.
function count(nodeName) {
  try { return $(nodeName).all().length; } catch (e) { return 0; }
}
const dispatched = count('[LOG] Dispatched');
const finals = count('[LOG] Final Result Handed Off');
const tech = count('[LOG] Technical Failure');
const unknown = count('[LOG] Dispatch Unknown');
const summary = { workflow: 'WF2', execution_id: $execution.id,
                  dispatched: dispatched, final_results: finals,
                  technical_failures: tech, unknown: unknown,
                  total: dispatched + finals + tech + unknown };
console.log('[WF2][exec=' + $execution.id + '] ciclo: ' + JSON.stringify(summary));
return [{ json: summary }];
''', 17)
    for n in (acc_log, fin_log, tech_log, unk_log):
        W.link(n, audit)
    return W
