// ============================================================================
// 🧾 BUILD CDR SQL (ASTERISK) — billsec real de las llamadas Asterisk del lote.
//
// LeadStudio fija durationSeconds en el POST /followups y no lo deja editar después,
// por eso la Activity de una llamada Asterisk contestada la crea WF9 al TERMINAR, con
// CDR.billsec (tiempo conectado; NO cdr.duration, que incluye el timbrado).
//
// Sólo lectura. Una consulta (UNION ALL) para los eventos Asterisk reclamados; cada fila
// lleva "ref" = event_key. La ventana se calcula con la hora del UNIQUEID del CDR
// (epoch de inicio del canal), que no depende de la zona horaria de MySQL.
//  - llamada diferida por WF2 (dispatched_at): [despacho - 45 min, despacho + 2 min]
//  - resto (webhook/polling): [inicio de la conversación - 15 min, + 2 min]
// El teléfono se compara con los últimos 10 dígitos de dst.
// Si el CDR no existe / no hay permiso, el nodo MySQL siguiente falla en modo "continuar"
// y ⚙️ Fase 1 usa la duración de ElevenLabs (nunca se detiene el procesador).
// ============================================================================
const CDR_TABLE = 'cdr';   // si el CDR está en otra base: 'asteriskcdrdb.cdr' (ver PROBE_CDR_READONLY.sh)
const CLAIM = 'claim_token';
function q(v) {
  if (v === null || v === undefined || v === '') return 'NULL';
  if (typeof v === 'number') return Number.isFinite(v) ? String(Math.trunc(v)) : 'NULL';
  return "'" + String(v).replace(/\u0000/g, '').replace(/[\r\n]+/g, ' ').replace(/\$/g, '').replace(/\\/g, '\\\\').replace(/'/g, "''")
    .replace(/\{\{/g, '{ {').replace(/\}\}/g, '} }') + "'";
}
// uniqueid = "<epoch>.<seq>" o "<systemname>-<epoch>.<seq>" (systemname puede llevar '-' o '.'): se toma el penúltimo tramo entre puntos
const EPOCH = "CAST(SUBSTRING_INDEX(SUBSTRING_INDEX(SUBSTRING_INDEX(c.uniqueid, '.', -2), '.', 1), '-', -1) AS UNSIGNED)";
const parts = [];
for (const it of $input.all()) {
  const ev = it.json || {};
  if (!ev.event_key || !ev[CLAIM] || ev.source !== 'elevenlabs' || ev.provider !== 'asterisk') continue;
  const digits = String(ev.phone || '').replace(/\D/g, '');
  if (digits.length < 8) continue;
  const deferred = Number(ev.dispatched_at_ms) > 0;
  const anchor = deferred ? Number(ev.dispatched_at_ms) : Number(ev.event_at_ms);
  if (!(anchor > 0)) continue;
  const lo = Math.floor(anchor / 1000) - (deferred ? 45 * 60 : 15 * 60);
  const hi = Math.floor(anchor / 1000) + 120;
  parts.push('SELECT ' + q(ev.event_key) + ' AS ref, c.uniqueid AS cdr_uniqueid, ' + EPOCH + ' AS cdr_start_s, c.duration AS cdr_duration, c.billsec AS cdr_billsec ' +
    'FROM ' + CDR_TABLE + ' c WHERE c.calldate >= UTC_TIMESTAMP() - INTERVAL 2 DAY AND c.disposition = ' + q('ANSWERED') + ' AND c.billsec > 0 ' +
    "AND RIGHT(REPLACE(REPLACE(c.dst, '+', ''), ' ', ''), 10) = " + q(digits.slice(-10)) + ' AND ' + EPOCH + ' BETWEEN ' + lo + ' AND ' + hi);
}
if (!parts.length) return [{ json: { cdr_rows: 0, cdr_sql: 'SELECT 1 AS noop' } }];
console.log(`🧾 CDR Asterisk: consulta para ${parts.length} llamada(s)`);
return [{ json: { cdr_rows: parts.length, cdr_sql: parts.join(' UNION ALL ') } }];
