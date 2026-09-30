"""
crm_notes.py — TODO texto humano que va a LeadStudio, en INGLÉS
================================================================

Regla obligatoria del suite V2 (ver docs/CRM_ENGLISH_RULE.md):

    Cualquier texto legible por una persona que se ESCRIBA o se ENVÍE a
    LeadStudio / CRM va en inglés. Siempre. Sin importar el país, el idioma
    del cliente ni el idioma del agente.

Esto cubre: notas de llamada, de follow-up, de callback, de creación de cuenta,
de pago, de grabación, de reconciliación, los errores legibles que se guardan en
el CRM, descripciones y resúmenes.

NO se traduce (viajan tal cual): nombre del cliente, teléfono, email, IDs, URLs,
route_key, IDs de proveedor. Los valores de enum/estado TÉCNICOS siguen el
contrato de la API de LeadStudio exactamente como lo define, no este módulo.

Por qué un módulo y no strings sueltos
--------------------------------------
v1 mezclaba idiomas dentro del mismo campo `notes` ("🟡 No answer (SIP 603) —
intento 3 (ciclo 1, pos 1/3) — próxima en 2h"), y el mensaje al usuario de WF7
estaba en hinglish. Con un catálogo único: (a) el texto es consistente entre los
siete workflows, (b) un test puede verificar que NO hay español en ninguna nota
que salga hacia el CRM, y (c) los nodos de n8n usan la copia JS generada de
ESTE archivo, así que no pueden divergir.

El equivalente JS embebido en los templates de n8n es una traducción literal de
este módulo; `tests/test_crm_english_v2.py` comprueba que los dos coinciden.
"""

import re

# ── catálogo de frases ────────────────────────────────────────────────
# Clave estable → frase en inglés. Se agrega, no se edita: cambiar una frase
# cambia el historial que ve el operador en el CRM.
PHRASES = {
    # llamadas
    'CALL_ANSWERED':          'Call answered.',
    'CALL_NO_ANSWER':         'Call was not answered.',
    'CALL_BUSY':              'Line was busy.',
    'CALL_VOICEMAIL':         'Call reached voicemail.',
    'CALL_CALLBACK':          'Customer requested a callback.',
    'CALL_WRONG_NUMBER':      'Wrong number.',
    'CALL_DNC':               'Customer asked not to be contacted again.',
    'CALL_FAILED':            'Call failed.',
    'CALL_UNKNOWN':           'Call result could not be determined.',
    # follow-up
    'FOLLOWUP_RETRY':         'Next attempt scheduled.',
    'FOLLOWUP_CLOSE':         'No further attempts: retry policy exhausted.',
    'FOLLOWUP_COMPLETE':      'Contact established; no retry scheduled.',
    'FOLLOWUP_CALLBACK':      'Callback scheduled.',
    'FOLLOWUP_NONE':          'Result recorded; nothing scheduled.',
    # cuentas
    'ACCOUNT_CREATED':        'Trading account created successfully.',
    'ACCOUNT_ALREADY_EXISTS': 'Customer already has a trading account; '
                              'credentials were not resent (issued once at creation).',
    'ACCOUNT_FAILED':         'Trading account could not be created.',
    'ACCOUNT_REQUESTED':      'Customer requested a trading account.',
    # pagos
    'PAYMENT_LINK_CREATED':   'Payment link sent successfully.',
    'PAYMENT_LINK_FAILED':    'Payment link could not be created.',
    'PAYMENT_CONFIRMED':      'Payment confirmed.',
    # grabaciones
    'RECORDING_ATTACHED':     'Call recording attached.',
    'RECORDING_SKIPPED':      'Call recording not attached: below minimum duration.',
    'RECORDING_MISSING':      'Call recording not available.',
    # reconciliación
    'RECONCILIATION_OPENED':  'Automatic reconciliation opened a discrepancy for review.',
    'RECONCILIATION_CLOSED':  'Reconciliation discrepancy resolved.',
    # errores legibles guardados en el CRM
    'ERR_CONFIG':             'Request rejected: country configuration is incomplete.',
    'ERR_COUNTRY_DISABLED':   'Request rejected: this country is currently disabled.',
    'ERR_TOOL_DISABLED':      'Request rejected: this tool is not enabled for the country.',
    'ERR_VALIDATION':         'Request rejected: invalid or missing data.',
    'ERR_PROVIDER':           'Request failed at the provider.',
    'ERR_AMBIGUOUS':          'Result unconfirmed; flagged for manual reconciliation.',
}

RESULT_PHRASE = {
    'ANSWERED': 'CALL_ANSWERED', 'NO_ANSWER': 'CALL_NO_ANSWER', 'BUSY': 'CALL_BUSY',
    'VOICEMAIL': 'CALL_VOICEMAIL', 'CALLBACK': 'CALL_CALLBACK',
    'WRONG_NUMBER': 'CALL_WRONG_NUMBER', 'DNC': 'CALL_DNC', 'FAILED': 'CALL_FAILED',
    'UNKNOWN': 'CALL_UNKNOWN',
}

ACTION_PHRASE = {
    'RETRY': 'FOLLOWUP_RETRY', 'CLOSE': 'FOLLOWUP_CLOSE', 'COMPLETE': 'FOLLOWUP_COMPLETE',
    'CALLBACK': 'FOLLOWUP_CALLBACK', 'NONE': 'FOLLOWUP_NONE',
}

MAX_NOTE = 900          # límite de `notes` en POST /followups (v1 ya recortaba a 900)

# Caracteres que solo aparecen si alguien escribió en español/hindi/etc.
# El test de idioma usa esto además de una lista de palabras.
_NON_ENGLISH_CHARS = re.compile(r'[áéíóúñüÁÉÍÓÚÑÜ¿¡ऀ-ॿ؀-ۿ]')
_SPANISH_WORDS = re.compile(
    r'\b(no contest[oó]|cuenta creada|llamada|intento|pr[oó]xima|correctamente|'
    r'usuario|contrase[nñ]a|grabaci[oó]n|pago|enlace|fall[oó]|d[ií]as|h[aá]biles|'
    r'buz[oó]n|ocupado|desconocido|pendiente|rechazado|creada|enviado)\b', re.I)


class NoteLanguageError(ValueError):
    pass


# Palabras funcionales del español que prácticamente no aparecen en una
# nota de CRM en inglés. Se exigen DOS distintas para declarar que un texto
# no es inglés: con una sola habría falsos positivos ("la carte", "de facto"),
# con dos ya es una frase en español. Deliberadamente NO incluye "son", "no",
# "a", "e", "o" ni "va", que sí son palabras inglesas.
_SPANISH_STOPWORDS = re.compile(
    r'\b(el|la|los|las|una|unos|unas|que|del|por|para|con|sin|como|pero|'
    r'cuando|donde|quiere|quiso|cliente|llamar|llamen|llame|llamada|'
    r'ma[ñn]ana|hoy|ayer|gracias|favor|n[uú]mero|dijo|pidi[oó]|est[aá]|'
    r'tiene|hacer|m[aá]s|muy|todo|nada|porque|tambi[eé]n|ahora|luego)\b',
    re.I)


def is_english(text):
    """Heurística conservadora: detecta el español/hindi/árabe que v1 metía
    en el CRM. No es un detector de idioma general — es una red que hace
    fallar el caso conocido: que alguien vuelva a escribir 'No contestó', o
    que un resumen del agente entre sin traducir.

    Tres señales, cualquiera basta:
      1. caracteres que el inglés no usa (acentos, devanagari, árabe)
      2. una expresión española inequívoca ('no contestó', 'cuenta creada')
      3. DOS palabras funcionales españolas distintas

    La tercera cubre el hueco que dejaban las dos primeras: 'El cliente
    quiere que lo llamen.' no lleva acentos ni ninguna de las expresiones
    de la lista, pero tiene cinco palabras funcionales.
    """
    t = str(text or '')
    if _NON_ENGLISH_CHARS.search(t) or _SPANISH_WORDS.search(t):
        return False
    distintas = {m.group(0).lower() for m in _SPANISH_STOPWORDS.finditer(t)}
    return len(distintas) < 2


def assert_english(text, field='notes'):
    if not is_english(text):
        raise NoteLanguageError(
            f'{field}: el texto que va al CRM debe estar en inglés (CRM_ENGLISH_RULE). '
            f'Recibido: {str(text)[:80]!r}')
    return text


def phrase(key):
    if key not in PHRASES:
        raise KeyError(f'frase desconocida: {key!r}')
    return PHRASES[key]


def _join(parts):
    out = ' '.join(p.strip() for p in parts if p and str(p).strip())
    return out[:MAX_NOTE]


def call_note(result, attempt=None, duration_seconds=None, sip_code=None,
              provider=None, route_key=None, summary=None, action=None,
              schedule_next_at=None, summary_language=None,
              summary_is_english=False):
    """Nota de una llamada para POST /leads/{id}/followups.

    Formato fijo, en inglés, con los datos técnicos como sufijo entre corchetes
    para que sigan siendo legibles y grepables en el CRM:

        Call was not answered. Next attempt scheduled.
        [attempt 3 · route IN_PROVEEDOR1 · provider proveedor1 · sip 603 ·
         next 2026-09-22T04:30:00Z]

    EL RESUMEN NO PASA EN CRUDO.

    Una versión anterior adjuntaba el resumen de ElevenLabs tal cual, con el
    argumento de que era "contenido del cliente". Eso metía hindi, nepalí y
    español dentro de una nota del CRM, que es exactamente lo que la regla
    prohíbe. No hay excepción: TODO texto legible que llega al CRM va en inglés.

    Ahora el resumen sólo se incluye si viene GARANTIZADO en inglés
    (`summary_language='en'` o `summary_is_english=True`), que es el caso
    cuando la fuente expone un campo de resumen en inglés. Si el idioma no
    está garantizado, el resumen NO entra: la nota lleva su texto canónico y
    el original se guarda como evidencia local (`wf_events.metadata_json`),
    donde sigue disponible sin contaminar el CRM.

    No se traduce por nuestra cuenta: inventar una traducción dentro de n8n
    sin un servicio configurado y verificado sería peor que no incluirla.
    """
    head = phrase(RESULT_PHRASE.get(str(result).upper(), 'CALL_UNKNOWN'))
    tail = phrase(ACTION_PHRASE[action]) if action in ACTION_PHRASE else ''
    meta = []
    if attempt is not None:
        meta.append(f'attempt {int(attempt)}')
    if route_key:
        meta.append(f'route {route_key}')
    if provider:
        meta.append(f'provider {provider}')
    if sip_code:
        meta.append(f'sip {sip_code}')
    if duration_seconds is not None:
        meta.append(f'duration {int(duration_seconds)}s')
    if schedule_next_at:
        meta.append(f'next {schedule_next_at}')
    note = _join([head, tail, ('[' + ' · '.join(meta) + ']') if meta else ''])
    resumen = safe_summary(summary, summary_language, summary_is_english)
    if resumen:
        note = (note + ' Agent summary: ' + resumen)[:MAX_NOTE]
    return note


def safe_summary(summary, language=None, is_english_flag=False):
    """El resumen que SÍ puede ir al CRM, o None.

    Tres condiciones, todas necesarias:
      1. la fuente declara que es inglés (`language='en'` o el flag), y
      2. el texto pasa la comprobación de idioma, y
      3. no va vacío

    Un resumen sin idioma declarado se descarta aunque "parezca" inglés: la
    heurística existe para cazar regresiones, no para autorizar contenido de
    idioma desconocido. Descartarlo no pierde nada — el original se guarda
    como evidencia local.
    """
    if not summary:
        return None
    declarado_ingles = bool(is_english_flag) or \
        str(language or '').strip().lower() in ('en', 'en-us', 'en-gb', 'eng', 'english')
    if not declarado_ingles:
        return None
    texto = str(summary).strip()
    if not is_english(texto):
        return None
    return texto


def account_note(status, market=None, username=None, portal_url=None, error=None):
    """status: CREATED | ALREADY_EXISTS | FAILED."""
    key = {'CREATED': 'ACCOUNT_CREATED', 'ALREADY_EXISTS': 'ACCOUNT_ALREADY_EXISTS',
           'FAILED': 'ACCOUNT_FAILED'}.get(str(status).upper())
    if not key:
        raise ValueError(f'status de cuenta desconocido: {status!r}')
    meta = []
    if market:
        meta.append(f'market {market}')
    if username:
        meta.append(f'username {username}')
    if portal_url:
        meta.append(f'portal {portal_url}')
    if error:
        meta.append(f'code {error_code_of(error)}')
    return _join([phrase(key), ('[' + ' · '.join(meta) + ']') if meta else ''])


def payment_note(status, amount=None, currency=None, provider=None, order_ref=None,
                 transaction_id=None, error=None):
    """status: LINK_CREATED | LINK_FAILED | CONFIRMED."""
    key = {'LINK_CREATED': 'PAYMENT_LINK_CREATED', 'LINK_FAILED': 'PAYMENT_LINK_FAILED',
           'CONFIRMED': 'PAYMENT_CONFIRMED'}.get(str(status).upper())
    if not key:
        raise ValueError(f'status de pago desconocido: {status!r}')
    meta = []
    if amount is not None and currency:
        meta.append(f'amount {currency} {amount}')
    if provider:
        meta.append(f'provider {provider}')
    if order_ref:
        meta.append(f'order {order_ref}')
    if transaction_id:
        meta.append(f'txn {transaction_id}')
    if error:
        meta.append(f'code {error_code_of(error)}')
    return _join([phrase(key), ('[' + ' · '.join(meta) + ']') if meta else ''])


def recording_note(status, duration_seconds=None, min_secs=None, route_key=None):
    """status: ATTACHED | SKIPPED | MISSING."""
    key = {'ATTACHED': 'RECORDING_ATTACHED', 'SKIPPED': 'RECORDING_SKIPPED',
           'MISSING': 'RECORDING_MISSING'}.get(str(status).upper())
    if not key:
        raise ValueError(f'status de grabación desconocido: {status!r}')
    meta = []
    if duration_seconds is not None:
        meta.append(f'duration {int(duration_seconds)}s')
    if min_secs is not None:
        meta.append(f'minimum {int(min_secs)}s')
    if route_key:
        meta.append(f'route {route_key}')
    return _join([phrase(key), ('[' + ' · '.join(meta) + ']') if meta else ''])


def error_note(error_code, detail=None):
    """Error legible que se GUARDA en el CRM. El código técnico va aparte."""
    key = {'CONFIG_ERROR': 'ERR_CONFIG', 'COUNTRY_DISABLED': 'ERR_COUNTRY_DISABLED',
           'TOOL_DISABLED': 'ERR_TOOL_DISABLED', 'VALIDATION_ERROR': 'ERR_VALIDATION',
           'PROVIDER_ERROR': 'ERR_PROVIDER', 'AMBIGUOUS': 'ERR_AMBIGUOUS',
           }.get(str(error_code).upper(), 'ERR_VALIDATION')
    meta = [f'code {error_code}']
    # `detail` solía ser texto libre del proveedor. Un "Pago rechazado por
    # banco" de la pasarela entraba así, en español, en una nota del CRM.
    # Ahora sólo pasan identificadores seguros: códigos, IDs, HTTP status.
    seguro = safe_detail(detail)
    if seguro:
        meta.append(f'detail {seguro}')
    return _join([phrase(key), '[' + ' · '.join(meta) + ']'])


# Lo que SÍ puede viajar en un campo técnico del CRM: códigos, enums, ids,
# números, rutas, monedas. Nada con espacios ni letras acentuadas — en el
# momento en que hay una frase, es prosa de alguien y puede no ser inglés.
# Un token técnico tiene que PARECER técnico. Una palabra corriente en
# minúsculas es prosa de alguien, y puede estar en cualquier idioma:
# "Pago rechazado por banco" son cuatro palabras que encajarían en un
# patrón permisivo. Se exige que cada token sea una de estas formas:
#   · MAYÚSCULAS con guiones bajos   PROVIDER_ERROR, AUTH_ERROR
#   · lleve un dígito                HTTP_503, 4001, v2
#   · lleve separador técnico        order:LM123, /v1/Collect, a.b.c
_SAFE_TOKEN = re.compile(r'''(?x)
    ^(?:
        [A-Z][A-Z0-9_]{1,63}                  # CÓDIGO_EN_MAYÚSCULAS
      | [A-Za-z0-9][A-Za-z0-9._:/+-]{0,63}    # con dígito o separador,
    )$                                        # filtrado abajo
''')


def _is_technical(token):
    if not _SAFE_TOKEN.match(token):
        return False
    if token.isupper() and len(token) > 1:
        return True
    if any(c.isdigit() for c in token):
        return True
    if any(c in '._:/+-' for c in token):
        return True
    return False
_ERROR_CODES = ('CONFIG_ERROR', 'AUTH_ERROR', 'PROVIDER_ERROR', 'CRM_ERROR',
                'VALIDATION_ERROR', 'RETRYABLE_ERROR', 'PERMANENT_ERROR',
                'UNKNOWN', 'TIMEOUT', 'AMBIGUOUS')


def safe_detail(detail):
    """Deja pasar un identificador técnico; descarta cualquier prosa.

    Acepta también varios tokens separados por espacio (`PROVIDER_ERROR 503`),
    porque eso sigue siendo técnico y se lee bien en el CRM. En cuanto
    aparece una palabra que no encaja, se descarta TODO: media frase es peor
    que ninguna.
    """
    if detail in (None, ''):
        return None
    texto = str(detail).strip()
    if not texto:
        return None
    partes = texto.split()
    if len(partes) > 4:
        return None
    for t in partes:
        if not _is_technical(t):
            return None
    return ' '.join(partes)[:64]


def error_code_of(error):
    """Convierte lo que sea en un código de error publicable.

    Si `error` ya es uno de los códigos del contrato, se usa. Si es el texto
    crudo del proveedor, se descarta y queda PROVIDER_ERROR: el original vive
    en la evidencia local, no en el CRM.
    """
    if error in (None, ''):
        return 'UNKNOWN'
    texto = str(error).strip().upper()
    for c in _ERROR_CODES:
        if texto == c:
            return c
    seguro = safe_detail(error)
    if seguro and seguro.upper() in _ERROR_CODES:
        return seguro.upper()
    return 'PROVIDER_ERROR'


def reconciliation_note(issue_type, detail=None):
    meta = [f'issue {issue_type}']
    seguro = safe_detail(detail)
    if seguro:
        meta.append(seguro)
    return _join([phrase('RECONCILIATION_OPENED'), '[' + ' · '.join(meta) + ']'])


def all_phrases_english():
    """Usado por los tests: todo el catálogo tiene que pasar is_english()."""
    return {k: v for k, v in PHRASES.items() if not is_english(v)}
