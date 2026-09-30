// Pruebas de los cambios de WF2 con los nodos REALES del WF2 parcheado.
const fs = require('fs');
const { World, iso } = require('./mocks');
const { runCode, mysqlNode, sql, close } = require('./harness');
const { renderQuery } = require('./pipeline');
const wf = JSON.parse(fs.readFileSync(process.env.WF2_FILE || (__dirname + '/../out/WF2_UNIFICADO_v2_PATCHED.json'), 'utf8'));
const by = Object.fromEntries(wf.nodes.map(n => [n.name, n]));
const results = [];
const check = (id, name, cond, detail) => results.push({ id, name, pass: !!cond, detail: cond ? '' : (detail || '') });
const U = n => `${String(n).padStart(8, '0')}-0000-4000-8000-${String(n).padStart(12, '0')}`;
const H = 3600e3;
const LOGIN = { '🔐 Login LeadStudio1': [{ accessToken: 'tok' }] };
async function run(name, w, opts) { const r = await runCode(by[name].parameters.jsCode, Object.assign({ http: w.httpFn() }, opts)); return r; }

(async () => {
  await sql('DELETE FROM wf_call_events'); await sql('DELETE FROM wf_call_followups');

  // ---- Límite duro en Fetch + Lock (ambos proveedores) ----
  for (const prov of ['Stringee', 'Asterisk']) {
    const w = new World();
    const ex = U(prov === 'Stringee' ? 901 : 902), ok = U(prov === 'Stringee' ? 903 : 904);
    w.lead(ex, { status: 'NOT_CONTACTED', attempts: 9, phone: '+919811100001' });
    w.lead(ok, { status: 'NOT_CONTACTED', attempts: 2, phone: '+919811100002' });
    const r = await run(`📥 Fetch + Lock (${prov})`, w, { items: [{}], nodes: LOGIN });
    const dispatched = r.out.map(i => i.json.lead_id);
    const L = w.leads.get(ex);
    check('HL-' + prov, `Fetch + Lock (${prov}): attempts ≥ 9 no se despacha → CLOSED + UNRESPONSIVE`, !dispatched.includes(ex) && L.status === 'CLOSED' && L.stage === 'UNRESPONSIVE', JSON.stringify({ dispatched, L }));
    check('HL-' + prov, `Fetch + Lock (${prov}): lead con attempts 2 sí se despacha (sin cambios de comportamiento)`, dispatched.includes(ok) && w.leads.get(ok).status === 'ATTEMPTING');
  }

  // ---- Classify Dial Result (Asterisk): intento 9 ----
  {
    const w = new World();
    const src = [{ json: { lead_id: U(911), country: 'india', call_attempts: 8 } }, { json: { lead_id: U(912), country: 'india', call_attempts: 8 } }];
    const items = [
      { json: { statusCode: 200, body: { success: false, message: 'Call failed: SIP 408 Request Timeout' } }, pairedItem: 0 },
      { json: { statusCode: 200, body: { success: true, conversation_id: 'conv_abc1234567' } }, pairedItem: 1 }
    ];
    const r = await run('🔍 Classify Dial Result (Asterisk)', w, { items, nodes: { '📥 Fetch + Lock (Asterisk)': src } });
    const a = r.out[0].json, b = r.out[1].json;
    check('C9', 'Asterisk intento 9 sin respuesta → CLOSED + UNRESPONSIVE, sin próximo intento', a.status === 'CLOSED' && a.stage === 'UNRESPONSIVE' && a.nextFollowUpAt === null && a.attempts === 9, JSON.stringify(a));
    check('C9', 'Asterisk intento 9 contestado → CONTACTED, sin stage de cierre', b.status === 'CONTACTED' && !b.stage, JSON.stringify(b));
  }

  // ---- POST → Stringee Worker: body con callback_url ----
  {
    const tpl = by['📞 POST → Stringee Worker'].parameters.jsonBody.replace(/^=/, '');
    const $json = { lead_id: U(920), phone: '+919812345678', full_name: 'Ravi "R" Kumar', country: 'india', language: 'hi', call_attempts: 6, _from_number: '917971730907' };
    const body = tpl.replace(/\{\{\s*([\s\S]+?)\s*\}\}/g, (_, e) => String(Function('$json', 'return (' + e + ');')($json)));
    let parsed = null; try { parsed = JSON.parse(body); } catch (e) { }
    check('CB', 'POST → Stringee Worker: JSON válido con callback_url de producción y el resto del body intacto', parsed &&
      parsed.callback_url === 'https://landmarket-n8n.dhsoig.easypanel.host/webhook/stringee-worker-callback' && parsed.call_attempts === 7 && parsed.phone === '919812345678' && parsed.agent_id === 'agent_5701kramx550e3qs2tm11661b48p', body);
  }

  // ---- Ledger (Stringee) y fila wf_call_followups (Asterisk) ----
  {
    const w = new World();
    const jid = 'cccccccc-1111-4111-8111-000000000001';
    const r = await run('🧾 Build Ledger SQL (Stringee)', w, { items: [
      { json: { provider: 'stringee', lead_id: U(930), job_id: jid, attempts: 4, last_call_time: iso(Date.now() - 5000), status: 'ATTEMPTING', patch_ok: true } },
      { json: { provider: 'stringee', lead_id: U(931), job_id: null, attempts: 2, status: 'UNREACHABLE' } }
    ], nodes: { '📥 Fetch + Lock (Stringee)': [{ json: { lead_id: U(930), phone: '+919811122233' } }] } });
    await mysqlNode(r.out.map(i => renderQuery(by['💾 Ledger Dispatch (Stringee)'].parameters.query, i, {})));
    await mysqlNode(r.out.map(i => renderQuery(by['💾 Ledger Dispatch (Stringee)'].parameters.query, i, {}))); // idempotente
    const rows = await sql("SELECT * FROM wf_call_events WHERE source='stringee'");
    check('LEDGER', 'ledger: 1 fila por job con job_id válido (idempotente), teléfono en dígitos, próximo chequeo +3 min', rows.length === 1 && rows[0].event_key === 'stringee:' + jid &&
      rows[0].phone === '919811122233' && rows[0].call_attempts === 4 && rows[0].dispatched_at, JSON.stringify(rows));
    const r2 = await run('🧾 Build Followup Row (Asterisk)', w, { items: [
      { json: { lead_id: U(940), followup_id: 'fu_a1', outcome: 'CONNECTED', callStatus: 'ANSWERED' } },
      { json: { lead_id: U(941), followup_id: 'fu_a2', outcome: 'NO_ANSWER', callStatus: 'NO_ANSWER' } },
      { json: { lead_id: U(942), followup_id: null, status: 'NOT_CONTACTED' } }
    ], nodes: { '📥 Fetch + Lock (Asterisk)': [{ json: { lead_id: U(940), phone: '+919800000940' } }, { json: { lead_id: U(941), phone: '+977980000941' } }] } });
    await mysqlNode(r2.out.map(i => renderQuery(by['💾 Save Followup ID (Asterisk)'].parameters.query, i, {})));
    await mysqlNode(r2.out.map(i => renderQuery(by['💾 Save Followup ID (Asterisk)'].parameters.query, i, {})));
    const f = await sql("SELECT followup_id, provider, recording_synced, phone FROM wf_call_followups WHERE provider='asterisk' ORDER BY followup_id");
    check('FCF-A', 'Asterisk: fila en wf_call_followups con followup_id real; CONNECTED → recording_synced=0, resto → 1; sin duplicados', f.length === 2 &&
      f[0].followup_id === 'fu_a1' && f[0].recording_synced === 0 && f[1].recording_synced === 1 && f[0].phone === '+919800000940', JSON.stringify(f));
  }

  // ---- Mantenimiento corregido ----
  const maint = async (w, dbItems) => run('🧹 Mantenimiento Leads (reactivar + liberar)', w, { items: dbItems });
  const dbQuery = async () => (await mysqlNode([by['🗄️ Leer Estado Eventos (Mantenimiento)'].parameters.query])).map(i => i.json);
  {
    await sql('DELETE FROM wf_call_events');
    const w = new World(); const old = iso(Date.now() - 45 * 60e3);
    const Ljob = U(950), L9 = U(951), Lfree = U(952), NA9 = U(953), NAdue = U(954), NAhold = U(955), NAstale = U(956), NAholdPast = U(957), NAnull = U(958);
    w.lead(Ljob, { status: 'ATTEMPTING', attempts: 3, updatedAt: old, phone: '+919800000950' });
    w.lead(L9, { status: 'ATTEMPTING', attempts: 9, updatedAt: old, phone: '+919800000951' });
    w.lead(Lfree, { status: 'ATTEMPTING', attempts: 3, updatedAt: old, phone: '+919800000952' });
    w.lead(NA9, { status: 'NO_ANSWER', attempts: 11, nextFollowUpAt: iso(Date.now() - H), updatedAt: iso(Date.now() - 3 * H), phone: '+919800000953' });
    w.lead(NAdue, { status: 'NO_ANSWER', attempts: 1, nextFollowUpAt: iso(Date.now() - 60e3), updatedAt: iso(Date.now() - 2 * H - 60e3), phone: '+919800000954' });
    w.lead(NAhold, { status: 'NO_ANSWER', attempts: 4, nextFollowUpAt: iso(Date.now() - 5 * H), updatedAt: iso(Date.now() - 10 * 60e3), phone: '+919800000955' });
    w.lead(NAstale, { status: 'NO_ANSWER', attempts: 6, nextFollowUpAt: iso(Date.now() - 4 * 86400e3), updatedAt: iso(Date.now() - 30 * 60e3), phone: '+919800000956' });
    w.lead(NAholdPast, { status: 'NO_ANSWER', attempts: 4, nextFollowUpAt: null, updatedAt: iso(Date.now() - 3 * H), phone: '+919800000957' });
    w.lead(NAnull, { status: 'NO_ANSWER', attempts: 1, nextFollowUpAt: null, updatedAt: iso(Date.now() - 3 * H), phone: '+919800000958' });
    // job Stringee sin resolver para Ljob; holds de WF9 para NAhold (futuro) y NAholdPast (vencido)
    await sql(`INSERT INTO wf_call_events (event_key, source, provider, external_id, lead_id, state, dispatched_at, next_attempt_at) VALUES ('stringee:j-950','stringee','stringee','j-950','${Ljob}','FAILED', UTC_TIMESTAMP(3) - INTERVAL 40 MINUTE, UTC_TIMESTAMP(3))`);
    await sql(`INSERT INTO wf_call_events (event_key, source, provider, external_id, lead_id, state, event_at, lifecycle_applied, next_retry_at, processed_at, next_attempt_at) VALUES ('elevenlabs:conv_h955','elevenlabs','asterisk','conv_h955','${NAhold}','DONE', UTC_TIMESTAMP(3) - INTERVAL 30 MINUTE, 1, UTC_TIMESTAMP(3) + INTERVAL 90 MINUTE, UTC_TIMESTAMP(3) - INTERVAL 9 MINUTE, UTC_TIMESTAMP(3))`);
    await sql(`INSERT INTO wf_call_events (event_key, source, provider, external_id, lead_id, state, event_at, lifecycle_applied, next_retry_at, processed_at, next_attempt_at) VALUES ('elevenlabs:conv_h957','elevenlabs','asterisk','conv_h957','${NAholdPast}','DONE', UTC_TIMESTAMP(3) - INTERVAL 4 HOUR, 1, UTC_TIMESTAMP(3) - INTERVAL 5 MINUTE, UTC_TIMESTAMP(3) - INTERVAL 3 HOUR - INTERVAL 30 SECOND, UTC_TIMESTAMP(3))`);
    const dbRows = await dbQuery();
    const r = await maint(w, dbRows);
    const st = id => w.leads.get(id).status, sg = id => w.leads.get(id).stage;
    check('M1', 'ATTEMPTING colgado con job Stringee sin resolver → NO se libera', st(Ljob) === 'ATTEMPTING', JSON.stringify(w.leads.get(Ljob)));
    check('M2', 'ATTEMPTING colgado con attempts 9 → CLOSED + UNRESPONSIVE', st(L9) === 'CLOSED' && sg(L9) === 'UNRESPONSIVE');
    check('M3', 'ATTEMPTING colgado sin job pendiente → NO_ANSWER (no suma intento)', st(Lfree) === 'NO_ANSWER' && w.leads.get(Lfree).attempts === 3);
    check('M4', 'NO_ANSWER con attempts ≥ 9 → CLOSED + UNRESPONSIVE (nunca NOT_CONTACTED)', st(NA9) === 'CLOSED' && sg(NA9) === 'UNRESPONSIVE');
    check('M5', 'NO_ANSWER vencido (nextFollowUpAt válido) → NOT_CONTACTED', st(NAdue) === 'NOT_CONTACTED');
    check('M6', 'NO_ANSWER con reintento retenido por WF9 a futuro → NO se reactiva aunque nextFollowUpAt viejo esté vencido', st(NAhold) === 'NO_ANSWER');
    check('M7', 'NO_ANSWER con reintento retenido ya vencido → NOT_CONTACTED', st(NAholdPast) === 'NOT_CONTACTED', JSON.stringify({ l: w.leads.get(NAholdPast), dbRows }));
    check('M8', 'NO_ANSWER con nextFollowUpAt viejo (anterior al último cambio) → se recalcula por ciclo 3x3 y NO se reactiva en falso', st(NAstale) === 'NO_ANSWER');
    check('M9', 'NO_ANSWER sin nextFollowUpAt (lead "muerto") → vencimiento recalculado (+2 h) → se reactiva', st(NAnull) === 'NOT_CONTACTED');
    const noNextPatch = w.calls.filter(c => c.method === 'PATCH' && c.body && c.body.nextFollowUpAt && c.body.nextFollowUpAt !== null).length === 0;
    check('M10', 'Mantenimiento ya no intenta escribir nextFollowUpAt (LeadStudio lo ignora)', noNextPatch);

    // fail-safe: la consulta MySQL falló
    const w2 = new World();
    w2.lead(Lfree, { status: 'ATTEMPTING', attempts: 3, updatedAt: old, phone: '+919800000952' });
    w2.lead(NAdue, { status: 'NO_ANSWER', attempts: 1, nextFollowUpAt: iso(Date.now() - 60e3), updatedAt: iso(Date.now() - 2 * H - 60e3), phone: '+919800000954' });
    w2.lead(NA9, { status: 'NO_ANSWER', attempts: 11, updatedAt: iso(Date.now() - 3 * H), phone: '+919800000953' });
    const r2 = await maint(w2, [{ json: { error: 'connect ECONNREFUSED' } }]);
    check('M11', 'si la consulta MySQL falla → no libera ni reactiva nada (fail-safe) y avisa con 🚨', w2.leads.get(Lfree).status === 'ATTEMPTING' && w2.leads.get(NAdue).status === 'NO_ANSWER' &&
      r2.logs.some(l => l[0] === 'error' && /Fail-safe/.test(l[1])), JSON.stringify(r2.logs));
    check('M12', 'aun con MySQL caído, attempts ≥ 9 se cierra (CLOSED + UNRESPONSIVE), nunca vuelve a la cola', w2.leads.get(NA9).status === 'CLOSED' && w2.leads.get(NA9).stage === 'UNRESPONSIVE');
  }

  await close();
  fs.writeFileSync(__dirname + '/sim_wf2_results.json', JSON.stringify(results, null, 2));
  for (const x of results) console.log((x.pass ? 'PASS' : 'FAIL') + ' [' + x.id + '] ' + x.name + (x.pass ? '' : '\n      ↳ ' + String(x.detail).slice(0, 900)));
  console.log(`\nTotal: ${results.filter(x => x.pass).length}/${results.length} PASS`);
  process.exit(results.every(x => x.pass) ? 0 : 1);
})().catch(e => { console.error(e); process.exit(1); });
