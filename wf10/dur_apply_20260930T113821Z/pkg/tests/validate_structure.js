// Validaciones estructurales obligatorias (1–11) + credenciales/secretos, sobre los archivos FINALES.
const fs = require('fs');
const AsyncFunction = Object.getPrototypeOf(async function () {}).constructor;
const results = [];
const check = (id, name, cond, detail) => results.push({ id, name, pass: !!cond, detail: cond ? '' : String(detail || '') });
const load = f => { const raw = fs.readFileSync(f, 'utf8'); return { raw, wf: JSON.parse(raw) }; };

const O9 = JSON.parse(fs.readFileSync(__dirname + '/../wf9_orig.json', 'utf8'));
const O2 = JSON.parse(fs.readFileSync(__dirname + '/../wf2_orig.json', 'utf8'));
const files = {
  WF9: process.env.WF9_FILE || (__dirname + '/../out/WF9_PRODUCTION_FIXED_STRINGEE_RELIABILITY.json'),
  WF2: process.env.WF2_FILE || (__dirname + '/../out/WF2_UNIFICADO_v2_PATCHED.json')
};
function ancestors(wf, name) {
  const parents = {};
  for (const [from, c] of Object.entries(wf.connections)) for (const outs of (c.main || [])) for (const t of (outs || [])) (parents[t.node] = parents[t.node] || new Set()).add(from);
  const seen = new Set(); const st = [name];
  while (st.length) { const n = st.pop(); for (const p of (parents[n] || [])) if (!seen.has(p)) { seen.add(p); st.push(p); } }
  return seen;
}
for (const [tag, file] of Object.entries(files)) {
  let wf;
  try { wf = load(file).wf; check('1', `${tag}: el JSON parsea`, true); } catch (e) { check('1', `${tag}: el JSON parsea`, false, e.message); continue; }
  check('2', `${tag}: estructura de workflow n8n (name, nodes, connections, settings)`, wf.name && Array.isArray(wf.nodes) && wf.connections && wf.settings && wf.settings.executionOrder === 'v1');
  const ids = wf.nodes.map(n => n.id), names = wf.nodes.map(n => n.name);
  check('3', `${tag}: IDs de nodo únicos (${ids.length})`, new Set(ids).size === ids.length, ids.filter((x, i) => ids.indexOf(x) !== i).join(','));
  check('3b', `${tag}: nombres de nodo únicos`, new Set(names).size === names.length);
  const nameSet = new Set(names);
  const badSrc = Object.keys(wf.connections).filter(n => !nameSet.has(n));
  const badDst = [];
  for (const c of Object.values(wf.connections)) for (const outs of (c.main || [])) for (const t of (outs || [])) if (!nameSet.has(t.node) || t.type !== 'main') badDst.push(t.node);
  check('4', `${tag}: todo origen de conexión existe`, badSrc.length === 0, badSrc.join(', '));
  check('5', `${tag}: todo destino de conexión existe`, badDst.length === 0, badDst.join(', '));
  // 6) $('NODE') en expresiones y código
  const refs = [];
  for (const n of wf.nodes) {
    const s = JSON.stringify(n.parameters);
    const re = /\$\(\s*(?:\\?["'])(.+?)(?:\\?["'])\s*\)/g; let m;
    while ((m = re.exec(s))) refs.push({ from: n.name, to: m[1].replace(/\\"/g, '"'), disabled: !!n.disabled });
  }
  const missing = refs.filter(r => !nameSet.has(r.to));
  check('6', `${tag}: toda referencia $('NODO') apunta a un nodo existente (${refs.length} referencias)`, missing.length === 0, JSON.stringify(missing));
  const notUp = refs.filter(r => !r.disabled && nameSet.has(r.to) && !ancestors(wf, r.from).has(r.to));
  check('6b', `${tag}: toda referencia $('NODO') en nodos activos apunta a un nodo que corre antes (ancestro)`, notUp.length === 0, JSON.stringify(notUp));
  const mysqlDollar = wf.nodes.filter(n => !n.disabled && n.type === 'n8n-nodes-base.mySql' && /\$\(/.test(String(n.parameters.query || '')) && !O2.nodes.concat(O9.nodes).some(o => o.id === n.id && JSON.stringify(o.parameters) === JSON.stringify(n.parameters)));
  check('6c', `${tag}: ningún nodo MySQL nuevo/modificado usa $('NODO') dentro de la query (en n8n se resuelve como undefined)`, mysqlDollar.length === 0, mysqlDollar.map(n => n.name).join(', '));
  // 7) sintaxis Code nodes (AsyncFunction)
  const bad = [];
  for (const n of wf.nodes.filter(n => n.type === 'n8n-nodes-base.code' && !n.disabled)) {
    try { new AsyncFunction('$input', '$', '$json', '$execution', '$getWorkflowStaticData', '$node', 'require', n.parameters.jsCode); } catch (e) { bad.push(n.name + ': ' + e.message); }
  }
  check('7', `${tag}: sintaxis válida de todos los Code nodes activos (AsyncFunction)`, bad.length === 0, bad.join(' | '));
  // 8) webhooks
  const hooks = wf.nodes.filter(n => n.type === 'n8n-nodes-base.webhook' && !n.disabled).map(n => n.parameters.httpMethod + ' ' + n.parameters.path);
  check('8', `${tag}: sin paths de webhook duplicados (${hooks.join(' ; ') || 'ninguno activo'})`, new Set(hooks).size === hooks.length);
  const hookIds = wf.nodes.filter(n => n.type === 'n8n-nodes-base.webhook').map(n => n.webhookId);
  check('8b', `${tag}: webhookId únicos`, new Set(hookIds).size === hookIds.length);
}

const W9 = load(files.WF9).wf, W2 = load(files.WF2).wf;
const by9 = Object.fromEntries(W9.nodes.map(n => [n.name, n])), byO9 = Object.fromEntries(O9.nodes.map(n => [n.name, n]));
const by2 = Object.fromEntries(W2.nodes.map(n => [n.name, n])), byO2 = Object.fromEntries(O2.nodes.map(n => [n.name, n]));

// 9) credenciales
const credsOf = wf => { const s = new Set(); for (const n of wf.nodes) for (const [t, c] of Object.entries(n.credentials || {})) s.add(t + ':' + c.id + ':' + c.name); return s; };
const c9 = credsOf(W9), cO9 = credsOf(O9), c2 = credsOf(W2), cO2 = credsOf(O2);
check('9', `WF9: sólo credenciales existentes (${[...c9].join(', ')})`, [...c9].every(x => cO9.has(x)) && c9.size > 0);
check('9', `WF2: sólo credenciales existentes y ninguna perdida (${[...c2].join(', ')})`, [...c2].every(x => cO2.has(x)) && [...cO2].every(x => c2.has(x)));
const credNodesKept = O2.nodes.filter(n => n.credentials).every(n => by2[n.name] && JSON.stringify(by2[n.name].credentials) === JSON.stringify(n.credentials));
check('9', 'WF2: los nodos con credenciales conservan exactamente su referencia', credNodesKept);

// 10) nodos importantes
const mustKeep9 = ['📥 ElevenLabs Post-Call Webhook', '✅ Respond OK', '⚙️ Parse Post-Call Data', '🔗 Build Stringee Conv-ID SQL (webhook)1', '💾 Update stringee_calls conv_id (webhook)1',
  '🔗 Build Stringee Conv-ID SQL (polling)1', '💾 Update stringee_calls conv_id (polling)1', 'Polling Cada 5min (última hora)', 'Traer Histórico ElevenLabs', '💾 Save Followup ID (WF9)1', '📝 Log Success'];
const lost9 = mustKeep9.filter(n => !by9[n] || by9[n].id !== byO9[n].id);
check('10', 'WF9: nodos importantes presentes con el mismo ID', lost9.length === 0, lost9.join(', '));
const hk = by9['📥 ElevenLabs Post-Call Webhook'], hkO = byO9['📥 ElevenLabs Post-Call Webhook'];
check('10', 'WF9: webhook de ElevenLabs intacto (path, webhookId, método, responseMode, rawBody)', JSON.stringify(hk.parameters) === JSON.stringify(hkO.parameters) && hk.webhookId === hkO.webhookId);
const convIdSame = ['🔗 Build Stringee Conv-ID SQL (webhook)1', '💾 Update stringee_calls conv_id (webhook)1', '🔗 Build Stringee Conv-ID SQL (polling)1', '💾 Update stringee_calls conv_id (polling)1']
  .every(n => JSON.stringify(by9[n].parameters) === JSON.stringify(byO9[n].parameters));
check('10', 'WF9: rama de enlace conv-id de Stringee sin cambios', convIdSame);
const disabledLeft = W9.nodes.filter(n => n.disabled);
check('10', 'WF9: nodos legado deshabilitados eliminados', disabledLeft.length === 0, disabledLeft.map(n => n.name).join(', '));
const lost2 = O2.nodes.filter(n => !by2[n.name] || by2[n.name].id !== n.id).map(n => n.name);
check('10', 'WF2: ningún nodo existente desapareció ni cambió de ID', lost2.length === 0, lost2.join(', '));
const untouched2 = ['⏰ Trigger Dialer (cada 1 min)', '🗄️ Leer Providers Activos (MySQL)', '⚙️ Calcular Providers Activos', '✅ Token OK?', '🔀 Asterisk Habilitado?', '🔀 Stringee Habilitado?',
  '📞 POST → ElevenLabs Dial (Asterisk)', '🔍 Classify Result (Stringee)', '🏁 Guardar Resultado (Stringee)', '🔐 Login LeadStudio1', '⏰ Trigger Mantenimiento (cada 5 min)']
  .filter(n => JSON.stringify(by2[n]) !== JSON.stringify(byO2[n]));
check('10', 'WF2: dial Asterisk, clasificación/guardado Stringee, login y triggers sin cambios', untouched2.length === 0, untouched2.join(', '));
// paquete publicado: sólo cambia la salida del Trigger de Mantenimiento; candidato "duración real": además Classify (Asterisk) -> ledger diferido -> Guardar
const documentedRewires = ['⏰ Trigger Mantenimiento (cada 5 min)'].concat(process.env.WF2_FILE ? ['🔍 Classify Dial Result (Asterisk)'] : []).sort().join(',');
check('10', 'WF2: conexiones originales conservadas salvo las documentadas',
  Object.keys(O2.connections).filter(k => JSON.stringify(O2.connections[k]) !== JSON.stringify(W2.connections[k])).sort().join(',') === documentedRewires);

// 11) polling
const pr = by9['Polling Cada 5min (última hora)'].parameters.rule.interval;
check('11', 'WF9: polling exactamente cada 3 minutos', pr.length === 1 && pr[0].field === 'minutes' && pr[0].minutesInterval === 3, JSON.stringify(pr));
check('11b', 'WF9: ventana de reconciliación 60 min', /const WINDOW_MIN = 60;/.test(by9['Traer Histórico ElevenLabs'].parameters.jsCode));
const proc = by9['⏱️ Procesar Inbox (cada 1 min)'].parameters.rule.interval[0];
check('11c', 'WF9: procesador cada 1 minuto; reconciliación de jobs no terminales cada 3 min (180 s)', proc.minutesInterval === 1 && /JOB_RECHECK_SEC: 180/.test(by9['⚙️ Fase 1 — Activity CRM'].parameters.jsCode));

// Agotamiento: sin LOST en rutas activas
const lostActive = [...W2.nodes, ...W9.nodes].filter(n => !n.disabled && /'LOST'|"LOST"|stage LOST/.test(JSON.stringify(n.parameters))).map(n => n.name);
check('19b', 'WF2 + WF9: ningún nodo activo usa LOST para agotamiento', lostActive.length === 0, lostActive.join(', '));

// WF2: callback_url
check('CB', 'WF2: POST → Stringee Worker envía callback_url de producción', by2['📞 POST → Stringee Worker'].parameters.jsonBody.includes('"callback_url": "https://landmarket-n8n.dhsoig.easypanel.host/webhook/stringee-worker-callback"'));

// secretos: mismas apariciones que antes (no se copian a nodos nuevos salvo la key a Fase 1)
const raw9 = fs.readFileSync(files.WF9, 'utf8'), rawO9 = JSON.stringify(O9), raw2 = fs.readFileSync(files.WF2, 'utf8'), rawO2 = JSON.stringify(O2);
const key = byO9['Traer Histórico ElevenLabs'].parameters.jsCode.match(/const KEY = '([^']+)';/)[1];
const nodesWith = (wf, s) => wf.nodes.filter(n => JSON.stringify(n).includes(s)).map(n => n.name);
check('SEC', 'WF9: la key de ElevenLabs sólo está en "Traer Histórico ElevenLabs" y "⚙️ Fase 1" (necesaria para leer conversaciones)',
  JSON.stringify(nodesWith(W9, key).sort()) === JSON.stringify(['Traer Histórico ElevenLabs', '⚙️ Fase 1 — Activity CRM'].sort()), nodesWith(W9, key).join(', '));
const hm = byO9['⚙️ Parse Post-Call Data'].parameters.jsCode.match(/const secret = '([^']+)';/)[1];
check('SEC', 'WF9: el secreto HMAC sigue sólo en "⚙️ Parse Post-Call Data"', JSON.stringify(nodesWith(W9, hm)) === JSON.stringify(['⚙️ Parse Post-Call Data']));
const pw = rawO2.match(/password: '([^']+)'/)[1];
check('SEC', 'WF2: la contraseña de LeadStudio no se copió a nodos nuevos (mismos 2 nodos de antes)', JSON.stringify(nodesWith(W2, pw).sort()) === JSON.stringify(nodesWith(O2, pw).sort()));
check('SEC', 'WF9/WF2: no se crearon credenciales nuevas ni IDs falsos', !/"credentials":\{"(?!mySql|httpHeaderAuth)/.test(raw9 + raw2));

fs.writeFileSync(__dirname + '/structure_results.json', JSON.stringify(results, null, 2));
for (const x of results) console.log((x.pass ? 'PASS' : 'FAIL') + ' [' + x.id + '] ' + x.name + (x.pass ? '' : '\n      ↳ ' + x.detail.slice(0, 500)));
console.log(`\nTotal: ${results.filter(x => x.pass).length}/${results.length} PASS`);
process.exit(results.every(x => x.pass) ? 0 : 1);
