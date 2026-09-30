// Arnés de pruebas: ejecuta el código de un Code node de n8n (modo "run once for all items")
// con $input / $ / $execution / this.helpers.httpRequest simulados, y MySQL real (MariaDB local).
const fs = require('fs');
const mysql = require('/home/claude/n8ntest/node_modules/mysql2/promise');
const AsyncFunction = Object.getPrototypeOf(async function () {}).constructor;

let pool;
async function db() {
  if (!pool) pool = mysql.createPool({ host: '127.0.0.1', user: 'n8n', password: 'n8ntest', database: 'asterisk', multipleStatements: true, supportBigNumbers: true, connectionLimit: 4 });
  return pool;
}
// Igual que el nodo MySQL v2.4 en modo "single" (misma lógica de n8n: une con ';' y sólo devuelve filas de SELECT)
const { splitQueryToStatements } = require('/home/claude/n8ntest/node_modules/n8n-nodes-base/dist/nodes/MySql/v2/helpers/utils.js');
async function mysqlNode(queries) {
  const p = await db();
  const single = queries.length > 1 ? queries.map(s => s.trim().replace(/;$/, '')).join(';') : queries[0];
  let [response] = await p.query(single);
  const statements = splitQueryToStatements(single);
  if (Array.isArray(response)) { if (statements.length === 1) response = [response]; } else response = [response];
  const out = [];
  for (const r of response) if (Array.isArray(r)) for (const row of r) out.push({ json: row });
  if (!out.length) {
    const allSelect = statements.filter(st => !st.startsWith('--')).every(st => st.replace(/\/\*.*?\*\//g, '').toLowerCase().startsWith('select'));
    if (!allSelect) out.push({ json: { success: true } });
  }
  return out;
}
async function sql(q) { const p = await db(); const [r] = await p.query(q); return r; }
async function close() { if (pool) await pool.end(); pool = null; }

function wrap(items) { return (items || []).map(j => (j && j.json) ? j : { json: j }); }

async function runCode(code, { items = [], nodes = {}, http, execId = '1', staticData = {} } = {}) {
  const input = wrap(items);
  const $input = { all: () => input, first: () => input[0], item: input[0] };
  const $ = (name) => {
    if (!(name in nodes)) throw new Error('Nodo referenciado no disponible en el test: ' + name);
    const arr = wrap(nodes[name]);
    return { all: () => arr, first: () => arr[0], item: arr[0] };
  };
  const logs = [];
  const cons = {
    log: (...a) => logs.push(['log', a.map(String).join(' ')]),
    warn: (...a) => logs.push(['warn', a.map(String).join(' ')]),
    error: (...a) => logs.push(['error', a.map(String).join(' ')])
  };
  const ctx = { helpers: { httpRequest: http || (async () => { throw new Error('http no simulado'); }) } };
  const fn = new AsyncFunction('$input', '$', '$execution', '$getWorkflowStaticData', 'console', 'require', code);
  const out = await fn.call(ctx, $input, $, { id: execId }, () => staticData, cons, require);
  return { out: wrap(out), logs };
}
module.exports = { runCode, mysqlNode, sql, close, db };
