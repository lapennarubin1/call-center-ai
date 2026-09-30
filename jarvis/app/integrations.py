"""
JARVIS — Integraciones externas
================================
Clientes para los servicios que Jarvis controla: n8n (workflows) y
Asterisk (PBX en vivo).

Todo con urllib de la stdlib — sin dependencias extra. Un servidor de
producción que corre 24/7 tiene menos superficie de fallo cuantas menos
librerías arrastre.

Cada error de red devuelve un mensaje que explica QUÉ pasó y DÓNDE, no
un stacktrace. Jarvis tiene que poder decir "n8n no responde" en voz
alta, y para eso necesita un texto legible.
"""
import json
import subprocess
import urllib.request
import urllib.error
from .config import CFG


class IntegrationError(Exception):
    pass


# ══════════════════════════════════════════════════════════════════
#  n8n
# ══════════════════════════════════════════════════════════════════

def n8n_request(method, path, body=None, timeout=15):
    if not CFG.N8N_API_KEY:
        raise IntegrationError(
            'la API key de n8n no está configurada (LM_N8N_API_KEY en el .env)')
    if not CFG.N8N_BASE_URL:
        raise IntegrationError('la URL de n8n no está configurada (LM_N8N_BASE_URL)')

    url = CFG.N8N_BASE_URL.rstrip('/') + path
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, method=method)
    req.add_header('X-N8N-API-KEY', CFG.N8N_API_KEY)
    req.add_header('Accept', 'application/json')
    if data:
        req.add_header('Content-Type', 'application/json')
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read()
            return json.loads(raw) if raw else {}
    except urllib.error.HTTPError as ex:
        detail = ex.read().decode('utf-8', 'replace')[:200]
        raise IntegrationError(f'n8n respondió {ex.code} en {method} {path}: {detail}')
    except urllib.error.URLError as ex:
        raise IntegrationError(f'no se pudo contactar n8n ({ex.reason})')
    except TimeoutError:
        raise IntegrationError(f'n8n no respondió en {timeout}s')


def n8n_list_workflows():
    items, cursor = [], None
    while True:
        path = '/api/v1/workflows?limit=250'
        if cursor:
            path += f'&cursor={cursor}'
        result = n8n_request('GET', path)
        page = result.get('data', result if isinstance(result, list) else [])
        items.extend(page)
        cursor = result.get('nextCursor') if isinstance(result, dict) else None
        if not cursor:
            break
    return [{'id': str(w['id']), 'name': w.get('name', '(sin nombre)'),
             'active': bool(w.get('active'))} for w in items]


def n8n_set_workflow(workflow_id, active):
    verb = 'activate' if active else 'deactivate'
    n8n_request('POST', f'/api/v1/workflows/{workflow_id}/{verb}')


# ══════════════════════════════════════════════════════════════════
#  Asterisk (estado en vivo del PBX)
# ══════════════════════════════════════════════════════════════════

def asterisk_cli(command, timeout=10):
    """
    Corre un comando del CLI de Asterisk. Solo lectura: la lista blanca
    de abajo es intencional — Jarvis puede mirar el estado del PBX pero
    no reconfigurarlo por voz. Reiniciar un trunk por una frase mal
    entendida cortaría llamadas en curso.
    """
    if not CFG.ASTERISK_ENABLED:
        raise IntegrationError('el acceso a Asterisk está desactivado (JARVIS_ASTERISK=0)')

    allowed_prefixes = (
        'pjsip show', 'core show', 'sip show', 'dialplan show',
        'odbc show', 'core restart' if False else 'pjsip list',
    )
    if not any(command.startswith(p) for p in allowed_prefixes):
        raise IntegrationError(f'comando de Asterisk no permitido: {command!r}')

    try:
        result = subprocess.run(['asterisk', '-rx', command],
                                capture_output=True, text=True, timeout=timeout)
    except FileNotFoundError:
        raise IntegrationError('Asterisk no está instalado o no está en el PATH de este servidor')
    except subprocess.SubprocessError as ex:
        raise IntegrationError(f'no se pudo ejecutar el comando de Asterisk: {ex}')

    if result.returncode != 0:
        raise IntegrationError(
            f'Asterisk devolvió error: {(result.stderr or result.stdout).strip()[:200]}')
    return result.stdout


def parse_pjsip_endpoints(raw):
    """
    Convierte la salida de texto de 'pjsip show endpoints' en datos.
    El formato es tabular y frágil, por eso se parsea defensivamente:
    si una línea no encaja, se ignora en vez de romper todo.
    """
    endpoints = []
    for line in raw.splitlines():
        stripped = line.strip()
        if not stripped.startswith('Endpoint:'):
            continue
        rest = stripped[len('Endpoint:'):].strip()
        if rest.startswith('<'):        # línea de encabezado
            continue
        parts = rest.split()
        if not parts:
            continue
        name = parts[0]
        # El estado son las palabras del medio: "Not in use", "Unavailable"...
        state = ' '.join(parts[1:-3]) if len(parts) > 3 else ' '.join(parts[1:])
        endpoints.append({'nombre': name, 'estado': state.strip() or 'desconocido'})
    return endpoints


def parse_pjsip_registrations(raw):
    regs = []
    for line in raw.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith('<') or stripped.startswith('='):
            continue
        if '/sip:' not in stripped:
            continue
        parts = stripped.split()
        if len(parts) >= 3:
            regs.append({
                'registro': parts[0],
                'estado': parts[-2] if parts[-1].startswith('(') else parts[-1],
            })
    return regs
