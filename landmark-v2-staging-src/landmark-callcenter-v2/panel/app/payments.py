"""Pagos: dos modos, ninguno inventado (§25).

Lo que se quitó
───────────────
La versión anterior daba por hecho que LeadStudio expone

    POST /api/leads/{id}/payment-link

Nadie lo verificó nunca. El WF7 REAL de producción no lo usa: India cobra
directamente contra OkPay (`api.wpay.one/v1/Collect`) y la rama de Nepal
está marcada `[DISABLED - CONFIGURE]` en el propio workflow. El endpoint
se elimina y NO se sustituye por otra suposición.

Los dos modos
─────────────
DIRECT_PROVIDER
    El país llama a su pasarela con un adaptador soportado. La lógica de
    firma vive AQUÍ, en código revisable, no como JavaScript guardado en
    la base de datos. Hoy hay uno: OKPAY_V1, extraído del WF7 vivo.

UNIVERSAL_ROUTER
    Una API central recibe la petición ya normalizada y decide ella qué
    pasarela usar. Puede ser el backend de Landmark, LeadStudio o el de
    otro cliente: es configuración (`endpoint` + `credential_ref`), no
    está cableado a ningún proveedor.

Nunca hay fallback de un modo al otro. Si el modo configurado no se
puede ejecutar, es CONFIG_ERROR. Caerse al otro modo significaría cobrar
por una vía que nadie autorizó.

Secretos
────────
Ni el merchant id ni la clave de firma se guardan en la base. En el WF7
de producción la clave viaja en claro dentro del JSON del workflow; aquí
llega como argumento desde una credencial de n8n y no se registra en
ningún sitio. Ninguna función de este módulo escribe una clave en un log,
en la auditoría ni en un mensaje de error.
"""
import hashlib
import json
import re
from decimal import Decimal, InvalidOperation


PAYMENT_MODES = ('DIRECT_PROVIDER', 'UNIVERSAL_ROUTER')

# ── Adaptadores soportados ───────────────────────────────────────────
# Añadir un país que use un adaptador que ya está aquí es CONFIGURACIÓN.
# Añadir una pasarela nueva es añadir una entrada aquí + su función de
# construcción. En ninguno de los dos casos se edita el JSON de WF7 (§25).
PAYMENT_ADAPTERS = {
    'OKPAY_V1': {
        'label': 'OkPay / wpay · POST /v1/Collect, MD5-signed form',
        'default_endpoint': 'https://api.wpay.one/v1/Collect',
        'content_type': 'application/x-www-form-urlencoded',
        # Lo que tiene que estar configurado en el país para poder cobrar.
        'requires_config': ['endpoint', 'currency', 'credential_ref',
                            'callback_url'],
        # Lo que tiene que traer la credencial de n8n. NUNCA la base.
        'requires_credential_fields': ['mch_id', 'sign_key'],
        'optional_config': ['return_url', 'pay_type', 'min_amount', 'max_amount'],
        'signer': 'md5_sorted_params',
        'verified_against': 'WF7+WF8 MULTI-PAIS, node "Build OkPay Signed Request"',
    },
}

# Los routers universales no traen lógica de proveedor: sólo transporte.
PAYMENT_ROUTERS = {
    'GENERIC_JSON_V1': {
        'label': 'Generic JSON router · POST {endpoint} with a normalized body',
        'requires_config': ['endpoint', 'credential_ref'],
        'optional_config': ['currency', 'http_method', 'callback_url'],
        'request_shape': {'lead_id': 'str', 'country_iso': 'str',
                          'amount': 'number', 'currency': 'str'},
        'response_shape': {'success': 'bool', 'payment_url': 'str',
                           'payment_id': 'str', 'provider': 'str'},
    },
}

_KEY_RE = re.compile(r'^[A-Z][A-Z0-9_]{2,63}$')
_URL_RE = re.compile(r'^https?://', re.I)


class PaymentConfigError(ValueError):
    """La configuración de pago del país no permite cobrar."""


# ── Validación ───────────────────────────────────────────────────────
def validate_payment_tool(cfg):
    """Problemas de la configuración CREATE_PAYMENT_LINK de un país.

    Lista vacía = se puede cobrar. Igual que el resto del panel: dice qué
    campo falta, no un 'está mal' genérico.

    Un país con el pago DESHABILITADO no genera ningún problema (§25): no
    poder cobrar en Nepal no puede impedir llamar ni abrir cuentas allí.
    """
    issues = []
    if not cfg:
        return issues
    if not cfg.get('enabled'):
        return issues

    mode = (cfg.get('mode') or '').strip().upper()
    if mode not in PAYMENT_MODES:
        issues.append({
            'field': 'payment.mode',
            'message': (f'Payment mode {mode or "(empty)"!r} is not valid. '
                        f'Use one of: {", ".join(PAYMENT_MODES)}.')})
        return issues

    if mode == 'DIRECT_PROVIDER':
        issues += _validate_direct(cfg)
    else:
        issues += _validate_router(cfg)

    amt = _amount_bounds(cfg)
    if amt.get('error'):
        issues.append({'field': 'payment.config_json', 'message': amt['error']})
    return issues


def _validate_direct(cfg):
    issues = []
    ak = (cfg.get('adapter_key') or '').strip()
    if not ak:
        issues.append({'field': 'payment.adapter_key',
                       'message': 'Direct provider mode needs an adapter.'})
        return issues
    if not _KEY_RE.match(ak):
        issues.append({'field': 'payment.adapter_key',
                       'message': f'Invalid adapter key format: {ak!r}.'})
        return issues
    if ak not in PAYMENT_ADAPTERS:
        issues.append({
            'field': 'payment.adapter_key',
            'message': (f'Payment adapter {ak!r} is not supported. '
                        f'Supported: {", ".join(sorted(PAYMENT_ADAPTERS))}. '
                        'It can be stored while disabled, but it cannot run.')})
        return issues

    meta = PAYMENT_ADAPTERS[ak]
    for campo in meta['requires_config']:
        v = (cfg.get(campo) or '').strip() if isinstance(cfg.get(campo), str) \
            else cfg.get(campo)
        if not v:
            issues.append({'field': f'payment.{campo}',
                           'message': f'{_label(campo)} is required by {ak}.'})
    for campo in ('endpoint', 'callback_url', 'return_url'):
        v = (cfg.get(campo) or '').strip()
        if v and not _URL_RE.match(v):
            issues.append({'field': f'payment.{campo}',
                           'message': f'{_label(campo)} must be an http(s) URL.'})
    # El router NO se mezcla con el proveedor directo.
    if (cfg.get('router_key') or '').strip():
        issues.append({
            'field': 'payment.router_key',
            'message': ('A direct provider must not also have a router. '
                        'Clear one of the two — the panel never falls back '
                        'from one mode to the other.')})
    return issues


def _validate_router(cfg):
    issues = []
    rk = (cfg.get('router_key') or '').strip()
    if not rk:
        issues.append({'field': 'payment.router_key',
                       'message': 'Universal router mode needs a router.'})
        return issues
    if rk not in PAYMENT_ROUTERS:
        issues.append({
            'field': 'payment.router_key',
            'message': (f'Payment router {rk!r} is not supported. '
                        f'Supported: {", ".join(sorted(PAYMENT_ROUTERS))}.')})
        return issues
    meta = PAYMENT_ROUTERS[rk]
    for campo in meta['requires_config']:
        if not (cfg.get(campo) or ''):
            issues.append({'field': f'payment.{campo}',
                           'message': f'{_label(campo)} is required by {rk}.'})
    ep = (cfg.get('endpoint') or '').strip()
    if ep and not _URL_RE.match(ep):
        issues.append({'field': 'payment.endpoint',
                       'message': 'Endpoint must be an http(s) URL.'})
    if (cfg.get('adapter_key') or '').strip():
        issues.append({
            'field': 'payment.adapter_key',
            'message': ('A universal router must not also have a direct '
                        'provider adapter. Clear one of the two.')})
    return issues


def _label(campo):
    return {'endpoint': 'Endpoint', 'currency': 'Currency',
            'credential_ref': 'Credential reference',
            'callback_url': 'Callback URL', 'return_url': 'Return URL',
            'router_key': 'Router', 'adapter_key': 'Adapter',
            }.get(campo, campo)


def _amount_bounds(cfg):
    try:
        raw = cfg.get('config_json')
        j = json.loads(raw) if isinstance(raw, str) and raw.strip() else (raw or {})
    except (ValueError, TypeError):
        return {'error': 'config_json is not valid JSON.'}
    if not isinstance(j, dict):
        return {'error': 'config_json must be a JSON object.'}
    out = {}
    for k in ('min_amount', 'max_amount'):
        if j.get(k) is None:
            continue
        try:
            out[k] = Decimal(str(j[k]))
        except (InvalidOperation, ValueError):
            return {'error': f'{k} in config_json is not a number.'}
    if 'min_amount' in out and 'max_amount' in out and out['min_amount'] > out['max_amount']:
        return {'error': 'min_amount is greater than max_amount.'}
    return out


def check_amount(cfg, amount):
    """Valida el importe contra los límites del país. Devuelve None si vale,
    o un mensaje en INGLÉS listo para el CRM si no."""
    b = _amount_bounds(cfg)
    if b.get('error'):
        raise PaymentConfigError(b['error'])
    try:
        a = Decimal(str(amount))
    except (InvalidOperation, ValueError, TypeError):
        return 'The payment amount is not a valid number.'
    if a <= 0:
        return 'The payment amount must be greater than zero.'
    if 'min_amount' in b and a < b['min_amount']:
        return f'The payment amount is below the minimum of {b["min_amount"]}.'
    if 'max_amount' in b and a > b['max_amount']:
        return f'The payment amount is above the maximum of {b["max_amount"]}.'
    return None


# ── Adaptador OKPAY_V1 ───────────────────────────────────────────────
# Portado 1:1 del nodo "Build OkPay Signed Request" del WF7 vivo. El
# algoritmo NO se ha cambiado: si cambiara, la pasarela rechazaría la
# firma. Lo que sí cambia respecto de producción:
#   · la clave de firma llega por argumento, no cableada en el código
#   · el mensaje de error va en inglés (§26). En producción era
#     "Payment link generate karne mein technical issue aaya..."

def _okpay_md5(text):
    """MD5 tal como lo calcula el WF7 de producción.

    Su implementación de MD5 en JavaScript empaqueta `charCodeAt(i)` en un
    byte por carácter. Para todo lo que quepa en un byte eso es exactamente
    MD5 sobre LATIN-1 — no sobre UTF-8. Verificado contra el código real:
    'Ñ-áé' da 5e158e7d... en producción y en latin-1, y 44e189a1... en utf-8.

    Firmar en UTF-8 daría una firma distinta y OkPay rechazaría el cobro.

    Un carácter por encima de U+00FF (devanagari, CJK) no cabe en un byte:
    el desplazamiento de producción se desborda sobre el byte siguiente y
    produce una firma corrupta que la pasarela rechazaría igual. Ahí NO se
    replica el desbordamiento — se falla en claro, con el campo señalado,
    en vez de mandar al cliente un link que no va a funcionar.
    """
    try:
        raw = text.encode('latin-1')
    except UnicodeEncodeError as ex:
        malo = text[ex.start:ex.end]
        raise PaymentConfigError(
            'The payment request contains a character the gateway cannot '
            f'sign: {malo!r}. Only Latin-1 characters are supported in '
            'out_trade_no, lead_id, phone and the URLs.') from None
    return hashlib.md5(raw).hexdigest()


def okpay_build_request(ctx, mch_id, sign_key, pay_type='UPI'):
    """Construye el cuerpo firmado de OkPay.

    ctx: lead_id, currency, out_trade_no, amount, phone, callback_url,
         return_url.
    mch_id / sign_key: vienen de la credencial. No se guardan ni se logean.

    Devuelve {'form_body', 'params', 'sign'} — `sign` es el MD5, que no es
    un secreto; la clave que lo genera nunca sale de aquí.
    """
    if not mch_id or not sign_key:
        raise PaymentConfigError(
            'OkPay needs mch_id and sign_key from its credential. '
            'They are never stored in the database.')

    def clean(v):
        return str(v if v is not None else '').replace('=', '').strip()

    params = {
        'mchId':        clean(mch_id),
        'currency':     clean(ctx.get('currency')),
        'out_trade_no': clean(ctx.get('out_trade_no')),
        'pay_type':     clean(pay_type or 'UPI'),
        'money':        clean(ctx.get('amount')),
        'notify_url':   clean(ctx.get('callback_url')),
        'returnUrl':    clean(ctx.get('return_url')),
        'phone':        clean(ctx.get('phone')),
        'attach':       clean(ctx.get('lead_id')),
    }
    filtered = {k: v for k, v in params.items() if v not in ('', None)}
    # Orden alfabético ignorando mayúsculas — tal cual lo hace producción
    # con localeCompare sobre las claves en minúscula.
    keys = sorted(filtered, key=lambda k: k.lower())
    base = '&'.join(f'{k}={filtered[k]}' for k in keys)
    sign = _okpay_md5(base + '&key=' + str(sign_key))

    from urllib.parse import quote
    partes = [f'{quote(k, safe="")}={quote(str(filtered[k]), safe="")}' for k in keys]
    partes.append('sign=' + sign)
    return {'form_body': '&'.join(partes), 'params': filtered, 'sign': sign,
            'content_type': PAYMENT_ADAPTERS['OKPAY_V1']['content_type']}


def okpay_parse_response(http):
    """Normaliza la respuesta de OkPay al contrato común.

    Éxito SOLO si: HTTP 2xx **y** body.code === 0 **y** hay URL. Las tres.
    Con dos de tres no hay link que mandarle al cliente, así que no es éxito.
    """
    status = http.get('statusCode', http.get('status'))
    body = http.get('body', http) or {}
    if isinstance(body, str):
        try:
            body = json.loads(body)
        except ValueError:
            body = {}
    code = body.get('code')
    data = body.get('data') or {}
    url = data.get('url')
    txn = data.get('transaction_Id') or data.get('transactionId')

    ok = bool(status and 200 <= int(status) < 300 and code == 0 and url)
    if ok:
        return {'success': True, 'provider': 'okpay',
                'payment_url': url, 'payment_id': txn,
                'http_status': status, 'error_class': None,
                # §26: lo que llega al CRM va en inglés, siempre.
                'crm_message': 'Payment link sent successfully.'}

    if status and int(status) in (401, 403):
        klass = 'AUTH_ERROR'
    elif status and int(status) >= 500:
        klass = 'PROVIDER_ERROR'
    elif not status:
        klass = 'UNKNOWN'
    else:
        klass = 'PROVIDER_ERROR'
    return {'success': False, 'provider': 'okpay',
            'payment_url': None, 'payment_id': txn,
            'http_status': status, 'error_class': klass,
            'detail': f'OkPay code={code} http={status} msg={body.get("msg")}',
            'crm_message': 'Payment link could not be created.'}


def router_parse_response(http):
    """Normaliza la respuesta de un UNIVERSAL_ROUTER.

    El router devuelve ya normalizado; aquí sólo se comprueba que cumple
    el contrato. Un router que dice success sin URL NO es un éxito: no hay
    nada que mandarle al cliente.
    """
    status = http.get('statusCode', http.get('status'))
    body = http.get('body', http) or {}
    if isinstance(body, str):
        try:
            body = json.loads(body)
        except ValueError:
            body = {}
    url = body.get('payment_url')
    ok = bool(status and 200 <= int(status) < 300 and body.get('success') and url)
    if ok:
        return {'success': True, 'provider': body.get('provider') or 'router',
                'payment_url': url, 'payment_id': body.get('payment_id'),
                'http_status': status, 'error_class': None,
                'crm_message': 'Payment link sent successfully.'}
    if status and int(status) in (401, 403):
        klass = 'AUTH_ERROR'
    elif status and int(status) >= 500:
        klass = 'PROVIDER_ERROR'
    elif not status:
        klass = 'UNKNOWN'
    elif body.get('success') and not url:
        klass = 'PROVIDER_ERROR'
    else:
        klass = 'PROVIDER_ERROR'
    return {'success': False, 'provider': body.get('provider') or 'router',
            'payment_url': None, 'payment_id': body.get('payment_id'),
            'http_status': status, 'error_class': klass,
            'detail': f'Router http={status} success={body.get("success")} '
                      f'url={"yes" if url else "no"}',
            'crm_message': 'Payment link could not be created.'}


# ── Resolución del modo ──────────────────────────────────────────────
def resolve(cfg):
    """Qué hay que ejecutar para este país. Lanza PaymentConfigError si no
    se puede — nunca devuelve el otro modo como alternativa."""
    if not cfg:
        raise PaymentConfigError('This country has no payment configuration.')
    if not cfg.get('enabled'):
        raise PaymentConfigError('Payment is disabled for this country.')
    issues = validate_payment_tool(cfg)
    if issues:
        raise PaymentConfigError('; '.join(i['message'] for i in issues))

    mode = cfg['mode'].strip().upper()
    if mode == 'DIRECT_PROVIDER':
        ak = cfg['adapter_key']
        return {'mode': mode, 'adapter_key': ak, 'router_key': None,
                'endpoint': cfg.get('endpoint')
                            or PAYMENT_ADAPTERS[ak]['default_endpoint'],
                'credential_ref': cfg.get('credential_ref'),
                'currency': cfg.get('currency'),
                'content_type': PAYMENT_ADAPTERS[ak]['content_type']}
    return {'mode': mode, 'adapter_key': None, 'router_key': cfg['router_key'],
            'endpoint': cfg.get('endpoint'),
            'credential_ref': cfg.get('credential_ref'),
            'currency': cfg.get('currency'),
            'content_type': 'application/json'}


def out_trade_no(lead_id, attempt_ref):
    """Identificador del pedido, determinista.

    Determinista a propósito: si el mismo lead pide el mismo link dos
    veces, sale el mismo out_trade_no, la UNIQUE de wf_payment_orders lo
    rechaza y no se generan dos cobros.
    """
    raw = f'{lead_id}:{attempt_ref}'
    return 'LM' + hashlib.sha256(raw.encode('utf-8')).hexdigest()[:24].upper()
