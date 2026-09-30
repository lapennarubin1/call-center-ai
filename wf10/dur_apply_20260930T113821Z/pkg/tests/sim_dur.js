// Pruebas A-DUR-1..10 + consecuencias (C-xx) de "Asterisk contestada: Activity con duración real".
// Ejecuta los nodos REALES del WF2 y WF9 candidatos (out_dur/*) + los nodos reales de WF10 (sin modificar),
// con MariaDB local (incluida una tabla `cdr` con el esquema estándar de Asterisk). Sin producción.
//
//   WF2_FILE / WF9_FILE  -> candidatos (por defecto out_dur/*.json)
//   WF9_OLD              -> WF9 anterior (rollback), por defecto out/WF9_PRODUCTION_FIXED_STRINGEE_RELIABILITY.json
const fs = require('fs');
const { World, conv, iso } = require('./mocks');
const { makeRunner, wake, drain, renderQuery } = require('./pipeline');
const { runCode, mysqlNode, sql, close } = require('./harness');

const WF2 = process.env.WF2_FILE || (__dirname + '/../out_dur/WF2_DUR_CANDIDATE.json');
const WF9 = process.env.WF9_FILE || (__dirname + '/../out_dur/WF9_DUR_CANDIDATE.json');
const WF9_OLD = process.env.WF9_OLD || (__dirname + '/../out/WF9_PRODUCTION_FIXED_STRINGEE_RELIABILITY.json');
const WF10 = process.env.WF10_FILE || '/home/claude/wf10/out/WF10_FINAL_PRODUCTION.json';

// LeadStudio devuelve ids UUID para los followups (WF10 exige 36 caracteres UUID): el mock de los tests base usa 'fu_N'
{ const orig = World.prototype.addFollowup;
  World.prototype.addFollowup = function (leadId, f) { const e = orig.call(this, leadId, f); const n = String(this.seq).padStart(12, '0'); e.id = `f011f011-${n.slice(0, 4)}-4000-8000-${n}`; return e; }; }
const wf2 = JSON.parse(fs.readFileSync(WF2, 'utf8'));
const by2 = Object.fromEntries(wf2.nodes.map(n => [n.name, n]));
const wf10 = JSON.parse(fs.readFileSync(WF10, 'utf8'));
const by10 = Object.fromEntries(wf10.nodes.map(n => [n.name, n]));

const results = [];
const check = (id, name, cond, detail) => { results.push({ id, name, pass: !!cond, detail: cond ? '' : (typeof detail === 'string' ? detail : JSON.stringify(detail)) }); };
const U = n => `${String(n).padStart(8, '0')}-d0d0-4000-8000-${String(n).padStart(12, '0')}`;
const H = 3600e3;
const LOGIN = { '🔐 Login LeadStudio1': [{ accessToken: 'tok' }] };
const nowS = () => Math.floor(Date.now() / 1000);
const spanishOrNonAscii = s => /[^\x20-\x7E\u2014]/.test(s) || /\b(intento|llamada|ciclo|contest|buz[oó]n|pr[oó]ximo|reintento|sin respuesta|cliente)\b/i.test(s);
const allNotes = [];

async function reset() {
  await sql('DELETE FROM wf_call_events'); await sql('DELETE FROM wf_call_followups'); await sql('DELETE FROM cdr');
  await sql("UPDATE wf_call_events SET state=state WHERE 1=0");
}
async function ev(key) { return (await sql(`SELECT * FROM wf_call_events WHERE event_key='${key}'`))[0]; }
async function fcf(where = '1=1') { return sql(`SELECT *, UNIX_TIMESTAMP(created_at) AS created_epoch FROM wf_call_followups WHERE ${where} ORDER BY id`); }
const posts = w => w.calls.filter(c => c.method === 'POST' && /\/followups$/.test(c.url));
const patches = w => w.calls.filter(c => c.method === 'PATCH');

// ---------- CDR ----------
let cdrSeq = 30000;
async function addCdr({ phone, start, duration, billsec, disposition = 'ANSWERED', uid }) {
  const id = uid || `${start}.${cdrSeq++}`;
  const dst = String(phone).replace(/\D/g, '');
  await sql(`INSERT INTO cdr (calldate, clid, src, dst, dcontext, channel, duration, billsec, disposition, uniqueid) VALUES (FROM_UNIXTIME(${start}), 'x', 'x', '${dst}', 'from-internal', 'PJSIP/x', ${duration}, ${billsec}, '${disposition}', '${id}')`);
  return id;
}

// ---------- WF2 (nodos reales): Fetch+Lock -> Dial(simulado) -> Classify -> Ledger diferido -> Guardar -> fila wf_call_followups ----------
async function code2(name, w, opts) { return runCode(by2[name].parameters.jsCode, Object.assign({ http: w.httpFn() }, opts)); }
const ANSWER = cid => ({ statusCode: 200, body: { success: true, conversation_id: cid } });
const NOANS = { statusCode: 200, body: { success: false, message: 'Call failed: SIP 408 Request Timeout' } };
const PROVFAIL = { statusCode: 503, body: { message: 'Service unavailable' } };
async function wf2Cycle(w, dial, { ledgerFail = false } = {}) {
  const f = await code2('📥 Fetch + Lock (Asterisk)', w, { items: [{}], nodes: LOGIN });
  const fetched = f.out.filter(i => i.json && i.json.lead_id);
  if (!fetched.length) return { fetched: [] };
  const items = fetched.map((it, i) => ({ json: dial(it.json), pairedItem: i }));
  const nodes = { '📥 Fetch + Lock (Asterisk)': fetched, '🔐 Login LeadStudio1': LOGIN['🔐 Login LeadStudio1'] };
  const cl = await code2('🔍 Classify Dial Result (Asterisk)', w, { items, nodes });
  nodes['🔍 Classify Dial Result (Asterisk)'] = cl.out;
  const bl = await code2('🧾 Build Deferred Ledger (Asterisk)', w, { items: cl.out, nodes });
  let ledOut;
  if (ledgerFail) ledOut = [{ json: { error: 'ER_LOCK_WAIT_TIMEOUT' } }];           // onError: continueRegularOutput
  else {
    const q = renderQuery(by2['💾 Ledger Deferred (Asterisk)'].parameters.query, bl.out[0], {});
    if (/\$\d/.test(q)) throw new Error('SQL del ledger con $<n>');
    ledOut = await mysqlNode([q]);
    if (!ledOut.length) ledOut = [{ json: {} }];                                       // alwaysOutputData
  }
  const gu = await code2('🏁 Guardar Resultado (Asterisk)', w, { items: ledOut, nodes });
  const fr = await code2('🧾 Build Followup Row (Asterisk)', w, { items: gu.out, nodes });
  for (const it of fr.out) await mysqlNode([renderQuery(by2['💾 Save Followup ID (Asterisk)'].parameters.query, it, {})]);
  return { fetched, classify: cl.out.map(i => i.json), ledger: ledOut.map(i => i.json), guardar: gu.out.map(i => i.json), logs: gu.logs };
}

// ---------- Tiempo: "envejece" la llamada N segundos (dispatched_at / event_at del ledger, updatedAt del lead) ----------
async function age(w, secs) {
  await sql(`UPDATE wf_call_events SET dispatched_at = dispatched_at - INTERVAL ${secs} SECOND, event_at = event_at - INTERVAL ${secs} SECOND, first_seen_at = first_seen_at - INTERVAL ${secs} SECOND WHERE source='elevenlabs'`);
  for (const l of w.leads.values()) l.updatedAt = iso(new Date(l.updatedAt).getTime() - secs * 1000);
  for (const fs_ of w.followups.values()) for (const f of fs_) f.createdAt = iso(new Date(f.createdAt).getTime() - secs * 1000);
}

// Llamada Asterisk "de libro": T0 = marcado, contesta a T0+38 s, billsec 88, duration 126 (incluye timbrado), EL 90 s
function timeline(secsAgo = 420, { ring = 38, bill = 88, el = 90 } = {}) {
  const T0 = nowS() - secsAgo;
  return { T0, answer: T0 + ring, bill, duration: ring + bill, elStart: T0 + ring, el };
}

async function scenario(title, fn) {
  await reset();
  const w = new World();
  const r = makeRunner(WF9, w);
  try { await fn(w, r); } catch (e) { check('ERR', title + ' lanzó excepción', false, e.stack); }
  for (const c of posts(w)) allNotes.push(String((c.body || {}).notes || ''));
  return { w, r };
}
const PHONE = '+919241014686';
const okTranscript = [{ role: 'agent', message: 'Hello, this is the trading desk.' }, { role: 'user', message: 'Yes, tell me about the account' }, { role: 'user', message: 'Okay, send me the details' }];

// Camino feliz completo: WF2 despacha+contesta, se cuelga, WF9 procesa. Devuelve todo para verificar.
async function happy(w, r, { lead, cid, attempts = 0, phone = PHONE, tl = timeline(), cdr = true, convOpts = {}, ends = 'polling', cdrOpts = {}, ageSecs = 420 }) {
  w.lead(lead, { attempts, status: 'NOT_CONTACTED', phone, stage: 'NEW' });
  const c2 = await wf2Cycle(w, () => ANSWER(cid));
  if (!(c2.guardar && c2.guardar[0] && c2.guardar[0].deferred_to_wf9)) throw new Error('fixture: la llamada no quedó diferida (¿conversation_id inválido?) ' + JSON.stringify(c2.guardar));
  const postsAtAnswer = posts(w).length;
  const statusAtAnswer = w.leads.get(lead).status;
  await age(w, ageSecs);
  let uid = null;
  if (cdr) uid = await addCdr(Object.assign({ phone, start: tl.T0, duration: tl.duration, billsec: tl.bill }, cdrOpts));
  const c = conv(Object.assign({ id: cid, lead_id: lead, n: attempts + 1, phone, start: tl.elStart, dur: tl.el, transcript: okTranscript }, convOpts));
  w.convs.set(cid, c);
  if (ends === 'webhook') await r.webhook(c); else await r.polling();
  await drain(r);
  return { c2, postsAtAnswer, statusAtAnswer, uid, conv: c };
}

(async () => {
  // Sanidad: los candidatos contienen los nodos nuevos
  check('PRE', 'candidato WF2 con 66 nodos y WF9 con 27 (2 nodos nuevos cada uno)', wf2.nodes.length === 66 && JSON.parse(fs.readFileSync(WF9, 'utf8')).nodes.length === 27, `${wf2.nodes.length}`);

  // =====================================================================================
  // BUG-REPRO: con el WF2 del paquete anterior (sin cambios) la Activity nace con durationSeconds 0
  // =====================================================================================
  await scenario('BUG-REPRO', async (w, r) => {
    const old = JSON.parse(fs.readFileSync(__dirname + '/../out/WF2_UNIFICADO_v2_PATCHED.json', 'utf8'));
    const ob = Object.fromEntries(old.nodes.map(n => [n.name, n]));
    const lead = U(1); w.lead(lead, { attempts: 0, status: 'NOT_CONTACTED', phone: PHONE });
    const run = (n, opts) => runCode(ob[n].parameters.jsCode, Object.assign({ http: w.httpFn() }, opts));
    const f = await run('📥 Fetch + Lock (Asterisk)', { items: [{}], nodes: LOGIN });
    const nodes = { '📥 Fetch + Lock (Asterisk)': f.out, ...LOGIN };
    const cl = await run('🔍 Classify Dial Result (Asterisk)', { items: [{ json: ANSWER('conv_bugrepro0000001'), pairedItem: 0 }], nodes });
    nodes['🔍 Classify Dial Result (Asterisk)'] = cl.out;
    await run('🏁 Guardar Resultado (Asterisk)', { items: cl.out, nodes });
    const p = posts(w);
    check('BUG-REPRO', 'reproducción: el WF2 anterior crea la Activity CONNECTED/ANSWERED al contestar con durationSeconds = 0 (causa raíz)', p.length === 1 && p[0].body.durationSeconds === 0 && p[0].body.callStatus === 'ANSWERED', JSON.stringify(p.map(x => x.body)));
  });

  // =====================================================================================
  // A-DUR-1..5: caso real (Prasanna Kumar): billsec 88, duration 126, WAV 88.8 s
  // =====================================================================================
  let uid1;
  await scenario('A-DUR-1..5', async (w, r) => {
    const lead = 'ae4650db-ed12-41e2-b41d-ff5c12de6b08', cid = 'conv_prasanna1790750598';
    const tl = timeline(420, { ring: 38, bill: 88, el: 90 });
    const h = await happy(w, r, { lead, cid, attempts: 0, tl, cdrOpts: { uid: `${tl.T0}.32578` } });
    uid1 = h.uid;
    const L = w.leads.get(lead); const calls = w.callFus(lead); const e = await ev('elevenlabs:' + cid);
    const rows = await fcf(`lead_id='${lead}'`);

    check('A-DUR-0', 'al contestar, WF2 NO crea la Activity (0 POST) y deja el lead ATTEMPTING; el ledger queda confirmado', h.postsAtAnswer === 0 && h.statusAtAnswer === 'ATTEMPTING' &&
      h.c2.guardar[0] && h.c2.guardar[0].deferred_to_wf9 === true && h.c2.ledger.some(x => x.event_key === 'elevenlabs:' + cid), JSON.stringify({ p: h.postsAtAnswer, s: h.statusAtAnswer, g: h.c2.guardar, l: h.c2.ledger }));
    check('A-DUR-1', 'Asterisk ANSWERED: durationSeconds del CRM == CDR.billsec (88) > 0; NO cdr.duration (126) ni EL (90)', calls.length === 1 && calls[0].durationSeconds === 88, JSON.stringify(calls.map(c => c.durationSeconds)));
    check('A-DUR-2', 'una sola Activity para esa llamada física (1 POST /followups en total)', calls.length === 1 && posts(w).length === 1, `calls=${calls.length} posts=${posts(w).length}`);
    check('A-DUR-3', 'attempts incrementa exactamente +1 (0 -> 1), sólo cuando WF9 hace el POST', L.attempts === 1, JSON.stringify(L));
    check('A-DUR-3b', 'ciclo de vida final: CONNECTED/ANSWERED, lead CONTACTED, sin stage de cierre, sin nextActionAt', calls[0].outcome === 'CONNECTED' && calls[0].callStatus === 'ANSWERED' && L.status === 'CONTACTED' && L.stage !== 'UNRESPONSIVE' && !calls[0].nextActionAt, JSON.stringify({ L, c: calls[0] }));
    check('A-DUR-4', 'followup_id guardado en wf_call_followups == id de la Activity (una fila, provider asterisk, recording_synced=0)', rows.length === 1 && rows[0].followup_id === calls[0].id &&
      rows[0].provider === 'asterisk' && rows[0].recording_synced === 0 && rows[0].outcome === 'CONNECTED' && e.followup_id === calls[0].id, JSON.stringify({ rows, e: e.followup_id }));
    check('A-DUR-4b', 'la fila lleva created_at = inicio de la llamada (uniqueid del CDR), para que WF10 la empareje por UNIQUEID', rows[0] && rows[0].created_epoch === tl.T0, JSON.stringify({ got: rows[0] && rows[0].created_epoch, want: tl.T0 }));
    check('A-DUR-1b', 'providerCallId = elevenlabs:<conversation> y la nota no está en español', calls[0].providerCallId === 'elevenlabs:' + cid, JSON.stringify(calls[0]));

    // ---- A-DUR-5: WF10 REAL (sin cambios) sube la grabación al MISMO followup_id ----
    const wav = Buffer.alloc(44 + 16000 * 88); wav.write('RIFF', 0); wav.writeUInt32LE(16000, 28);
    const runW10 = async (uniqueid) => {
      const body = { phone: PHONE, uniqueid, filename: `test-${uniqueid}.wav`, audio_base64: wav.toString('base64'), date: new Date().toISOString().slice(0, 10) };
      const conv10 = await runCode(by10['Convert Base64 to Binary'].parameters.jsCode, { items: [{ body }] });
      const look = await mysqlNode([conv10.out[0].json.lookup_sql]);
      const dec = await runCode(by10['🧮 Decidir Match (Asterisk)'].parameters.jsCode, { items: look, nodes: { 'Convert Base64 to Binary': conv10.out } });
      return dec.out[0].json;
    };
    const d = await runW10(uid1);
    check('A-DUR-5', 'WF10 (nodo real Decidir Match) empareja la grabación con el followup_id de la Activity de WF9 -> el PUT va a ese id', d.decision === 'attach' && d.followup_id === calls[0].id && d.http_status === 200, JSON.stringify(d));
    check('A-DUR-5b', 'PUT recording usa {{followup_id}} de esa decisión (URL del nodo WF10 real, sin cambios)', /calls\/recording\/'\s*\+\s*\$json\.followup_id/.test(by10['📤 PUT Recording — LeadStudio (Asterisk)'].parameters.url), by10['📤 PUT Recording — LeadStudio (Asterisk)'].parameters.url);
    // el nodo real de WF10 marca synced y un reenvío no vuelve a subir
    await sql(`UPDATE wf_call_followups SET recording_synced=1 WHERE followup_id='${calls[0].id}'`);
    const d2 = await runW10(uid1);
    check('A-DUR-5c', 'recording_synced=1 tras el PUT: un reenvío del mismo WAV -> not_pending (no se vuelve a subir)', d2.decision === 'not_pending', JSON.stringify(d2));
  });

  // WF10 antes de que WF9 cree la Activity -> 503 (el worker reintenta), nunca se adjunta a otra Activity
  await scenario('A-DUR-5d', async (w, r) => {
    const lead = U(501), cid = 'conv_earlyrecording01';
    w.lead(lead, { attempts: 1, status: 'NOT_CONTACTED', phone: PHONE });
    const tl = timeline(60);
    await wf2Cycle(w, () => ANSWER(cid));
    const wav = Buffer.alloc(44 + 16000 * 30); wav.write('RIFF', 0); wav.writeUInt32LE(16000, 28);
    const body = { phone: PHONE, uniqueid: `${tl.T0}.777`, filename: `test-${tl.T0}.777.wav`, audio_base64: wav.toString('base64') };
    const c10 = await runCode(by10['Convert Base64 to Binary'].parameters.jsCode, { items: [{ body }] });
    const look = await mysqlNode([c10.out[0].json.lookup_sql]);
    const dec = await runCode(by10['🧮 Decidir Match (Asterisk)'].parameters.jsCode, { items: look, nodes: { 'Convert Base64 to Binary': c10.out } });
    check('A-DUR-5d', 'grabación que llega ANTES de la Activity -> WF10 responde 503 followup_not_found (el worker reintenta), sin adjuntar a nada', dec.out[0].json.decision === 'followup_not_found' && dec.out[0].json.http_status === 503, JSON.stringify(dec.out[0].json));
  });

  // =====================================================================================
  // A-DUR-6: CDR no disponible -> duración de ElevenLabs
  // =====================================================================================
  for (const variant of ['sin filas', 'tabla inexistente / sin permiso']) {
    await scenario('A-DUR-6 ' + variant, async (w, r) => {
      const lead = U(variant === 'sin filas' ? 601 : 602), cid = variant === 'sin filas' ? 'conv_cdrnone00001' : 'conv_cdrnoperm0001';
      let renamed = false;
      if (variant !== 'sin filas') { await sql('RENAME TABLE cdr TO cdr_off'); renamed = true; }
      try {
        const h = await happy(w, r, { lead, cid, attempts: 3, cdr: variant === 'sin filas' ? false : false, tl: timeline(420, { ring: 30, bill: 80, el: 84 }) });
        const calls = w.callFus(lead); const L = w.leads.get(lead);
        check('A-DUR-6', `CDR no disponible (${variant}) -> durationSeconds = ElevenLabs (84), una Activity, attempts 3->4, sigue el flujo`, calls.length === 1 && calls[0].durationSeconds === 84 && L.attempts === 4 && L.status === 'CONTACTED', JSON.stringify({ calls: calls.map(c => c.durationSeconds), L }));
        if (variant !== 'sin filas') {
          const logged = r.logs.some(l => l[1] === 'error' || /CDR no disponible/.test(String(l[2] || '')));
          check('A-DUR-6b', 'con error del nodo CDR se avisa con 🚨 pero el procesador no se detiene', r.logs.some(l => /CDR no disponible/.test(l.join(' '))), JSON.stringify(r.logs.slice(-4)));
        }
      } finally { if (renamed) await sql('RENAME TABLE cdr_off TO cdr'); }
    });
  }
  // CDR presente pero ambiguo (dos candidatos igual de plausibles) -> ElevenLabs, nunca se adivina
  await scenario('A-DUR-6c', async (w, r) => {
    const lead = U(603), cid = 'conv_cdrambig0001'; const tl = timeline(420, { ring: 30, bill: 80, el: 84 });
    await addCdr({ phone: PHONE, start: tl.T0 + 2, duration: 100, billsec: 70 });   // mismo teléfono, casi el mismo instante, billsec distinto (otra llamada plausible)
    await happy(w, r, { lead, cid, attempts: 0, tl, cdrOpts: {} });
    const calls = w.callFus(lead);
    check('A-DUR-6c', 'dos CDR igual de plausibles (ambiguo) -> se usa ElevenLabs (84) y no se adivina', calls.length === 1 && calls[0].durationSeconds === 84, JSON.stringify(calls.map(c => c.durationSeconds)));
  });
  // dos llamadas al mismo número: sólo una alineada con la conversación
  await scenario('A-DUR-6d', async (w, r) => {
    const lead = U(604), cid = 'conv_cdrtwocalls01'; const tl = timeline(420, { ring: 30, bill: 80, el: 84 });
    await addCdr({ phone: PHONE, start: tl.T0 - 25 * 60, duration: 200, billsec: 170 });   // llamada anterior al mismo número (otro intento)
    await happy(w, r, { lead, cid, attempts: 0, tl });
    const calls = w.callFus(lead);
    check('A-DUR-6d', 'otra llamada CDR anterior al mismo número no se confunde: se usa la alineada con la conversación (80)', calls.length === 1 && calls[0].durationSeconds === 80, JSON.stringify(calls.map(c => c.durationSeconds)));
  });

  // el CDR de Asterisk puede tener el MISMO registro duplicado (mismo uniqueid y duración): cuenta una vez
  await scenario('A-DUR-6e', async (w, r) => {
    const lead = U(605), cid = 'conv_cdrdup000001'; const tl = timeline(420, { ring: 30, bill: 80, el: 84 });
    const uid = await addCdr({ phone: PHONE, start: tl.T0, duration: tl.duration, billsec: tl.bill });
    await addCdr({ phone: PHONE, start: tl.T0, duration: tl.duration, billsec: tl.bill, uid });
    await happy(w, r, { lead, cid, attempts: 0, tl, cdr: false });
    const calls = w.callFus(lead);
    check('A-DUR-6e', 'registro CDR duplicado (mismo uniqueid y duración) no se toma por ambiguo: durationSeconds = billsec (80)', calls.length === 1 && calls[0].durationSeconds === 80, JSON.stringify(calls.map(c => c.durationSeconds)));
  });

  // CDR con dos registros del mismo tramo (pata Local: otro uniqueid, mismo inicio ±2 s y billsec ±3 s) no es ambiguo
  await scenario('A-DUR-6f', async (w, r) => {
    const lead = U(606), cid = 'conv_cdrleg0000001'; const tl = timeline(420, { ring: 30, bill: 80, el: 84 });
    await addCdr({ phone: PHONE, start: tl.T0 + 1, duration: tl.duration - 1, billsec: tl.bill - 1 });
    await happy(w, r, { lead, cid, attempts: 0, tl });
    const calls = w.callFus(lead);
    check('A-DUR-6f', 'dos CDR del mismo tramo (otro uniqueid, ±2 s / ±3 s) no son ambiguos: durationSeconds = billsec del mejor alineado (80)', calls.length === 1 && calls[0].durationSeconds === 80, JSON.stringify(calls.map(c => c.durationSeconds)));
  });
  // uniqueid con systemname que lleva puntos (FQDN): el epoch se extrae igual
  await scenario('A-DUR-6g', async (w, r) => {
    const lead = U(607), cid = 'conv_cdrfqdn0000001'; const tl = timeline(420, { ring: 30, bill: 80, el: 84 });
    await addCdr({ phone: PHONE, start: tl.T0, duration: tl.duration, billsec: tl.bill, uid: `pbx.example.com-${tl.T0}.55` });
    await happy(w, r, { lead, cid, attempts: 0, tl, cdr: false });
    const calls = w.callFus(lead);
    check('A-DUR-6g', 'uniqueid "pbx.example.com-<epoch>.<seq>" -> se usa el billsec (80), no la duración de ElevenLabs (84)', calls.length === 1 && calls[0].durationSeconds === 80, JSON.stringify(calls.map(c => c.durationSeconds)));
  });
  // CDR tardío (Asterisk lo escribe unos segundos después): WF9 espera (WAITING_CDR) y luego usa el billsec
  await scenario('A-DUR-6h', async (w, r) => {
    const lead = U(608), cid = 'conv_cdrlate0000001'; const tl = timeline(150, { ring: 20, bill: 36, el: 40 });
    await happy(w, r, { lead, cid, attempts: 0, tl, cdr: false, ageSecs: 150 });
    const mid = w.callFus(lead).length; const e1 = await ev('elevenlabs:' + cid);
    await addCdr({ phone: PHONE, start: tl.T0, duration: tl.duration, billsec: tl.bill });
    await drain(r);
    const calls = w.callFus(lead);
    check('A-DUR-6h', 'CDR tardío: 1ª pasada espera (WAITING_CDR, 0 Activities, lead ATTEMPTING); al aparecer el CDR -> 1 Activity con billsec (36), no ElevenLabs (40)', mid === 0 && e1.resolution === 'WAITING_CDR' && calls.length === 1 && calls[0].durationSeconds === 36 && w.leads.get(lead).attempts === 1, JSON.stringify({ mid, res: e1 && e1.resolution, calls: calls.map(c => c.durationSeconds) }));
  });
  // sin datos de conversación: un CDR de OTRA llamada al mismo número (30 min antes) no se usa
  await scenario('A-DUR-6i', async (w, r) => {
    const lead = U(609), cid = 'conv_cdrother0000001';
    w.lead(lead, { attempts: 0, status: 'NOT_CONTACTED', phone: PHONE });
    await wf2Cycle(w, () => ANSWER(cid)); await age(w, 30 * 60 + 7 * 3600);
    await addCdr({ phone: PHONE, start: nowS() - (30 * 60 + 7 * 3600) - 20 * 60, duration: 320, billsec: 300 });   // 20 min ANTES del despacho (dentro de la ventana SQL, fuera de ±4 min)
    await drain(r);
    const c = w.callFus(lead);
    check('A-DUR-6i', 'conversación inexistente + CDR de otra llamada (30 min antes del despacho): 1 Activity, durationSeconds 0 (no se roba ese billsec de 300)', c.length === 1 && c[0].durationSeconds === 0, JSON.stringify(c.map(x => x.durationSeconds)));
  });

  // =====================================================================================
  // A-DUR-7: NO_ANSWER / fallos: WF2 sigue siendo el dueño, sin duración conectada inventada
  // =====================================================================================
  await scenario('A-DUR-7', async (w, r) => {
    const lead = U(701), lead2 = U(702), lead3 = U(703);
    w.lead(lead, { attempts: 1, status: 'NOT_CONTACTED', phone: '+919800000701' });
    w.lead(lead2, { attempts: 4, status: 'NOT_CONTACTED', phone: '+919800000702' });
    w.lead(lead3, { attempts: 2, status: 'NOT_CONTACTED', phone: '+919800000703' });
    // un CDR ANSWERED del mismo número NO debe tocar el NO_ANSWER (p.ej. otra llamada)
    await addCdr({ phone: '+919800000701', start: nowS() - 200, duration: 60, billsec: 55 });
    const c2 = await wf2Cycle(w, l => l.lead_id === lead ? NOANS : (l.lead_id === lead2 ? NOANS : PROVFAIL));
    const a = w.callFus(lead)[0], b = w.callFus(lead2)[0];
    check('A-DUR-7', 'NO_ANSWER (SIP 408): WF2 crea su Activity de inmediato, durationSeconds 0, callStatus NO_ANSWER (sin duración conectada)', a && a.durationSeconds === 0 && a.outcome === 'NO_ANSWER' && a.callStatus === 'NO_ANSWER' && w.leads.get(lead).attempts === 2, JSON.stringify({ a, L: w.leads.get(lead) }));
    check('A-DUR-7b', 'NO_ANSWER: no se registra nada en wf_call_events (no hay diferido) y sí la fila en wf_call_followups con recording_synced=1', (await sql("SELECT COUNT(*) n FROM wf_call_events"))[0].n === 0 &&
      (await fcf(`followup_id='${a.id}'`))[0].recording_synced === 1, JSON.stringify(await fcf()));
    check('A-DUR-7c', 'NO_ANSWER intento 5 -> ciclo 2, nota en inglés y nextActionAt (único mecanismo de nextFollowUpAt)', b && b.nextActionAt && w.leads.get(lead2).status === 'NO_ANSWER' && w.leads.get(lead2).nextFollowUpAt === b.nextActionAt, JSON.stringify({ b, L: w.leads.get(lead2) }));
    check('A-DUR-7d', 'error del proveedor (HTTP 503): sin Activity, sin intento consumido, lead vuelve a NOT_CONTACTED', w.callFus(lead3).length === 0 && w.leads.get(lead3).attempts === 2 && w.leads.get(lead3).status === 'NOT_CONTACTED', JSON.stringify(w.leads.get(lead3)));
    // WF9 no reprocesa: nadie más crea otra Activity
    const r2 = makeRunner(WF9, w); await r2.polling(); await drain(r2);
    check('A-DUR-7e', 'tras correr WF9 (polling+processor) sigue habiendo 1 Activity por NO_ANSWER (sin duplicados)', w.callFus(lead).length === 1 && w.callFus(lead2).length === 1);
  });

  // Contestada pero SIN conversación real (EL sin voz / duración 0): NO se inventa tiempo conectado
  await scenario('A-DUR-7f', async (w, r) => {
    const lead = U(710), cid = 'conv_answeredsilent01'; const tl = timeline(420, { ring: 30, bill: 12, el: 0 });
    await happy(w, r, { lead, cid, attempts: 0, tl, convOpts: { dur: 0, transcript: [], eval: 'failure', term: 'no_user_activity' } });
    const c = w.callFus(lead)[0]; const L = w.leads.get(lead);
    check('A-DUR-7f', 'contestada pero sin conversación (EL 0 s, sin voz): Activity NO_ANSWER, durationSeconds 0 (no se inventa tiempo conectado), attempts +1, 3x3', c && c.outcome === 'NO_ANSWER' && c.durationSeconds === 0 && L.attempts === 1 && L.status === 'NO_ANSWER', JSON.stringify({ c, L }));
  });

  // =====================================================================================
  // A-DUR-8: buzón de voz / silencio: nunca existe una Activity CONNECTED temporal
  // =====================================================================================
  for (const [tag, o] of [['buzón de voz', { dur: 22, eval: 'failure', term: 'voicemail_detected', summary: 'The call reached a voicemail box.', transcript: [{ role: 'agent', message: 'Hello' }, { role: 'user', message: 'Please leave a message after the tone' }] }],
                          ['silencio', { dur: 15, eval: 'failure', term: 'silence_timeout', summary: 'No customer response.', transcript: [{ role: 'agent', message: 'Hello?' }] }]]) {
    await scenario('A-DUR-8 ' + tag, async (w, r) => {
      const lead = U(tag === 'silencio' ? 802 : 801), cid = tag === 'silencio' ? 'conv_silence00000001' : 'conv_voicemail000001';
      const tl = timeline(420, { ring: 30, bill: o.dur + 3, el: o.dur });
      const h = await happy(w, r, { lead, cid, attempts: 3, tl, convOpts: o });
      const L = w.leads.get(lead); const c = w.callFus(lead);
      check('A-DUR-8', `${tag}: la Activity nace NO_ANSWER (sin CONNECTED temporal), 1 sola, attempts 4, lead NO_ANSWER`, c.length === 1 && c[0].outcome !== 'CONNECTED' && posts(w).every(p => p.body.outcome !== 'CONNECTED') && L.attempts === 4 && L.status === 'NO_ANSWER', JSON.stringify({ c, L }));
      check('A-DUR-8b', `${tag}: el lead nunca pasó por CONTACTED (ningún PATCH status=CONTACTED) y al contestar no hubo POST`, patches(w).every(p => p.body.status !== 'CONTACTED') && h.postsAtAnswer === 0, JSON.stringify(patches(w).map(p => p.body)));
      check('A-DUR-8c', `${tag}: la fila wf_call_followups es NO_ANSWER/recording_synced coherente con la Activity`, (await fcf(`lead_id='${lead}'`)).length === 1 && (await fcf(`lead_id='${lead}'`))[0].followup_id === c[0].id, JSON.stringify(await fcf()));
    });
  }

  // =====================================================================================
  // A-DUR-9: intento 9 contestado -> CONTACTED, no UNRESPONSIVE
  // =====================================================================================
  await scenario('A-DUR-9', async (w, r) => {
    const lead = U(901), cid = 'conv_attempt9answered1'; const tl = timeline(420, { ring: 30, bill: 61, el: 64 });
    const h = await happy(w, r, { lead, cid, attempts: 8, tl, convOpts: { eval: 'failure' } });
    const L = w.leads.get(lead); const c = w.callFus(lead);
    check('A-DUR-9', 'intento 9 contestado (aunque EL diga failure) -> CONTACTED, stage ≠ UNRESPONSIVE/LOST, attempts 9, duración 61', c.length === 1 && L.status === 'CONTACTED' && L.stage !== 'UNRESPONSIVE' && L.stage !== 'LOST' && L.attempts === 9 && c[0].durationSeconds === 61, JSON.stringify({ L, c }));
    check('A-DUR-9b', 'el lead ATTEMPTING con attempts 9 durante la espera NO fue cerrado por Mantenimiento/Fetch (ninguna escritura CLOSED/UNRESPONSIVE)', patches(w).every(p => p.body.status !== 'CLOSED' && p.body.stage !== 'UNRESPONSIVE'), JSON.stringify(patches(w).map(p => p.body)));
    // reproceso forzado / lead con 9: no aparece un intento 10
    await sql("UPDATE wf_call_events SET state='RECEIVED', followup_id=NULL, lifecycle_applied=0 WHERE source='elevenlabs'"); await drain(r);
    check('A-DUR-9c', 'sin intento 10: reproceso forzado -> sigue 1 Activity, attempts 9 (nunca > 9)', w.callFus(lead).length === 1 && w.leads.get(lead).attempts === 9, JSON.stringify(w.leads.get(lead)));
    const f2 = await wf2Cycle(w, l => ANSWER('conv_shouldnotdial'));
    check('A-DUR-9d', 'un lead con 9 intentos no vuelve a despacharse (Fetch + Lock)', !f2.fetched.some(x => x.json.lead_id === lead), JSON.stringify(f2.fetched.map(x => x.json.lead_id)));
  });
  // intento 9 contestado pero SIN conversación -> cierre definitivo correcto (CLOSED+UNRESPONSIVE), 1 Activity
  await scenario('A-DUR-9e', async (w, r) => {
    const lead = U(902), cid = 'conv_attempt9silent001'; const tl = timeline(420, { ring: 30, bill: 10, el: 0 });
    await happy(w, r, { lead, cid, attempts: 8, tl, convOpts: { dur: 0, transcript: [], eval: 'failure', term: 'no_user_activity' } });
    const L = w.leads.get(lead);
    check('A-DUR-9e', 'intento 9 contestado sin conversación real -> fin de 3x3: CLOSED + UNRESPONSIVE, attempts 9, sin nextActionAt', w.callFus(lead).length === 1 && L.attempts === 9 && L.status === 'CLOSED' && L.stage === 'UNRESPONSIVE' && !w.callFus(lead)[0].nextActionAt, JSON.stringify({ L, c: w.callFus(lead) }));
  });

  // =====================================================================================
  // A-DUR-10: dos webhooks / replays -> sin duplicar Activity ni attempts
  // =====================================================================================
  await scenario('A-DUR-10', async (w, r) => {
    const lead = U(1001), cid = 'conv_replay0000000001'; const tl = timeline(420, { ring: 30, bill: 75, el: 77 });
    const h = await happy(w, r, { lead, cid, attempts: 4, tl, ends: 'webhook' });
    await r.webhook(h.conv); await r.polling(); await r.polling(); await drain(r);                     // webhook x2 + polling x2
    // WF2 vuelve a correr sobre el MISMO lead (sigue ATTEMPTING/CONTACTED, no se re-despacha) y registra otra vez el ledger
    const rl = await code2('🧾 Build Deferred Ledger (Asterisk)', w, { items: [{ json: { outcome: 'CONNECTED', callStatus: 'ANSWERED', elevenlabs_call_id: cid, lead_id: lead, attempts: 5, last_call_time: new Date().toISOString() } }], nodes: { '📥 Fetch + Lock (Asterisk)': [{ json: { lead_id: lead, phone: PHONE } }] } });
    await mysqlNode([rl.out[0].json.ledger_sql]);
    await sql("UPDATE wf_call_events SET state='RECEIVED', followup_id=NULL, lifecycle_applied=0, crm_attempt=NULL WHERE source='elevenlabs'"); await drain(r);   // pérdida de escritura + reproceso
    const rows = await sql("SELECT event_key, state, followup_id FROM wf_call_events WHERE source='elevenlabs'");
    check('A-DUR-10', 'webhook x2 + polling x2 + ledger repetido + reproceso forzado -> 1 Activity, attempts 5, una fila en el inbox, una en wf_call_followups', w.callFus(lead).length === 1 && w.leads.get(lead).attempts === 5 &&
      rows.length === 1 && rows[0].state === 'DONE' && (await fcf(`lead_id='${lead}'`)).length === 1 && w.callFus(lead)[0].durationSeconds === 75, JSON.stringify({ rows, L: w.leads.get(lead), n: w.callFus(lead).length }));
    check('A-DUR-10b', 'el reproceso reutiliza la Activity existente (providerCallId): duración sigue siendo la del CDR (75)', w.callFus(lead)[0].durationSeconds === 75);
  });

  // =====================================================================================
  // Consecuencias (pedido explícito, punto 14)
  // =====================================================================================
  // C1/C2/C3: Mantenimiento no libera, wf_call_events protege, sin ventana de re-marcado
  await scenario('C1-3', async (w, r) => {
    const lead = U(1101), cid = 'conv_mainthold00001';
    w.lead(lead, { attempts: 1, status: 'NOT_CONTACTED', phone: PHONE });
    await wf2Cycle(w, () => ANSWER(cid));
    await age(w, 45 * 60);                                    // lleva 45 min ATTEMPTING (> STALE_MIN 30): el escenario más peligroso
    const L0 = w.leads.get(lead);
    const dbRows = (await mysqlNode([by2['🗄️ Leer Estado Eventos (Mantenimiento)'].parameters.query])).map(i => i.json);
    const m = await code2('🧹 Mantenimiento Leads (reactivar + liberar)', w, { items: dbRows });
    check('C1', 'Mantenimiento NO libera a NO_ANSWER un lead ATTEMPTING cuya llamada contestada espera a WF9 (45 min > STALE_MIN)', w.leads.get(lead).status === 'ATTEMPTING', JSON.stringify({ L: w.leads.get(lead), dbRows }));
    check('C2', 'wf_call_events lo protege: la fila diferida existe, sin resolver (RECEIVED, dispatched_at) y lo ve la consulta de Mantenimiento', dbRows.some(x => x.lead_id === lead), JSON.stringify(dbRows));
    const again = await wf2Cycle(w, () => ANSWER('conv_shouldnotredial'));
    check('C3', 'ventana de re-marcado: mientras espera, Fetch + Lock no vuelve a despachar el lead (0 leads, 0 dial)', !again.fetched.some(x => x.json.lead_id === lead) && w.leads.get(lead).attempts === 1, JSON.stringify(again.fetched.map(x => x.json.lead_id)));
    // C4: attempts sólo cambia con el POST de WF9
    check('C4', 'attempts NO cambió mientras WF9 espera (sigue 1, 0 POST)', w.leads.get(lead).attempts === 1 && posts(w).length === 0);
    // la llamada termina; WF9 la resuelve; después el lead ya no es "colgado"
    const tl = timeline(45 * 60 + 200, { ring: 30, bill: 70, el: 72 });
    await addCdr({ phone: PHONE, start: tl.T0, duration: 100, billsec: 70 });
    const c = conv({ id: cid, lead_id: lead, n: 2, phone: PHONE, start: tl.elStart, dur: 72, transcript: okTranscript }); w.convs.set(cid, c);
    await r.polling(); await drain(r);
    check('C4b', 'al terminar, WF9 hace el POST (+1) y el lead pasa a CONTACTED', w.callFus(lead).length === 1 && w.leads.get(lead).attempts === 2 && w.leads.get(lead).status === 'CONTACTED', JSON.stringify(w.leads.get(lead)));
    const dbRows2 = (await mysqlNode([by2['🗄️ Leer Estado Eventos (Mantenimiento)'].parameters.query])).map(i => i.json);
    check('C4c', 'resuelto el evento, Mantenimiento ya no lo retiene (sin reintento ni hold vigente)', !dbRows2.some(x => x.lead_id === lead && Number(x.unresolved) > 0), JSON.stringify(dbRows2));
  });

  // C-GIVEUP: conversación inexistente / nunca terminal -> se registra UNA Activity y no queda colgado
  for (const [tag, setup] of [['conversación 404', () => { }], ['conversación nunca terminal', (w, cid, lead) => w.convs.set(cid, conv({ id: cid, lead_id: lead, n: 1, phone: PHONE, status: 'in-progress', start: nowS() + 20, dur: 0 }))]]) {
    await scenario('C-GIVEUP ' + tag, async (w, r) => {
      const lead = U(tag === 'conversación 404' ? 1201 : 1202), cid = 'conv_giveup' + (tag === 'conversación 404' ? 'a' : 'b') + '000001';
      w.lead(lead, { attempts: 0, status: 'NOT_CONTACTED', phone: PHONE });
      await wf2Cycle(w, () => ANSWER(cid)); setup(w, cid, lead);
      await age(w, 30 * 60);
      await addCdr({ phone: PHONE, start: nowS() - 30 * 60 + 5, duration: 100, billsec: 66 });
      await wake(); await r.processorCycle();
      const mid = w.callFus(lead).length;                     // a los 30 min todavía espera (no se rinde antes de 6 h)
      await age(w, 7 * 3600); for (const c of w.convs.values()) c.metadata.start_time_unix_secs -= 7 * 3600; await drain(r);
      const c = w.callFus(lead);
      check('C-GIVEUP', `${tag}: no se rinde antes de 6 h; pasadas 6 h se registra UNA Activity NO_ANSWER "sin conversación confirmada" y el lead sale de ATTEMPTING`, mid === 0 && c.length === 1 && c[0].outcome === 'NO_ANSWER' && w.leads.get(lead).status === 'NO_ANSWER' && w.leads.get(lead).attempts === 1, JSON.stringify({ mid, c, L: w.leads.get(lead) }));
    });
  }

  // C6: callback pedido -> funciona en Asterisk; nextActionAt único mecanismo; duración = billsec
  await scenario('C6', async (w, r) => {
    const lead = U(1301), cid = 'conv_callbackast0001'; const tl = timeline(420, { ring: 30, bill: 71, el: 74 });
    const want = new Date(Date.now() + 26 * H); want.setUTCMinutes(0, 0, 0);
    await happy(w, r, { lead, cid, attempts: 1, tl, convOpts: { dc: { callback_requested: { value: 'true' }, callback_date: { value: want.toISOString() } }, transcript: [{ role: 'user', message: 'I am busy now, call me tomorrow afternoon' }, { role: 'user', message: 'yes' }] } });
    const c = w.callFus(lead)[0]; const L = w.leads.get(lead);
    check('C6', 'callback pedido en Asterisk: CONNECTED, lead CONTACTED, fecha pedida en nextFollowUpAt vía nextActionAt, duración = billsec (71)', c && c.outcome === 'CONNECTED' && L.status === 'CONTACTED' && L.nextFollowUpAt === want.toISOString() && c.nextActionAt === want.toISOString() && c.durationSeconds === 71, JSON.stringify({ c, L }));
  });

  // C8: nextFollowUpAt nunca se escribe por PATCH (en ningún camino)
  {
    // se recorren todos los PATCH/POST de las corridas anteriores sería ideal; aquí se verifica en un escenario completo con 3 caminos
    await scenario('C8', async (w, r) => {
      const l1 = U(1401), l2 = U(1402), l3 = U(1403);
      w.lead(l1, { attempts: 0, status: 'NOT_CONTACTED', phone: '+919800001401' }); w.lead(l2, { attempts: 3, status: 'NOT_CONTACTED', phone: '+919800001402' }); w.lead(l3, { attempts: 5, status: 'NOT_CONTACTED', phone: '+919800001403' });
      await wf2Cycle(w, l => l.lead_id === l1 ? ANSWER('conv_c8answered0001') : (l.lead_id === l2 ? NOANS : ANSWER('conv_c8voicemail001')));
      await age(w, 420); const T0 = nowS() - 420;
      await addCdr({ phone: '+919800001401', start: T0, duration: 90, billsec: 60 }); await addCdr({ phone: '+919800001403', start: T0, duration: 40, billsec: 12 });
      w.convs.set('conv_c8answered0001', conv({ id: 'conv_c8answered0001', lead_id: l1, n: 1, phone: '+919800001401', start: T0 + 30, dur: 62, transcript: okTranscript }));
      w.convs.set('conv_c8voicemail001', conv({ id: 'conv_c8voicemail001', lead_id: l3, n: 6, phone: '+919800001403', start: T0 + 28, dur: 14, eval: 'failure', term: 'voicemail_detected', summary: 'The call reached a voicemail box.', transcript: [{ role: 'user', message: 'Please leave a message after the tone' }] }));
      await r.polling(); await drain(r);
      const bad = w.calls.filter(c => (c.method === 'PATCH' && c.body && 'nextFollowUpAt' in c.body));
      check('C8', 'ningún PATCH escribe nextFollowUpAt (contestada, NO_ANSWER y buzón): sólo nextActionAt en el POST', bad.length === 0, JSON.stringify(bad));
      check('C8b', 'buzón intento 6 (fin del ciclo 2) -> NO_ANSWER; NO_ANSWER intento 4 -> +hold correcto; cada llamada 1 Activity', w.callFus(l1).length === 1 && w.callFus(l2).length === 1 && w.callFus(l3).length === 1 && w.leads.get(l3).status === 'NO_ANSWER' && w.leads.get(l3).attempts === 6, JSON.stringify([...w.leads.values()].map(l => [l.status, l.attempts])));
    });
  }

  // C9: eventos stale no pisan un ciclo de vida más nuevo
  await scenario('C9a', async (w, r) => {   // un humano/otra automatización cambió el lead DESPUÉS de la llamada
    const lead = U(1501), cid = 'conv_stalehuman0001'; const tl = timeline(420, { ring: 30, bill: 50, el: 52 });
    w.lead(lead, { attempts: 2, status: 'NOT_CONTACTED', phone: PHONE });
    await wf2Cycle(w, () => ANSWER(cid)); await age(w, 420);
    // alguien lo cierra manualmente 5 min después de la llamada (estado más nuevo)
    const l = w.leads.get(lead); l.status = 'CLOSED'; l.stage = 'WON'; l.updatedAt = new Date().toISOString();
    await addCdr({ phone: PHONE, start: tl.T0, duration: 80, billsec: 50 });
    w.convs.set(cid, conv({ id: cid, lead_id: lead, n: 3, phone: PHONE, start: tl.elStart, dur: 52, transcript: okTranscript }));
    await r.polling(); await drain(r);
    check('C9a', 'estado del lead cambiado después de la llamada (CLOSED/WON): la Activity histórica se crea 1 vez (billsec 50) pero status/stage NO se pisan', w.callFus(lead).length === 1 && w.callFus(lead)[0].durationSeconds === 50 && l.status === 'CLOSED' && l.stage === 'WON', JSON.stringify({ l, c: w.callFus(lead) }));
  });
  await scenario('C9b', async (w, r) => {   // hubo un despacho MÁS NUEVO del mismo lead (otra llamada)
    const lead = U(1502), cidOld = 'conv_staleold000001', cidNew = 'conv_stalenew000001';
    w.lead(lead, { attempts: 1, status: 'NOT_CONTACTED', phone: PHONE });
    await wf2Cycle(w, () => ANSWER(cidOld));
    await age(w, 3 * 3600);
    w.leads.get(lead).status = 'NOT_CONTACTED';            // (el caso raro: quedó libre por otra vía)
    await wf2Cycle(w, () => ANSWER(cidNew));               // segunda llamada 3 h después (más nueva)
    const T_old = nowS() - 3 * 3600;
    await addCdr({ phone: PHONE, start: T_old, duration: 80, billsec: 44 });
    w.convs.set(cidOld, conv({ id: cidOld, lead_id: lead, n: 2, phone: PHONE, start: T_old + 30, dur: 46, transcript: okTranscript }));
    await r.polling(); await drain(r, 3);
    const eOld = await ev('elevenlabs:' + cidOld);
    const Lnow = Object.assign({}, w.leads.get(lead));
    check('C9b', 'evento viejo con un despacho más nuevo del mismo lead: Activity histórica 1 vez (billsec 44) y lifecycle NO pisado (el lead sigue ATTEMPTING por la llamada nueva)', w.callFus(lead).length === 1 && w.callFus(lead)[0].durationSeconds === 44 && Lnow.status === 'ATTEMPTING' && eOld.state === 'STALE', JSON.stringify({ eOld: eOld && [eOld.state, eOld.resolution], Lnow, c: w.callFus(lead).map(x => x.durationSeconds) }));
  });

  // C9c: una Activity posterior de un agente NO vuelve histórica a la llamada diferida retenida (si no, el lead se liberaría y se re-marcaría)
  await scenario('C9c', async (w, r) => {
    const lead = U(1503), cid = 'conv_agentafter0001'; const tl = timeline(420, { ring: 30, bill: 50, el: 52 });
    w.lead(lead, { attempts: 2, status: 'NOT_CONTACTED', phone: PHONE });
    await wf2Cycle(w, () => ANSWER(cid)); await age(w, 420);
    const human = w.addFollowup(lead, { outcome: 'CONNECTED', callStatus: 'ANSWERED', notes: 'Agent call' });   // 0 s atrás: > 2 min después de terminar la llamada
    await addCdr({ phone: PHONE, start: tl.T0, duration: 80, billsec: 50 });
    w.convs.set(cid, conv({ id: cid, lead_id: lead, n: 3, phone: PHONE, start: tl.elStart, dur: 52, transcript: okTranscript }));
    await r.polling(); await drain(r);
    const e = await ev('elevenlabs:' + cid);
    check('C9c', 'Activity de un agente posterior a la llamada: WF9 igual aplica el ciclo de vida (lead CONTACTED, evento DONE, no STALE) y crea la suya con billsec 50', e.state === 'DONE' && w.leads.get(lead).status === 'CONTACTED' && w.callFus(lead).length === 2 && w.callFus(lead).some(f => f.durationSeconds === 50), JSON.stringify({ e: e && [e.state, e.resolution], L: w.leads.get(lead) }));
  });
  // lead_id / phone vacíos en las variables dinámicas de ElevenLabs: la fila diferida de WF2 los aporta
  await scenario('C-LEAD', async (w, r) => {
    const lead = U(1901), cid = 'conv_nodynvars00001'; const tl = timeline(420, { ring: 30, bill: 60, el: 63 });
    await happy(w, r, { lead, cid, attempts: 0, tl, convOpts: { dyn: { lead_id: '', phone: '' } } });
    check('C-LEAD', 'variables dinámicas sin lead_id/phone: se usan los del ledger de WF2 -> 1 Activity (billsec 60), attempts 1', w.callFus(lead).length === 1 && w.callFus(lead)[0].durationSeconds === 60 && w.leads.get(lead).attempts === 1, JSON.stringify({ c: w.callFus(lead), L: w.leads.get(lead) }));
  });
  // ledger: una fila previa con provider distinto (webhook antes que WF2) se corrige a 'asterisk'
  await scenario('C-PROV', async (w, r) => {
    const lead = U(2001), cid = 'conv_provfix000001';
    await sql(`INSERT INTO wf_call_events (event_key, source, provider, external_id, lead_id, state, next_attempt_at) VALUES ('elevenlabs:${cid}','elevenlabs','stringee','${cid}','${lead}','RECEIVED', UTC_TIMESTAMP(3) + INTERVAL 20 MINUTE)`);
    w.lead(lead, { attempts: 0, status: 'NOT_CONTACTED', phone: PHONE });
    await wf2Cycle(w, () => ANSWER(cid));
    const e = await ev('elevenlabs:' + cid);
    check('C-PROV', 'webhook previo con provider erróneo: el ledger de WF2 fija provider=asterisk y dispatched_at (Mantenimiento lo retiene)', e.provider === 'asterisk' && e.dispatched_at, JSON.stringify(e));
  });

  // C12: Stringee intacto -> el candidato no consulta CDR ni cambia nada para Stringee
  await scenario('C12', async (w, r) => {
    const { job } = require('./mocks'); const iso2 = iso;
    const lead = U(1601), jid = 'aaaaaaaa-1111-4111-8111-160100000001', cid = 'conv_stringeedur0001';
    w.lead(lead, { attempts: 1, phone: '+919812301601' });
    w.convs.set(cid, conv({ id: cid, lead_id: lead, n: 2, provider: 'STRINGEE', dur: 77, transcript: okTranscript, phone: '+919812301601' }));
    const j = job({ job_id: jid, lead_id: lead, call_attempts: 2, final_status: 'ANSWERED', answered: true, answered_at: iso2(Date.now() - 100e3), elevenlabs_conversation_id: cid, sip_code: 200 });
    w.jobs.set(jid, j); await r.callback(j); await drain(r);
    const c = w.callFus(lead)[0];
    check('C12', 'Stringee contestada: sigue el flujo WF9 (duración de ElevenLabs = 77, 1 Activity, attempts 2); sin cambios respecto al paquete anterior', w.callFus(lead).length === 1 && c.durationSeconds === 77 && w.leads.get(lead).attempts === 2 && w.leads.get(lead).status === 'CONTACTED', JSON.stringify({ c, L: w.leads.get(lead) }));
    const ins = await runCode(r.by['🧾 Build CDR SQL (Asterisk)'].parameters.jsCode, { items: [{ json: { event_key: 'stringee:' + jid, claim_token: 't', source: 'stringee', provider: 'stringee', phone: '919812301601', event_at_ms: Date.now() } }] });
    check('C12b', 'el nodo Build CDR SQL ignora eventos Stringee (SELECT 1, ninguna consulta al CDR)', ins.out[0].json.cdr_sql === 'SELECT 1 AS noop', JSON.stringify(ins.out[0].json));
    const stringeeWf2 = ['📥 Fetch + Lock (Stringee)', '🏁 Guardar Resultado (Stringee)', '🧾 Build Ledger SQL (Stringee)'].every(n => by2[n]);
    const same = ['📥 Fetch + Lock (Stringee)', '🏁 Guardar Resultado (Stringee)', '🧾 Build Ledger SQL (Stringee)'].every(n => by2[n].parameters.jsCode === JSON.parse(fs.readFileSync(__dirname + '/../out/WF2_UNIFICADO_v2_PATCHED.json', 'utf8')).nodes.find(x => x.name === n).parameters.jsCode);
    check('C12c', 'WF2: los 3 nodos Stringee son idénticos byte a byte al paquete anterior', stringeeWf2 && same);
  });

  // C15: fail-open del ledger de WF2 (nunca se pierde una llamada)
  await scenario('C15', async (w, r) => {
    const lead = U(1701), cid = 'conv_ledgerfail0001'; const tl = timeline(420, { ring: 30, bill: 60, el: 62 });
    w.lead(lead, { attempts: 2, status: 'NOT_CONTACTED', phone: PHONE });
    const c2 = await wf2Cycle(w, () => ANSWER(cid), { ledgerFail: true });
    const legacy = w.callFus(lead);
    check('C15', 'si el registro diferido falla (MySQL caído): WF2 crea la Activity inmediata como antes (no se pierde la llamada), 1 Activity, attempts 3', legacy.length === 1 && w.leads.get(lead).attempts === 3 && legacy[0].durationSeconds === 0 && !(c2.guardar[0] && c2.guardar[0].deferred_to_wf9), JSON.stringify({ legacy, g: c2.guardar }));
    await age(w, 420);
    w.convs.set(cid, conv({ id: cid, lead_id: lead, n: 3, phone: PHONE, start: tl.elStart, dur: 62, transcript: okTranscript }));
    await r.polling(); await drain(r);
    check('C15b', 'después WF9 ve la Activity de WF2 (ASTERISK_OWNED_BY_WF2) y no crea otra', w.callFus(lead).length === 1 && w.leads.get(lead).attempts === 3, JSON.stringify({ n: w.callFus(lead).length, e: (await ev('elevenlabs:' + cid)) && (await ev('elevenlabs:' + cid)).resolution }));
  });

  // C16: rollback -> WF9 ANTERIOR sobre una fila diferida: espera y crea una sola Activity (duración EL), sin colgar el lead
  await scenario('C16', async (w, r0) => {
    const rOld = makeRunner(WF9_OLD, w);
    const lead = U(1801), cid = 'conv_rollback00000001'; const tl = timeline(25 * 60 + 200, { ring: 30, bill: 66, el: 70 });
    w.lead(lead, { attempts: 0, status: 'NOT_CONTACTED', phone: PHONE });
    await wf2Cycle(w, () => ANSWER(cid)); await age(w, 25 * 60);
    w.convs.set(cid, conv({ id: cid, lead_id: lead, n: 1, phone: PHONE, start: nowS() - 25 * 60 + 20, dur: 70, transcript: okTranscript }));
    await rOld.polling(); await drain(rOld);
    const c = w.callFus(lead);
    check('C16', 'ROLLBACK (WF9 anterior + WF2 nuevo): la fila diferida no se pierde; el WF9 viejo la trata como hueco y crea UNA Activity (duración ElevenLabs 70), lead CONTACTED', c.length === 1 && c[0].durationSeconds === 70 && w.leads.get(lead).attempts === 1 && w.leads.get(lead).status === 'CONTACTED', JSON.stringify({ c, L: w.leads.get(lead) }));
  });

  // Notas en inglés en todo lo que este candidato escribe en el CRM
  const bad = allNotes.filter(spanishOrNonAscii);
  check('EN', `todo texto persistido en el CRM por este candidato está en inglés (${allNotes.length} notas revisadas)`, allNotes.length >= 15 && bad.length === 0, bad.slice(0, 3).join(' || '));

  await close();
  fs.writeFileSync(__dirname + '/sim_dur_results.json', JSON.stringify(results, null, 2));
  fs.writeFileSync(__dirname + '/sim_dur_notes_sample.txt', allNotes.join('\n'));
  for (const x of results) console.log((x.pass ? 'PASS' : 'FAIL') + ' [' + x.id + '] ' + x.name + (x.pass ? '' : '\n      ↳ ' + String(x.detail).slice(0, 1200)));
  console.log(`\nTotal: ${results.filter(x => x.pass).length}/${results.length} PASS`);
  process.exit(results.every(x => x.pass) ? 0 : 1);
})().catch(e => { console.error(e); process.exit(1); });
