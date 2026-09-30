// Workflows mínimos para n8n REAL (1.123.82): los nodos nuevos del candidato, copiados tal cual, con nodos "stub" en lugar de los vecinos.
const fs = require('fs');
const w2 = JSON.parse(fs.readFileSync(__dirname + '/../out_dur/WF2_DUR_CANDIDATE.json', 'utf8'));
const w9 = JSON.parse(fs.readFileSync(__dirname + '/../out_dur/WF9_DUR_CANDIDATE.json', 'utf8'));
const pick = (w, n) => JSON.parse(JSON.stringify(w.nodes.find(x => x.name === n)));
let pos = 0;
const stub = (name, js) => ({ id: 'stub' + (++pos), name, type: 'n8n-nodes-base.code', typeVersion: 2, position: [pos * 220, 0], parameters: { jsCode: js } });
const start = () => ({ id: 'start', name: 'Start', type: 'n8n-nodes-base.manualTrigger', typeVersion: 1, position: [0, 0], parameters: {} });
function build(id, name, nodes) {
  const conn = {}; for (let i = 0; i < nodes.length - 1; i++) conn[nodes[i].name] = { main: [[{ node: nodes[i + 1].name, type: 'main', index: 0 }]] };
  nodes.forEach((n, i) => n.position = [i * 240, 0]);
  return { id, name, nodes, connections: conn, active: false, settings: {}, pinData: {} };
}
// ---- WF9: Claim (stub) -> Build CDR SQL -> CDR MySQL -> reporte
const now = Date.now();
const claimStub = stub('🔒 Claim + Leer Eventos (MySQL)', `
const now = Date.now();
return [
 { json: { event_key: 'elevenlabs:conv_n8nAAA00000001', claim_token: 'tok', source: 'elevenlabs', provider: 'asterisk', phone: '919241014686', dispatched_at_ms: now - 420000, event_at_ms: now - 400000 } },
 { json: { event_key: 'elevenlabs:conv_n8nBBB00000001', claim_token: 'tok', source: 'elevenlabs', provider: 'asterisk', phone: '919812300777', dispatched_at_ms: now - 420000, event_at_ms: now - 400000 } },
 { json: { event_key: 'stringee:job-1', claim_token: 'tok', source: 'stringee', provider: 'stringee', phone: '919241014686', event_at_ms: now - 400000 } }
];`);
const rep9 = stub('REPORT', `return $input.all().map(i => ({ json: i.json }));`);
fs.writeFileSync('/home/claude/n8ntest/dur/wf9_cdr_mini.json', JSON.stringify(build('durMiniCdr000001', 'DUR MINI CDR', [start(), claimStub, pick(w9, '🧾 Build CDR SQL (Asterisk)'), pick(w9, '🗄️ CDR Asterisk (billsec)'), rep9])));
// ---- WF2: Fetch (stub) -> Classify (stub) -> Build Deferred Ledger -> Ledger MySQL -> Login(stub) -> Guardar -> Build Followup Row
const fetchStub = stub('📥 Fetch + Lock (Asterisk)', `return [{ json: { lead_id: '00000abc-0000-4000-8000-00000000abcd', phone: '+919241014686', country: 'india', call_attempts: 2 } }];`);
const classStub = stub('🔍 Classify Dial Result (Asterisk)', `return [{ json: { provider: 'asterisk', lead_id: '00000abc-0000-4000-8000-00000000abcd', country: 'india', elevenlabs_call_id: 'conv_n8nLEDGER00001', last_call_time: new Date().toISOString(), status: 'CONTACTED', stage: null, attempts: 3, nextFollowUpAt: null, last_call_status: 'ANSWERED', outcome: 'CONNECTED', callStatus: 'ANSWERED', notes: 'Call connected successfully: conv_n8nLEDGER00001', logFollowup: true } }];`);
const loginStub = stub('🔐 Login LeadStudio1', `return [{ json: { accessToken: 'tok' } }];`);
// el Login debe ir antes de Guardar pero Guardar toma $input del ledger: se ejecuta en una rama previa
const nodes2 = [start(), fetchStub, loginStub, classStub, pick(w2, '🧾 Build Deferred Ledger (Asterisk)'), pick(w2, '💾 Ledger Deferred (Asterisk)'), pick(w2, '🏁 Guardar Resultado (Asterisk)'), pick(w2, '🧾 Build Followup Row (Asterisk)')];
fs.writeFileSync('/home/claude/n8ntest/dur/wf2_ledger_mini.json', JSON.stringify(build('durMiniLedger0001', 'DUR MINI LEDGER', nodes2)));
console.log('ok');
