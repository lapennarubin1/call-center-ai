"""
routes_config.py — Configuración multi-país V2.1
================================================

Módulo del Landmark Panel. analytics.py NO se toca.

Modelo
------
  countries               un país: prefijo, huso, idioma (el huso es del país)
  voice_providers         proveedores de voz. Solo llaman.
  followup_policies       políticas reutilizables por clave (STANDARD_CALL_RETRY)
  call_routes             país × proveedor. VARIAS rutas activas por país.
  route_capacity_windows  franjas con capacidad, en hora local del país
  route_telegram_targets  destinos Telegram por ruta y propósito
  country_tool_configs    tools de ElevenLabs por país (CONFIG_ROUTER / CUSTOM_ENDPOINT)
  route_audit             auditoría

Tres interruptores independientes (V2.2)
--------------------------------------
  countries.enabled        PAÍS      India OFF → ninguna ruta de India llama
  voice_providers.enabled  PROVEEDOR PROVEEDOR1 OFF → ninguna ruta suya llama, en ningún país
  call_routes.enabled      RUTA      IN_PROVEEDOR1 OFF → solo esa ruta

  calling_now = país ON y proveedor ON y ruta ON y no archivados
                y ruta READY y país READY y el horario lo permite

  READY es configuración completa; ON/OFF es decisión operativa. Son ejes
  distintos: se puede dejar todo configurado y encender el país al final.

Proveedor ≠ adapter
-------------------
  voice_providers.code        proveedor COMERCIAL (proveedor1, stringee…)
  voice_providers.adapter_key implementación TÉCNICA (ELEVENLABS_SIP,
                              STRINGEE_WORKER…). Un PROVEEDOR2 SIP reutiliza
                              ELEVENLABS_SIP. El catálogo vive en ADAPTERS.

Estados de una ruta
-------------------
  ACTIVE    enabled=1, archived_at NULL
  DISABLED  enabled=0, archived_at NULL
  ARCHIVED  archived_at != NULL  (no se borra nunca; su route_key sigue
            resolviendo para post-calls tardíos y no se reutiliza)

Seguridad operativa
-------------------
  · Activar una ruta pasa por validate_route(): si falta algo → ConfigError.
  · Además, en tiempo de ejecución la API vuelve a validar cada ruta y una
    ruta enabled-pero-inválida devuelve calling_now=false (fail-closed). Así
    ninguna edición posterior (borrar el último chat de Telegram, archivar una
    política, apagar un proveedor, un UPDATE manual en la base) puede dejar
    llamando a una ruta mal configurada.
  · En MySQL el esquema lo crea SOLO la migración. El panel verifica que
    exista y falla con un mensaje claro si no: una única fuente de verdad del
    esquema, sin deriva entre el .sql y el código.
"""

import json
import os
import re
from datetime import datetime, timedelta, timezone as _tz
from zoneinfo import ZoneInfo, available_timezones

import followup_engine as fe

# ══════════════════════════════════════════════════════════════════════
#  Constantes
# ══════════════════════════════════════════════════════════════════════

WEEKDAY_CODES = ['mon', 'tue', 'wed', 'thu', 'fri', 'sat', 'sun']
DAY_MIN = 1440
WEEK_MIN = 7 * DAY_MIN

TELEGRAM_PURPOSES = ['recording', 'account', 'payment', 'alert']
# Catálogo de adapters SOPORTADOS por los templates n8n. adapter_key en la base es
# VARCHAR (no ENUM), pero un adapter que no está acá no puede activarse: no hay
# código en WF2 que sepa llamarlo.
ADAPTERS = {
    'ELEVENLABS_SIP': {
        'label': 'ElevenLabs SIP trunk · POST /v1/convai/sip-trunk/outbound-call',
        'route_requires': ['elevenlabs_agent_id', 'elevenlabs_phone_number_id'],
        'provider_requires_endpoint': False,
        'result_timing': 'inmediato si falla sin conversation_id; si no, post-call',
    },
    'STRINGEE_WORKER': {
        'label': 'Stringee AI worker · POST {endpoint}/call',
        # caller_id: WF2 v1 SIEMPRE envía from_number. Si el worker lo exige es
        # PENDING_VERIFICATION (auditoría del worker); mientras tanto se exige.
        'route_requires': ['elevenlabs_agent_id', 'caller_id'],
        'provider_requires_endpoint': True,
        'result_timing': 'siempre post-call (job_id = solo DISPATCHED)',
    },
}
_ADAPTER_KEY_RE = re.compile(r'^[A-Z][A-Z0-9_]{2,63}$')
_FIELD_LABELS = {
    'elevenlabs_agent_id': 'ElevenLabs Agent ID',
    'elevenlabs_phone_number_id': 'ElevenLabs Phone Number ID',
    'caller_id': 'Caller ID / from_number',
}
TOOL_TYPES = ['CREATE_ACCOUNT', 'CREATE_PAYMENT_LINK', 'CALLBACK']
TOOL_MODES = ['CONFIG_ROUTER', 'CUSTOM_ENDPOINT']
HTTP_METHODS = ['GET', 'POST', 'PUT', 'PATCH']
ROUTE_STATUSES = ['active', 'disabled', 'archived', 'all']

COMMON_TIMEZONES = [
    'Asia/Kolkata', 'Asia/Kathmandu', 'Asia/Dubai', 'Asia/Karachi',
    'America/Mexico_City', 'America/Bogota', 'America/Caracas',
    'Europe/Madrid', 'UTC',
]

_ROUTE_KEY_RE  = re.compile(r'^[A-Z]{2}_[A-Z0-9][A-Z0-9_]{0,60}$')
_ISO_RE        = re.compile(r'^[A-Z]{2}$')
_PREFIX_RE     = re.compile(r'^\+[1-9][0-9]{0,3}$')
_HHMM_RE       = re.compile(r'^([01]\d|2[0-3]):[0-5]\d$')
_CHATID_RE     = re.compile(r'^-?\d{5,20}$')
_LANG_RE       = re.compile(r'^[a-z]{2}(-[a-z0-9]{2,8})?$')
_POLICY_KEY_RE = re.compile(r'^[A-Z][A-Z0-9_]{2,63}$')
_CRED_REF_RE   = re.compile(r'^[A-Z][A-Z0-9_]{2,63}$')

# Nombres de clave que delatan un secreto metido en config_json.
_SECRET_KEY_RE = re.compile(r'(api[_-]?key|secret|password|passwd|token|private[_-]?key|cppwd)',
                            re.I)


class ConfigError(ValueError):
    """Configuración incompleta o inválida. Lleva la lista de faltantes."""

    def __init__(self, message, issues=None):
        super().__init__(message)
        self.issues = issues or []


# ══════════════════════════════════════════════════════════════════════
#  Validadores de campo
# ══════════════════════════════════════════════════════════════════════

def validate_route_key(v):
    v = (v or '').strip().upper()
    if not _ROUTE_KEY_RE.match(v):
        raise ValueError('route_key debe ser ISO_PROVEEDOR en mayúsculas, p. ej. IN_PROVEEDOR1')
    return v


def validate_iso(v):
    v = (v or '').strip().upper()
    if not _ISO_RE.match(v):
        raise ValueError('ISO debe ser de 2 letras, p. ej. IN, NP, MX')
    return v


def validate_prefix(v):
    v = (v or '').strip()
    if not _PREFIX_RE.match(v):
        raise ValueError('dial prefix debe empezar con + y tener 1-4 dígitos, p. ej. +91')
    return v


def validate_timezone(v):
    v = (v or '').strip()
    if v not in available_timezones():
        raise ValueError(f'timezone IANA desconocida: {v!r}')
    return v


def validate_language(v):
    v = (v or '').strip().lower()
    if not _LANG_RE.match(v):
        raise ValueError(f'language inválido: {v!r} (usá "es", "hi", "en"…)')
    return v


def validate_hhmm(v, field):
    v = (v or '').strip()
    if len(v) == 8 and v.count(':') == 2:
        v = v[:5]
    if not _HHMM_RE.match(v):
        raise ValueError(f'{field} debe ser HH:MM en 24h, p. ej. 09:00')
    return v


def validate_days(days):
    days = [d.strip().lower()[:3] for d in (days or []) if d and d.strip()]
    if not days:
        raise ValueError('seleccioná al menos un día')
    for d in days:
        if d not in WEEKDAY_CODES:
            raise ValueError(f'día inválido: {d!r}')
    return [d for d in WEEKDAY_CODES if d in set(days)]


def validate_capacity(v, field='capacity', allow_zero=True):
    try:
        n = int(v)
    except (TypeError, ValueError):
        raise ValueError(f'{field} debe ser un número entero')
    if n < 0 or (n == 0 and not allow_zero):
        raise ValueError(f'{field} debe ser {"> 0" if not allow_zero else ">= 0"}')
    if n > 500:
        raise ValueError(f'{field} > 500 parece un error de tipeo; revisalo')
    return n


def validate_chat_id(v):
    v = (v or '').strip()
    if not _CHATID_RE.match(v):
        raise ValueError(f'chat_id de Telegram inválido: {v!r}')
    return v


def validate_config_json(raw, field='config_json'):
    """Objeto JSON opcional. Rechaza claves con nombre de secreto."""
    if raw in (None, '', b''):
        return None
    if isinstance(raw, dict):
        obj = raw
    else:
        try:
            obj = json.loads(raw)
        except (ValueError, TypeError) as ex:
            raise ValueError(f'{field} no es JSON válido: {ex}')
    if not isinstance(obj, dict):
        raise ValueError(f'{field} debe ser un objeto JSON')

    def walk(o, path):
        if isinstance(o, dict):
            for k, v in o.items():
                if _SECRET_KEY_RE.search(str(k)):
                    raise ValueError(
                        f'{field}.{path}{k}: parece un secreto. Los secretos van en '
                        f'credentials de n8n; acá solo se guarda credential_ref')
                walk(v, f'{path}{k}.')
        elif isinstance(o, list):
            for i, v in enumerate(o):
                walk(v, f'{path}[{i}].')
    walk(obj, '')
    return json.dumps(obj, ensure_ascii=False)


def _json_obj(raw, default=None):
    if raw in (None, ''):
        return default
    try:
        return json.loads(raw) if isinstance(raw, str) else raw
    except (ValueError, TypeError):
        return default


# ══════════════════════════════════════════════════════════════════════
#  Esquema
# ══════════════════════════════════════════════════════════════════════

REQUIRED_TABLES = ['countries', 'voice_providers', 'followup_policies', 'call_routes',
                   'route_capacity_windows', 'route_telegram_targets',
                   'country_tool_configs', 'route_audit']

_SCHEMA_OK = set()


def ensure_routes_tables(db):
    """sqlite: crea el esquema (tests/demo).
    mysql:  VERIFICA que la migración esté aplicada. No crea nada: el único
            DDL de producción es MIGRATION_001 V2.1.
    """
    key = (id(db.conn), db.driver)
    if key in _SCHEMA_OK:
        return
    if db.driver == 'mysql':
        missing = [t for t in REQUIRED_TABLES if not db.table_exists(t)]
        if missing:
            raise ConfigError(
                'Falta aplicar MIGRATION_001_MULTI_COUNTRY_CONFIG_V2_2.sql '
                f'(tablas ausentes: {", ".join(missing)})')
        _SCHEMA_OK.add(key)
        return

    stmts = [
        """CREATE TABLE IF NOT EXISTS countries (
            iso TEXT PRIMARY KEY, country_name TEXT NOT NULL,
            enabled INTEGER NOT NULL DEFAULT 0, dial_prefix TEXT NOT NULL,
            national_number_len INTEGER, timezone TEXT NOT NULL,
            language TEXT NOT NULL DEFAULT 'en', archived_at DATETIME, notes TEXT,
            created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
            updated_at DATETIME DEFAULT CURRENT_TIMESTAMP)""",
        """CREATE TABLE IF NOT EXISTS voice_providers (
            id INTEGER PRIMARY KEY AUTOINCREMENT, code TEXT NOT NULL UNIQUE,
            display_name TEXT NOT NULL, adapter_key TEXT NOT NULL, endpoint TEXT,
            account_ref TEXT, enabled INTEGER NOT NULL DEFAULT 0, notes TEXT,
            created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
            updated_at DATETIME DEFAULT CURRENT_TIMESTAMP)""",
        """CREATE TABLE IF NOT EXISTS followup_policies (
            id INTEGER PRIMARY KEY AUTOINCREMENT, policy_key TEXT NOT NULL UNIQUE,
            name TEXT NOT NULL, description TEXT, policy_json TEXT NOT NULL,
            archived_at DATETIME, created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
            updated_at DATETIME DEFAULT CURRENT_TIMESTAMP)""",
        """CREATE TABLE IF NOT EXISTS call_routes (
            id INTEGER PRIMARY KEY AUTOINCREMENT, route_key TEXT NOT NULL UNIQUE,
            iso TEXT NOT NULL, provider_id INTEGER NOT NULL,
            enabled INTEGER NOT NULL DEFAULT 0, archived_at DATETIME,
            priority INTEGER NOT NULL DEFAULT 100, caller_id TEXT,
            elevenlabs_agent_id TEXT, elevenlabs_phone_number_id TEXT,
            capacity_default INTEGER NOT NULL DEFAULT 1, followup_policy_id INTEGER,
            recording_enabled INTEGER NOT NULL DEFAULT 1,
            recording_min_secs INTEGER NOT NULL DEFAULT 60,
            recording_upload_crm INTEGER NOT NULL DEFAULT 1,
            recording_telegram INTEGER NOT NULL DEFAULT 1, notes TEXT,
            created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
            updated_at DATETIME DEFAULT CURRENT_TIMESTAMP)""",
        """CREATE TABLE IF NOT EXISTS route_capacity_windows (
            id INTEGER PRIMARY KEY AUTOINCREMENT, route_id INTEGER NOT NULL,
            day_mask TEXT NOT NULL DEFAULT 'mon,tue,wed,thu,fri',
            start_local TEXT NOT NULL, end_local TEXT NOT NULL, capacity INTEGER NOT NULL,
            created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
            updated_at DATETIME DEFAULT CURRENT_TIMESTAMP)""",
        """CREATE TABLE IF NOT EXISTS route_telegram_targets (
            id INTEGER PRIMARY KEY AUTOINCREMENT, route_id INTEGER NOT NULL,
            purpose TEXT NOT NULL, chat_id TEXT NOT NULL,
            enabled INTEGER NOT NULL DEFAULT 1,
            created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
            UNIQUE (route_id, purpose, chat_id))""",
        """CREATE TABLE IF NOT EXISTS country_tool_configs (
            id INTEGER PRIMARY KEY AUTOINCREMENT, country_iso TEXT NOT NULL,
            tool_type TEXT NOT NULL, enabled INTEGER NOT NULL DEFAULT 0,
            mode TEXT NOT NULL DEFAULT 'CONFIG_ROUTER', provider_key TEXT, endpoint TEXT,
            http_method TEXT, credential_ref TEXT, market TEXT, currency TEXT,
            config_json TEXT, notes TEXT,
            created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
            updated_at DATETIME DEFAULT CURRENT_TIMESTAMP,
            UNIQUE (country_iso, tool_type))""",
        """CREATE TABLE IF NOT EXISTS route_audit (
            id INTEGER PRIMARY KEY AUTOINCREMENT, route_id INTEGER, route_key TEXT,
            action TEXT NOT NULL, field TEXT, old_value TEXT, new_value TEXT,
            actor TEXT NOT NULL, changed_at DATETIME DEFAULT CURRENT_TIMESTAMP)""",
    ]
    for s in stmts:
        db.execute(s)
    _SCHEMA_OK.add(key)


def _now_sql(db):
    return 'NOW()' if db.driver == 'mysql' else "datetime('now')"


# ══════════════════════════════════════════════════════════════════════
#  Auditoría
# ══════════════════════════════════════════════════════════════════════

def audit(db, actor, action, route_id=None, route_key=None, field=None,
          old_value=None, new_value=None):
    clip = (lambda v: None if v is None else str(v)[:255])
    db.execute(
        "INSERT INTO route_audit (route_id, route_key, action, field, old_value, new_value, actor) "
        "VALUES (§,§,§,§,§,§,§)",
        (route_id, route_key, action, field, clip(old_value), clip(new_value), actor or 'unknown'))


def audit_recent(db, limit=60):
    ensure_routes_tables(db)
    return db.q("SELECT * FROM route_audit ORDER BY id DESC LIMIT §", (int(limit),))


# ══════════════════════════════════════════════════════════════════════
#  Países
# ══════════════════════════════════════════════════════════════════════

def countries_list(db, include_archived=True):
    ensure_routes_tables(db)
    where = '' if include_archived else 'WHERE archived_at IS NULL'
    return db.q(f"SELECT * FROM countries {where} ORDER BY iso")


def country_get(db, iso):
    ensure_routes_tables(db)
    return db.one("SELECT * FROM countries WHERE iso = §", ((iso or '').upper(),))


def country_upsert(db, actor, form):
    ensure_routes_tables(db)
    iso = validate_iso(form.get('iso'))
    name = (form.get('country_name') or '').strip()
    if not name:
        raise ValueError('el nombre del país es obligatorio')
    data = {
        'country_name': name[:64],
        'dial_prefix': validate_prefix(form.get('dial_prefix')),
        'timezone': validate_timezone(form.get('timezone')),
        'language': validate_language(form.get('language') or 'en'),
    }
    nlen = str(form.get('national_number_len') or '').strip()
    if nlen:
        n = int(nlen)
        if not 4 <= n <= 15:
            raise ValueError('national_number_len fuera de rango (4-15)')
        data['national_number_len'] = n
    else:
        data['national_number_len'] = None
    data['notes'] = ((form.get('notes') or '').strip() or None)

    current = country_get(db, iso)
    if not current:
        cols = ['iso'] + list(data)
        db.execute(f"INSERT INTO countries ({','.join(cols)}) VALUES ({','.join(['§'] * len(cols))})",
                   (iso, *data.values()))
        audit(db, actor, 'country_create', None, iso, 'country', None, name)
        return iso

    if current.get('archived_at'):
        raise ValueError(f'{iso} está archivado: desarchivalo antes de editarlo')
    for k, v in data.items():
        if str(current.get(k)) != str(v):
            db.execute(f"UPDATE countries SET {k} = § WHERE iso = §", (v, iso))
            audit(db, actor, 'country_update', None, iso, k, current.get(k), v)
    return iso


def country_set_archived(db, actor, iso, archived):
    c = country_get(db, iso)
    if not c:
        raise ValueError('país inexistente')
    if archived:
        if int(c.get('enabled') or 0):
            raise ValueError(f'{iso} está encendido: apagalo antes de archivarlo')
        active = db.one("SELECT COUNT(*) AS n FROM call_routes "
                        "WHERE iso=§ AND enabled=1 AND archived_at IS NULL", (c['iso'],))
        if active and int(active['n']) > 0:
            raise ValueError(f'{iso} tiene rutas activas: deshabilitalas antes de archivar el país')
        db.execute(f"UPDATE countries SET archived_at = {_now_sql(db)} WHERE iso = §", (c['iso'],))
    else:
        db.execute("UPDATE countries SET archived_at = NULL, enabled = 0 WHERE iso = §", (c['iso'],))
    audit(db, actor, 'country_archive' if archived else 'country_unarchive',
          None, c['iso'], 'archived', c.get('archived_at'), 'yes' if archived else None)


def validate_country(db, iso, routes=None):
    """COUNTRY READY = datos del país válidos + todas sus tools ENABLED válidas.

    Regla de activación (definida, V2.2): un país se puede ENCENDER sin ninguna
    ruta READY. Sirve para staging: se enciende el país y las rutas se van
    activando de a una. Encendido y sin rutas READY simplemente no llama; el
    panel lo muestra como advertencia, no como error.
    """
    issues = []
    add = (lambda code, field, msg: issues.append({'code': code, 'field': field, 'message': msg}))
    c = country_get(db, iso)
    if not c:
        return {'ready': False, 'issues': [{'code': 'MISSING', 'field': 'country',
                                            'message': f'país {iso!r} inexistente'}],
                'routes_total': 0, 'routes_ready': 0, 'tools_ready': False, 'tool_issues': []}
    if c.get('archived_at'):
        add('ARCHIVED', 'country', f"{c['iso']} está archivado")
    for fn, field in ((validate_iso, 'iso'), (validate_prefix, 'dial_prefix'),
                      (validate_timezone, 'timezone'), (validate_language, 'language')):
        try:
            fn(c.get(field))
        except ValueError as ex:
            add('INVALID', f'country.{field}', str(ex))
    tool_issues = []
    for t in tools_for_country(db, c['iso']):
        tool_issues += validate_tool(t)
    issues += tool_issues

    if routes is None:
        routes = [r for r in routes_list(db, 'all') if r['iso'] == c['iso'] and not r.get('archived_at')]
    ready = sum(1 for r in routes if validate_route(db, r)['ready'])
    return {'ready': not issues, 'issues': issues, 'routes_total': len(routes),
            'routes_ready': ready, 'tools_ready': not tool_issues, 'tool_issues': tool_issues}


def country_set_enabled(db, actor, iso, enabled):
    c = country_get(db, iso)
    if not c:
        raise ValueError('país inexistente')
    if enabled:
        if c.get('archived_at'):
            raise ValueError(f'{iso} está archivado')
        rep = validate_country(db, c['iso'])
        if not rep['ready']:
            raise ConfigError(f"CONFIG_ERROR: país {c['iso']} NOT READY", rep['issues'])
    db.execute("UPDATE countries SET enabled = § WHERE iso = §", (1 if enabled else 0, c['iso']))
    audit(db, actor, 'country_enable' if enabled else 'country_disable', None, c['iso'],
          'enabled', c.get('enabled'), 1 if enabled else 0)


# ══════════════════════════════════════════════════════════════════════
#  Proveedores
# ══════════════════════════════════════════════════════════════════════

def providers_list(db, only_enabled=False):
    ensure_routes_tables(db)
    where = "WHERE enabled = 1" if only_enabled else ""
    return db.q(f"SELECT * FROM voice_providers {where} ORDER BY code")


def provider_get(db, provider_id):
    ensure_routes_tables(db)
    return db.one("SELECT * FROM voice_providers WHERE id = §", (provider_id,))


def validate_provider(p):
    issues = []
    add = (lambda code, field, msg: issues.append({'code': code, 'field': field, 'message': msg}))
    ak = (p.get('adapter_key') or '').strip()
    if ak not in ADAPTERS:
        add('UNKNOWN_ADAPTER', 'provider.adapter_key',
            f"adapter {ak!r} no soportado por los templates (soportados: {', '.join(ADAPTERS)})")
    elif ADAPTERS[ak]['provider_requires_endpoint'] and not (p.get('endpoint') or '').strip():
        add('MISSING', 'provider.endpoint', f"el adapter {ak} necesita endpoint")
    return issues


def provider_upsert(db, actor, code, display_name, adapter_key, endpoint=None,
                    account_ref=None, enabled=False, notes=None):
    """Se puede guardar un proveedor con un adapter todavía no soportado, pero
    solo APAGADO. Encenderlo exige adapter conocido y sus requisitos."""
    ensure_routes_tables(db)
    code = (code or '').strip().lower()
    if not re.match(r'^[a-z0-9_-]{2,32}$', code):
        raise ValueError('código de proveedor inválido (a-z, 0-9, _ , -)')
    adapter_key = (adapter_key or '').strip().upper()
    if not _ADAPTER_KEY_RE.match(adapter_key):
        raise ValueError('adapter_key: MAYÚSCULAS, dígitos y _, p. ej. ELEVENLABS_SIP')
    data = {'display_name': (display_name or code)[:64], 'adapter_key': adapter_key,
            'endpoint': (endpoint or '').strip() or None,
            'account_ref': (account_ref or '').strip() or None,
            'enabled': 1 if enabled else 0, 'notes': notes or None}
    if data['enabled']:
        issues = validate_provider(data)
        if issues:
            raise ConfigError(f'CONFIG_ERROR: proveedor {code} no puede encenderse', issues)

    existing = db.one("SELECT * FROM voice_providers WHERE code = §", (code,))
    if existing:
        for k, v in data.items():
            if str(existing.get(k)) != str(v):
                db.execute(f"UPDATE voice_providers SET {k} = § WHERE id = §", (v, existing['id']))
                act = ('provider_enable' if v else 'provider_disable') if k == 'enabled' else 'provider_update'
                audit(db, actor, act, None, code, k, existing.get(k), v)
        return existing['id']
    pid, _ = db.execute(
        "INSERT INTO voice_providers (code, display_name, adapter_key, endpoint, account_ref, enabled, notes) "
        "VALUES (§,§,§,§,§,§,§)", (code, *data.values()))
    audit(db, actor, 'provider_create', None, code, 'adapter_key', None, adapter_key)
    return pid


def provider_set_enabled(db, actor, provider_id, enabled):
    p = provider_get(db, provider_id)
    if not p:
        raise ValueError('proveedor inexistente')
    return provider_upsert(db, actor, p['code'], p['display_name'], p['adapter_key'],
                           p.get('endpoint'), p.get('account_ref'), enabled, p.get('notes'))


# ══════════════════════════════════════════════════════════════════════
#  Políticas de follow-up (reutilizables, sin borrado físico)
# ══════════════════════════════════════════════════════════════════════

def policies_list(db, include_archived=True):
    ensure_routes_tables(db)
    where = '' if include_archived else 'WHERE archived_at IS NULL'
    rows = db.q(f"SELECT * FROM followup_policies {where} ORDER BY policy_key")
    for r in rows:
        r['policy'] = _json_obj(r['policy_json'], {})
    return rows


def policy_get(db, policy_id):
    ensure_routes_tables(db)
    return db.one("SELECT * FROM followup_policies WHERE id = §", (policy_id,))


def policy_upsert(db, actor, policy_key, name, policy_json, description=None):
    ensure_routes_tables(db)
    key = (policy_key or '').strip().upper()
    if not _POLICY_KEY_RE.match(key):
        raise ValueError('policy_key: mayúsculas, dígitos y _, p. ej. STANDARD_CALL_RETRY')
    try:
        policy = fe.validate_policy(policy_json)
    except fe.PolicyError as ex:
        raise ValueError(f'política inválida: {ex}')
    serialized = json.dumps(policy, ensure_ascii=False)
    current = db.one("SELECT * FROM followup_policies WHERE policy_key = §", (key,))
    if current:
        if current.get('archived_at'):
            raise ValueError(f'{key} está archivada; creá una nueva clave')
        db.execute("UPDATE followup_policies SET name=§, description=§, policy_json=§ WHERE id=§",
                   (name or key, description, serialized, current['id']))
        audit(db, actor, 'policy_update', None, key, 'policy_json',
              (current['policy_json'] or '')[:120], serialized[:120])
        return current['id']
    pid, _ = db.execute("INSERT INTO followup_policies (policy_key, name, description, policy_json) "
                        "VALUES (§,§,§,§)", (key, name or key, description, serialized))
    audit(db, actor, 'policy_create', None, key, 'policy', None, key)
    return pid


def policy_set_archived(db, actor, policy_id, archived):
    p = policy_get(db, policy_id)
    if not p:
        raise ValueError('política inexistente')
    if archived:
        used = db.q("SELECT route_key FROM call_routes WHERE followup_policy_id=§ "
                    "AND archived_at IS NULL", (policy_id,))
        if used:
            raise ValueError('la política está asignada a rutas no archivadas: '
                             + ', '.join(u['route_key'] for u in used))
        db.execute(f"UPDATE followup_policies SET archived_at = {_now_sql(db)} WHERE id=§",
                   (policy_id,))
    else:
        db.execute("UPDATE followup_policies SET archived_at = NULL WHERE id=§", (policy_id,))
    audit(db, actor, 'policy_archive' if archived else 'policy_unarchive',
          None, p['policy_key'], 'archived', None, 'yes' if archived else None)


# ══════════════════════════════════════════════════════════════════════
#  Tools por país
# ══════════════════════════════════════════════════════════════════════

def tools_for_country(db, iso):
    ensure_routes_tables(db)
    return db.q("SELECT * FROM country_tool_configs WHERE country_iso=§ ORDER BY tool_type",
                ((iso or '').upper(),))


def tools_all(db):
    ensure_routes_tables(db)
    return db.q("SELECT * FROM country_tool_configs ORDER BY country_iso, tool_type")


def tool_upsert(db, actor, iso, tool_type, form):
    """Guarda la config de una tool. Se permite guardar incompleta mientras esté
    disabled; habilitarla exige que pase validate_tool()."""
    ensure_routes_tables(db)
    iso = validate_iso(iso)
    if not country_get(db, iso):
        raise ValueError(f'país inexistente: {iso}')
    if tool_type not in TOOL_TYPES:
        raise ValueError(f'tool_type debe ser uno de {TOOL_TYPES}')
    mode = (form.get('mode') or 'CONFIG_ROUTER').strip().upper()
    if mode not in TOOL_MODES:
        raise ValueError(f'mode debe ser uno de {TOOL_MODES}')

    method = (form.get('http_method') or '').strip().upper() or None
    # Se valida TAL CUAL se escribió, sin pasar a mayúsculas: un secreto real
    # (sk_live_…, tokens mixtos) no cumple el formato de nombre lógico.
    cred = (form.get('credential_ref') or '').strip() or None
    currency = (form.get('currency') or '').strip().upper() or None
    data = {
        'enabled': 1 if str(form.get('enabled')) in ('1', 'true', 'on', 'yes') else 0,
        'mode': mode,
        'provider_key': (form.get('provider_key') or '').strip().lower() or None,
        'endpoint': (form.get('endpoint') or '').strip() or None,
        'http_method': method,
        'credential_ref': cred,
        'market': (form.get('market') or '').strip().upper() or None,
        'currency': currency,
        'config_json': validate_config_json(form.get('config_json')),
        'notes': (form.get('notes') or '').strip() or None,
    }
    if cred and not _CRED_REF_RE.match(cred):
        raise ValueError('credential_ref es un NOMBRE lógico (p. ej. PAKISTAN_ACCOUNT_API), '
                         'no el secreto')
    if currency and not re.match(r'^[A-Z]{3}$', currency):
        raise ValueError('currency debe ser un código ISO de 3 letras')

    if data['enabled']:
        issues = validate_tool({**data, 'tool_type': tool_type, 'country_iso': iso})
        if issues:
            raise ConfigError(f'{iso} {tool_type}: configuración incompleta', issues)

    current = db.one("SELECT * FROM country_tool_configs WHERE country_iso=§ AND tool_type=§",
                     (iso, tool_type))
    if current:
        for k, v in data.items():
            if str(current.get(k)) != str(v):
                db.execute(f"UPDATE country_tool_configs SET {k}=§ WHERE id=§", (v, current['id']))
                audit(db, actor, 'tool_update', None, iso, f'{tool_type}.{k}', current.get(k), v)
        return current['id']
    cols = ['country_iso', 'tool_type'] + list(data)
    tid, _ = db.execute(
        f"INSERT INTO country_tool_configs ({','.join(cols)}) VALUES ({','.join(['§'] * len(cols))})",
        (iso, tool_type, *data.values()))
    audit(db, actor, 'tool_create', None, iso, tool_type, None, mode)
    return tid


def validate_tool(tool):
    """Devuelve lista de issues. Una tool DISABLED no se valida (no bloquea nada)."""
    if not int(tool.get('enabled') or 0):
        return []
    t, mode = tool.get('tool_type'), tool.get('mode')
    label = f"{tool.get('country_iso')} {t}"
    issues = []

    def miss(field, msg):
        issues.append({'code': 'TOOL_INCOMPLETE', 'field': f'{t}.{field}', 'message': f'{label}: {msg}'})

    cfg = _json_obj(tool.get('config_json'), {}) or {}
    if mode == 'CONFIG_ROUTER':
        if t in ('CREATE_ACCOUNT', 'CREATE_PAYMENT_LINK') and not tool.get('provider_key'):
            miss('provider_key', 'falta el proveedor (p. ej. cashstudio, okpay)')
        if t == 'CREATE_ACCOUNT' and not tool.get('market'):
            miss('market', 'falta el market de CashStudio (p. ej. IND, NPL)')
        if t == 'CREATE_PAYMENT_LINK' and not tool.get('currency'):
            miss('currency', 'falta la moneda')
    elif mode == 'CUSTOM_ENDPOINT':
        ep = tool.get('endpoint') or ''
        if not ep:
            miss('endpoint', 'falta el endpoint')
        elif not ep.startswith('https://'):
            miss('endpoint', 'el endpoint externo debe ser https://')
        if (tool.get('http_method') or '') not in HTTP_METHODS:
            miss('http_method', f'método HTTP debe ser uno de {HTTP_METHODS}')
        if not tool.get('credential_ref'):
            miss('credential_ref', 'falta credential_ref (nombre de la credential en n8n)')
        if t == 'CREATE_PAYMENT_LINK' and not tool.get('currency'):
            miss('currency', 'falta la moneda')
    else:
        miss('mode', f'modo desconocido {mode!r}')

    if t == 'CREATE_PAYMENT_LINK':
        mn, mx = cfg.get('min_amount'), cfg.get('max_amount')
        if mn is not None and mx is not None:
            try:
                if float(mn) > float(mx):
                    miss('config_json', 'min_amount supera max_amount')
            except (TypeError, ValueError):
                miss('config_json', 'min_amount/max_amount deben ser numéricos')
    return issues


# ══════════════════════════════════════════════════════════════════════
#  Rutas
# ══════════════════════════════════════════════════════════════════════

def route_status(r):
    if r.get('archived_at'):
        return 'ARCHIVED'
    return 'ACTIVE' if int(r.get('enabled') or 0) else 'DISABLED'


_ROUTE_FIELDS = ['priority', 'caller_id', 'elevenlabs_agent_id', 'elevenlabs_phone_number_id',
                 'capacity_default', 'followup_policy_id', 'recording_enabled',
                 'recording_min_secs', 'recording_upload_crm', 'recording_telegram', 'notes']


def _clean_route_fields(form):
    out = {}
    has = (lambda k: k in form and form.get(k) is not None)
    if has('capacity_default'):
        out['capacity_default'] = validate_capacity(form.get('capacity_default'), 'capacity_default')
    if has('priority'):
        out['priority'] = int(form.get('priority') or 100)
    for k in ('recording_enabled', 'recording_upload_crm', 'recording_telegram'):
        if has(k):
            out[k] = 1 if str(form.get(k)) in ('1', 'true', 'on', 'yes') or form.get(k) is True else 0
    if has('recording_min_secs'):
        n = int(form.get('recording_min_secs') or 0)
        if not 0 <= n <= 3600:
            raise ValueError('recording_min_secs fuera de rango (0-3600)')
        out['recording_min_secs'] = n
    if has('followup_policy_id'):
        v = str(form.get('followup_policy_id') or '').strip()
        out['followup_policy_id'] = int(v) if v else None
    for k, maxlen in (('caller_id', 32), ('elevenlabs_agent_id', 64),
                      ('elevenlabs_phone_number_id', 64), ('notes', 255)):
        if has(k):
            v = (form.get(k) or '').strip()
            out[k] = v[:maxlen] if v else None
    return out


def route_get(db, route_id):
    ensure_routes_tables(db)
    return db.one("SELECT * FROM call_routes WHERE id = §", (route_id,))


def route_create(db, actor, form):
    """Una ruta nace SIEMPRE deshabilitada. Se puede guardar incompleta."""
    ensure_routes_tables(db)
    iso = validate_iso(form.get('iso'))
    country = country_get(db, iso)
    if not country:
        raise ValueError(f'primero creá el país {iso}')
    if country.get('archived_at'):
        raise ValueError(f'{iso} está archivado')
    provider = provider_get(db, int(form.get('provider_id') or 0))
    if not provider:
        raise ValueError('proveedor inexistente')

    key = (form.get('route_key') or '').strip().upper() or f"{iso}_{provider['code'].upper()}"
    key = validate_route_key(key)
    if not key.startswith(iso + '_'):
        raise ValueError(f'route_key debe empezar con {iso}_')
    existing = db.one("SELECT route_key, archived_at FROM call_routes WHERE route_key=§", (key,))
    if existing:
        if existing.get('archived_at'):
            raise ValueError(f'{key} pertenece a una ruta archivada y no se reutiliza. '
                             f'Usá otra clave, p. ej. {key}_2')
        raise ValueError(f'ya existe la ruta {key}')

    data = _clean_route_fields(form)
    data.setdefault('capacity_default', 1)
    data['enabled'] = 0
    cols = ['route_key', 'iso', 'provider_id'] + list(data)
    rid, _ = db.execute(
        f"INSERT INTO call_routes ({','.join(cols)}) VALUES ({','.join(['§'] * len(cols))})",
        (key, iso, provider['id'], *data.values()))
    audit(db, actor, 'create', rid, key, 'route', None, key)
    return rid


def route_update(db, actor, route_id, form):
    """Si la ruta está ACTIVA, el estado resultante se valida ANTES de escribir:
    no se puede dejar una ruta activa incompleta desde el formulario."""
    ensure_routes_tables(db)
    current = route_get(db, route_id)
    if not current:
        raise ValueError('ruta inexistente')
    if current.get('archived_at'):
        raise ValueError(f"{current['route_key']} está archivada y es de solo lectura")

    data = _clean_route_fields(form)
    changes = {k: v for k, v in data.items() if str(current.get(k)) != str(v)}
    if not changes:
        return 0

    if int(current.get('enabled') or 0):
        prospective = {**current, **changes}
        report = validate_route(db, prospective)
        if not report['ready']:
            raise ConfigError(f"{current['route_key']} está activa: el cambio la dejaría incompleta",
                              report['issues'])

    for field, new in changes.items():
        db.execute(f"UPDATE call_routes SET {field} = § WHERE id = §", (new, route_id))
        audit(db, actor, 'update', route_id, current['route_key'], field, current.get(field), new)
    return len(changes)


def route_set_enabled(db, actor, route_id, enabled):
    current = route_get(db, route_id)
    if not current:
        raise ValueError('ruta inexistente')
    if current.get('archived_at'):
        raise ValueError(f"{current['route_key']} está archivada")
    if enabled:
        report = validate_route(db, current)
        if not report['ready']:
            raise ConfigError(f"CONFIG_ERROR: {current['route_key']} NOT READY", report['issues'])
    db.execute("UPDATE call_routes SET enabled = § WHERE id = §", (1 if enabled else 0, route_id))
    audit(db, actor, 'enable' if enabled else 'disable', route_id, current['route_key'],
          'enabled', current['enabled'], 1 if enabled else 0)


def route_archive(db, actor, route_id):
    """Reemplaza al hard delete. Deshabilita y archiva; nada se borra."""
    current = route_get(db, route_id)
    if not current:
        raise ValueError('ruta inexistente')
    if current.get('archived_at'):
        return
    db.execute(f"UPDATE call_routes SET enabled = 0, archived_at = {_now_sql(db)} WHERE id = §",
               (route_id,))
    audit(db, actor, 'archive', route_id, current['route_key'], 'archived_at', None, 'archived')


def route_unarchive(db, actor, route_id):
    """Vuelve como DISABLED, con su misma configuración. Nunca se reactiva sola."""
    current = route_get(db, route_id)
    if not current or not current.get('archived_at'):
        raise ValueError('la ruta no está archivada')
    db.execute("UPDATE call_routes SET archived_at = NULL, enabled = 0 WHERE id = §", (route_id,))
    audit(db, actor, 'unarchive', route_id, current['route_key'], 'archived_at', 'archived', None)


def routes_list(db, status='all'):
    """Rutas con país, proveedor, política, franjas, Telegram y tools del país.

    status: active | disabled | archived | all
    """
    ensure_routes_tables(db)
    status = (status or 'all').lower()
    if status not in ROUTE_STATUSES:
        status = 'all'
    where = {
        'active': 'WHERE r.enabled = 1 AND r.archived_at IS NULL',
        'disabled': 'WHERE r.enabled = 0 AND r.archived_at IS NULL',
        'archived': 'WHERE r.archived_at IS NOT NULL',
        'all': '',
    }[status]
    rows = db.q(f"""
        SELECT r.*,
               c.country_name, c.dial_prefix, c.national_number_len, c.timezone, c.language,
               c.archived_at AS country_archived_at, c.enabled AS country_enabled,
               p.code AS provider_code, p.display_name AS provider_name, p.adapter_key,
               p.endpoint AS provider_endpoint, p.enabled AS provider_enabled,
               fp.policy_key, fp.policy_json, fp.archived_at AS policy_archived_at
          FROM call_routes r
          LEFT JOIN countries c         ON c.iso = r.iso
          LEFT JOIN voice_providers p   ON p.id = r.provider_id
          LEFT JOIN followup_policies fp ON fp.id = r.followup_policy_id
          {where}
         ORDER BY r.iso, r.priority, r.route_key""")
    _attach_children(db, rows)
    return rows


def route_get_full(db, route_key):
    """Resolución por clave, INCLUIDAS las archivadas (post-call tardío)."""
    key = (route_key or '').strip().upper()
    rows = [r for r in routes_list(db, 'all') if r['route_key'] == key]
    return rows[0] if rows else None


def _attach_children(db, rows):
    if not rows:
        return
    windows = windows_all(db)
    targets = db.q("SELECT * FROM route_telegram_targets ORDER BY route_id, purpose, id")
    tools = tools_all(db)
    for r in rows:
        r['status'] = route_status(r)
        r['windows'] = [w for w in windows if w['route_id'] == r['id']]
        r['telegram_rows'] = [t for t in targets if t['route_id'] == r['id']]
        r['telegram'] = {}
        for t in r['telegram_rows']:
            if int(t.get('enabled', 1)):
                r['telegram'].setdefault(t['purpose'], []).append(t['chat_id'])
        r['tools'] = [t for t in tools if t['country_iso'] == r['iso']]
        r['policy'] = _json_obj(r.get('policy_json'))


# ══════════════════════════════════════════════════════════════════════
#  VALIDACIÓN DE ACTIVACIÓN — el checklist
# ══════════════════════════════════════════════════════════════════════

def validate_route(db, route):
    """ROUTE READY = configuración completa de la ruta. NO mira interruptores:
    una ruta puede estar READY con el país o el proveedor apagados.

    Devuelve {'ready': bool, 'issues': [{code, field, message}]}.
    """
    issues = []
    add = (lambda code, field, msg: issues.append({'code': code, 'field': field, 'message': msg}))

    try:
        validate_route_key(route.get('route_key'))
    except ValueError as ex:
        add('INVALID', 'route_key', str(ex))
    if route.get('archived_at'):
        add('ARCHIVED', 'archived_at', 'la ruta está archivada')

    country = country_get(db, route.get('iso')) if route.get('iso') else None
    if not country:
        add('MISSING', 'country', f"país {route.get('iso')!r} inexistente")
    else:
        if country.get('archived_at'):
            add('ARCHIVED', 'country', f"{country['iso']} está archivado")
        try:
            validate_timezone(country.get('timezone'))
        except ValueError as ex:
            add('INVALID', 'country.timezone', str(ex))

    provider = provider_get(db, route.get('provider_id')) if route.get('provider_id') else None
    adapter = None
    if not provider:
        add('MISSING', 'provider', 'proveedor inexistente')
    else:
        issues += validate_provider(provider)
        adapter = ADAPTERS.get(provider.get('adapter_key'))

    try:
        validate_capacity(route.get('capacity_default'), 'capacity_default', allow_zero=False)
    except ValueError as ex:
        add('INVALID', 'capacity_default', str(ex))
    windows = route.get('windows')
    if windows is None and route.get('id'):
        windows = windows_for_route(db, route['id'])
    for w in windows or []:
        try:
            validate_days(w['days'])
            validate_hhmm(w['start_local'], 'start')
            validate_hhmm(w['end_local'], 'end')
            validate_capacity(w['capacity'])
            if w['start_local'] == w['end_local']:
                raise ValueError('inicio y fin iguales')
        except ValueError as ex:
            add('INVALID', 'schedule', f"franja {w.get('start_local')}-{w.get('end_local')}: {ex}")
    try:
        _assert_no_overlap(windows or [])
    except ValueError as ex:
        add('INVALID', 'schedule', str(ex))

    pid = route.get('followup_policy_id')
    pol = policy_get(db, pid) if pid else None
    if not pol:
        add('MISSING', 'followup_policy', 'falta asignar una política de follow-up')
    else:
        if pol.get('archived_at'):
            add('ARCHIVED', 'followup_policy', f"la política {pol['policy_key']} está archivada")
        try:
            fe.validate_policy(pol['policy_json'])
        except fe.PolicyError as ex:
            add('INVALID', 'followup_policy', f"{pol['policy_key']}: {ex}")

    if int(route.get('recording_enabled') or 0):
        try:
            n = int(route.get('recording_min_secs'))
            if not 0 <= n <= 3600:
                raise ValueError
        except (TypeError, ValueError):
            add('INVALID', 'recording_min_secs', 'debe estar entre 0 y 3600')
        if int(route.get('recording_telegram') or 0):
            targets = route.get('telegram')
            if targets is None and route.get('id'):
                targets = {}
                for t in db.q("SELECT * FROM route_telegram_targets WHERE route_id=§ AND enabled=1",
                              (route['id'],)):
                    targets.setdefault(t['purpose'], []).append(t['chat_id'])
            if not (targets or {}).get('recording'):
                add('MISSING', 'telegram.recording',
                    'grabaciones a Telegram activadas pero sin chat de destino "recording"')
            for purpose, chats in (targets or {}).items():
                for chat in chats:
                    try:
                        validate_chat_id(chat)
                    except ValueError as ex:
                        add('INVALID', f'telegram.{purpose}', str(ex))

    # requisitos del ADAPTER, genéricos: ningún "if sip / if stringee" acá
    if adapter:
        for field in adapter['route_requires']:
            if not (route.get(field) or '').strip():
                add('MISSING', field, f"falta {_FIELD_LABELS.get(field, field)} "
                                      f"(requerido por {provider['adapter_key']})")

    return {'ready': not issues, 'issues': issues}


# ══════════════════════════════════════════════════════════════════════
#  Franjas — modelo de línea de tiempo SEMANAL
#
#  Cada franja se proyecta sobre los 10.080 minutos de la semana
#  (lun 00:00 = 0). Una franja lun 20:00→02:00 ocupa [1200, 1560), o sea
#  hasta el MARTES 02:00 real. Dom 22:00→02:00 da la vuelta al lunes.
#  El solapamiento y la resolución horaria usan exactamente el mismo modelo,
#  así que no pueden contradecirse.
# ══════════════════════════════════════════════════════════════════════

def _as_hhmm(v):
    if isinstance(v, timedelta):
        total = int(v.total_seconds())
        return f"{total // 3600:02d}:{(total % 3600) // 60:02d}"
    return str(v)[:5]


def _minutes(hhmm):
    h, m = str(hhmm).split(':')[:2]
    return int(h) * 60 + int(m)


def week_intervals(days, start, end):
    """Intervalos [desde, hasta) en minutos-de-semana. Parte los que dan la vuelta."""
    s, e = _minutes(start), _minutes(end)
    out = []
    for d in days:
        base = WEEKDAY_CODES.index(d) * DAY_MIN
        a = base + s
        b = base + e if s < e else base + DAY_MIN + e
        if b <= WEEK_MIN:
            out.append((a, b))
        else:
            out.append((a, WEEK_MIN))
            out.append((0, b - WEEK_MIN))
    return out


def _assert_no_overlap(windows, candidate=None, exclude_id=None):
    existing = [w for w in windows if not (exclude_id and w.get('id') == exclude_id)]
    if candidate:
        cand_iv = week_intervals(candidate['days'], candidate['start_local'], candidate['end_local'])
        pairs = [(candidate, cand_iv, w) for w in existing]
    else:
        pairs = []
        for i, w1 in enumerate(existing):
            iv1 = week_intervals(w1['days'], w1['start_local'], w1['end_local'])
            pairs += [(w1, iv1, w2) for w2 in existing[i + 1:]]
    for w1, iv1, w2 in pairs:
        iv2 = week_intervals(w2['days'], w2['start_local'], w2['end_local'])
        for a1, b1 in iv1:
            for a2, b2 in iv2:
                if a1 < b2 and a2 < b1:
                    d = WEEKDAY_CODES[(max(a1, a2) // DAY_MIN) % 7]
                    m = max(a1, a2) % DAY_MIN
                    raise ValueError(
                        f"se solapa con la franja {w2['start_local']}-{w2['end_local']} "
                        f"({','.join(w2['days'])}) desde {d} {m // 60:02d}:{m % 60:02d}")


def windows_all(db):
    ensure_routes_tables(db)
    rows = db.q("SELECT * FROM route_capacity_windows ORDER BY route_id, start_local")
    for w in rows:
        w['start_local'] = _as_hhmm(w['start_local'])
        w['end_local'] = _as_hhmm(w['end_local'])
        w['days'] = [d for d in str(w['day_mask']).split(',') if d]
    return rows


def windows_for_route(db, route_id):
    return [w for w in windows_all(db) if w['route_id'] == route_id]


def _window_input(days, start_local, end_local, capacity):
    day_list = validate_days(days)
    start = validate_hhmm(start_local, 'hora de inicio')
    end = validate_hhmm(end_local, 'hora de fin')
    if start == end:
        raise ValueError('la hora de inicio y la de fin no pueden ser iguales')
    return {'days': day_list, 'start_local': start, 'end_local': end,
            'capacity': validate_capacity(capacity)}


def window_add(db, actor, route_id, days, start_local, end_local, capacity):
    route = route_get(db, route_id)
    if not route:
        raise ValueError('ruta inexistente')
    if route.get('archived_at'):
        raise ValueError('ruta archivada: solo lectura')
    w = _window_input(days, start_local, end_local, capacity)
    _assert_no_overlap(windows_for_route(db, route_id), candidate=w)
    wid, _ = db.execute(
        "INSERT INTO route_capacity_windows (route_id, day_mask, start_local, end_local, capacity) "
        "VALUES (§,§,§,§,§)",
        (route_id, ','.join(w['days']), w['start_local'] + ':00', w['end_local'] + ':00', w['capacity']))
    audit(db, actor, 'window_add', route_id, route['route_key'], 'window', None,
          f"{','.join(w['days'])} {w['start_local']}-{w['end_local']} cap={w['capacity']}")
    return wid


def window_update(db, actor, window_id, days, start_local, end_local, capacity):
    old = db.one("SELECT * FROM route_capacity_windows WHERE id = §", (window_id,))
    if not old:
        raise ValueError('franja inexistente')
    route = route_get(db, old['route_id'])
    if route.get('archived_at'):
        raise ValueError('ruta archivada: solo lectura')
    w = _window_input(days, start_local, end_local, capacity)
    _assert_no_overlap(windows_for_route(db, old['route_id']), candidate=w, exclude_id=window_id)
    db.execute("UPDATE route_capacity_windows SET day_mask=§, start_local=§, end_local=§, capacity=§ "
               "WHERE id=§", (','.join(w['days']), w['start_local'] + ':00',
                              w['end_local'] + ':00', w['capacity'], window_id))
    audit(db, actor, 'window_update', old['route_id'], route['route_key'], 'window',
          f"{old['day_mask']} {_as_hhmm(old['start_local'])}-{_as_hhmm(old['end_local'])} cap={old['capacity']}",
          f"{','.join(w['days'])} {w['start_local']}-{w['end_local']} cap={w['capacity']}")


def window_delete(db, actor, window_id):
    w = db.one("SELECT * FROM route_capacity_windows WHERE id = §", (window_id,))
    if not w:
        raise ValueError('franja inexistente')
    route = route_get(db, w['route_id'])
    if route.get('archived_at'):
        raise ValueError('ruta archivada: solo lectura')
    db.execute("DELETE FROM route_capacity_windows WHERE id = §", (window_id,))
    audit(db, actor, 'window_delete', w['route_id'], route['route_key'], 'window',
          f"{w['day_mask']} {_as_hhmm(w['start_local'])}-{_as_hhmm(w['end_local'])}", None)


# ══════════════════════════════════════════════════════════════════════
#  Telegram
# ══════════════════════════════════════════════════════════════════════

def telegram_add(db, actor, route_id, purpose, chat_id):
    route = route_get(db, route_id)
    if not route:
        raise ValueError('ruta inexistente')
    if route.get('archived_at'):
        raise ValueError('ruta archivada: solo lectura')
    if purpose not in TELEGRAM_PURPOSES:
        raise ValueError(f'purpose debe ser uno de {TELEGRAM_PURPOSES}')
    chat_id = validate_chat_id(chat_id)
    if db.one("SELECT id FROM route_telegram_targets WHERE route_id=§ AND purpose=§ AND chat_id=§",
              (route_id, purpose, chat_id)):
        raise ValueError('ese chat ya está asignado a este propósito')
    db.execute("INSERT INTO route_telegram_targets (route_id, purpose, chat_id) VALUES (§,§,§)",
               (route_id, purpose, chat_id))
    audit(db, actor, 'telegram_add', route_id, route['route_key'], purpose, None, chat_id)


def telegram_delete(db, actor, target_id):
    t = db.one("SELECT * FROM route_telegram_targets WHERE id = §", (target_id,))
    if not t:
        raise ValueError('destino inexistente')
    route = route_get(db, t['route_id'])
    if route.get('archived_at'):
        raise ValueError('ruta archivada: solo lectura')
    db.execute("DELETE FROM route_telegram_targets WHERE id = §", (target_id,))
    audit(db, actor, 'telegram_delete', t['route_id'], route['route_key'], t['purpose'],
          t['chat_id'], None)


# ══════════════════════════════════════════════════════════════════════
#  RESOLUCIÓN HORARIA — zoneinfo, nunca en n8n
# ══════════════════════════════════════════════════════════════════════

def resolve_now(route, windows, now_utc=None):
    """(calling_now, capacity_now, info) — interruptores + horario.

    La READINESS no se evalúa acá (necesita la base); la combina effective().
    """
    now_utc = now_utc or datetime.now(_tz.utc)
    if now_utc.tzinfo is None:
        now_utc = now_utc.replace(tzinfo=_tz.utc)
    try:
        tz = ZoneInfo(route.get('timezone') or '')
    except Exception:
        return False, 0, {'mode': 'error', 'error': f"timezone inválida: {route.get('timezone')!r}",
                          'blocked_by': ['INVALID_TIMEZONE']}

    local = now_utc.astimezone(tz)
    minute_of_week = local.weekday() * DAY_MIN + local.hour * 60 + local.minute
    base = {'local_time': local.strftime('%H:%M'), 'local_day': WEEKDAY_CODES[local.weekday()]}

    blocked = []
    if not int(route.get('country_enabled', 1) or 0):
        blocked.append('COUNTRY_DISABLED')
    if route.get('country_archived_at'):
        blocked.append('COUNTRY_ARCHIVED')
    if not int(route.get('provider_enabled', 1) or 0):
        blocked.append('PROVIDER_DISABLED')
    if not int(route.get('enabled') or 0):
        blocked.append('ROUTE_DISABLED')
    if route.get('archived_at'):
        blocked.append('ROUTE_ARCHIVED')

    cap, info = None, None
    if not windows:
        cap, info = int(route.get('capacity_default') or 0), {**base, 'mode': 'default'}
    else:
        for w in windows:
            for a, b in week_intervals(w['days'], w['start_local'], w['end_local']):
                if a <= minute_of_week < b:
                    cap = int(w['capacity'])
                    info = {**base, 'mode': 'window', 'window_id': w['id'],
                            'window': f"{w['start_local']}-{w['end_local']}", 'days': w['days']}
                    break
            if info:
                break
        if info is None:
            cap, info = 0, {**base, 'mode': 'outside_window'}
            blocked.append('OUTSIDE_SCHEDULE')
    if cap == 0 and 'OUTSIDE_SCHEDULE' not in blocked:
        blocked.append('ZERO_CAPACITY')

    info['blocked_by'] = blocked
    ok = not blocked
    return ok, (cap if ok else 0), info


# ══════════════════════════════════════════════════════════════════════
#  Serialización al contrato (TEMPLATE_DATA_CONTRACT_V2_1 · bloque route)
# ══════════════════════════════════════════════════════════════════════

def _tool_to_api(t):
    return {
        'tool_type': t['tool_type'],
        'enabled': bool(int(t.get('enabled') or 0)),
        'mode': t['mode'],
        'provider_key': t.get('provider_key'),
        'endpoint': t.get('endpoint'),
        'http_method': t.get('http_method'),
        'credential_ref': t.get('credential_ref'),
        'market': t.get('market'),
        'currency': t.get('currency'),
        'config': _json_obj(t.get('config_json'), {}) or {},
    }


def effective(db, route, now_utc=None, country_cache=None):
    """El estado EFECTIVO de una ruta, combinando los tres interruptores,
    archivado, readiness de ruta y de país, y horario.

    calling_now = country ON ∧ provider ON ∧ route ON ∧ no archivados
                  ∧ ROUTE READY ∧ COUNTRY READY ∧ horario
    """
    calling, cap, info = resolve_now(route, route.get('windows') or [], now_utc)
    blocked = list(info.get('blocked_by') or [])
    rr = validate_route(db, route) if route['status'] != 'ARCHIVED' else {'ready': False, 'issues': []}
    cache = country_cache if country_cache is not None else {}
    if route['iso'] not in cache:
        cache[route['iso']] = validate_country(db, route['iso'], routes=[])
    cr = cache[route['iso']]
    if route['status'] != 'ARCHIVED' and not rr['ready']:
        blocked.append('ROUTE_NOT_READY')
    if not cr['ready']:
        blocked.append('COUNTRY_NOT_READY')
    ok = not blocked
    mode = info.get('mode')
    if not ok and any(b in blocked for b in ('ROUTE_NOT_READY', 'COUNTRY_NOT_READY')):
        mode = 'config_error'
    return {'calling_now': ok, 'capacity_now': cap if ok else 0,
            'capacity_source': {**info, 'mode': mode, 'blocked_by': blocked},
            'blocked_by': blocked, 'route_ready': rr['ready'], 'route_issues': rr['issues'],
            'country_ready': cr['ready'], 'country_issues': cr['issues']}


def route_to_api(db, route, now_utc=None, country_cache=None):
    eff = effective(db, route, now_utc, country_cache)
    return {
        'route_id': route['id'],
        'route_key': route['route_key'],
        'status': route['status'],
        'iso': route['iso'],
        'country': route.get('country_name'),
        'provider': route.get('provider_code'),
        'adapter_key': route.get('adapter_key'),
        'provider_endpoint': route.get('provider_endpoint'),
        'priority': route.get('priority'),
        'switches': {
            'country_enabled': bool(int(route.get('country_enabled') or 0)),
            'provider_enabled': bool(int(route.get('provider_enabled') or 0)),
            'route_enabled': bool(int(route.get('enabled') or 0)),
        },
        'ready': eff['route_ready'],
        'country_ready': eff['country_ready'],
        'config_issues': eff['route_issues'] + eff['country_issues'],
        'blocked_by': eff['blocked_by'],
        'calling_now': eff['calling_now'],
        'capacity_now': eff['capacity_now'],
        'capacity_default': route.get('capacity_default'),
        'capacity_source': eff['capacity_source'],
        'dial_prefix': route.get('dial_prefix'),
        'national_number_len': route.get('national_number_len'),
        'language': route.get('language'),
        'timezone': route.get('timezone'),
        'caller_id': route.get('caller_id'),
        'elevenlabs_agent_id': route.get('elevenlabs_agent_id'),
        'elevenlabs_phone_number_id': route.get('elevenlabs_phone_number_id'),
        'followup_policy_key': route.get('policy_key'),
        'followup_policy': route.get('policy'),
        'recording': {
            'enabled': bool(int(route.get('recording_enabled') or 0)),
            'min_secs': route.get('recording_min_secs'),
            'upload_crm': bool(int(route.get('recording_upload_crm') or 0)),
            'telegram': bool(int(route.get('recording_telegram') or 0)),
        },
        'recording_enabled': bool(int(route.get('recording_enabled') or 0)),
        'recording_min_secs': route.get('recording_min_secs'),
        'telegram': route.get('telegram') or {},
        'tools': {t['tool_type']: _tool_to_api(t) for t in route.get('tools') or []},
        'archived_at': str(route['archived_at']) if route.get('archived_at') else None,
    }


VALID_MODES = ('V2_PRIMARY', 'LEGACY_BACKUP')


def operating_mode(db):
    """El modo de operación. FALLA CERRADO.

    Devuelve 'V2_PRIMARY', 'LEGACY_BACKUP' o 'UNKNOWN'.

    Una versión anterior devolvía 'V2_PRIMARY' cuando el ajuste faltaba,
    estaba corrupto o la consulta fallaba. Eso convertía «no sé en qué
    modo estoy» en «permiso para llamar», que es exactamente al revés de
    lo que debe hacer un sistema que marca teléfonos de clientes.

    Ahora un estado desconocido es 'UNKNOWN', y 'UNKNOWN' no autoriza a
    llamar. Leer y editar configuración sigue permitido: lo único que se
    bloquea es el discado.
    """
    try:
        r = db.one("SELECT setting_value AS v FROM app_settings "
                   "WHERE setting_key = 'lm_operating_mode'")
    except Exception:
        return 'UNKNOWN'
    if not r or r.get('v') is None:
        return 'UNKNOWN'
    v = str(r.get('v') or '').strip().upper()
    return v if v in VALID_MODES else 'UNKNOWN'


def dispatch_allowed(db):
    """¿Está V2 autorizado a llamar? Sólo con el modo explícito en
    V2_PRIMARY."""
    return operating_mode(db) == 'V2_PRIMARY'


def routes_active_payload(db, include_all=False, include_archived=False, now_utc=None):
    """
    sin flags          → rutas que deben llamar AHORA (WF2)
    include_all        → todas las no archivadas (WF14, panel)
    include_archived   → además las archivadas (reconciliación)

    CERROJO DEL MODO DE OPERACIÓN
    ─────────────────────────────
    En LEGACY_BACKUP esta llamada devuelve CERO rutas invocables, aunque
    el país, el proveedor y la ruta estén encendidos y la configuración
    esté completa. Es el cerrojo que impide que los dos sistemas llamen
    a la vez: WF2 pregunta a quién llamar y la respuesta es "a nadie".

    La condición efectiva pasa a ser:

        operating_mode == V2_PRIMARY
        AND country.enabled AND provider.enabled AND route.enabled
        AND NOT archivada AND READY AND dentro de horario

    Editar configuración sigue permitido en LEGACY_BACKUP, y una ruta
    puede figurar READY. Lo que no puede es llamar.

    `include_all` e `include_archived` siguen devolviendo TODAS las rutas:
    el panel, WF14 y la reconciliación necesitan ver la configuración
    completa en cualquier modo. Lo que cambia es que ninguna figura como
    `calling_now`, y llevan `OPERATING_MODE_LEGACY_BACKUP` en `blocked_by`.
    El cerrojo es sobre llamar, no sobre leer.
    """
    now_utc = now_utc or datetime.now(_tz.utc)
    modo = operating_mode(db)
    # El modo bloquea LLAMAR en cualquier vista. En include_all el panel
    # necesita ver que el motivo es el modo y no un interruptor: si no,
    # alguien se pasa la tarde revisando una configuración que está bien.
    # 'UNKNOWN' bloquea igual que 'LEGACY_BACKUP'. No saber en qué modo
    # se está NO es permiso para llamar.
    bloqueado = modo != 'V2_PRIMARY'
    cache = {}
    out = []
    for r in routes_list(db, 'all'):
        if r['status'] == 'ARCHIVED' and not include_archived:
            continue
        item = route_to_api(db, r, now_utc, cache)
        if bloqueado and item.get('calling_now'):
            # Deja de ser invocable, y se dice POR QUÉ: si WF2 no llama y
            # nadie explica el motivo, alguien pasará una tarde revisando
            # interruptores que están todos bien.
            item['calling_now'] = False
            item['capacity_now'] = 0
            item.setdefault('blocked_by', []).append(
                'OPERATING_MODE_UNKNOWN' if modo == 'UNKNOWN'
                else 'OPERATING_MODE_LEGACY_BACKUP')
        if include_all or include_archived or item['calling_now']:
            out.append(item)
    scope = 'archived' if include_archived else ('all' if include_all else 'active')
    payload = {'generated_at': now_utc.strftime('%Y-%m-%dT%H:%M:%SZ'), 'scope': scope,
               'operating_mode': modo,
               'dispatch_allowed': modo == 'V2_PRIMARY',
               'count': len(out), 'routes': out}
    if modo == 'UNKNOWN':
        payload['error_code'] = 'OPERATING_MODE_UNKNOWN'
        payload['blocked_reason'] = (
            'The operating mode is missing, invalid or unreadable, so the '
            'panel cannot confirm that V2 is the system allowed to call. '
            'No routes are returned. Set it in Call Center - Legacy Backup.')
    elif bloqueado:
        payload['blocked_reason'] = (
            'Operating mode is LEGACY_BACKUP, so V2 must not place calls. '
            'Switch to V2_PRIMARY in Call Center - Legacy Backup to resume.')
    return payload


def route_by_key_payload(db, route_key, now_utc=None):
    """Para WF9/WF10: resuelve una ruta por clave AUNQUE esté archivada."""
    r = route_get_full(db, route_key)
    return route_to_api(db, r, now_utc) if r else None


def country_tools_payload(db, iso):
    """Para WF3/WF7 en modo CONFIG_ROUTER. Las tools son del PAÍS: IN_PROVEEDOR1
    e IN_STRINGEE usan exactamente la misma config de India.

    Incluye country_enabled/country_ready: la regla (N8N_TEMPLATE_STANDARD §9) es
    que WF3/WF7 NO ejecutan una tool si el país está apagado.
    """
    c = country_get(db, iso)
    if not c or c.get('archived_at'):
        return None
    rep = validate_country(db, c['iso'], routes=[])
    return {'iso': c['iso'], 'country': c['country_name'], 'timezone': c['timezone'],
            'country_enabled': bool(int(c.get('enabled') or 0)), 'country_ready': rep['ready'],
            'tools': {t['tool_type']: _tool_to_api(t)
                      for t in tools_for_country(db, c['iso']) if int(t.get('enabled') or 0)}}


# ══════════════════════════════════════════════════════════════════════
#  Service token + transporte
# ══════════════════════════════════════════════════════════════════════

def service_token():
    return os.getenv('LM_ROUTES_API_TOKEN', '').strip()


def transport_is_safe(remote_addr, is_secure, forwarded_proto=None):
    """El token no debe viajar por HTTP público.

    Se acepta: origen en red privada/loopback (red Docker/VPS interna), o HTTPS
    (directo o terminado en un proxy que marca X-Forwarded-Proto: https).
    Se rechaza: HTTP plano desde una IP pública.

    Nota: cuando esto rechaza, el token YA viajó en claro. El rechazo sirve para
    que una mala configuración falle en el primer intento y no quede corriendo.
    Si se dispara en producción, rotar LM_ROUTES_API_TOKEN.
    """
    import ipaddress
    if is_secure or (forwarded_proto or '').lower() == 'https':
        return True
    try:
        ip = ipaddress.ip_address((remote_addr or '').split('%')[0])
    except ValueError:
        return False
    return ip.is_private or ip.is_loopback
