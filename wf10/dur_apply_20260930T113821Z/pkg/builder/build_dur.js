// ============================================================================
// build_dur.js — CANDIDATO "duración real en Asterisk contestada" (WF2 + WF9).
//
// NO despliega nada. Parte de los JSON del paquete ya publicado (WF2 parcheado y WF9 v3),
// aplica cambios por NOMBRE DE NODO con pre-imagen EXACTA (si el nodo de producción difiere,
// el script falla y dice cuál) y escribe los candidatos en out_dur/ + los diffs por nodo.
//
// Uso:  node build_dur.js [--wf2 archivo.json] [--wf9 archivo.json] [--out dir]
//       (con --wf2/--wf9 se puede verificar el diff contra un export REAL de producción)
// ============================================================================
const fs = require('fs');
const path = require('path');
const crypto = require('crypto');
const cp = require('child_process');
const ROOT = __dirname;
const arg = (k, d) => { const i = process.argv.indexOf(k); return i > 0 ? process.argv[i + 1] : d; };
const WF2_IN = arg('--wf2', ROOT + '/out/WF2_UNIFICADO_v2_PATCHED.json');
const WF9_IN = arg('--wf9', ROOT + '/out/WF9_PRODUCTION_FIXED_STRINGEE_RELIABILITY.json');
const OUT = arg('--out', ROOT + '/out_dur');
fs.mkdirSync(OUT, { recursive: true });
const snip = n => fs.readFileSync(ROOT + '/src_dur/snip/' + n, 'utf8').replace(/\n$/, '');
const src = f => fs.readFileSync(ROOT + '/src_dur/' + f, 'utf8');
const L = (...l) => l.join('\n');
const clone = o => JSON.parse(JSON.stringify(o));
const uid = name => { const h = crypto.createHash('sha1').update('wf-dur-v1:' + name).digest('hex'); return `${h.slice(0, 8)}-${h.slice(8, 12)}-5${h.slice(13, 16)}-a${h.slice(17, 20)}-${h.slice(20, 32)}`; };

function Patcher(wf, label) {
  const by = Object.fromEntries(wf.nodes.map(n => [n.name, n]));
  const before = {};        // nodo -> {campo: texto original}
  const log = [];
  const P = {
    wf, by, log, before, added: [],
    rep(node, field, from, to, count = 1) {
      const n = by[node];
      if (!n) throw new Error(`[${label}] no existe el nodo ${node}`);
      const s = n.parameters[field];
      if (typeof s !== 'string') throw new Error(`[${label}] ${node}.${field} no es texto`);
      const parts = s.split(from);
      if (parts.length - 1 !== count) throw new Error(`[${label}] ${node}.${field}: se esperaban ${count} ocurrencia(s) de la pre-imagen y hay ${parts.length - 1}:\n${from.slice(0, 200)}`);
      before[node] = before[node] || {};
      if (!(field in before[node])) before[node][field] = s;
      n.parameters[field] = parts.join(to);
      log.push({ node, field, kind: 'replace', count });
    },
    add(n) {
      n.id = uid(n.name);
      if (by[n.name]) throw new Error(`[${label}] el nodo nuevo ya existe: ${n.name}`);
      wf.nodes.push(n); by[n.name] = n; P.added.push(n.name); log.push({ node: n.name, kind: 'add' });
      return n;
    },
    rewire(from, expectTo, newChain) {
      // reemplaza from -> expectTo por from -> newChain[0] -> ... -> expectTo (pre-imagen exigida)
      const c = wf.connections[from];
      const cur = c && c.main && c.main[0] && c.main[0].map(x => x.node);
      if (!cur || cur.length !== 1 || cur[0] !== expectTo) throw new Error(`[${label}] ${from} debía conectar sólo a ${expectTo} y conecta a ${JSON.stringify(cur)}`);
      const link = (a, b) => { wf.connections[a] = { main: [[{ node: b, type: 'main', index: 0 }]] }; };
      link(from, newChain[0]);
      for (let i = 0; i < newChain.length - 1; i++) link(newChain[i], newChain[i + 1]);
      link(newChain[newChain.length - 1], expectTo);
      log.push({ node: from, kind: 'rewire', to: newChain.concat(expectTo) });
    }
  };
  return P;
}

const wf2 = JSON.parse(fs.readFileSync(WF2_IN, 'utf8'));
const wf9 = JSON.parse(fs.readFileSync(WF9_IN, 'utf8'));
const orig2 = clone(wf2), orig9 = clone(wf9);
const P2 = Patcher(wf2, 'WF2');
const P9 = Patcher(wf9, 'WF9');
const MYSQL_CRED2 = clone(P2.by['🗄️ Leer Estado Eventos (Mantenimiento)'].credentials);
const MYSQL_CRED9 = clone(P9.by['🔒 Claim + Leer Eventos (MySQL)'].credentials);

// ============================================================================
// WF2 — 1) registro diferido de la llamada Asterisk contestada + 2) Guardar Resultado
// ============================================================================
const G = '🏁 Guardar Resultado (Asterisk)';
P2.rep(G, 'jsCode', L('const out = [];', 'let fails = 0;', '', 'for (const item of $input.all()) {'), snip('w2_guardar_head.txt'));
P2.rep(G, 'jsCode', L('  // ------------------------------------------------------------', '  // FOLLOWUP', '  // ------------------------------------------------------------', '  let fu = null;'), snip('w2_guardar_block.txt'));
P2.rep(G, 'jsCode', L('console.log(', '  `🏁 Asterisk: ${out.length} guardados | ` +', '  `${fails} PATCH fallidos`', ');'),
  L('console.log(', '  `🏁 Asterisk: ${out.length} guardados | ` +', '  `${deferred} diferidos a WF9 | ` +', '  `${fails} PATCH fallidos`', ');'));

const cls = P2.by['🔍 Classify Dial Result (Asterisk)'];
const gp = P2.by[G].position;
P2.add({ parameters: { jsCode: src('wf2/ledger_deferred_asterisk.js') }, name: '🧾 Build Deferred Ledger (Asterisk)', type: 'n8n-nodes-base.code', typeVersion: 2,
  position: [gp[0] - 240, gp[1] + 240] });
P2.add({ parameters: { operation: 'executeQuery', query: '{{ $json.ledger_sql }}', options: {} }, name: '💾 Ledger Deferred (Asterisk)', type: 'n8n-nodes-base.mySql', typeVersion: 2.4,
  position: [gp[0], gp[1] + 240], credentials: clone(MYSQL_CRED2), alwaysOutputData: true, retryOnFail: true, maxTries: 2, waitBetweenTries: 1000, onError: 'continueRegularOutput' });
P2.rewire('🔍 Classify Dial Result (Asterisk)', G, ['🧾 Build Deferred Ledger (Asterisk)', '💾 Ledger Deferred (Asterisk)']);

// 3) Mantenimiento: la llamada diferida también retiene el ATTEMPTING mientras no esté resuelta
P2.rep('🗄️ Leer Estado Eventos (Mantenimiento)', 'query',
  "FROM wf_call_events WHERE source = 'stringee' AND state IN ('RECEIVED', 'PROCESSING', 'FAILED') AND lead_id IS NOT NULL GROUP BY lead_id",
  "FROM wf_call_events WHERE (source = 'stringee' OR (source = 'elevenlabs' AND provider = 'asterisk' AND dispatched_at IS NOT NULL)) AND state IN ('RECEIVED', 'PROCESSING', 'FAILED') AND lead_id IS NOT NULL GROUP BY lead_id");
// Produccion usa otro texto de log en Mantenimiento. Cambio cosmetico omitido; logica intacta.
// Nota visual WF2 omitida: solo documentacion.

// ============================================================================
// WF9 — CDR + Asterisk contestada diferida
// ============================================================================
const F1 = '⚙️ Fase 1 — Activity CRM';
P9.rep(F1, 'jsCode', L('  ASTERISK_MATCH_AFTER_SEC: 20 * 60,'), snip('f1_wait.txt'));
P9.rep(F1, 'jsCode', L(
  '//  - Asterisk: WF2 es dueño de la Activity. WF9 sólo (a) corrige el ciclo de',
  '//    vida cuando WF2 marcó CONNECTED pero no hubo conversación real (buzón /',
  '//    silencio), sin crear Activity, o (b) registra la llamada si WF2 no dejó',
  '//    ninguna Activity para ella (hueco real).'), L(
  '//  - Asterisk CONTESTADA (ledger de WF2, dispatched_at): WF2 NO crea la Activity (LeadStudio',
  '//    fija durationSeconds en el POST y no lo deja editar). WF9 crea la ÚNICA Activity al terminar',
  '//    la llamada, con CDR.billsec (respaldo: duración de ElevenLabs) y el ciclo de vida final.',
  '//    Asterisk SIN ledger (WF2 anterior): WF2 es dueño de la Activity. WF9 sólo (a) corrige el ciclo de',
  '//    vida cuando WF2 marcó CONNECTED pero no hubo conversación real (buzón /',
  '//    silencio), sin crear Activity, o (b) registra la llamada si WF2 no dejó',
  '//    ninguna Activity para ella (hueco real).'));
P9.rep(F1, 'jsCode', '// ---------- CRM ----------', snip('f1_helpers.txt'));
// Diferida retenida por nosotros (lead sigue ATTEMPTING, sin despacho más nuevo): una Activity posterior de un agente
// o un evento de otro lead-cycle no convierte esta llamada en histórica (si no, el lead se libera y se re-marca a los 30 min).
P9.rep(F1, 'jsCode', "  if (newer) reasons.push('newer_crm_call');",
  "  const heldDeferred = !!x.deferred && status === 'ATTEMPTING' && !(Number(ev.lead_newer_dispatch) > 0);\n  if (newer && !heldDeferred) reasons.push('newer_crm_call');");
P9.rep(F1, 'jsCode', "  if (latest && latest > x.callStartMs + 1000) reasons.push('newer_lifecycle_event');",
  "  if (latest && latest > x.callStartMs + 1000 && !heldDeferred) reasons.push('newer_lifecycle_event');");
// lead_id / phone vacíos en las variables dinámicas de ElevenLabs: la fila diferida de WF2 los trae
P9.rep(F1, 'jsCode', "  const leadId = String(dyn.lead_id || '').trim();\n  const phone = String(dyn.phone || '').replace(/[^0-9+]/g, '');",
  "  const leadId = String(dyn.lead_id || (deferred ? ev.lead_id : '') || '').trim();\n  const phone = String(dyn.phone || (deferred ? ev.phone : '') || '').replace(/[^0-9+]/g, '');");
P9.rep(F1, 'jsCode', "  if (upd > Math.max(x.callEndMs, oursMs) + 120e3) reasons.push(status === 'ATTEMPTING' ? 'relocked_by_newer_dispatch' : 'lead_updated_after_call');", snip('f1_stale.txt'));
P9.rep(F1, 'jsCode', "  if (x.resolutionPrefix) resolution = x.resolutionPrefix + '_' + resolution;",
  L("  if (x.resolutionPrefix) resolution = x.resolutionPrefix + '_' + resolution;", "  if (x.durationSource) resolution = resolution + ':' + x.durationSource;   // cdr | el (auditoría de la fuente de durationSeconds)"));
P9.rep(F1, 'jsCode', L('      call_status: x.cls.callStatus,', '      recording_synced: x.cls.recordingSynced', '    },'),
  L('      call_status: x.cls.callStatus,', '      recording_synced: x.cls.recordingSynced,',
    '      created_epoch: x.provider === \'asterisk\' ? Math.floor((x.fcfEpochMs || x.callStartMs) / 1000) : null   // hora de INICIO de la llamada', '    },'));
P9.rep(F1, 'jsCode', 'async function processElevenLabs(ev, ctx) {', snip('f1_defer_fn.txt'));
P9.rep(F1, 'jsCode', L(
  '  // Asterisk ya enriquecido y todavía dentro de la espera de WF2: no hace falta llamar a ninguna API',
  "  if (ev.provider === 'asterisk' && ev.lead_id && Number(ev.event_at_ms) > 0) {"), L(
  '  // Asterisk CONTESTADA registrada por WF2 en el ledger (dispatched_at): WF2 NO creó la Activity, la crea WF9 al terminar',
  "  const deferred = ev.provider === 'asterisk' && Number(ev.dispatched_at_ms) > 0;",
  '  // Asterisk (sin ledger) ya enriquecido y todavía dentro de la espera de WF2: no hace falta llamar a ninguna API',
  "  if (!deferred && ev.provider === 'asterisk' && ev.lead_id && Number(ev.event_at_ms) > 0) {"));
P9.rep(F1, 'jsCode', "    if (ageSec(Number(ev.first_seen_at_ms) || NOW) > WAIT.CONV_404_GIVEUP_SEC) return skip(ev, 'CONVERSATION_NOT_FOUND');",
  "    if (ageSec(Number(ev.first_seen_at_ms) || NOW) > WAIT.CONV_404_GIVEUP_SEC) return deferred ? deferredWithoutConversation(ev, ctx, 'conversation_404') : skip(ev, 'CONVERSATION_NOT_FOUND');");
P9.rep(F1, 'jsCode', "    if (ageSec(startMs) > 3 * 3600) console.error('🚨 Conversación sin estado terminal >3 h:', convId, status);\n    return requeue(ev, WAIT.CONV_RECHECK_SEC, Object.assign({}, base, { resolution: 'WAITING_CONVERSATION' }));",
  "    if (ageSec(startMs) > 3 * 3600) console.error('🚨 Conversación sin estado terminal >3 h:', convId, status);\n" +
  "    if (deferred && ageSec(startMs) > WAIT.ASTERISK_DEFER_GIVEUP_SEC) return deferredWithoutConversation(ev, ctx, 'conversation_not_terminal');\n" +
  "    return requeue(ev, WAIT.CONV_RECHECK_SEC, Object.assign({}, base, { resolution: 'WAITING_CONVERSATION' }));");
P9.rep(F1, 'jsCode', '  const readyAt = startMs + Math.max(WAIT.ASTERISK_READY_SEC, dur + 180) * 1000;',
  L('  // diferida: la conversación ya es terminal; sólo se espera a que ElevenLabs/CDR se asienten. Sin ledger: espera a WF2 (legado).',
    '  const readyAt = deferred ? endMs + WAIT.ASTERISK_DEFER_SETTLE_SEC * 1000 : startMs + Math.max(WAIT.ASTERISK_READY_SEC, dur + 180) * 1000;'));
P9.rep(F1, 'jsCode', '    const inWin = fus.filter(f => isCall(f) && !taken.has(String(f.id)) && (ms(f.createdAt) || 0) >= from && (ms(f.createdAt) || 0) <= to);',
  L('    // diferida: la Activity de esta llamada NO puede existir todavía salvo por conversation_id / providerCallId / Ref (nunca por ventana de tiempo)',
    '    const inWin = deferred ? [] : fus.filter(f => isCall(f) && !taken.has(String(f.id)) && (ms(f.createdAt) || 0) >= from && (ms(f.createdAt) || 0) <= to);'));
P9.rep(F1, 'jsCode', L(
  "  const evGap = Object.assign({}, ev, { followup_id: gapOurs ? String(gapOurs.id) : null });",
  "  return applyCall(evGap, {",
  "    provider: 'asterisk', leadId, phone, N: dyn.call_attempts, refs: ['elevenlabs:' + convId],",
  "    cls, callStartMs: startMs, callEndMs: endMs, convId, resolutionPrefix: 'ASTERISK_GAP'",
  "  }, ctx);"), snip('f1_apply.txt'));
P9.rep(F1, 'jsCode', L(
  "  return 'INSERT INTO wf_call_followups (phone, lead_id, followup_id, provider, outcome, call_status, recording_synced) ' +",
  "    'SELECT ' + [q(f.phone || ''), q(f.lead_id), q(f.followup_id), q(f.provider), q(f.outcome), q(f.call_status), q(Number(f.recording_synced) ? 1 : 0)].join(', ') +",
  "    ' FROM DUAL WHERE NOT EXISTS (SELECT 1 FROM wf_call_followups WHERE followup_id = ' + q(f.followup_id) + ')';"),
  L(snip('f1_fcfsql.txt').split('\n').slice(1).join('\n')));   // (la línea "const f = r.fcf;" ya existe antes)
// la 1ª línea del snippet ("  const f = r.fcf;") queda fuera a propósito; se conserva el comentario y el return
P9.rep(F1, 'jsCode', L('const START = Date.now();', 'const rows = $input.all().map(i => i.json || {});'), snip('f1_main_rows.txt'));

// claim: un despacho más nuevo del mismo lead (para no confundirlo con una automatización post-llamada)
P9.rep('🔒 Build Claim SQL', 'jsCode', "  '(SELECT GROUP_CONCAT(x.followup_id) FROM wf_call_events x",
  "  '(SELECT COUNT(*) FROM wf_call_events x WHERE x.lead_id = e.lead_id AND x.event_key <> e.event_key AND e.dispatched_at IS NOT NULL AND x.dispatched_at IS NOT NULL AND x.dispatched_at > e.dispatched_at) AS lead_newer_dispatch,',\n" +
  "  '(SELECT GROUP_CONCAT(x.followup_id) FROM wf_call_events x");

// nodos nuevos: CDR
const cl = P9.by['🔒 Claim + Leer Eventos (MySQL)'].position;
P9.add({ parameters: { jsCode: src('wf9/build_cdr_sql.js') }, name: '🧾 Build CDR SQL (Asterisk)', type: 'n8n-nodes-base.code', typeVersion: 2, position: [cl[0] + 120, cl[1] + 220] });
P9.add({ parameters: { operation: 'executeQuery', query: '{{ $json.cdr_sql }}', options: {} }, name: '🗄️ CDR Asterisk (billsec)', type: 'n8n-nodes-base.mySql', typeVersion: 2.4,
  position: [cl[0] + 360, cl[1] + 220], credentials: clone(MYSQL_CRED9), alwaysOutputData: true, onError: 'continueRegularOutput' });
P9.rewire('🔒 Claim + Leer Eventos (MySQL)', F1, ['🧾 Build CDR SQL (Asterisk)', '🗄️ CDR Asterisk (billsec)']);

// Nota visual WF9 omitida: solo documentacion.

// ---------------------------------------------------------------------------
// salida
// ---------------------------------------------------------------------------
wf2.active = false; wf9.active = false;
const o2 = OUT + '/WF2_DUR_CANDIDATE.json', o9 = OUT + '/WF9_DUR_CANDIDATE.json';
fs.writeFileSync(o2, JSON.stringify(wf2, null, 2));
fs.writeFileSync(o9, JSON.stringify(wf9, null, 2));

// diffs por nodo (unified)
function nodeDiffs(orig, nw, label) {
  const ob = Object.fromEntries(orig.nodes.map(n => [n.name, n]));
  let out = '';
  const tmp = fs.mkdtempSync(path.join(require('os').tmpdir(), 'dd-'));
  for (const n of nw.nodes) {
    const o = ob[n.name];
    for (const f of ['jsCode', 'query', 'content']) {
      const a = o ? (o.parameters[f] || '') : '', b = n.parameters[f] || '';
      if (a === b) continue;
      fs.writeFileSync(tmp + '/a', a + '\n'); fs.writeFileSync(tmp + '/b', b + '\n');
      let d = '';
      try { cp.execSync(`diff -u -L "${label} :: ${n.name} :: ${f} (ANTES)" -L "${label} :: ${n.name} :: ${f} (DESPUÉS)" ${tmp}/a ${tmp}/b`, { encoding: 'utf8' }); }
      catch (e) { d = e.stdout || ''; }
      out += (o ? '' : `### NODO NUEVO: ${n.name}\n`) + d + '\n';
    }
  }
  return out;
}
fs.writeFileSync(OUT + '/DIFF_WF2.patch', nodeDiffs(orig2, wf2, 'WF2'));
fs.writeFileSync(OUT + '/DIFF_WF9.patch', nodeDiffs(orig9, wf9, 'WF9'));
const conn = (o, n) => { const r = []; const keys = new Set([...Object.keys(o.connections), ...Object.keys(n.connections)]); for (const k of keys) { const a = JSON.stringify(o.connections[k] || null), b = JSON.stringify(n.connections[k] || null); if (a !== b) r.push({ from: k, antes: (o.connections[k] && o.connections[k].main || []).map(x => x.map(y => y.node)), despues: (n.connections[k] && n.connections[k].main || []).map(x => x.map(y => y.node)) }); } return r; };
fs.writeFileSync(OUT + '/CHANGES_dur.json', JSON.stringify({
  wf2: { in: path.basename(WF2_IN), nodes_before: orig2.nodes.length, nodes_after: wf2.nodes.length, added: P2.added, replacements: P2.log.filter(x => x.kind === 'replace').length, connections: conn(orig2, wf2) },
  wf9: { in: path.basename(WF9_IN), nodes_before: orig9.nodes.length, nodes_after: wf9.nodes.length, added: P9.added, replacements: P9.log.filter(x => x.kind === 'replace').length, connections: conn(orig9, wf9) }
}, null, 2));
console.log(`WF2 candidato: ${orig2.nodes.length} -> ${wf2.nodes.length} nodos (${P2.log.filter(x => x.kind === 'replace').length} reemplazos exactos, ${P2.added.length} nuevos)`);
console.log(`WF9 candidato: ${orig9.nodes.length} -> ${wf9.nodes.length} nodos (${P9.log.filter(x => x.kind === 'replace').length} reemplazos exactos, ${P9.added.length} nuevos)`);
