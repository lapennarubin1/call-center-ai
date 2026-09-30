// Ejecuta los nodos REALES del JSON generado (código de los Code nodes + plantillas de los nodos MySQL)
const fs = require('fs');
const { runCode, mysqlNode, sql } = require('./harness');

function loadWf(file) {
  const wf = JSON.parse(fs.readFileSync(file, 'utf8'));
  const by = Object.fromEntries(wf.nodes.map(n => [n.name, n]));
  return { wf, by };
}
// Resuelve la plantilla de un nodo MySQL como lo hace n8n ({{ $json.x }} / {{ $('N').first().json.x }})
function renderQuery(template, item, nodeOutputs) {
  return template.replace(/\{\{\s*([\s\S]+?)\s*\}\}/g, (_, expr) => {
    let m;
    if ((m = expr.match(/^\$json\.([A-Za-z0-9_]+)$/))) return String(item.json[m[1]]);
    if ((m = expr.match(/^\$\('([^']+)'\)\.first\(\)\.json\.([A-Za-z0-9_]+)$/))) {
      const arr = nodeOutputs[m[1]]; if (!arr) throw new Error('nodo no ejecutado: ' + m[1]);
      return String((arr[0].json || arr[0])[m[2]]);
    }
    throw new Error('expresión no soportada en test: ' + expr);
  });
}
async function mysqlRun(node, items, nodeOutputs) {
  const qs = items.map(it => renderQuery(node.parameters.query, it, nodeOutputs));
  // n8n (v2.4, modo legacy) convierte $<n> en placeholders: el test falla si aparece alguno
  for (const q of qs) if (/\$\d/.test(q)) throw new Error('SQL con $<n> (n8n lo trataría como parámetro): ' + q.slice(0, 120));
  return mysqlNode(qs);
}

function makeRunner(file, world) {
  const { wf, by } = loadWf(file);
  const code = name => { if (!by[name]) throw new Error('falta nodo ' + name); return by[name].parameters.jsCode; };
  const http = world.httpFn();
  const logs = [];
  const run = async (name, opts) => { const r = await runCode(code(name), Object.assign({ http }, opts)); logs.push(...r.logs.map(l => [name, ...l])); return r.out; };

  return {
    wf, by, logs,
    async processorCycle() {
      const o = {};
      o['🔒 Build Claim SQL'] = await run('🔒 Build Claim SQL', { items: [{}] });
      const read = await mysqlRun(by['🔒 Claim + Leer Eventos (MySQL)'], o['🔒 Build Claim SQL'], o);
      const rows = read.filter(r => r.json && r.json.event_key);
      if (!rows.length) return { events: 0 };
      o['🔒 Claim + Leer Eventos (MySQL)'] = read;
      // Candidato "duración real": claim -> Build CDR SQL -> CDR (MySQL, continuar si falla) -> Fase 1
      let fase1Items = rows;
      if (by['🧾 Build CDR SQL (Asterisk)']) {
        o['🧾 Build CDR SQL (Asterisk)'] = await run('🧾 Build CDR SQL (Asterisk)', { items: rows });
        let cdrOut;
        try { cdrOut = await mysqlRun(by['🗄️ CDR Asterisk (billsec)'], o['🧾 Build CDR SQL (Asterisk)'], o); }
        catch (e) { cdrOut = [{ json: { error: String(e.message || e) } }]; }   // onError: continueRegularOutput
        if (!cdrOut.length) cdrOut = [{ json: {} }];                              // alwaysOutputData
        o['🗄️ CDR Asterisk (billsec)'] = cdrOut;
        fase1Items = cdrOut;
      }
      o['⚙️ Fase 1 — Activity CRM'] = await run('⚙️ Fase 1 — Activity CRM', { items: fase1Items, nodes: { '🔒 Claim + Leer Eventos (MySQL)': read } });
      await mysqlRun(by['💾 Save Followup ID (WF9)1'], o['⚙️ Fase 1 — Activity CRM'], o);
      o['⚙️ Fase 2 — Ciclo de vida + cierre'] = await run('⚙️ Fase 2 — Ciclo de vida + cierre', { items: [{ success: true }], nodes: o });
      await mysqlRun(by['💾 Finalizar Eventos (MySQL)'], o['⚙️ Fase 2 — Ciclo de vida + cierre'], o);
      const summary = await run('📝 Log Success', { items: [{ success: true }], nodes: o });
      return { events: rows.length, p1: o['⚙️ Fase 1 — Activity CRM'].map(i => i.json), p2: o['⚙️ Fase 2 — Ciclo de vida + cierre'].map(i => i.json), summary: summary[0].json };
    },
    async callback(jobObj) {
      const o = {};
      o.b = await run('🧾 Build Inbox SQL (Stringee callback)', { items: [{ headers: {}, body: jobObj }] });
      await mysqlRun(by['💾 Insert Inbox (Stringee callback)'], o.b, o);
      return o.b[0].json;
    },
    async webhook(detail, type = 'post_call_transcription') {
      const o = {};
      o.p = await run('⚙️ Parse Post-Call Data', { items: [{ headers: {}, body: { type, event_timestamp: Math.floor(Date.now() / 1000), data: detail } }] });
      await mysqlRun(by['💾 Insert Inbox (ElevenLabs webhook)'], o.p, o);
      o.c = await run('🔗 Build Stringee Conv-ID SQL (webhook)1', { items: o.p });
      return { parse: o.p[0].json, convid: o.c[0].json };
    },
    async polling() {
      const o = {};
      o.t = await run('Traer Histórico ElevenLabs', { items: [{}] });
      if (!o.t.length) return { items: 0 };
      o.b = await run('🧾 Build Inbox SQL (polling)', { items: o.t });
      await mysqlRun(by['💾 Insert Inbox (polling)'], o.b, o);
      o.c = await run('🔗 Build Stringee Conv-ID SQL (polling)1', { items: o.t });
      return { items: o.t.length, convid: o.c.map(i => i.json) };
    },
    async ledger(entries, locked) {
      // mismo código que se inserta en WF2 después de 🏁 Guardar Resultado (Stringee)
      const c = fs.readFileSync(__dirname + '/../src/wf2/ledger_stringee.js', 'utf8');
      const r = await runCode(c, { items: entries, nodes: { '📥 Fetch + Lock (Stringee)': locked } });
      await mysqlNode(r.out.map(i => i.json.ledger_sql));
      return r.out[0].json;
    }
  };
}
async function wake(where = "state IN ('RECEIVED','FAILED')") {
  await sql("UPDATE wf_call_events SET next_attempt_at = UTC_TIMESTAMP(3) - INTERVAL 1 SECOND WHERE " + where);
}
async function drain(runner, max = 8) {
  const all = [];
  for (let i = 0; i < max; i++) {
    await wake();
    const r = await runner.processorCycle();
    all.push(r);
    const pending = await sql("SELECT COUNT(*) n FROM wf_call_events WHERE state IN ('RECEIVED','FAILED','PROCESSING')");
    if (!Number(pending[0].n)) break;
  }
  return all;
}
module.exports = { loadWf, makeRunner, wake, drain, renderQuery };
