"""
wf_settings.py — Parámetros operativos del suite V2 (tabla wf_settings)
=======================================================================

Por qué existe
--------------
N8N_TEMPLATE_STANDARD §1 prohíbe literales de negocio en los nodos. Umbrales
como "un DISPATCHING de más de 5 minutos se reconcilia" o "la ruta de
compatibilidad para un post-call sin route_key es X" son configuración
operativa: viven acá, los lee el panel y los lee n8n por
`GET /api/settings` (fail-closed, igual que /api/routes/active).

Qué NO vive acá
---------------
Secretos. Nunca. Igual que `country_tool_configs`, esta tabla guarda
REFERENCIAS lógicas (`credential_ref`), jamás una API key. `set_setting()`
rechaza una clave o un valor con pinta de secreto antes de escribir.
"""

import re

# Mismo criterio que routes_config._SECRET_KEY_RE: si el NOMBRE de la clave
# delata un secreto, no se guarda.
_SECRET_KEY_RE = re.compile(
    r'(api[_-]?key|secret|password|passwd|token|private[_-]?key|cppwd|bearer)', re.I)
_KEY_RE = re.compile(r'^[a-z][a-z0-9_]{2,63}$')

VALUE_TYPES = ['int', 'string', 'bool', 'json']

# Defaults de seguridad: si la tabla no tiene la fila (base a medio migrar,
# alguien la borró), el suite usa esto y NO inventa un comportamiento distinto
# del documentado. Deben coincidir con el seed de 002.
DEFAULTS = {
    'reconcile_dispatching_minutes': 5,
    'reconcile_unknown_minutes': 5,
    'reconcile_dispatched_minutes': 60,
    'reconcile_claimed_minutes': 10,
    'reconcile_ledger_minutes': 15,
    'tech_retry_max': 8,
    'tech_retry_backoff_cap_minutes': 60,
    'postcall_polling_window_minutes': 20,
    'legacy_compat_route_key': '',
    'crm_notes_language': 'en',
    'recording_default_min_secs': 60,
    'wf14_leadstudio_page_size': 200,
}


class SettingError(ValueError):
    pass


def ensure_tables_sqlite(db):
    """Solo tests/demo. En MariaDB la tabla la crea la migración 002."""
    if db.driver == 'mysql':
        return
    db.execute("""CREATE TABLE IF NOT EXISTS wf_settings (
        setting_key TEXT PRIMARY KEY, setting_value TEXT,
        value_type TEXT NOT NULL DEFAULT 'string', scope TEXT NOT NULL DEFAULT 'suite',
        description TEXT, created_at DATETIME DEFAULT (datetime('now')),
        updated_at DATETIME DEFAULT (datetime('now')))""")


def _coerce(value, value_type):
    if value is None:
        return None
    if value_type == 'int':
        try:
            return int(str(value).strip())
        except (TypeError, ValueError):
            raise SettingError(f'valor no entero: {value!r}')
    if value_type == 'bool':
        return str(value).strip().lower() in ('1', 'true', 'yes', 'on')
    if value_type == 'json':
        import json
        try:
            return json.loads(value)
        except (TypeError, ValueError) as ex:
            raise SettingError(f'valor no es JSON válido: {ex}')
    return str(value)


def all_settings(db):
    """{key: {value, value_type, scope, description}} con los defaults
    completados para las claves que falten."""
    out = {}
    try:
        rows = db.q("SELECT * FROM wf_settings ORDER BY scope, setting_key")
    except Exception:
        rows = []
    for r in rows:
        out[r['setting_key']] = {
            'value': _coerce(r.get('setting_value'), r.get('value_type') or 'string'),
            'raw': r.get('setting_value'),
            'value_type': r.get('value_type') or 'string',
            'scope': r.get('scope') or 'suite',
            'description': r.get('description'),
            'from_default': False,
        }
    for k, v in DEFAULTS.items():
        if k not in out:
            out[k] = {'value': v, 'raw': str(v), 'from_default': True,
                      'value_type': 'int' if isinstance(v, int) else 'string',
                      'scope': 'suite', 'description': 'default de aplicación (fila ausente)'}
    return out


def get_setting(db, key, default=None):
    row = None
    try:
        row = db.one("SELECT setting_value, value_type FROM wf_settings WHERE setting_key=§", (key,))
    except Exception:
        row = None
    if not row:
        return DEFAULTS.get(key, default)
    return _coerce(row.get('setting_value'), row.get('value_type') or 'string')


def set_setting(db, actor, key, value, value_type=None, description=None, scope=None):
    """Escribe una fila. Rechaza claves con pinta de secreto y valida el tipo.

    No borra: una clave que no exista se crea; una que exista se actualiza.
    """
    key = str(key or '').strip()
    if not _KEY_RE.match(key):
        raise SettingError(f'clave inválida: {key!r} (minúsculas, guion bajo, 3-64)')
    if _SECRET_KEY_RE.search(key):
        raise SettingError(f'la clave {key!r} parece un secreto: usar una credential de n8n '
                           'y guardar acá solo la REFERENCIA lógica')
    value = '' if value is None else str(value)
    if _SECRET_KEY_RE.search(value) and '=' in value:
        raise SettingError('el valor parece contener un secreto embebido')

    existing = None
    try:
        existing = db.one("SELECT value_type, scope, description FROM wf_settings WHERE setting_key=§",
                          (key,))
    except Exception:
        existing = None
    vt = value_type or (existing or {}).get('value_type') or 'string'
    if vt not in VALUE_TYPES:
        raise SettingError(f'value_type inválido: {vt!r}')
    _coerce(value, vt)                       # valida antes de escribir

    sc = scope or (existing or {}).get('scope') or 'suite'
    de = description if description is not None else (existing or {}).get('description')

    if existing:
        db.execute("UPDATE wf_settings SET setting_value=§, value_type=§, scope=§, description=§ "
                   "WHERE setting_key=§", (value, vt, sc, de, key))
    else:
        db.execute("INSERT INTO wf_settings (setting_key, setting_value, value_type, scope, "
                   "description) VALUES (§,§,§,§,§)", (key, value, vt, sc, de))

    # auditoría: se reusa route_audit para no inventar una tabla nueva
    try:
        now = "UTC_TIMESTAMP()" if db.driver == 'mysql' else "datetime('now')"
        db.execute(f"INSERT INTO route_audit (route_id, route_key, action, field, old_value, "
                   f"new_value, actor, changed_at) VALUES (NULL, NULL, 'SETTING', §, §, §, §, {now})",
                   (key, str((existing or {}).get('setting_value', ''))[:255], value[:255], actor))
    except Exception:
        pass
    return get_setting(db, key)


def settings_payload(db):
    """Lo que n8n lee por GET /api/settings. Sin secretos por construcción."""
    s = all_settings(db)
    return {'settings': {k: v['value'] for k, v in s.items()},
            'types': {k: v['value_type'] for k, v in s.items()},
            'defaults_used': sorted(k for k, v in s.items() if v['from_default'])}
