// ── LM CRM NOTES ────────────────────────────────────────────────────────────
// Port 1:1 de app/crm_notes.py. TODO texto humano que se escribe o se envía a
// LeadStudio / CRM va en INGLÉS, sin importar el país, el idioma del cliente ni
// el del agente (docs/CRM_ENGLISH_RULE.md).
//
// NO se traducen: nombre del cliente, teléfono, email, IDs, URLs, route_key,
// IDs de proveedor. Los enums técnicos siguen el contrato de LeadStudio.
//
// tests/test_crm_english_v2.py compara este catálogo con el de Python: si
// alguien edita una frase en un solo lado, el test falla.

const LM_PHRASES = {
  CALL_ANSWERED: 'Call answered.',
  CALL_NO_ANSWER: 'Call was not answered.',
  CALL_BUSY: 'Line was busy.',
  CALL_VOICEMAIL: 'Call reached voicemail.',
  CALL_CALLBACK: 'Customer requested a callback.',
  CALL_WRONG_NUMBER: 'Wrong number.',
  CALL_DNC: 'Customer asked not to be contacted again.',
  CALL_FAILED: 'Call failed.',
  CALL_UNKNOWN: 'Call result could not be determined.',
  FOLLOWUP_RETRY: 'Next attempt scheduled.',
  FOLLOWUP_CLOSE: 'No further attempts: retry policy exhausted.',
  FOLLOWUP_COMPLETE: 'Contact established; no retry scheduled.',
  FOLLOWUP_CALLBACK: 'Callback scheduled.',
  FOLLOWUP_NONE: 'Result recorded; nothing scheduled.',
  ACCOUNT_CREATED: 'Trading account created successfully.',
  ACCOUNT_ALREADY_EXISTS: 'Customer already has a trading account; credentials were not resent (issued once at creation).',
  ACCOUNT_FAILED: 'Trading account could not be created.',
  ACCOUNT_REQUESTED: 'Customer requested a trading account.',
  PAYMENT_LINK_CREATED: 'Payment link sent successfully.',
  PAYMENT_LINK_FAILED: 'Payment link could not be created.',
  PAYMENT_CONFIRMED: 'Payment confirmed.',
  RECORDING_ATTACHED: 'Call recording attached.',
  RECORDING_SKIPPED: 'Call recording not attached: below minimum duration.',
  RECORDING_MISSING: 'Call recording not available.',
  RECONCILIATION_OPENED: 'Automatic reconciliation opened a discrepancy for review.',
  RECONCILIATION_CLOSED: 'Reconciliation discrepancy resolved.',
  ERR_CONFIG: 'Request rejected: country configuration is incomplete.',
  ERR_COUNTRY_DISABLED: 'Request rejected: this country is currently disabled.',
  ERR_TOOL_DISABLED: 'Request rejected: this tool is not enabled for the country.',
  ERR_VALIDATION: 'Request rejected: invalid or missing data.',
  ERR_PROVIDER: 'Request failed at the provider.',
  ERR_AMBIGUOUS: 'Result unconfirmed; flagged for manual reconciliation.'
};

const LM_RESULT_PHRASE = {
  ANSWERED: 'CALL_ANSWERED', NO_ANSWER: 'CALL_NO_ANSWER', BUSY: 'CALL_BUSY',
  VOICEMAIL: 'CALL_VOICEMAIL', CALLBACK: 'CALL_CALLBACK',
  WRONG_NUMBER: 'CALL_WRONG_NUMBER', DNC: 'CALL_DNC', FAILED: 'CALL_FAILED',
  UNKNOWN: 'CALL_UNKNOWN'
};
const LM_ACTION_PHRASE = {
  RETRY: 'FOLLOWUP_RETRY', CLOSE: 'FOLLOWUP_CLOSE', COMPLETE: 'FOLLOWUP_COMPLETE',
  CALLBACK: 'FOLLOWUP_CALLBACK', NONE: 'FOLLOWUP_NONE'
};
const LM_MAX_NOTE = 900;

function lmPhrase(key) {
  if (!(key in LM_PHRASES)) throw new Error('frase desconocida: ' + key);
  return LM_PHRASES[key];
}

function lmJoin(parts) {
  return parts.filter(p => p && String(p).trim())
              .map(p => String(p).trim()).join(' ').slice(0, LM_MAX_NOTE);
}


// Gemelo 1:1 de crm_notes.is_english. Heuristica conservadora: no es un
// detector de idioma, es una red que hace fallar el caso conocido — que
// alguien vuelva a meter "No contesto" o devanagari en una nota del CRM.
var LM_NON_ENGLISH_CHARS = /[\u00e1\u00e9\u00ed\u00f3\u00fa\u00f1\u00fc\u00c1\u00c9\u00cd\u00d3\u00da\u00d1\u00dc\u00bf\u00a1\u0900-\u097f\u0600-\u06ff]/;
var LM_SPANISH_WORDS = new RegExp(
  '\\b(no contest[o\u00f3]|cuenta creada|llamada|intento|pr[o\u00f3]xima|correctamente|' +
  'usuario|contrase[n\u00f1]a|grabaci[o\u00f3]n|pago|enlace|fall[o\u00f3]|d[i\u00ed]as|h[a\u00e1]biles|' +
  'buz[o\u00f3]n|ocupado|desconocido|pendiente|rechazado|creada|enviado)\\b', 'i');

// Palabras funcionales del espanol que casi no aparecen en una nota en
// ingles. Se exigen DOS distintas: con una habria falsos positivos
// ("la carte"), con dos ya es una frase en espanol. NO incluye "son",
// "no", "a", "e", "o" ni "va", que si son palabras inglesas.
var LM_SPANISH_STOPWORDS = new RegExp(
  '\\b(el|la|los|las|una|unos|unas|que|del|por|para|con|sin|como|pero|' +
  'cuando|donde|quiere|quiso|cliente|llamar|llamen|llame|llamada|' +
  'ma[\u00f1n]ana|hoy|ayer|gracias|favor|n[u\u00fa]mero|dijo|pidi[o\u00f3]|' +
  'est[a\u00e1]|tiene|hacer|m[a\u00e1]s|muy|todo|nada|porque|tambi[e\u00e9]n|' +
  'ahora|luego)\\b', 'gi');

function lmIsEnglish(text) {
  var t = String(text === null || text === undefined ? '' : text);
  if (LM_NON_ENGLISH_CHARS.test(t) || LM_SPANISH_WORDS.test(t)) return false;
  var vistas = {}, n = 0, m;
  LM_SPANISH_STOPWORDS.lastIndex = 0;
  while ((m = LM_SPANISH_STOPWORDS.exec(t)) !== null) {
    var w = m[0].toLowerCase();
    if (!vistas[w]) { vistas[w] = true; n++; }
    if (n >= 2) return false;
  }
  return true;
}

// ── Nada que no sea inglés entra al CRM ──────────────────────────────
// Una version anterior adjuntaba el resumen de ElevenLabs tal cual. Eso
// metia hindi, nepali y espanol dentro de notas del CRM. No hay excepcion.
// Gemelo 1:1 de crm_notes.safe_summary / safe_detail / error_code_of.

// lmcore.js ya declara LM_ERROR_CODES (el contrato de errores). Aqui se
// usa esa misma lista mas los tres estados que solo aparecen en notas.
var LM_PUBLISHABLE_CODES = (typeof LM_ERROR_CODES !== 'undefined'
  ? LM_ERROR_CODES : ['CONFIG_ERROR', 'AUTH_ERROR', 'VALIDATION_ERROR',
                      'PROVIDER_ERROR', 'CRM_ERROR', 'RETRYABLE_ERROR',
                      'PERMANENT_ERROR'])
  .concat(['UNKNOWN', 'TIMEOUT', 'AMBIGUOUS']);

// Un token tecnico tiene que PARECER tecnico: MAYUSCULAS, con digito, o
// con separador. Una palabra corriente en minusculas es prosa de alguien.
function lmIsTechnicalToken(t) {
  if (!t || t.length > 64) return false;
  if (!/^[A-Za-z0-9][A-Za-z0-9._:/+-]*$/.test(t)) return false;
  if (t.length > 1 && t === t.toUpperCase() && /[A-Z]/.test(t)) return true;
  if (/[0-9]/.test(t)) return true;
  if (/[._:/+-]/.test(t)) return true;
  return false;
}

function lmSafeDetail(detail) {
  if (detail === null || detail === undefined || detail === '') return null;
  var texto = String(detail).trim();
  if (!texto) return null;
  var partes = texto.split(/\s+/);
  if (partes.length > 4) return null;
  for (var i = 0; i < partes.length; i++) {
    if (!lmIsTechnicalToken(partes[i])) return null;
  }
  return partes.join(' ').slice(0, 64);
}

function lmErrorCodeOf(error) {
  if (error === null || error === undefined || error === '') return 'UNKNOWN';
  var t = String(error).trim().toUpperCase();
  if (LM_PUBLISHABLE_CODES.indexOf(t) >= 0) return t;
  var seguro = lmSafeDetail(error);
  if (seguro && LM_PUBLISHABLE_CODES.indexOf(seguro.toUpperCase()) >= 0) {
    return seguro.toUpperCase();
  }
  return 'PROVIDER_ERROR';
}

// El resumen solo entra si la fuente GARANTIZA que es ingles. Sin idioma
// declarado se descarta, aunque "parezca" ingles: la heuristica existe
// para cazar regresiones, no para autorizar contenido de idioma
// desconocido. El original se guarda como evidencia local.
function lmSafeSummary(summary, language, isEnglishFlag) {
  if (!summary) return null;
  var lang = String(language || '').trim().toLowerCase();
  var declarado = !!isEnglishFlag ||
    lang === 'en' || lang === 'en-us' || lang === 'en-gb' ||
    lang === 'eng' || lang === 'english';
  if (!declarado) return null;
  var texto = String(summary).trim();
  if (!texto) return null;
  if (!lmIsEnglish(texto)) return null;
  return texto;
}

function lmCallNote(o) {
  o = o || {};
  const head = lmPhrase(LM_RESULT_PHRASE[String(o.result || '').toUpperCase()] || 'CALL_UNKNOWN');
  const tail = LM_ACTION_PHRASE[o.action] ? lmPhrase(LM_ACTION_PHRASE[o.action]) : '';
  const meta = [];
  if (o.attempt !== null && o.attempt !== undefined) meta.push('attempt ' + parseInt(o.attempt, 10));
  if (o.route_key) meta.push('route ' + o.route_key);
  if (o.provider) meta.push('provider ' + o.provider);
  if (o.sip_code) meta.push('sip ' + o.sip_code);
  if (o.duration_seconds !== null && o.duration_seconds !== undefined) {
    meta.push('duration ' + parseInt(o.duration_seconds, 10) + 's');
  }
  if (o.schedule_next_at) meta.push('next ' + o.schedule_next_at);
  let note = lmJoin([head, tail, meta.length ? '[' + meta.join(' · ') + ']' : '']);
  var __sum = lmSafeSummary(o.summary, o.summary_language, o.summary_is_english);
  if (__sum) note = (note + ' Agent summary: ' + __sum).slice(0, LM_MAX_NOTE);
  return note;
}

function lmAccountNote(o) {
  o = o || {};
  const key = { CREATED: 'ACCOUNT_CREATED', ALREADY_EXISTS: 'ACCOUNT_ALREADY_EXISTS',
                FAILED: 'ACCOUNT_FAILED' }[String(o.status || '').toUpperCase()];
  if (!key) throw new Error('status de cuenta desconocido: ' + o.status);
  const meta = [];
  if (o.market) meta.push('market ' + o.market);
  if (o.username) meta.push('username ' + o.username);
  if (o.portal_url) meta.push('portal ' + o.portal_url);
  if (o.error) meta.push('code ' + lmErrorCodeOf(o.error));
  return lmJoin([lmPhrase(key), meta.length ? '[' + meta.join(' · ') + ']' : '']);
}

function lmPaymentNote(o) {
  o = o || {};
  const key = { LINK_CREATED: 'PAYMENT_LINK_CREATED', LINK_FAILED: 'PAYMENT_LINK_FAILED',
                CONFIRMED: 'PAYMENT_CONFIRMED' }[String(o.status || '').toUpperCase()];
  if (!key) throw new Error('status de pago desconocido: ' + o.status);
  const meta = [];
  if (o.amount !== null && o.amount !== undefined && o.currency) {
    meta.push('amount ' + o.currency + ' ' + o.amount);
  }
  if (o.provider) meta.push('provider ' + o.provider);
  if (o.order_ref) meta.push('order ' + o.order_ref);
  if (o.transaction_id) meta.push('txn ' + o.transaction_id);
  if (o.error) meta.push('code ' + lmErrorCodeOf(o.error));
  return lmJoin([lmPhrase(key), meta.length ? '[' + meta.join(' · ') + ']' : '']);
}

function lmRecordingNote(o) {
  o = o || {};
  const key = { ATTACHED: 'RECORDING_ATTACHED', SKIPPED: 'RECORDING_SKIPPED',
                MISSING: 'RECORDING_MISSING' }[String(o.status || '').toUpperCase()];
  if (!key) throw new Error('status de grabacion desconocido: ' + o.status);
  const meta = [];
  if (o.duration_seconds !== null && o.duration_seconds !== undefined) {
    meta.push('duration ' + parseInt(o.duration_seconds, 10) + 's');
  }
  if (o.min_secs !== null && o.min_secs !== undefined) {
    meta.push('minimum ' + parseInt(o.min_secs, 10) + 's');
  }
  if (o.route_key) meta.push('route ' + o.route_key);
  return lmJoin([lmPhrase(key), meta.length ? '[' + meta.join(' · ') + ']' : '']);
}

function lmErrorNote(errorCode, detail) {
  const key = { CONFIG_ERROR: 'ERR_CONFIG', COUNTRY_DISABLED: 'ERR_COUNTRY_DISABLED',
                TOOL_DISABLED: 'ERR_TOOL_DISABLED', VALIDATION_ERROR: 'ERR_VALIDATION',
                PROVIDER_ERROR: 'ERR_PROVIDER', AMBIGUOUS: 'ERR_AMBIGUOUS'
              }[String(errorCode || '').toUpperCase()] || 'ERR_VALIDATION';
  const meta = ['code ' + errorCode];
  var __d = lmSafeDetail(detail);
  if (__d) meta.push('detail ' + __d);
  return lmJoin([lmPhrase(key), '[' + meta.join(' · ') + ']']);
}

function lmReconciliationNote(issueType, detail) {
  const meta = ['issue ' + issueType];
  var __d2 = lmSafeDetail(detail);
  if (__d2) meta.push(__d2);
  return lmJoin([lmPhrase('RECONCILIATION_OPENED'), '[' + meta.join(' · ') + ']']);
}
