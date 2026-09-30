// ── LM FOLLOW-UP ENGINE ─────────────────────────────────────────────────────
// Port 1:1 de app/followup_engine.py (la implementación de referencia que
// cubren los tests de la fundación). Cualquier diferencia entre este archivo y
// el módulo Python es un BUG: tests/test_engine_parity_v2.py los compara
// ejecutando los dos con los mismos casos.
//
// El motor NO conoce proveedores ni países. Recibe un CALL RESULT normalizado
// y una política, y devuelve la decisión. Sin "if stringee", sin "if India",
// sin aritmética modular.

const LM_FINAL_RESULTS = ['ANSWERED', 'NO_ANSWER', 'BUSY', 'FAILED', 'VOICEMAIL',
                          'CALLBACK', 'WRONG_NUMBER', 'DNC', 'UNKNOWN'];
const LM_ACTIONS = ['RETRY', 'CLOSE', 'COMPLETE', 'CALLBACK', 'NONE'];
const LM_DELAY_RE = /^\+(\d{1,4})(m|h|d|bd)$/;

// Pista para el paso CRM. El enum real de LeadStudio es PENDING_VERIFICATION (PV-1).
const LM_CRM_STATUS_HINT = {
  RETRY: 'NO_ANSWER', CLOSE: 'CLOSED', COMPLETE: 'CONTACTED',
  CALLBACK: null, NONE: null
};

class LmPolicyError extends Error {}

function lmValidatePolicy(policy) {
  if (typeof policy === 'string') {
    try { policy = JSON.parse(policy); }
    catch (e) { throw new LmPolicyError('policy no es JSON valido: ' + e.message); }
  }
  if (!policy || typeof policy !== 'object' || Array.isArray(policy)) {
    throw new LmPolicyError('policy debe ser un objeto JSON');
  }
  const codes = policy.no_answer_sip_codes || [];
  if (!Array.isArray(codes) || codes.some(c => !/^\d{3}$/.test(String(c)))) {
    throw new LmPolicyError('no_answer_sip_codes debe ser una lista de codigos SIP de 3 digitos');
  }
  const aliases = policy.result_aliases || {};
  if (typeof aliases !== 'object' || Array.isArray(aliases)) {
    throw new LmPolicyError('result_aliases debe ser un objeto');
  }
  for (const k of Object.keys(aliases)) {
    const v = aliases[k];
    if (LM_FINAL_RESULTS.indexOf(k) < 0 || LM_FINAL_RESULTS.indexOf(v) < 0) {
      throw new LmPolicyError(`alias invalido ${k}->${v}: ambos deben ser resultados finales`);
    }
    if (k === v) throw new LmPolicyError(`alias circular ${k}->${v}`);
  }
  const rules = policy.rules;
  if (!Array.isArray(rules) || !rules.length) {
    throw new LmPolicyError('rules debe ser una lista no vacia');
  }
  const seen = new Set();
  rules.forEach((r, i) => {
    if (!r || typeof r !== 'object') throw new LmPolicyError(`rules[${i}] debe ser un objeto`);
    if (LM_FINAL_RESULTS.indexOf(r.result) < 0) {
      throw new LmPolicyError(`rules[${i}].result invalido: ${JSON.stringify(r.result)}`);
    }
    if (r.attempt !== '*' && !(Number.isInteger(r.attempt) && r.attempt >= 1)) {
      throw new LmPolicyError(`rules[${i}].attempt debe ser entero >= 1 o "*"`);
    }
    if (LM_ACTIONS.indexOf(r.action) < 0) {
      throw new LmPolicyError(`rules[${i}].action invalido: ${JSON.stringify(r.action)}`);
    }
    if (r.action === 'RETRY') {
      if (!r.delay || !LM_DELAY_RE.test(String(r.delay))) {
        throw new LmPolicyError(`rules[${i}]: RETRY exige delay +Nm/+Nh/+Nd/+Nbd`);
      }
    } else if (r.delay !== null && r.delay !== undefined && r.delay !== '') {
      throw new LmPolicyError(`rules[${i}]: delay solo aplica a RETRY`);
    }
    const key = r.result + '|' + r.attempt;
    if (seen.has(key)) {
      throw new LmPolicyError(`regla duplicada para result=${r.result} attempt=${r.attempt}`);
    }
    seen.add(key);
  });
  const cb = policy.callback_default;
  if (cb !== null && cb !== undefined && !LM_DELAY_RE.test(String(cb))) {
    throw new LmPolicyError('callback_default debe ser +Nm/+Nh/+Nd/+Nbd');
  }
  const ua = policy.unmatched_action === undefined ? 'NONE' : policy.unmatched_action;
  if (ua !== 'NONE' && ua !== 'CLOSE') {
    throw new LmPolicyError('unmatched_action solo admite NONE o CLOSE');
  }
  return policy;
}

// +Nm/+Nh = duracion absoluta. +Nd = N dias de calendario a la MISMA hora local
// (respeta DST). +Nbd = N dias habiles lun-vie en el huso del pais, misma hora
// local. Feriados: NO (PENDING_VERIFICATION).
function lmApplyDelay(delay, nowUtc, tzName) {
  const m = LM_DELAY_RE.exec(String(delay || ''));
  if (!m) throw new LmPolicyError('delay invalido: ' + JSON.stringify(delay));
  const n = parseInt(m[1], 10), unit = m[2];
  if (unit === 'm') return new Date(nowUtc.getTime() + n * 60000);
  if (unit === 'h') return new Date(nowUtc.getTime() + n * 3600000);

  const p = lmZonedParts(nowUtc, tzName);
  let y = p.year, mo = p.month, d = p.day;
  if (unit === 'd') {
    const t = new Date(Date.UTC(y, mo - 1, d + n));
    y = t.getUTCFullYear(); mo = t.getUTCMonth() + 1; d = t.getUTCDate();
  } else {                                   // bd
    let added = 0, t = new Date(Date.UTC(y, mo - 1, d));
    while (added < n) {
      t = new Date(t.getTime() + 86400000);
      const wd = lmLocalWeekday(t.getUTCFullYear(), t.getUTCMonth() + 1, t.getUTCDate());
      if (wd < 5) added++;
    }
    y = t.getUTCFullYear(); mo = t.getUTCMonth() + 1; d = t.getUTCDate();
  }
  return lmWallToUtc(y, mo, d, p.hour, p.minute, p.second, tzName);
}

function lmClassifySipCode(policy, sipCode) {
  policy = lmValidatePolicy(policy);
  if (sipCode === null || sipCode === undefined || sipCode === '') return 'UNKNOWN';
  const list = (policy.no_answer_sip_codes || []).map(String);
  return list.indexOf(String(sipCode)) >= 0 ? 'NO_ANSWER' : 'FAILED';
}

function lmFindRule(policy, result, attempt) {
  let exact = null, wildcard = null;
  for (const r of policy.rules) {
    if (r.result !== result) continue;
    if (r.attempt === attempt) exact = r;
    else if (r.attempt === '*') wildcard = r;
  }
  return exact || wildcard;
}

function lmParseTs(v) {
  if (!v) return null;
  const d = (v instanceof Date) ? v : new Date(String(v).replace(' ', 'T'));
  return isNaN(d) ? null : d;
}

// Devuelve el bloque `decision` del FOLLOWUP_ENGINE_CONTRACT.
function lmResolve(policy, attempt, result, nowUtc, tzName, callbackAt, sipCode) {
  policy = lmValidatePolicy(policy);
  tzName = tzName || 'UTC';
  nowUtc = nowUtc || new Date();
  if (sipCode !== null && sipCode !== undefined && sipCode !== '' &&
      (result === 'FAILED' || result === 'UNKNOWN')) {
    result = lmClassifySipCode(policy, sipCode);
  }
  if (result === 'DISPATCHED') {
    throw new LmPolicyError('DISPATCHED no es un resultado final: el motor no se llama con el');
  }
  if (LM_FINAL_RESULTS.indexOf(result) < 0) {
    throw new LmPolicyError('resultado desconocido: ' + JSON.stringify(result));
  }
  if (!Number.isInteger(attempt) || attempt < 1) {
    throw new LmPolicyError('attempt debe ser entero >= 1');
  }

  const aliases = policy.result_aliases || {};
  const effective = aliases[result] || result;
  const rule = lmFindRule(policy, effective, attempt);
  const action = rule ? rule.action : (policy.unmatched_action || 'NONE');

  let schedule = null;
  if (action === 'RETRY') {
    schedule = lmApplyDelay(rule.delay, nowUtc, tzName);
  } else if (action === 'CALLBACK') {
    const cb = lmParseTs(callbackAt);
    schedule = (cb && cb > nowUtc) ? cb
             : lmApplyDelay(policy.callback_default || '+24h', nowUtc, tzName);
  }

  return {
    result: result,
    sip_code: (sipCode === null || sipCode === undefined || sipCode === '') ? null : String(sipCode),
    effective_result: effective,
    attempt: attempt,
    action: action,
    delay: rule ? (rule.delay || null) : null,
    schedule_next_at: schedule ? schedule.toISOString().replace(/\.\d{3}Z$/, 'Z') : null,
    rule_matched: rule ? { result: rule.result, attempt: rule.attempt } : null,
    crm_status_hint: LM_CRM_STATUS_HINT[action] === undefined ? null : LM_CRM_STATUS_HINT[action]
  };
}
