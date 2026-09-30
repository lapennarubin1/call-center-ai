// ═══════════════════════════════════════════════════════════════════════
//  lmpay — adaptadores de pago para los nodos Code de WF7/WF8
//
//  Gemelo 1:1 de panel/app/payments.py. Las dos implementaciones se
//  comparan en test_payments_v2.py contra la referencia extraída del WF7
//  que corre HOY en producción: si divergen, OkPay rechaza el cobro.
//
//  Ninguna credencial vive aquí. mchId y la clave de firma llegan por
//  argumento desde la credential de n8n.
// ═══════════════════════════════════════════════════════════════════════

// MD5 sobre bytes LATIN-1, que es lo que hace el WF7 de producción: su
// implementación empaqueta charCodeAt(i) en un byte por carácter. Firmar
// en UTF-8 daría otra firma y la pasarela la rechazaría.
function lmMd5Latin1(text) {
  const bytes = [];
  for (let i = 0; i < text.length; i++) {
    const c = text.charCodeAt(i);
    if (c > 0xFF) {
      // Por encima de un byte el empaquetado de producción se desborda
      // sobre el byte siguiente y produce una firma corrupta que la
      // pasarela rechazaría igual. Se falla en claro en vez de mandarle
      // al cliente un link que no va a funcionar.
      throw new Error('LM_PAY_NON_LATIN1:' + text.charAt(i));
    }
    bytes.push(c);
  }
  return lmMd5Bytes(bytes);
}

function lmMd5Bytes(bytes) {
  function rh(n) { let s = '', j;
    for (j = 0; j <= 3; j++)
      s += '0123456789abcdef'.charAt((n >> (j * 8 + 4)) & 0xF)
         + '0123456789abcdef'.charAt((n >> (j * 8)) & 0xF);
    return s; }
  function ad(x, y) { const l = (x & 0xFFFF) + (y & 0xFFFF);
    const m = (x >> 16) + (y >> 16) + (l >> 16); return (m << 16) | (l & 0xFFFF); }
  function rl(n, c) { return (n << c) | (n >>> (32 - c)); }
  function cm(q, a, b, x, s, t) { return ad(rl(ad(ad(a, q), ad(x, t)), s), b); }
  function ff(a,b,c,d,x,s,t){return cm((b&c)|((~b)&d),a,b,x,s,t);}
  function gg(a,b,c,d,x,s,t){return cm((b&d)|(c&(~d)),a,b,x,s,t);}
  function hh(a,b,c,d,x,s,t){return cm(b^c^d,a,b,x,s,t);}
  function ii(a,b,c,d,x,s,t){return cm(c^(b|(~d)),a,b,x,s,t);}
  const nblk = ((bytes.length + 8) >> 6) + 1, blks = new Array(nblk * 16).fill(0);
  let i;
  for (i = 0; i < bytes.length; i++) blks[i >> 2] |= bytes[i] << ((i % 4) * 8);
  blks[i >> 2] |= 0x80 << ((i % 4) * 8);
  blks[nblk * 16 - 2] = bytes.length * 8;
  let a = 1732584193, b = -271733879, c = -1732584194, d = 271733878;
  for (i = 0; i < blks.length; i += 16) {
    const olda=a, oldb=b, oldc=c, oldd=d, x=blks;
    a=ff(a,b,c,d,x[i+0],7,-680876936);d=ff(d,a,b,c,x[i+1],12,-389564586);c=ff(c,d,a,b,x[i+2],17,606105819);b=ff(b,c,d,a,x[i+3],22,-1044525330);
    a=ff(a,b,c,d,x[i+4],7,-176418897);d=ff(d,a,b,c,x[i+5],12,1200080426);c=ff(c,d,a,b,x[i+6],17,-1473231341);b=ff(b,c,d,a,x[i+7],22,-45705983);
    a=ff(a,b,c,d,x[i+8],7,1770035416);d=ff(d,a,b,c,x[i+9],12,-1958414417);c=ff(c,d,a,b,x[i+10],17,-42063);b=ff(b,c,d,a,x[i+11],22,-1990404162);
    a=ff(a,b,c,d,x[i+12],7,1804603682);d=ff(d,a,b,c,x[i+13],12,-40341101);c=ff(c,d,a,b,x[i+14],17,-1502002290);b=ff(b,c,d,a,x[i+15],22,1236535329);
    a=gg(a,b,c,d,x[i+1],5,-165796510);d=gg(d,a,b,c,x[i+6],9,-1069501632);c=gg(c,d,a,b,x[i+11],14,643717713);b=gg(b,c,d,a,x[i+0],20,-373897302);
    a=gg(a,b,c,d,x[i+5],5,-701558691);d=gg(d,a,b,c,x[i+10],9,38016083);c=gg(c,d,a,b,x[i+15],14,-660478335);b=gg(b,c,d,a,x[i+4],20,-405537848);
    a=gg(a,b,c,d,x[i+9],5,568446438);d=gg(d,a,b,c,x[i+14],9,-1019803690);c=gg(c,d,a,b,x[i+3],14,-187363961);b=gg(b,c,d,a,x[i+8],20,1163531501);
    a=gg(a,b,c,d,x[i+13],5,-1444681467);d=gg(d,a,b,c,x[i+2],9,-51403784);c=gg(c,d,a,b,x[i+7],14,1735328473);b=gg(b,c,d,a,x[i+12],20,-1926607734);
    a=hh(a,b,c,d,x[i+5],4,-378558);d=hh(d,a,b,c,x[i+8],11,-2022574463);c=hh(c,d,a,b,x[i+11],16,1839030562);b=hh(b,c,d,a,x[i+14],23,-35309556);
    a=hh(a,b,c,d,x[i+1],4,-1530992060);d=hh(d,a,b,c,x[i+4],11,1272893353);c=hh(c,d,a,b,x[i+7],16,-155497632);b=hh(b,c,d,a,x[i+10],23,-1094730640);
    a=hh(a,b,c,d,x[i+13],4,681279174);d=hh(d,a,b,c,x[i+0],11,-358537222);c=hh(c,d,a,b,x[i+3],16,-722521979);b=hh(b,c,d,a,x[i+6],23,76029189);
    a=hh(a,b,c,d,x[i+9],4,-640364487);d=hh(d,a,b,c,x[i+12],11,-421815835);c=hh(c,d,a,b,x[i+15],16,530742520);b=hh(b,c,d,a,x[i+2],23,-995338651);
    a=ii(a,b,c,d,x[i+0],6,-198630844);d=ii(d,a,b,c,x[i+7],10,1126891415);c=ii(c,d,a,b,x[i+14],15,-1416354905);b=ii(b,c,d,a,x[i+5],21,-57434055);
    a=ii(a,b,c,d,x[i+12],6,1700485571);d=ii(d,a,b,c,x[i+3],10,-1894986606);c=ii(c,d,a,b,x[i+10],15,-1051523);b=ii(b,c,d,a,x[i+1],21,-2054922799);
    a=ii(a,b,c,d,x[i+8],6,1873313359);d=ii(d,a,b,c,x[i+15],10,-30611744);c=ii(c,d,a,b,x[i+6],15,-1560198380);b=ii(b,c,d,a,x[i+13],21,1309151649);
    a=ii(a,b,c,d,x[i+4],6,-145523070);d=ii(d,a,b,c,x[i+11],10,-1120210379);c=ii(c,d,a,b,x[i+2],15,718787259);b=ii(b,c,d,a,x[i+9],21,-343485551);
    a=ad(a,olda);b=ad(b,oldb);c=ad(c,oldc);d=ad(d,oldd);
  }
  return rh(a) + rh(b) + rh(c) + rh(d);
}

// Catálogo de adaptadores soportados. Espejo de PAYMENT_ADAPTERS.
var LM_PAY_ADAPTERS = ['OKPAY_V1'];
var LM_PAY_ROUTERS  = ['GENERIC_JSON_V1'];

function lmPayClean(v) { return String(v === null || v === undefined ? '' : v).replace(/=/g, '').trim(); }

// Construye la petición firmada de OkPay. Idéntico al nodo de producción.
function lmOkpayBuild(ctx, mchId, signKey, payType) {
  if (!mchId || !signKey) {
    throw new Error('LM_PAY_NO_CREDENTIAL');
  }
  const params = {
    mchId:        lmPayClean(mchId),
    currency:     lmPayClean(ctx.currency),
    out_trade_no: lmPayClean(ctx.out_trade_no),
    pay_type:     lmPayClean(payType || 'UPI'),
    money:        lmPayClean(ctx.amount),
    notify_url:   lmPayClean(ctx.callback_url),
    returnUrl:    lmPayClean(ctx.return_url),
    phone:        lmPayClean(ctx.phone),
    attach:       lmPayClean(ctx.lead_id),
  };
  const filtered = {};
  Object.keys(params).forEach(function (k) {
    if (params[k] !== '' && params[k] !== null && params[k] !== undefined) filtered[k] = params[k];
  });
  const keys = Object.keys(filtered).sort(function (a, b) {
    const la = a.toLowerCase(), lb = b.toLowerCase();
    return la < lb ? -1 : (la > lb ? 1 : 0);
  });
  const base = keys.map(function (k) { return k + '=' + filtered[k]; }).join('&');
  const sign = lmMd5Latin1(base + '&key=' + String(signKey));
  const parts = keys.map(function (k) {
    return encodeURIComponent(k) + '=' + encodeURIComponent(filtered[k]);
  });
  parts.push('sign=' + sign);
  return { form_body: parts.join('&'), params: filtered, sign: sign,
           content_type: 'application/x-www-form-urlencoded' };
}

// Normaliza la respuesta de OkPay. Éxito sólo con las TRES condiciones.
function lmOkpayParse(http) {
  const status = (http && (http.statusCode !== undefined ? http.statusCode : http.status)) || null;
  let body = (http && http.body) || http || {};
  if (typeof body === 'string') { try { body = JSON.parse(body); } catch (e) { body = {}; } }
  const data = body.data || {};
  const url = data.url || null;
  const txn = data.transaction_Id || data.transactionId || null;
  const ok = !!(status && status >= 200 && status < 300 && body.code === 0 && url);
  if (ok) {
    return { success: true, provider: 'okpay', payment_url: url, payment_id: txn,
             http_status: status, error_class: null,
             crm_message: 'Payment link sent successfully.' };
  }
  let klass;
  if (status === 401 || status === 403) klass = 'AUTH_ERROR';
  else if (!status) klass = 'UNKNOWN';
  else if (status >= 500) klass = 'PROVIDER_ERROR';
  else klass = 'PROVIDER_ERROR';
  return { success: false, provider: 'okpay', payment_url: null, payment_id: txn,
           http_status: status, error_class: klass,
           detail: 'OkPay code=' + body.code + ' http=' + status + ' msg=' + (body.msg || ''),
           crm_message: 'Payment link could not be created.' };
}

// Normaliza la respuesta de un router universal.
function lmRouterParse(http) {
  const status = (http && (http.statusCode !== undefined ? http.statusCode : http.status)) || null;
  let body = (http && http.body) || http || {};
  if (typeof body === 'string') { try { body = JSON.parse(body); } catch (e) { body = {}; } }
  const url = body.payment_url || null;
  const ok = !!(status && status >= 200 && status < 300 && body.success && url);
  if (ok) {
    return { success: true, provider: body.provider || 'router', payment_url: url,
             payment_id: body.payment_id || null, http_status: status, error_class: null,
             crm_message: 'Payment link sent successfully.' };
  }
  let klass;
  if (status === 401 || status === 403) klass = 'AUTH_ERROR';
  else if (!status) klass = 'UNKNOWN';
  else klass = 'PROVIDER_ERROR';
  return { success: false, provider: body.provider || 'router', payment_url: null,
           payment_id: body.payment_id || null, http_status: status, error_class: klass,
           detail: 'Router http=' + status + ' success=' + body.success +
                   ' url=' + (url ? 'yes' : 'no'),
           crm_message: 'Payment link could not be created.' };
}

// Identificador de pedido determinista: el mismo pedido nunca genera dos cobros.
function lmOutTradeNo(leadId, attemptRef) {
  const raw = String(leadId) + ':' + String(attemptRef);
  // FNV-1a de 128 bits en 4 palabras, hex en mayúsculas. Espejo del hash
  // de Python: allí es sha256; aquí el workflow sólo necesita que sea
  // determinista y estable, y el valor autoritativo lo calcula el panel.
  return 'LM' + lmMd5Latin1(raw).slice(0, 24).toUpperCase();
}
