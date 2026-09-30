"""Facturación de proveedores de voz — UNA identidad en todo el panel.

Por qué existe este módulo
──────────────────────────
El panel tenía dos registros del mismo proveedor comercial:

    SIP Balance   sip_providers · sip_provider_pricing · sip_deposits
    Call Center   voice_providers · call_routes

El usuario tenía que dar de alta Provider1 dos veces, y las dos fichas
podían contradecirse. Aquí se unifican SIN tocar SIP Balance: éste sigue
siendo el dueño de depósitos, precios históricos y saldo; V2 sólo apunta
a su fila con `voice_providers.legacy_sip_provider_id` y lee.

Lo que este módulo NO hace, a propósito:
    · no escribe en sip_deposits ni en sip_provider_pricing
    · no borra ni desactiva nada de v1
    · no calcula el saldo con el proveedor: eso es de SIP Balance

Dos números distintos que nunca se mezclan (§38)
────────────────────────────────────────────────
    CONTRACT RATE   lo que el contrato dice que cuesta. Existe sólo si
                    el modelo tiene una tarifa unitaria real.
    EFFECTIVE COST  una división que hacemos nosotros: cuota fija ÷ uso.
                    Es un indicador de gestión, NO una tarifa. Presentarlo
                    como tarifa sería inventar un contrato que no existe.

Un proveedor MONTHLY_FLAT no tiene precio por minuto. No es cero: es que
la pregunta no aplica. Por eso `price_per_minute_display()` devuelve
'N/A' y nunca '$0/min' — mostrar cero haría creer que el minuto es gratis.
"""
import re
from decimal import Decimal, InvalidOperation


# ── Registro de modelos de facturación ───────────────────────────────
# §35: VARCHAR en la base + este registro en la aplicación. Añadir un
# modelo es tocar este diccionario, no migrar un ENUM.
BILLING_MODELS = {
    'PER_MINUTE': {
        'label': 'Per minute',
        'help': 'Billed on billable minutes at a rate per minute.',
        'needs_unit_rate': True,          # hace falta una tarifa por ruta
        'rate_field': 'price_per_minute',
        'rate_unit': '/min',
        'needs_monthly_fee': False,
        'has_contract_rate': True,
        'usage_label': 'Billable minutes',
    },
    'MONTHLY_FLAT': {
        'label': 'Monthly flat',
        'help': ('Fixed monthly fee, unlimited usage. There is no per-minute '
                 'contract rate — an effective cost can be calculated, but it '
                 'is not a rate.'),
        'needs_unit_rate': False,
        'rate_field': None,
        'rate_unit': None,
        'needs_monthly_fee': True,
        'has_contract_rate': False,
        'usage_label': 'Unlimited',
    },
    'PER_CALL': {
        'label': 'Per call',
        'help': 'Billed on billable calls at a rate per call.',
        'needs_unit_rate': True,
        'rate_field': 'price_per_call',
        'rate_unit': '/call',
        'needs_monthly_fee': False,
        'has_contract_rate': True,
        'usage_label': 'Billable calls',
    },
    'INCLUDED': {
        'label': 'Included',
        'help': 'Bundled in another contract. No separate charge for this provider.',
        'needs_unit_rate': False,
        'rate_field': None,
        'rate_unit': None,
        'needs_monthly_fee': False,
        'has_contract_rate': False,
        'usage_label': 'Included',
    },
    'CUSTOM': {
        'label': 'Custom',
        'help': ('Billed under terms the panel does not model. Costs are not '
                 'calculated; operational metrics are still reported.'),
        'needs_unit_rate': False,
        'rate_field': None,
        'rate_unit': None,
        'needs_monthly_fee': False,
        'has_contract_rate': False,
        'usage_label': 'Per contract',
    },
}
DEFAULT_MODEL = 'PER_MINUTE'
_MODEL_RE = re.compile(r'^[A-Z][A-Z0-9_]{2,31}$')
_CURRENCY_RE = re.compile(r'^[A-Z]{3}$')


class BillingError(ValueError):
    """Configuración de facturación inválida."""


def is_supported(model):
    return model in BILLING_MODELS


def model_meta(model):
    """Metadatos del modelo. Un modelo desconocido no revienta la vista:
    se describe como no soportado para que el panel lo pueda mostrar en
    rojo en vez de caerse."""
    return BILLING_MODELS.get(model) or {
        'label': f'{model} (unsupported)',
        'help': 'This billing model is stored but not supported by the panel.',
        'needs_unit_rate': False, 'rate_field': None, 'rate_unit': None,
        'needs_monthly_fee': False, 'has_contract_rate': False,
        'usage_label': 'Unknown',
    }


def _dec(v):
    if v is None or v == '':
        return None
    try:
        return Decimal(str(v))
    except (InvalidOperation, ValueError):
        return None


# ── Validación ───────────────────────────────────────────────────────
def validate_provider_billing(provider, routes=()):
    """Devuelve la lista de problemas de facturación de un proveedor.

    Lista vacía = facturación completa. Se usa igual que el resto de
    validaciones del panel: alimenta READY / NOT READY con los campos
    concretos que faltan, en vez de un 'está mal' sin detalle.

    IMPORTANTE (§37): a un MONTHLY_FLAT no se le exige tarifa por minuto.
    Pedírsela sería inventar un dato que su contrato no tiene.
    """
    issues = []
    model = (provider.get('billing_model') or '').strip()
    if not model:
        issues.append({'field': 'billing.billing_model',
                       'message': 'Billing model is not set.'})
        return issues
    if not _MODEL_RE.match(model):
        issues.append({'field': 'billing.billing_model',
                       'message': f'Invalid billing model format: {model!r}.'})
        return issues
    if not is_supported(model):
        issues.append({'field': 'billing.billing_model',
                       'message': (f'Billing model {model!r} is not supported. '
                                   f'Supported: {", ".join(sorted(BILLING_MODELS))}.')})
        return issues

    meta = BILLING_MODELS[model]
    cur = (provider.get('billing_currency') or '').strip().upper()
    if not _CURRENCY_RE.match(cur):
        issues.append({'field': 'billing.billing_currency',
                       'message': 'Billing currency must be a 3-letter code, e.g. USD.'})

    if meta['needs_monthly_fee']:
        fee = _dec(provider.get('monthly_fee'))
        if fee is None:
            issues.append({'field': 'billing.monthly_fee',
                           'message': 'Monthly fee is required for a monthly flat provider.'})
        elif fee < 0:
            issues.append({'field': 'billing.monthly_fee',
                           'message': 'Monthly fee cannot be negative.'})

    if meta['needs_unit_rate']:
        field = meta['rate_field']
        sin_tarifa = [r['route_key'] for r in routes
                      if not r.get('archived_at') and _dec(r.get(field)) is None]
        if sin_tarifa:
            issues.append({
                'field': f'billing.{field}',
                'message': ('These routes have no rate, so their cost cannot be '
                            'calculated: ' + ', '.join(sorted(sin_tarifa)))})
        negativas = [r['route_key'] for r in routes
                     if (_dec(r.get(field)) or Decimal(0)) < 0]
        if negativas:
            issues.append({'field': f'billing.{field}',
                           'message': 'Negative rate on: ' + ', '.join(sorted(negativas))})
    return issues


def validate_model_change(model, monthly_fee=None):
    """Valida antes de guardar. Lanza BillingError con un mensaje que se
    puede enseñar tal cual."""
    model = (model or '').strip()
    if not is_supported(model):
        raise BillingError(
            f'Billing model {model!r} is not supported. '
            f'Supported: {", ".join(sorted(BILLING_MODELS))}.')
    if BILLING_MODELS[model]['needs_monthly_fee']:
        fee = _dec(monthly_fee)
        if fee is None or fee < 0:
            raise BillingError('A monthly flat provider needs a monthly fee of 0 or more.')
    return model


# ── Presentación ─────────────────────────────────────────────────────
def price_per_minute_display(provider, route=None):
    """Lo que se pinta en la columna Price/min.

    §37 explícito: para un MONTHLY_FLAT devuelve 'N/A', nunca '$0/min'.
    Cero diría que el minuto no cuesta nada; N/A dice que el contrato no
    se mide en minutos, que es lo cierto.
    """
    meta = model_meta(provider.get('billing_model'))
    if meta['rate_field'] != 'price_per_minute':
        return {'value': None, 'text': 'N/A', 'is_rate': False,
                'why': meta['usage_label']}
    rate = _dec((route or {}).get('price_per_minute'))
    if rate is None:
        return {'value': None, 'text': 'not set', 'is_rate': False,
                'why': 'No rate configured for this route.'}
    cur = (provider.get('billing_currency') or 'USD').upper()
    return {'value': rate, 'text': f'{rate:.4f} {cur}/min', 'is_rate': True,
            'why': 'Contract rate for this route.'}


def usage_display(provider):
    return model_meta(provider.get('billing_model'))['usage_label']


# ── Coste ────────────────────────────────────────────────────────────
def contract_cost(provider, usage, route_rates=None):
    """Coste CONTRACTUAL del período, por modelo (§39).

    usage: {'billable_minutes': N, 'billable_calls': N,
            'by_route': {route_key: {'billable_minutes':N,'billable_calls':N}}}

    Devuelve dict con 'amount' (Decimal o None), 'currency', 'basis' y
    'note'. amount None significa "no se calcula", que NO es lo mismo que
    cero — un CUSTOM sin reglas no cuesta 0, cuesta lo que diga su
    contrato, y el panel no debe fingir que lo sabe.
    """
    model = provider.get('billing_model')
    meta = model_meta(model)
    cur = (provider.get('billing_currency') or 'USD').upper()
    route_rates = route_rates or {}
    out = {'model': model, 'model_label': meta['label'], 'currency': cur,
           'amount': None, 'basis': None, 'note': None,
           'is_contract': True}

    if model == 'PER_MINUTE':
        total, faltan = Decimal('0'), []
        for rk, u in (usage.get('by_route') or {}).items():
            rate = _dec(route_rates.get(rk))
            if rate is None:
                faltan.append(rk)
                continue
            total += rate * Decimal(str(u.get('billable_minutes') or 0))
        out['amount'] = total
        out['basis'] = 'billable minutes × route rate'
        if faltan:
            out['note'] = ('Partial: no rate configured for '
                           + ', '.join(sorted(faltan)))
    elif model == 'PER_CALL':
        total, faltan = Decimal('0'), []
        for rk, u in (usage.get('by_route') or {}).items():
            rate = _dec(route_rates.get(rk))
            if rate is None:
                faltan.append(rk)
                continue
            total += rate * Decimal(str(u.get('billable_calls') or 0))
        out['amount'] = total
        out['basis'] = 'billable calls × route rate'
        if faltan:
            out['note'] = ('Partial: no rate configured for '
                           + ', '.join(sorted(faltan)))
    elif model == 'MONTHLY_FLAT':
        out['amount'] = _dec(provider.get('monthly_fee'))
        out['basis'] = 'fixed monthly fee'
        # Esto es lo que §39 pide dejar claro: la cuota no la generó
        # ninguna llamada concreta. Repartirla por llamada sería inventar.
        out['note'] = ('The fee is owed for the period regardless of usage. '
                       'Individual calls did not generate this amount.')
    elif model == 'INCLUDED':
        out['amount'] = Decimal('0')
        out['basis'] = 'included in another contract'
        out['note'] = 'No separate charge for this provider.'
    else:                                    # CUSTOM o no soportado
        out['amount'] = None
        out['basis'] = 'not modelled'
        out['note'] = 'Cost is not calculated for this billing model.'
    return out


def effective_cost(provider, usage):
    """EFFECTIVE COST — una división nuestra, no una tarifa (§38).

    Sólo tiene sentido cuando hay una cuota fija que repartir. Cada
    resultado viaja con su etiqueta 'EFFECTIVE COST' y con
    is_contract_rate=False, para que quien lo pinte no pueda presentarlo
    por error como el precio del proveedor.
    """
    model = provider.get('billing_model')
    cur = (provider.get('billing_currency') or 'USD').upper()
    base = {'label': 'EFFECTIVE COST', 'is_contract_rate': False,
            'currency': cur, 'model': model,
            'disclaimer': ('Calculated as fixed fee ÷ usage. It is not the '
                           "provider's contract rate.")}
    if model != 'MONTHLY_FLAT':
        return dict(base, applicable=False, items={},
                    note='Effective cost only applies to a fixed-fee provider.')

    fee = _dec(provider.get('monthly_fee'))
    if fee is None:
        return dict(base, applicable=False, items={},
                    note='No monthly fee configured.')

    def div(n):
        n = Decimal(str(n or 0))
        return (fee / n) if n > 0 else None

    return dict(base, applicable=True, fee=fee, items={
        'per_call':          {'value': div(usage.get('calls')),
                              'unit': f'{cur}/call',
                              'basis': 'monthly fee ÷ calls in the billing period'},
        'per_answered_call': {'value': div(usage.get('answered')),
                              'unit': f'{cur}/answered call',
                              'basis': 'monthly fee ÷ answered calls'},
        'per_talk_minute':   {'value': div(usage.get('talk_minutes')),
                              'unit': f'{cur}/talk minute',
                              'basis': 'monthly fee ÷ talk minutes'},
    }, note=None)


# ── Acceso a datos ───────────────────────────────────────────────────
def providers_billing(db):
    """Proveedores V2 con su facturación y su vínculo con SIP Balance."""
    rows = db.q("""
        SELECT vp.id, vp.code, vp.display_name, vp.adapter_key, vp.enabled,
               vp.billing_model, vp.billing_currency, vp.monthly_fee,
               vp.billing_start_date, vp.billing_notes,
               vp.legacy_sip_provider_id,
               (SELECT COUNT(*) FROM call_routes r
                 WHERE r.provider_id = vp.id AND r.archived_at IS NULL) AS route_count
          FROM voice_providers vp
         ORDER BY vp.code""")
    legacy = {}
    if db.table_exists('sip_providers'):
        for r in db.q("SELECT id, name, active, billing_start_date FROM sip_providers"):
            legacy[r['id']] = r
    for r in rows:
        meta = model_meta(r.get('billing_model'))
        r['billing_model_label'] = meta['label']
        r['billing_supported'] = is_supported(r.get('billing_model'))
        r['shows_price_per_minute'] = meta['rate_field'] == 'price_per_minute'
        r['usage_label'] = meta['usage_label']
        r['legacy'] = legacy.get(r.get('legacy_sip_provider_id'))
        # Un proveedor sin vínculo NO es un error: Stringee no es un trunk
        # SIP y nunca tendrá ficha en SIP Balance.
        r['legacy_expected'] = r.get('adapter_key') == 'ELEVENLABS_SIP'
        r['legacy_missing'] = bool(r['legacy_expected'] and not r['legacy'])
    return rows


def route_rates(db, provider_id=None):
    """{route_key: price_per_minute o price_per_call}, según el modelo del
    proveedor de esa ruta."""
    sql = """
        SELECT r.route_key, r.price_per_minute, r.price_per_call,
               vp.billing_model
          FROM call_routes r
          JOIN voice_providers vp ON vp.id = r.provider_id
         WHERE r.archived_at IS NULL"""
    args = ()
    if provider_id is not None:
        sql += " AND r.provider_id = §"
        args = (provider_id,)
    out = {}
    for r in db.q(sql, args):
        field = model_meta(r['billing_model'])['rate_field']
        out[r['route_key']] = r.get(field) if field else None
    return out


def set_provider_billing(db, actor, code, model, currency=None, monthly_fee=None,
                         start_date=None, notes=None, reason=None):
    """Guarda la facturación de un proveedor y deja rastro.

    Sólo escribe en voice_providers. Nunca toca sip_* — apagar o cambiar
    la facturación de un proveedor NO puede perder depósitos ni historial
    (§41).
    """
    cur = db.one("""SELECT id, code, billing_model, billing_currency, monthly_fee,
                           billing_start_date, billing_notes
                      FROM voice_providers WHERE code = §""", (code,))
    if not cur:
        raise BillingError(f'Unknown provider: {code!r}')

    model = validate_model_change(model, monthly_fee)
    currency = (currency or cur.get('billing_currency') or 'USD').strip().upper()
    if not _CURRENCY_RE.match(currency):
        raise BillingError('Billing currency must be a 3-letter code, e.g. USD.')
    # Un modelo sin cuota fija no guarda cuota: dejar un número viejo
    # colgando haría que la UI mostrara una cuota que ya no se cobra.
    fee = _dec(monthly_fee) if BILLING_MODELS[model]['needs_monthly_fee'] else None

    db.execute("""UPDATE voice_providers
                     SET billing_model=§, billing_currency=§, monthly_fee=§,
                         billing_start_date=§, billing_notes=§
                   WHERE code=§""",
               (model, currency, fee, start_date or None, notes or None, code))

    for field, old, new in (('billing_model', cur.get('billing_model'), model),
                            ('billing_currency', cur.get('billing_currency'), currency),
                            ('monthly_fee', cur.get('monthly_fee'), fee),
                            ('billing_start_date', cur.get('billing_start_date'), start_date),
                            ('billing_notes', cur.get('billing_notes'), notes)):
        if str(old or '') != str(new or ''):
            audit(db, actor, 'PROVIDER', code, 'BILLING_UPDATE',
                  field, old, new, reason)
    return True


def set_route_rate(db, actor, route_key, price_per_minute=None,
                   price_per_call=None, notes=None, reason=None):
    """Tarifa de UNA ruta. Dos rutas del mismo país pueden tener tarifas
    distintas: es justo lo que sip_provider_pricing no podía expresar por
    su UNIQUE(provider_id, country) (§36)."""
    cur = db.one("""SELECT r.route_key, r.price_per_minute, r.price_per_call,
                           r.billing_notes, r.archived_at, vp.billing_model
                      FROM call_routes r
                      JOIN voice_providers vp ON vp.id = r.provider_id
                     WHERE r.route_key = §""", (route_key,))
    if not cur:
        raise BillingError(f'Unknown route: {route_key!r}')

    ppm = _dec(price_per_minute)
    ppc = _dec(price_per_call)
    if ppm is not None and ppm < 0:
        raise BillingError('Price per minute cannot be negative.')
    if ppc is not None and ppc < 0:
        raise BillingError('Price per call cannot be negative.')

    db.execute("""UPDATE call_routes
                     SET price_per_minute=§, price_per_call=§, billing_notes=§
                   WHERE route_key=§""", (ppm, ppc, notes or None, route_key))
    for field, old, new in (('price_per_minute', cur.get('price_per_minute'), ppm),
                            ('price_per_call', cur.get('price_per_call'), ppc),
                            ('billing_notes', cur.get('billing_notes'), notes)):
        if str(old or '') != str(new or ''):
            audit(db, actor, 'ROUTE', route_key, 'RATE_UPDATE',
                  field, old, new, reason)
    return True


def link_legacy_provider(db, actor, code, legacy_id, reason=None):
    """Vincula un proveedor V2 con su ficha de SIP Balance, para que el
    usuario no tenga que darlo de alta dos veces (§34)."""
    if not db.table_exists('sip_providers'):
        raise BillingError('SIP Balance tables are not present in this database.')
    if legacy_id in (None, '', '0', 0):
        legacy_id = None
    else:
        legacy_id = int(legacy_id)
        if not db.one("SELECT id FROM sip_providers WHERE id = §", (legacy_id,)):
            raise BillingError(f'No SIP Balance provider with id {legacy_id}.')
        otro = db.one("""SELECT code FROM voice_providers
                          WHERE legacy_sip_provider_id = § AND code <> §""",
                      (legacy_id, code))
        if otro:
            raise BillingError(
                f'That SIP Balance provider is already linked to {otro["code"]!r}. '
                'One SIP Balance provider maps to one voice provider.')
    cur = db.one("SELECT legacy_sip_provider_id FROM voice_providers WHERE code=§", (code,))
    if not cur:
        raise BillingError(f'Unknown provider: {code!r}')
    db.execute("UPDATE voice_providers SET legacy_sip_provider_id=§ WHERE code=§",
               (legacy_id, code))
    audit(db, actor, 'PROVIDER', code, 'LEGACY_LINK', 'legacy_sip_provider_id',
          cur.get('legacy_sip_provider_id'), legacy_id, reason)
    return True


def audit(db, actor, scope, scope_ref, action, field=None,
          old=None, new=None, reason=None):
    """Rastro de un cambio. Nunca guarda credenciales: sólo qué campo
    cambió y entre qué valores."""
    db.execute("""INSERT INTO billing_audit
                    (scope, scope_ref, action, field, old_value, new_value,
                     reason, actor)
                  VALUES (§,§,§,§,§,§,§,§)""",
               (scope, scope_ref, action, field,
                None if old is None else str(old)[:255],
                None if new is None else str(new)[:255],
                (reason or None), actor))


def audit_log(db, scope=None, limit=100):
    sql = ("SELECT scope, scope_ref, action, field, old_value, new_value, "
           "reason, actor, changed_at FROM billing_audit")
    args = ()
    if scope:
        sql += " WHERE scope = §"
        args = (scope,)
    sql += " ORDER BY changed_at DESC, id DESC LIMIT " + str(int(limit))
    return db.q(sql, args)


# ── Alta de proveedor en UN solo flujo (§4) ──────────────────────────
def unlinked_sip_providers(db, for_code=None):
    """Fichas de SIP Balance que todavía no están vinculadas a un
    proveedor V2. Es lo que se ofrece en el selector de vinculación.

    Se incluye la que ya tenga `for_code`, para que su propia ficha
    aparezca seleccionada en vez de desaparecer del desplegable.
    """
    if not db.table_exists('sip_providers'):
        return []
    filas = db.q("""SELECT sp.id, sp.name, sp.active, sp.billing_start_date,
                           (SELECT COUNT(*) FROM sip_provider_pricing p
                             WHERE p.provider_id = sp.id) AS pricing_rows,
                           (SELECT COUNT(*) FROM sip_deposits d
                             WHERE d.provider_id = sp.id) AS deposits,
                           (SELECT vp.code FROM voice_providers vp
                             WHERE vp.legacy_sip_provider_id = sp.id
                             LIMIT 1) AS linked_to
                      FROM sip_providers sp
                     ORDER BY sp.name""")
    return [f for f in filas
            if not f['linked_to'] or (for_code and f['linked_to'] == for_code)]


def orphan_sip_providers(db):
    """Fichas de SIP Balance sin proveedor V2 detrás.

    Existen legítimamente: alguien puede dar de alta un proveedor en SIP
    Balance sólo para llevar su cuenta corriente, sin que llame nadie. La
    pantalla las marca 'Billing-only' con un botón de vincular, en vez de
    crear un duplicado por su cuenta (§4).
    """
    return [f for f in unlinked_sip_providers(db) if not f['linked_to']]


def create_provider(db, actor, code, display_name, adapter_key,
                    billing_model=None, billing_currency=None, monthly_fee=None,
                    endpoint=None, notes=None,
                    legacy_sip_provider_id=None, create_legacy_name=None,
                    reason=None):
    """Da de alta un proveedor V2 y, en la MISMA operación, resuelve su
    identidad de facturación.

    Tres caminos para la parte de SIP Balance:
      · `legacy_sip_provider_id` → vincula una ficha que ya existe
      · `create_legacy_name`     → crea la ficha y la vincula
      · ninguno                  → queda sin vincular (correcto para
                                   Stringee, que no es un trunk SIP)

    Lo que evita: que el usuario tenga que dar de alta el mismo proveedor
    dos veces, una aquí y otra en SIP Balance.

    El proveedor nace APAGADO. Darlo de alta no es encenderlo.
    """
    code = (code or '').strip().lower()
    if not re.fullmatch(r'[a-z][a-z0-9_-]{1,31}', code):
        raise BillingError('Provider code must be lowercase letters, digits, '
                           '"-" or "_", starting with a letter.')
    if db.one("SELECT id FROM voice_providers WHERE code = §", (code,)):
        raise BillingError(f'A provider with code {code!r} already exists.')
    adapter_key = (adapter_key or '').strip().upper()
    if not adapter_key:
        raise BillingError('An adapter is required.')

    modelo = validate_model_change(billing_model or DEFAULT_MODEL, monthly_fee)
    moneda = (billing_currency or 'USD').strip().upper()
    if not _CURRENCY_RE.match(moneda):
        raise BillingError('Billing currency must be a 3-letter code, e.g. USD.')

    legacy_id = None
    if create_legacy_name:
        if not db.table_exists('sip_providers'):
            raise BillingError('SIP Balance tables are not present in this database.')
        nombre = str(create_legacy_name).strip()
        if not nombre:
            raise BillingError('The SIP Balance record needs a name.')
        ya = db.one("SELECT id FROM sip_providers WHERE name = §", (nombre,))
        if ya:
            raise BillingError(
                f'SIP Balance already has a provider named {nombre!r}. '
                'Link that one instead of creating a second record.')
        db.execute("INSERT INTO sip_providers (name, active) VALUES (§, 1)", (nombre,))
        legacy_id = db.one("SELECT id FROM sip_providers WHERE name = §",
                           (nombre,))['id']
    elif legacy_sip_provider_id:
        legacy_id = int(legacy_sip_provider_id)

    fee = _dec(monthly_fee) if BILLING_MODELS[modelo]['needs_monthly_fee'] else None
    db.execute("""INSERT INTO voice_providers
                    (code, display_name, adapter_key, endpoint, enabled,
                     billing_model, billing_currency, monthly_fee, notes)
                  VALUES (§,§,§,§,0,§,§,§,§)""",
               (code, (display_name or code).strip(), adapter_key,
                (endpoint or None), modelo, moneda, fee, (notes or None)))
    audit(db, actor, 'PROVIDER', code, 'CREATE', 'code', None, code, reason)

    if legacy_id is not None:
        # Reutiliza la validación de link_legacy_provider: una ficha de SIP
        # Balance no puede quedar vinculada a dos proveedores V2.
        link_legacy_provider(db, actor, code, legacy_id,
                             reason=reason or 'created with the provider')
    return {'code': code, 'legacy_sip_provider_id': legacy_id, 'enabled': 0}


def adapter_needs_sip_balance(adapter_key):
    """¿Este adaptador factura contra un trunk SIP?

    ELEVENLABS_SIP sí: su consumo se mide en minutos sobre un trunk que
    SIP Balance ya contabiliza. STRINGEE_WORKER no: es cuota mensual y
    nunca tendrá ficha ahí. Pedírsela sería inventar un dato.
    """
    return (adapter_key or '').strip().upper() == 'ELEVENLABS_SIP'
