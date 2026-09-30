// Simulación de los escenarios obligatorios con los nodos reales del WF9 generado.
const { World, job, conv, iso } = require('./mocks');
const { makeRunner, wake, drain } = require('./pipeline');
const { sql, close } = require('./harness');
const OUT = process.env.WF9_FILE || (__dirname + '/../out/WF9_PRODUCTION_FIXED_STRINGEE_RELIABILITY.json');

const results = [];
function check(id, name, cond, detail) { results.push({ id, name, pass: !!cond, detail: cond ? '' : (detail || '') }); }
const U = n => `${String(n).padStart(8, '0')}-0000-4000-8000-${String(n).padStart(12, '0')}`;
const J = n => `${String(n).padStart(8, 'a')}-1111-4111-8111-${String(n).padStart(12, '0')}`;
const H = 3600e3;
const approx = (a, b, tol = 90e3) => Math.abs(a - b) <= tol;
async function reset() { await sql('DELETE FROM wf_call_events'); await sql('DELETE FROM wf_call_followups'); }
async function ev(key) { return (await sql(`SELECT * FROM wf_call_events WHERE event_key='${key}'`))[0]; }
function spanishOrNonAscii(s) {
  return /[^\x20-\x7E]/.test(s) || /\b(intento|llamada|ciclo|contest|buz[oó]n|pr[oó]ximo|reintento|sin respuesta|cliente)\b/i.test(s);
}

async function scenario(title, fn) {
  await reset();
  const w = new World();
  const r = makeRunner(OUT, w);
  try { await fn(w, r); } catch (e) { check('ERR', title + ' lanzó excepción', false, e.stack); }
  return { w, r };
}

(async () => {
  const allNotes = [];
  const collect = w => { for (const c of w.calls) if (c.method === 'POST' && /followups$/.test(c.url) && c.body) allNotes.push(String(c.body.notes || '')); };

  // ---------------- V17 + V16: SIP 480 intento 7; re-ejecutar el mismo job ----------------
  let S = await scenario('V17', async (w, r) => {
    const lead = U(17), jid = '995edb6e-0940-47c9-a2b2-3f4f58636dd5';
    w.lead(lead, { attempts: 6, status: 'ATTEMPTING', updatedAt: iso(Date.now() - 125e3) });
    const j = job({ job_id: jid, lead_id: lead, call_attempts: 7, final_status: 'UNAVAILABLE', telephony_status: 'UNAVAILABLE', sip_code: 480, sip_reason: 'Temporarily Unavailable' });
    w.jobs.set(jid, j);
    await r.ledger([{ json: { job_id: jid, lead_id: lead, attempts: 7, last_call_time: iso(Date.now() - 125e3), patch_ok: true } }], [{ json: { lead_id: lead, phone: '+919812345678' } }]);
    await r.callback(j);
    await drain(r);
    const L = w.leads.get(lead); const calls = w.callFus(lead); const e = await ev('stringee:' + jid);
    const startMs = new Date(j.started_at).getTime();
    check('17', 'SIP 480 intento 7 → 1 Activity', calls.length === 1, JSON.stringify(calls));
    check('17', 'SIP 480 intento 7 → status NO_ANSWER, attempts 7', L.status === 'NO_ANSWER' && L.attempts === 7, JSON.stringify(L));
    check('17', 'SIP 480 intento 7 → próximo intento +2 h (desde la hora de la llamada)', approx(new Date(L.nextFollowUpAt).getTime(), startMs + 2 * H), L.nextFollowUpAt);
    check('17', 'SIP 480 → outcome NO_ANSWER / callStatus NO_ANSWER / nota con SIP 480', calls[0] && calls[0].outcome === 'NO_ANSWER' && calls[0].callStatus === 'NO_ANSWER' && /SIP 480 \(Temporarily Unavailable\)/.test(calls[0].notes), calls[0] && calls[0].notes);
    check('17', 'evento DONE con followup_id y wf_call_followups recording_synced=1', e.state === 'DONE' && e.followup_id === calls[0].id &&
      (await sql(`SELECT recording_synced r FROM wf_call_followups WHERE followup_id='${calls[0].id}'`))[0].r === 1, JSON.stringify(e));
    // V16: callback repetido + reproceso forzado sin followup_id guardado (pérdida de escritura)
    await r.callback(j); await drain(r);
    await sql(`UPDATE wf_call_events SET state='RECEIVED', followup_id=NULL, lifecycle_applied=0, crm_attempt=NULL WHERE event_key='stringee:${jid}'`);
    await drain(r);
    check('16', 'mismo job_id re-ejecutado (callback duplicado + reproceso forzado) → sigue 1 Activity, attempts 7', w.callFus(lead).length === 1 && w.leads.get(lead).attempts === 7, JSON.stringify(w.callFus(lead)));
    check('16', 'wf_call_followups sin duplicados', (await sql(`SELECT COUNT(*) n FROM wf_call_followups WHERE lead_id='${lead}'`))[0].n === 1);
    collect(w);
  });

  // ---------------- V18: intento 8 sin éxito → +3 h ----------------
  await scenario('V18', async (w, r) => {
    const lead = U(18), jid = J(18);
    w.lead(lead, { attempts: 7 });
    const j = job({ job_id: jid, lead_id: lead, call_attempts: 8, final_status: 'NO_ANSWER', sip_code: 408 });
    w.jobs.set(jid, j); await r.callback(j); await drain(r);
    const L = w.leads.get(lead);
    check('18', 'intento 8 sin éxito → NO_ANSWER, attempts 8, +3 h', L.status === 'NO_ANSWER' && L.attempts === 8 && approx(new Date(L.nextFollowUpAt).getTime(), new Date(j.started_at).getTime() + 3 * H), JSON.stringify(L));
    collect(w);
  });

  // ---------------- V19: intento 9 sin éxito → CLOSED + UNRESPONSIVE + nextFollowUpAt null ----------------
  await scenario('V19', async (w, r) => {
    const lead = U(19), jid = J(19);
    w.lead(lead, { attempts: 8, nextFollowUpAt: iso(Date.now() - 60e3) });
    const j = job({ job_id: jid, lead_id: lead, call_attempts: 9, final_status: 'BUSY', telephony_status: 'BUSY', sip_code: 486 });
    w.jobs.set(jid, j); await r.callback(j); await drain(r);
    const L = w.leads.get(lead); const calls = w.callFus(lead);
    check('19', 'intento 9 sin éxito → CLOSED + stage UNRESPONSIVE + nextFollowUpAt null', L.status === 'CLOSED' && L.stage === 'UNRESPONSIVE' && L.nextFollowUpAt === null, JSON.stringify(L));
    check('19', 'intento 9 → NO se usa LOST y la Activity no programa otro intento', L.stage !== 'LOST' && calls.length === 1 && !w.followups.get(lead).some(f => f.type === 'TASK'), JSON.stringify(w.followups.get(lead)));
    collect(w);
  });

  // ---------------- V20: intento 9 contestado → CONTACTED, no UNRESPONSIVE ----------------
  await scenario('V20', async (w, r) => {
    const lead = U(20), jid = J(20), cid = 'conv_v20answered0001';
    w.lead(lead, { attempts: 8 });
    w.convs.set(cid, conv({ id: cid, lead_id: lead, n: 9, provider: 'STRINGEE', dur: 95, eval: 'failure',
      transcript: [{ role: 'agent', message: 'Hello' }, { role: 'user', message: 'Yes, tell me about the account' }, { role: 'user', message: 'I will think about it' }] }));
    const j = job({ job_id: jid, lead_id: lead, call_attempts: 9, final_status: 'ANSWERED', telephony_status: 'ANSWERED', answered: true, answered_at: iso(Date.now() - 110e3), elevenlabs_conversation_id: cid, sip_code: 200 });
    w.jobs.set(jid, j); await r.callback(j); await drain(r);
    const L = w.leads.get(lead);
    check('20', 'intento 9 contestado (call_successful=failure pero con conversación real) → CONTACTED, stage ≠ UNRESPONSIVE', L.status === 'CONTACTED' && L.stage !== 'UNRESPONSIVE' && L.stage !== 'LOST', JSON.stringify(L));
    check('20', 'Activity CONNECTED / ANSWERED, recording_synced=0', w.callFus(lead)[0].outcome === 'CONNECTED' && (await sql(`SELECT recording_synced r FROM wf_call_followups WHERE lead_id='${lead}'`))[0].r === 0);
    collect(w);
  });

  // ---------------- V21: intento >= 10 no se puede programar ----------------
  await scenario('V21', async (w, r) => {
    const lead = U(21), jid = J(21);
    w.lead(lead, { attempts: 12 });
    const j = job({ job_id: jid, lead_id: lead, call_attempts: 13, final_status: 'NO_ANSWER' });
    w.jobs.set(jid, j); await r.callback(j); await drain(r);
    const L = w.leads.get(lead); const c = w.callFus(lead)[0];
    const posts = w.calls.filter(x => x.method === 'POST' && /followups$/.test(x.url));
    check('21', 'llamada 13 (bug histórico) → Activity registrada, sin nextActionAt, CLOSED + UNRESPONSIVE', c && L.status === 'CLOSED' && L.stage === 'UNRESPONSIVE' && posts.every(p => !p.body.nextActionAt), JSON.stringify({ L, posts: posts.map(p => p.body) }));
    check('21', 'nota: "beyond the configured maximum of 9"', c && /beyond the configured maximum of 9/.test(c.notes), c && c.notes);
    collect(w);
  });

  // ---------------- V22: fallo telefónico Stringee sin puente / sin conversación ----------------
  await scenario('V22', async (w, r) => {
    const lead = U(22), jid = J(22);
    w.lead(lead, { attempts: 2 });
    const j = job({ job_id: jid, lead_id: lead, call_attempts: 3, final_status: 'FAILED', telephony_status: 'FAILED', status: 'failed', sip_code: 503, sip_reason: 'Service Unavailable', bridge_started: false, elevenlabs_conversation_id: null, ringing_at: null, exit_code: 1 });
    w.jobs.set(jid, j); await r.callback(j); await drain(r);
    const L = w.leads.get(lead); const c = w.callFus(lead)[0];
    check('22', 'fallo telefónico (SIP 503, sin puente, sin conversación) → registrado en el CRM', c && c.callStatus === 'FAILED' && L.attempts === 3 && L.status === 'NO_ANSWER', JSON.stringify({ L, c }));
    check('22', 'intento 3 (fin ciclo 1) → +2 días hábiles', L.nextFollowUpAt && new Date(L.nextFollowUpAt).getTime() > Date.now() + 40 * H, L.nextFollowUpAt);
    // fallo del proveedor SIN evidencia telefónica → sin Activity, sin intento, NOT_CONTACTED, auditado
    const lead2 = U(222), jid2 = J(222);
    w.lead(lead2, { attempts: 4, status: 'ATTEMPTING', updatedAt: iso(Date.now() - 125e3) });
    const j2 = job({ job_id: jid2, lead_id: lead2, call_attempts: 5, final_status: 'FAILED', status: 'failed', sip_code: null, ringing_at: null, stringee_call_id: null, answered: false, error: 'worker_restarted' });
    w.jobs.set(jid2, j2); await r.callback(j2); await drain(r);
    const L2 = w.leads.get(lead2); const e2 = await ev('stringee:' + jid2);
    check('22b', 'FAILED sin evidencia telefónica → sin Activity, sin intento, NOT_CONTACTED, auditado', w.callFus(lead2).length === 0 && L2.attempts === 4 && L2.status === 'NOT_CONTACTED' && e2.resolution === 'PROVIDER_FAILURE_RELEASED', JSON.stringify({ L2, e2 }));
    collect(w);
  });

  // ---------------- V23: Stringee contestada: callback + webhook + polling → 1 sola Activity ----------------
  await scenario('V23', async (w, r) => {
    const lead = U(23), jid = J(23), cid = 'conv_v23stringee00001';
    w.lead(lead, { attempts: 0 });
    const c = conv({ id: cid, lead_id: lead, n: 1, provider: 'STRINGEE', dur: 45, transcript: [{ role: 'agent', message: 'Hi' }, { role: 'user', message: 'Hello, who is this?' }, { role: 'user', message: 'Okay send details' }] });
    w.convs.set(cid, c);
    const j = job({ job_id: jid, lead_id: lead, call_attempts: 1, final_status: 'ANSWERED', answered: true, answered_at: iso(Date.now() - 100e3), elevenlabs_conversation_id: cid, sip_code: 200 });
    w.jobs.set(jid, j);
    const wh = await r.webhook(c);           // webhook de ElevenLabs primero
    const pl = await r.polling();            // polling también la ve
    check('CONVID', 'rama conv-id Stringee conservada (webhook y polling generan el UPDATE de stringee_calls)',
      /UPDATE stringee_calls/.test(wh.convid.sql || '') && pl.convid.some(x => /UPDATE stringee_calls/.test(x.sql || '')) && wh.parse.elevenlabs_call_id === cid && wh.parse.country === 'india',
      JSON.stringify({ wh: wh.convid, pl: pl.convid }));
    await r.callback(j);          // callback del worker
    await drain(r);
    await r.webhook(c); await r.polling(); await drain(r); // re-entregas
    const e = await ev('elevenlabs:' + cid);
    check('23', 'contestada Stringee con conversación: callback + webhook + polling → exactamente 1 Activity', w.callFus(lead).length === 1 && w.leads.get(lead).attempts === 1, JSON.stringify(w.callFus(lead)));
    check('23', 'evento ElevenLabs queda SKIPPED LINKED_TO_STRINGEE_JOB', e.state === 'SKIPPED' && e.resolution === 'LINKED_TO_STRINGEE_JOB', JSON.stringify(e));
    collect(w);
  });

  // ---------------- V24: evento viejo intento 7 no pisa resultado más nuevo intento 8 ----------------
  await scenario('V24', async (w, r) => {
    const lead = U(24), j7 = J(247), j8 = J(248);
    w.lead(lead, { attempts: 6 });
    const now = Date.now();
    const job7 = job({ job_id: j7, lead_id: lead, call_attempts: 7, final_status: 'NO_ANSWER', created_at: iso(now - 6 * H - 60e3), started_at: iso(now - 6 * H), finished_at: iso(now - 6 * H + 60e3) });
    const cid8 = 'conv_v24attempt8call1';
    w.convs.set(cid8, conv({ id: cid8, lead_id: lead, n: 8, provider: 'STRINGEE', dur: 70, transcript: [{ role: 'user', message: 'Yes I am interested, call me' }, { role: 'user', message: 'Thanks' }] }));
    const job8 = job({ job_id: j8, lead_id: lead, call_attempts: 8, final_status: 'ANSWERED', answered: true, answered_at: iso(now - 115e3), elevenlabs_conversation_id: cid8, sip_code: 200 });
    w.jobs.set(j7, job7); w.jobs.set(j8, job8);
    await r.callback(job8); await drain(r);            // llega primero el resultado más nuevo
    const before = Object.assign({}, w.leads.get(lead));
    await r.callback(job7); await drain(r);            // después el viejo (callback perdido y recuperado)
    const L = w.leads.get(lead); const e7 = await ev('stringee:' + j7);
    const act7 = w.callFus(lead).find(f => f.providerCallId === 'stringee:' + j7);
    check('24', 'evento viejo (intento 7) → Activity histórica creada 1 vez', !!act7 && /Historical call recovered during reconciliation/.test(act7.notes) && /Call attempt 7 of 9/.test(act7.notes) && e7.state === 'STALE', JSON.stringify({ e7, act7 }));
    check('24', 'evento viejo NO cambia status / stage / nextFollowUpAt (sigue CONTACTED)', L.status === 'CONTACTED' && L.stage === before.stage && L.nextFollowUpAt === before.nextFollowUpAt, JSON.stringify({ before, L }));
    check('24', 'evento viejo no programa reintento (sin nextActionAt)', w.calls.filter(x => x.method === 'POST' && x.body && x.body.providerCallId === 'stringee:' + j7).every(p => !('nextActionAt' in p.body)));
    collect(w);
  });

  // ---------------- V12 / V13: conversación en curso no se finaliza; terminada sí ----------------
  await scenario('V12', async (w, r) => {
    const lead = U(12), cid = 'conv_v12asterisk00001';
    w.lead(lead, { attempts: 2, status: 'NO_ANSWER' });
    const start = Math.floor(Date.now() / 1000) - 25 * 60;
    const c = conv({ id: cid, lead_id: lead, n: 3, phone: '+919812300012', status: 'in-progress', start, dur: 0 });
    w.convs.set(cid, c);
    await r.polling(); await drain(r, 3);
    const e1 = await ev('elevenlabs:' + cid);
    check('12', 'conversación in-progress → NO se finaliza (RECEIVED, sin tocar el CRM)', e1.state === 'RECEIVED' && e1.resolution === 'WAITING_CONVERSATION' && w.calls.filter(x => /lead-studio/.test(x.url)).length === 0, JSON.stringify(e1));
    c.status = 'done'; c.metadata.call_duration_secs = 0; c.transcript = [];
    await r.polling(); await drain(r);
    const e2 = await ev('elevenlabs:' + cid);
    check('13', 'conversación terminada → elegible y procesada (hueco Asterisk sin Activity de WF2 → 1 Activity)', ['DONE', 'STALE', 'SKIPPED'].includes(e2.state) && e2.processed_at && w.callFus(lead).length === 1, JSON.stringify(e2));
    collect(w);
  });

  // ---------------- V14: fallo del POST al CRM no marca procesado ----------------
  await scenario('V14', async (w, r) => {
    const lead = U(14), jid = J(14);
    w.lead(lead, { attempts: 1 });
    const j = job({ job_id: jid, lead_id: lead, call_attempts: 2, final_status: 'NO_ANSWER' });
    w.jobs.set(jid, j); await r.callback(j);
    w.failPost = 5;
    await wake(); await r.processorCycle();
    const e1 = await ev('stringee:' + jid);
    check('14', 'POST al CRM falla → evento FAILED (no DONE), sin processed_at, error guardado', e1.state === 'FAILED' && !e1.processed_at && /POST followup/.test(e1.last_error) && w.callFus(lead).length === 0, JSON.stringify(e1));
    w.failPost = 0; await drain(r);
    const e2 = await ev('stringee:' + jid);
    check('14', 'reintento posterior → exactamente 1 Activity y DONE', e2.state === 'DONE' && w.callFus(lead).length === 1 && w.leads.get(lead).attempts === 2);
    // PATCH falla después del POST: la Activity no se duplica y el ciclo de vida se reintenta
    const lead2 = U(142), jid2 = J(142);
    w.lead(lead2, { attempts: 3 });
    const j2 = job({ job_id: jid2, lead_id: lead2, call_attempts: 4, final_status: 'NO_ANSWER' });
    w.jobs.set(jid2, j2); await r.callback(j2);
    w.failPatch = 4; await wake(); await r.processorCycle();
    const e3 = await ev('stringee:' + jid2);
    w.failPatch = 0; await drain(r);
    const e4 = await ev('stringee:' + jid2);
    check('14', 'PATCH falla tras el POST → FAILED con followup_id; al reintentar: 1 sola Activity y status aplicado', e3.state === 'FAILED' && e3.followup_id && e4.state === 'DONE' && w.callFus(lead2).length === 1 && w.leads.get(lead2).status === 'NO_ANSWER', JSON.stringify({ e3, e4 }));
    collect(w);
  });

  // ---------------- V15: misma conversación ElevenLabs re-ejecutada → sin duplicado ----------------
  await scenario('V15', async (w, r) => {
    const lead = U(15), cid = 'conv_v15asteriskgap01';
    w.lead(lead, { attempts: 4, status: 'NOT_CONTACTED' });
    const c = conv({ id: cid, lead_id: lead, n: 5, phone: '+919812300015', start: Math.floor(Date.now() / 1000) - 30 * 60, dur: 0, transcript: [] });
    w.convs.set(cid, c);
    await r.webhook(c); await r.polling(); await drain(r);
    await r.webhook(c); await r.polling(); await drain(r);
    await sql(`UPDATE wf_call_events SET state='RECEIVED', followup_id=NULL WHERE event_key='elevenlabs:${cid}'`); await drain(r);
    check('15', 'misma conversación ElevenLabs (webhook ×2 + polling ×2 + reproceso forzado) → 1 sola Activity', w.callFus(lead).length === 1 && w.leads.get(lead).attempts === 5, JSON.stringify(w.callFus(lead)));
    collect(w);
  });

  // ---------------- Asterisk: WF2 ya registró la llamada → WF9 NO duplica ----------------
  await scenario('A1', async (w, r) => {
    const lead = U(31), cid = 'conv_a1asteriskwf2001';
    const start = Math.floor(Date.now() / 1000) - 25 * 60;
    w.lead(lead, { attempts: 3, status: 'NO_ANSWER' });
    const f = w.addFollowup(lead, { outcome: 'NO_ANSWER', callStatus: 'NO_ANSWER', notes: 'No answer (SIP 408) — end of cycle 1 (3/3) — next cycle in 2 business days.' });
    f.createdAt = iso(start * 1000 + 70e3);
    w.convs.set(cid, conv({ id: cid, lead_id: lead, n: 3, phone: '+919812300031', start, dur: 0, transcript: [] }));
    await r.polling(); await drain(r);
    const e = await ev('elevenlabs:' + cid);
    check('A1', 'Asterisk con Activity de WF2 → WF9 no crea un segundo followup (ASTERISK_OWNED_BY_WF2)', w.callFus(lead).length === 1 && e.resolution === 'ASTERISK_OWNED_BY_WF2' && w.leads.get(lead).attempts === 3, JSON.stringify(e));
    collect(w);
  });

  // ---------------- Asterisk buzón: WF2 marcó CONNECTED → corrección sin Activity + hold ----------------
  await scenario('A2', async (w, r) => {
    const lead = U(32), cid = 'conv_a2voicemail00001';
    const start = Math.floor(Date.now() / 1000) - 25 * 60;
    w.lead(lead, { attempts: 4, status: 'CONTACTED', nextFollowUpAt: iso(Date.now() - 20 * H) });
    const f = w.addFollowup(lead, { outcome: 'CONNECTED', callStatus: 'ANSWERED', notes: 'Call connected successfully: ' + cid });
    f.createdAt = iso(start * 1000 + 40e3);
    w.convs.set(cid, conv({ id: cid, lead_id: lead, n: 4, phone: '+919812300032', start, dur: 22, eval: 'failure', term: 'voicemail_detected',
      summary: 'The call reached a voicemail box.', transcript: [{ role: 'agent', message: 'Hello' }, { role: 'user', message: 'Please leave a message after the tone' }] }));
    await r.polling(); await drain(r);
    const L = w.leads.get(lead); const e = await ev('elevenlabs:' + cid);
    check('A2', 'buzón de voz Asterisk (WF2=CONNECTED) → status NO_ANSWER sin Activity nueva', L.status === 'NO_ANSWER' && w.callFus(lead).length === 1 && L.attempts === 4, JSON.stringify(L));
    check('A2', 'reintento retenido en next_retry_at (+2 h desde la llamada, intento 4)', e.next_retry_at && approx(new Date(e.next_retry_at + 'Z').getTime(), start * 1000 + 2 * H), JSON.stringify(e));
    collect(w);
  });

  // ---------------- Stringee contestada sin conversation_id → 1 Activity, 3x3, recording_synced=0 ----------------
  await scenario('S16', async (w, r) => {
    const lead = U(16), jid = J(16);
    w.lead(lead, { attempts: 0 });
    const j = job({ job_id: jid, lead_id: lead, call_attempts: 1, final_status: 'ANSWERED', answered: true, answered_at: iso(Date.now() - 90e3), elevenlabs_conversation_id: null, sip_code: 200 });
    w.jobs.set(jid, j); await r.callback(j); await drain(r);
    const L = w.leads.get(lead); const c = w.callFus(lead);
    check('S16', 'contestada sin conversation_id → 1 Activity, NO_ANSWER (no CONTACTED), +2 h, recording_synced=0', c.length === 1 && L.status === 'NO_ANSWER' &&
      (await sql(`SELECT recording_synced r FROM wf_call_followups WHERE lead_id='${lead}'`))[0].r === 0, JSON.stringify({ L, c }));
    collect(w);
  });

  // ---------------- callback pedido por el cliente ----------------
  await scenario('S18', async (w, r) => {
    const lead = U(181), jid = J(181), cid = 'conv_s18callback00001';
    const want = new Date(Date.now() + 26 * H); want.setUTCMinutes(0, 0, 0);
    w.lead(lead, { attempts: 1 });
    w.convs.set(cid, conv({ id: cid, lead_id: lead, n: 2, provider: 'STRINGEE', dur: 80, dc: { callback_requested: { value: 'true' }, callback_date: { value: want.toISOString() } },
      transcript: [{ role: 'user', message: 'I am busy now, call me tomorrow afternoon' }, { role: 'user', message: 'yes' }] }));
    const j = job({ job_id: jid, lead_id: lead, call_attempts: 2, final_status: 'ANSWERED', answered: true, answered_at: iso(Date.now() - 100e3), elevenlabs_conversation_id: cid, sip_code: 200 });
    w.jobs.set(jid, j); await r.callback(j); await drain(r);
    const L = w.leads.get(lead); const c = w.callFus(lead)[0];
    check('S18', 'callback pedido → CONNECTED / ANSWERED / CONTACTED, fecha pedida en nextFollowUpAt, sin outcome CALLBACK', c.outcome === 'CONNECTED' && c.callStatus === 'ANSWERED' && L.status === 'CONTACTED' && L.nextFollowUpAt === want.toISOString() && /requested a callback/.test(c.notes), JSON.stringify({ L, c }));
    collect(w);
  });

  // ---------------- worker 404 y job en curso ----------------
  await scenario('S19', async (w, r) => {
    const lead = U(191), jid = J(191), jid2 = J(192);
    w.lead(lead, { attempts: 2 });
    await r.ledger([{ json: { job_id: jid, lead_id: lead, attempts: 3, last_call_time: iso(Date.now() - 60e3) } }], [{ json: { lead_id: lead, phone: '+919812300191' } }]);
    await wake(); await r.processorCycle();
    const e = await ev('stringee:' + jid);
    check('S19', 'worker 404 → FAILED con reintento, sin Activity, sin consumir intento', e.state === 'FAILED' && w.callFus(lead).length === 0 && w.leads.get(lead).attempts === 2, JSON.stringify(e));
    const lead2 = U(192); w.lead(lead2, { attempts: 0 });
    w.jobs.set(jid2, job({ job_id: jid2, lead_id: lead2, status: 'running', final_status: null, finished_at: null }));
    await r.ledger([{ json: { job_id: jid2, lead_id: lead2, attempts: 1, last_call_time: iso(Date.now() - 60e3) } }], [{ json: { lead_id: lead2, phone: '+919812300192' } }]);
    await wake(); await r.processorCycle();
    const e2 = await ev('stringee:' + jid2);
    check('S20', 'job no terminal → queda pendiente (RECEIVED, recheck 3 min), sin tocar el CRM', e2.state === 'RECEIVED' && e2.resolution === 'WAITING_JOB_FINAL' && w.callFus(lead2).length === 0, JSON.stringify(e2));
  });

  // ---------------- conversación Stringee sin job en el ledger (previa al despliegue) ----------------
  await scenario('S24', async (w, r) => {
    const lead = U(241), cid = 'conv_s24fallback00001', lead2 = U(242), cid2 = 'conv_s24legacy0000001';
    const start = Math.floor(Date.now() / 1000) - 50 * 60;
    w.lead(lead, { attempts: 2, status: 'ATTEMPTING', updatedAt: iso(start * 1000 - 5e3) });
    w.convs.set(cid, conv({ id: cid, lead_id: lead, n: 3, provider: 'STRINGEE', start, dur: 30, transcript: [{ role: 'user', message: 'Who is calling?' }, { role: 'user', message: 'Not interested' }] }));
    w.lead(lead2, { attempts: 5, status: 'NO_ANSWER' });
    const old = w.addFollowup(lead2, { outcome: 'NO_ANSWER', callStatus: 'NO_ANSWER', notes: 'legacy WF9 activity' }); old.createdAt = iso(start * 1000 + 90e3);
    w.convs.set(cid2, conv({ id: cid2, lead_id: lead2, n: 5, provider: 'STRINGEE', start, dur: 0 }));
    await r.polling(); await drain(r);
    check('S24', 'conversación Stringee sin job (pre-despliegue) → 1 Activity desde la conversación', w.callFus(lead).length === 1 && w.leads.get(lead).status === 'CONTACTED');
    check('S24', 'si ya existe una Activity en la ventana (WF9 viejo) → ALREADY_IN_CRM, sin duplicar', w.callFus(lead2).length === 1 && (await ev('elevenlabs:' + cid2)).resolution === 'ALREADY_IN_CRM');
    collect(w);
  });

  // ---------------- claim concurrente ----------------
  await scenario('CLAIM', async (w, r) => {
    const lead = U(301);
    w.lead(lead, { attempts: 0 });
    for (let i = 0; i < 3; i++) {
      const jid = J(3010 + i);
      w.jobs.set(jid, job({ job_id: jid, lead_id: lead, call_attempts: 1 + i, status: 'running', final_status: null, finished_at: null }));
      await r.ledger([{ json: { job_id: jid, lead_id: lead, attempts: 1 + i, last_call_time: iso(Date.now() - (300 - i) * 1000) } }], [{ json: { lead_id: lead, phone: '+919812300301' } }]);
    }
    const lead2 = U(302), jidX = J(3020);
    w.lead(lead2, { attempts: 0 });
    await r.ledger([{ json: { job_id: jidX, lead_id: lead2, attempts: 1, last_call_time: iso(Date.now() - 300e3) } }], [{ json: { lead_id: lead2, phone: '+919812300302' } }]);
    await wake();
    const { runCode } = require('./harness');
    const code = r.by['🔒 Build Claim SQL'].parameters.jsCode;
    const a = (await runCode(code, { items: [{}], execId: 'A' })).out[0].json;
    const b = (await runCode(code, { items: [{}], execId: 'B' })).out[0].json;
    await sql(a.claim_sql);
    await sql("UPDATE wf_call_events SET state='RECEIVED', claim_token=NULL, lease_until=NULL WHERE lead_id='" + lead2 + "'"); // se libera sólo el otro lead
    await sql(b.claim_sql);
    const ra = await sql(a.read_sql), rb = await sql(b.read_sql);
    const leadsA = new Set(ra.map(x => x.lead_id)), leadsB = new Set(rb.map(x => x.lead_id));
    check('CLAIM', 'dos procesadores simultáneos: ningún evento reclamado dos veces y el mismo lead no se reparte', ra.length === 3 && rb.length === 1 && !leadsB.has(lead) && ![...ra].some(x => rb.find(y => y.event_key === x.event_key)), JSON.stringify({ ra: ra.map(x => x.event_key), rb: rb.map(x => x.event_key) }));
  });

  // ---------------- REPLAY real de 995edb6e (el lead fue re-marcado después de ese job) ----------------
  for (const variant of ['ATTEMPTING', 'NO_ANSWER']) {
    await scenario('REPLAY-' + variant, async (w, r) => {
      const lead = '4f9ff42c-b49b-4d9b-b389-9163e845bb4a', jid = '995edb6e-0940-47c9-a2b2-3f4f58636dd5';
      const callStart = Date.now() - 3 * H;               // el job ocurrió hace 3 h
      w.lead(lead, { attempts: 6, status: variant, stage: 'FOLLOW_UP', nextFollowUpAt: '2026-09-24T11:45:25.160Z', updatedAt: iso(Date.now() - 20 * 60e3) });
      const j = job({ job_id: jid, lead_id: lead, call_attempts: 7, final_status: 'UNAVAILABLE', telephony_status: 'UNAVAILABLE', sip_code: 480, sip_reason: 'Temporarily Unavailable',
        created_at: iso(callStart - 2e3), started_at: iso(callStart), ringing_at: iso(callStart + 3e3), finished_at: iso(callStart + 25e3) });
      w.jobs.set(jid, j);
      await r.callback(j); await drain(r); await r.callback(j); await drain(r);
      const L = w.leads.get(lead); const c = w.callFus(lead); const e = await ev('stringee:' + jid);
      check('REPLAY', `replay 995edb6e con el lead en ${variant} y re-marcado después → 1 Activity histórica, attempts 7, status/stage/nextFollowUpAt sin cambios`,
        c.length === 1 && L.attempts === 7 && L.status === variant && L.stage === 'FOLLOW_UP' && L.nextFollowUpAt === '2026-09-24T11:45:25.160Z' && e.state === 'STALE' &&
        /Historical call recovered/.test(c[0].notes) && /Call attempt 7 of 9/.test(c[0].notes), JSON.stringify({ L, e, n: c[0] && c[0].notes }));
      collect(w);
    });
  }

  // ---------------- idempotencia si LeadStudio ignora providerCallId (sólo queda "Ref:" en notes) ----------------
  await scenario('REF', async (w, r) => {
    w.dropProviderCallId = true;
    const lead = U(401), jid = J(401), cid = 'conv_ref40100000001';
    w.lead(lead, { attempts: 1 });
    const j = job({ job_id: jid, lead_id: lead, call_attempts: 2, final_status: 'NO_ANSWER' });
    w.jobs.set(jid, j); await r.callback(j); await drain(r);
    await sql(`UPDATE wf_call_events SET state='RECEIVED', followup_id=NULL, lifecycle_applied=0 WHERE event_key='stringee:${jid}'`); await drain(r);
    const lead2 = U(402); w.lead(lead2, { attempts: 4, status: 'NOT_CONTACTED' });
    w.convs.set(cid, conv({ id: cid, lead_id: lead2, n: 5, phone: '+919812300402', start: Math.floor(Date.now() / 1000) - 30 * 60, dur: 0 }));
    await r.polling(); await drain(r);
    await sql(`UPDATE wf_call_events SET state='RECEIVED', followup_id=NULL, lifecycle_applied=0 WHERE event_key='elevenlabs:${cid}'`); await drain(r);
    check('REF', 'sin providerCallId en el CRM: reproceso forzado (Stringee y hueco Asterisk) no duplica gracias a "Ref:" en notes',
      w.callFus(lead).length === 1 && w.callFus(lead2).length === 1 && w.leads.get(lead).attempts === 2 && w.leads.get(lead2).attempts === 5,
      JSON.stringify({ a: w.callFus(lead), b: w.callFus(lead2) }));
    collect(w);
  });

  // ---------------- V25: todo lo escrito en el CRM está en inglés ----------------
  const bad = allNotes.filter(spanishOrNonAscii);
  check('25', `todo texto persistido en el CRM está en inglés (${allNotes.length} notas revisadas)`, allNotes.length > 10 && bad.length === 0, bad.slice(0, 3).join(' || '));

  await close();
  const fs = require('fs');
  fs.writeFileSync(__dirname + '/sim_results.json', JSON.stringify(results, null, 2));
  fs.writeFileSync(__dirname + '/sim_notes_sample.txt', allNotes.slice(0, 40).join('\n'));
  for (const x of results) console.log((x.pass ? 'PASS' : 'FAIL') + ' [' + x.id + '] ' + x.name + (x.pass ? '' : '\n      ↳ ' + String(x.detail).slice(0, 700)));
  console.log(`\nTotal: ${results.filter(x => x.pass).length}/${results.length} PASS`);
  process.exit(results.every(x => x.pass) ? 0 : 1);
})();
