"""
JARVIS — Sistema de Skills
===========================
Una "skill" es una capacidad concreta de Jarvis: consultar cuentas
abiertas, apagar un país, ver el saldo SIP. Cada una se declara con el
decorador @skill y se auto-registra; el cerebro las descubre solas y se
las ofrece a Claude como herramientas.

Para agregar una capacidad nueva NO hay que tocar el cerebro ni el
servidor — se crea la función con su @skill en el módulo que
corresponda y aparece sola. Ese es el punto: que crecer sea barato.

    @skill(
        name='cuentas_abiertas',
        description='Cuántas cuentas se abrieron en un período',
        params={'dias': {'type': 'integer', 'description': 'Días hacia atrás'}},
        required=['dias'],
    )
    def cuentas_abiertas(dias, ctx):
        ...

Dos categorías, y la diferencia importa:
  - READ  → consulta. Se ejecuta directo.
  - WRITE → modifica algo real (apagar workflows, cambiar horarios).
            Pide confirmación al operador antes de correr, porque una
            frase mal escuchada por el micrófono no puede apagar la
            operación de un país.
"""
import time
import json
import functools

READ = 'read'
WRITE = 'write'

_REGISTRY = {}


class SkillError(Exception):
    """
    Error esperable de una skill (parámetro inválido, servicio caído).
    Se le devuelve a Claude como texto para que lo explique en lenguaje
    natural, en vez de reventar la request entera.
    """


def skill(name, description, params=None, required=None, category=READ,
          confirm_prompt=None, examples=None):
    def decorator(fn):
        spec = {
            'name': name,
            'description': description,
            'category': category,
            'confirm_prompt': confirm_prompt,
            'examples': examples or [],
            'input_schema': {
                'type': 'object',
                'properties': params or {},
                'required': required or [],
            },
            'fn': fn,
        }
        _REGISTRY[name] = spec

        @functools.wraps(fn)
        def wrapper(*a, **kw):
            return fn(*a, **kw)
        wrapper._skill_spec = spec
        return wrapper
    return decorator


def all_skills():
    return dict(_REGISTRY)


def get_skill(name):
    return _REGISTRY.get(name)


def anthropic_tools():
    """Las skills en el formato de herramientas que espera la API."""
    return [{
        'name': s['name'],
        'description': _describe(s),
        'input_schema': s['input_schema'],
    } for s in _REGISTRY.values()]


def _describe(spec):
    """
    La descripción que ve el modelo. Le agregamos los ejemplos y una
    marca clara cuando la skill modifica algo — el modelo elige mucho
    mejor la herramienta correcta cuando sabe qué frases la disparan.
    """
    text = spec['description']
    if spec['category'] == WRITE:
        text += ' [ACCIÓN: modifica el sistema real]'
    if spec['examples']:
        text += ' Ejemplos de cuándo usarla: ' + '; '.join(f'"{e}"' for e in spec['examples'])
    return text


def run_skill(name, params, ctx):
    """
    Ejecuta una skill midiendo cuánto tarda y registrando el resultado.

    Nunca propaga excepciones: cualquier fallo vuelve como un dict con
    'error' para que el modelo lo lea y se lo explique al operador. Un
    Jarvis que dice "no pude consultar el saldo, la base no responde"
    es infinitamente más útil que uno que devuelve un 500 mudo.
    """
    spec = _REGISTRY.get(name)
    if not spec:
        return {'error': f'skill desconocida: {name}'}

    started = time.time()
    try:
        result = spec['fn'](ctx=ctx, **params)
        ms = int((time.time() - started) * 1000)
        _log_run(ctx, name, True, ms, None)
        if spec['category'] == WRITE:
            _audit(ctx, name, params, result)
        return result
    except SkillError as ex:
        ms = int((time.time() - started) * 1000)
        _log_run(ctx, name, False, ms, str(ex))
        return {'error': str(ex)}
    except TypeError as ex:
        # Parámetros que no encajan con la firma — típico cuando el
        # modelo inventa un argumento. Mensaje claro para que corrija
        # en la vuelta siguiente en vez de reintentar igual.
        ms = int((time.time() - started) * 1000)
        _log_run(ctx, name, False, ms, str(ex))
        return {'error': f'parámetros inválidos para {name}: {ex}'}
    except Exception as ex:
        ms = int((time.time() - started) * 1000)
        _log_run(ctx, name, False, ms, f'{type(ex).__name__}: {ex}')
        return {'error': f'{name} falló: {type(ex).__name__}: {ex}'}


def _log_run(ctx, name, ok, ms, error):
    try:
        ctx.jdb.execute(
            "INSERT INTO skill_runs (skill, ok, ms, error) VALUES (§,§,§,§)",
            (name, 1 if ok else 0, ms, error))
    except Exception:
        pass   # la telemetría nunca puede tumbar una respuesta


def _audit(ctx, name, params, result):
    try:
        ctx.jdb.execute(
            "INSERT INTO audit_log (session_id, skill, params, result) VALUES (§,§,§,§)",
            (ctx.session_id, name, json.dumps(params, ensure_ascii=False, default=str),
             json.dumps(result, ensure_ascii=False, default=str)[:2000]))
    except Exception:
        pass


def failing_skills(ctx, hours=24, min_failures=3):
    """
    Skills que vienen fallando seguido. El cerebro lo inyecta en el
    prompt para que Jarvis avise ("el saldo SIP no lo puedo consultar
    desde ayer") en lugar de reintentar a ciegas cada vez.
    """
    try:
        rows = ctx.jdb.q("""
            SELECT skill, COUNT(*) AS fails
            FROM skill_runs
            WHERE ok = 0 AND created_at >= datetime('now', §)
            GROUP BY skill HAVING COUNT(*) >= §""",
            (f'-{int(hours)} hours', min_failures))
        return [r['skill'] for r in rows]
    except Exception:
        return []
