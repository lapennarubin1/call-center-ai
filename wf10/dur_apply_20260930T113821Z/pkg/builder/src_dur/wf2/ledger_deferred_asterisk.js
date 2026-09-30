// ============================================================================
// 🧾 BUILD DEFERRED LEDGER (ASTERISK) — llamada Asterisk CONTESTADA.
//
// Por qué existe: LeadStudio fija durationSeconds en el POST /followups y NO lo deja
// editar después (PATCH /followups/{id} -> 400 "Nothing to update"). Este nodo corre
// cuando la llamada acaba de ser contestada (la API de ElevenLabs responde al contestar,
// no al colgar), o sea antes de conocer el billsec real. Por eso la Activity de una
// llamada contestada la crea WF9 al TERMINAR la llamada (con CDR.billsec).
//
// Aquí sólo se deja un registro durable en wf_call_events con la MISMA clave que usan el
// webhook y el polling de ElevenLabs (elevenlabs:<conversation_id>): mismo evento, sin
// duplicados. dispatched_at es la marca "esta llamada la creará WF9".
// Luego se CONFIRMA con un SELECT: 🏁 Guardar Resultado (Asterisk) sólo difiere la Activity
// de las llamadas confirmadas; si el registro falla, crea la Activity como antes (nunca se
// pierde una llamada).
// Sin llamadas contestadas en este lote -> consulta vacía (SELECT 1).
// Horas en UTC explícito.
// ============================================================================
const CONV_RE = /^conv_[A-Za-z0-9]{8,64}$/;
function q(v) {
  if (v === null || v === undefined || v === '') return 'NULL';
  if (typeof v === 'number') return Number.isFinite(v) ? String(Math.trunc(v)) : 'NULL';
  return "'" + String(v).replace(/\u0000/g, '').replace(/[\r\n]+/g, ' ').replace(/\$/g, '').replace(/\\/g, '\\\\').replace(/'/g, "''")
    .replace(/\{\{/g, '{ {').replace(/\}\}/g, '} }') + "'";
}
function dt(v) {
  const t = v ? new Date(v).getTime() : NaN;
  return Number.isFinite(t) ? "'" + new Date(t).toISOString().replace('T', ' ').replace('Z', '') + "'" : 'UTC_TIMESTAMP(3)';
}
const locked = $('📥 Fetch + Lock (Asterisk)').all().map(i => i.json || {});
const phoneOf = id => { const l = locked.find(x => x.lead_id === id); return l ? String(l.phone || '').replace(/[^0-9+]/g, '').slice(0, 32) : null; };
const rows = [];
const keys = [];
for (const it of $input.all()) {
  const j = it.json || {};
  if (String(j.outcome || '').toUpperCase() !== 'CONNECTED' || String(j.callStatus || '').toUpperCase() !== 'ANSWERED') continue;
  const conv = String(j.elevenlabs_call_id || '');
  if (!CONV_RE.test(conv) || !j.lead_id) continue;
  const att = Number(j.attempts);
  const key = 'elevenlabs:' + conv;
  if (keys.includes(q(key))) continue;
  keys.push(q(key));
  rows.push('(' + [q(key), q('elevenlabs'), q('asterisk'), q(conv), q(String(j.lead_id).slice(0, 64)), q(phoneOf(j.lead_id)),
    q(Number.isInteger(att) && att >= 1 && att <= 99 ? att : null), dt(j.last_call_time), dt(j.last_call_time), 'UTC_TIMESTAMP(3)', q('RECEIVED'),
    'UTC_TIMESTAMP(3) + INTERVAL 3 MINUTE', q(JSON.stringify({ via: 'wf2_asterisk_answered_deferred' }))].join(', ') + ')');
}
if (!rows.length) return [{ json: { ledger_rows: 0, ledger_sql: 'SELECT 1 AS noop' } }];
const ledger_sql = 'INSERT INTO wf_call_events (event_key, source, provider, external_id, lead_id, phone, call_attempts, ' +
  'event_at, dispatched_at, first_seen_at, state, next_attempt_at, payload) VALUES ' + rows.join(', ') +
  ' ON DUPLICATE KEY UPDATE provider = VALUES(provider), lead_id = COALESCE(lead_id, VALUES(lead_id)), ' +
  'phone = COALESCE(phone, VALUES(phone)), call_attempts = COALESCE(call_attempts, VALUES(call_attempts)), ' +
  'event_at = COALESCE(event_at, VALUES(event_at)), dispatched_at = COALESCE(dispatched_at, VALUES(dispatched_at))' +
  '; SELECT event_key FROM wf_call_events WHERE dispatched_at IS NOT NULL AND event_key IN (' + keys.join(', ') + ')';
console.log(`🧾 Asterisk contestadas: ${rows.length} llamada(s) registradas para que WF9 cree la Activity al terminar`);
return [{ json: { ledger_rows: rows.length, ledger_sql } }];
