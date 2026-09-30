// ── LM CORE ─────────────────────────────────────────────────────────────────
// Utilidades compartidas por los Code nodes del suite V2. Se INLINEA en cada
// nodo que las necesita (n8n no tiene "require" de archivos del repo).
// Fuente única: tools/js/lmcore.js — no editar la copia dentro del JSON.

const LM_ERROR_CODES = ['CONFIG_ERROR', 'AUTH_ERROR', 'VALIDATION_ERROR', 'PROVIDER_ERROR',
                        'CRM_ERROR', 'RETRYABLE_ERROR', 'PERMANENT_ERROR'];

function lmMaskPhone(p) {
  const s = String(p || '').replace(/[^0-9]/g, '');
  return s ? s.slice(-4) : '';
}

// Línea de log estándar (N8N_TEMPLATE_STANDARD §10). Nunca teléfono completo,
// nunca tokens, nunca accessToken.
function lmLog(wf, ctx, fields) {
  const f = Object.assign({
    call_job_id: null, route_key: null, country: null, provider: null, adapter_key: null,
    lead_id: null, attempt: null, conversation_id: null, followup_id: null,
    result: null, error_code: null, action: null
  }, fields || {});
  const parts = Object.keys(f).filter(k => f[k] !== null && f[k] !== undefined)
    .map(k => `${k}=${f[k]}`);
  const line = `[${wf}][exec=${ctx.execution_id || '-'}]` +
               `[${f.route_key || '-'}] ` + parts.join(' ') +
               (ctx.phone ? ` phone=...${lmMaskPhone(ctx.phone)}` : '');
  console.log(line);
  return line;
}

function lmNowIso() {
  return new Date().toISOString().replace(/\.\d{3}Z$/, 'Z');
}

function lmToSqlUtc(d) {
  const x = (d instanceof Date) ? d : new Date(d);
  if (isNaN(x)) return null;
  return x.toISOString().slice(0, 19).replace('T', ' ');
}

// ── zona horaria sin librerías ──────────────────────────────────────────────
// n8n corre sobre Node con ICU completo: Intl resuelve DST correctamente.
function lmZonedParts(date, tz) {
  const fmt = new Intl.DateTimeFormat('en-CA', {
    timeZone: tz, hour12: false, year: 'numeric', month: '2-digit', day: '2-digit',
    hour: '2-digit', minute: '2-digit', second: '2-digit'
  });
  const out = {};
  for (const p of fmt.formatToParts(date)) {
    if (p.type !== 'literal') out[p.type] = Number(p.value);
  }
  if (out.hour === 24) out.hour = 0;
  return out;
}

// offset = (hora local expresada como si fuera UTC) - (instante real)
function lmTzOffsetMs(date, tz) {
  const p = lmZonedParts(date, tz);
  return Date.UTC(p.year, p.month - 1, p.day, p.hour, p.minute, p.second)
         - (Math.floor(date.getTime() / 1000) * 1000);
}

// Hora de pared local -> instante UTC. Itera porque el offset depende del
// instante (cambio de horario). Dos pasadas bastan para cualquier zona real.
function lmWallToUtc(y, m, d, hh, mm, ss, tz) {
  const wall = Date.UTC(y, m - 1, d, hh, mm, ss);
  let guess = wall;
  for (let i = 0; i < 3; i++) {
    const off = lmTzOffsetMs(new Date(guess), tz);
    const next = wall - off;
    if (next === guess) break;
    guess = next;
  }
  return new Date(guess);
}

// Día de la semana LOCAL (0=lunes .. 6=domingo), para los días hábiles.
function lmLocalWeekday(y, m, d) {
  const dow = new Date(Date.UTC(y, m - 1, d)).getUTCDay();   // 0=domingo
  return (dow + 6) % 7;                                       // 0=lunes
}
