"""Escaneo de secretos en claro sobre TODO el paquete.

Busca:
  - claves de API reales (patrones de proveedor)
  - password=/token=/secret= con valor literal
  - cadenas de alta entropía
  - JWT
  - URLs con credenciales embebidas
Excluye lo que es, por diseño, un MARCADOR (__X_CREDENTIAL__), un
nombre de variable de entorno, o un ejemplo obviamente falso.
"""
import os, re, json, math, sys

# Correr una herramienta no puede dejar .pyc dentro de lo que se
# entrega: tests/test_package_hygiene_v2.py lo comprueba.
sys.dont_write_bytecode = True
os.environ['PYTHONDONTWRITEBYTECODE'] = '1'

ROOT = '.'
SKIP_DIRS = {'__pycache__', '.git', '.pytest_cache'}

PATTERNS = [
    ('elevenlabs_key',   re.compile(r'\bsk_[A-Za-z0-9]{24,}')),
    ('openai_key',       re.compile(r'\bsk-[A-Za-z0-9]{32,}')),
    ('aws_key',          re.compile(r'\bAKIA[0-9A-Z]{16}\b')),
    ('google_key',       re.compile(r'\bAIza[0-9A-Za-z_\-]{35}\b')),
    ('slack_token',      re.compile(r'\bxox[baprs]-[0-9A-Za-z\-]{10,}')),
    ('telegram_token',   re.compile(r'\b\d{8,10}:[A-Za-z0-9_\-]{35}\b')),
    ('jwt',              re.compile(r'\beyJ[A-Za-z0-9_\-]{10,}\.[A-Za-z0-9_\-]{10,}\.[A-Za-z0-9_\-]{10,}')),
    ('private_key',      re.compile(r'-----BEGIN [A-Z ]*PRIVATE KEY-----')),
    ('url_with_creds',   re.compile(r'\b[a-z]{2,10}://[^/\s:@"\']{2,}:[^/\s@"\']{2,}@')),
    # Shell: un valor por defecto en claro dentro de ${VAR:-...}, o PASS=valor
    # SECSCAN-OK: el ejemplo de la regla, no es una credencial
    ('shell_default',    re.compile(
        r'''(?ix)\b\w*(?:pass|passwd|password|secret|token|api_?key|apikey)\w*
            =\s*["']?\$\{[A-Za-z_][A-Za-z0-9_]*:-([^}"'\s]{6,})\}''')),
    ('shell_literal',    re.compile(
        r'''(?ix)^\s*(?:export\s+)?\w*(?:pass|passwd|password|secret|token|api_?key|apikey)\w*
            =["']([^"'\n$\s]{8,})["']\s*$''')),
    ('sql_identified_by', re.compile(
        r'''(?ix)IDENTIFIED\s+BY\s+["']([^"'\n]{4,})["']''')),
    # SECSCAN-OK: los dos renglones siguientes son los EJEMPLOS de la regla
    # os.getenv('LM_PASS', 'literal')  ·  os.environ.get("TOKEN", "literal")
    #
    # Este patron se le escapaba al escaner. En panel/app/server.py habia
    # CUATRO contrasenas reales asi, y el escaner reportaba 0 hallazgos.
    # Un escaner que no ve el caso real es peor que no tenerlo: da una
    # confianza que no corresponde.
    ('getenv_default',   re.compile(
        r'''(?ix)\b(?:os\.)?(?:environ\.get|getenv)\s*\(\s*
            ["'][A-Z0-9_]*(?:PASS|PASSWD|PASSWORD|SECRET|TOKEN|API_?KEY|
                            APIKEY|CREDENTIAL|PRIVATE_?KEY)[A-Z0-9_]*["']
            \s*,\s*["']([^"'\n]{4,})["']''')),
    # SECSCAN-OK: ejemplo de la regla, no es una credencial
    # config.get('password', 'literal')  ·  d.get("api_key", "literal")
    ('dict_get_default', re.compile(
        r'''(?ix)\.get\s*\(\s*
            ["'][a-z0-9_]*(?:pass|passwd|password|secret|token|api_?key|
                            apikey|credential)[a-z0-9_]*["']
            \s*,\s*["']([^"'\n]{6,})["']''')),
    # El escaner veia os.getenv('X', 'literal') pero NO la forma mas simple
    # de todas: asignarle el literal a una variable que se llama como la
    # credencial. Un control positivo con cuatro formas corrientes pasaba
    # limpio. Vale para asignacion, para clave de entorno y para argumento
    # con nombre, que es como lo escriben los arneses de test.
    # Caza la forma mas simple de todas: darle el literal a algo que se
    # llama como la credencial — por asignacion, por clave de entorno, por
    # argumento con nombre o por clave de diccionario. El escaner veia
    # os.getenv('X', 'literal') y no veia esta; un control positivo con
    # cuatro formas corrientes pasaba limpio.
    ('env_name_literal', re.compile(
        r"""(?ix)\b[A-Z][A-Z0-9_]*
            (?:PASS|PASSWD|PASSWORD|SECRET|TOKEN|API_?KEY|APIKEY|CREDENTIAL)
            ["\']?\s*\]?\s*[:=]\s*["\']([^"\'\n]{4,})["\']""")),
    ('assigned_secret',  re.compile(
        r'''(?ix)\b(password|passwd|pwd|secret|api_?key|apikey|access_?token|
              auth_?token|bearer|client_?secret|webhook_?secret)\b
            ["\']?            # comilla de cierre si la clave venia entrecomillada
            \s*[:=>]{1,2}\s*  # :  =  =>  :=
            ["\']([^"\'\n]{6,})["\']''')),
]

# Un valor es ACEPTABLE si es claramente un marcador / referencia / placeholder.
OK_VALUE = re.compile(r'''(?ix)
      ^__[A-Z0-9_]+__$                 # __PANEL_TOKEN_CREDENTIAL__
    | ^\{\{.*\}\}$                     # expresión n8n
    | ^\$\{?[A-Za-z_]                  # $VAR / ${VAR}
    | ^(os\.|process\.env|getenv|env\.)# lectura de entorno
    | ^(LM_|ELEVENLABS_|N8N_|MYSQL_|DB_|TELEGRAM_|STRINGEE_|LEADSTUDIO_)[A-Z0-9_]*$
    | ^(x-|X-)                         # nombre de cabecera
    | ^(changeme|CHANGEME|xxx+|XXX+|\.\.\.|TODO|REPLACE|REEMPLAZAR|placeholder|PLACEHOLDER)
    | ^(true|false|null|none|None)$
    | ^(header|headerAuth|genericCredentialType|httpHeaderAuth|predefinedCredentialType)$
    | ^[<\[].*[>\]]$                   # <tu-token>  [TOKEN]
    | ^(tu|su|your|the)\b
    | ^(sha256|bcrypt|argon2|scrypt|pbkdf2)
    | ^%s$ | ^\?$
''')

def entropy(s):
    if not s:
        return 0.0
    f = {}
    for c in s:
        f[c] = f.get(c, 0) + 1
    return -sum((n/len(s)) * math.log2(n/len(s)) for n in f.values())

# Exencion explicita: la linea (o la anterior) lleva  SECSCAN-OK: <motivo>
# Se exige el motivo escrito; un marcador desnudo no exime.
ALLOW = re.compile(r'SECSCAN-OK:\s*\S+')

findings = []
scanned = 0
allowed = []
for dirpath, dirnames, filenames in os.walk(ROOT):
    dirnames[:] = [d for d in dirnames if d not in SKIP_DIRS]
    for fn in filenames:
        p = os.path.join(dirpath, fn)
        try:
            text = open(p, encoding='utf-8', errors='replace').read()
        except Exception:
            continue
        scanned += 1
        lines = text.splitlines()
        for i, line in enumerate(lines, 1):
            if len(line) > 4000:
                line = line[:4000]
            for name, rx in PATTERNS:
                for m in rx.finditer(line):
                    # si la regla captura el VALOR en un grupo, se evalua ese
                    # grupo; si no captura nada, la coincidencia entera.
                    val = m.group(m.lastindex) if m.lastindex else m.group(0)
                    if OK_VALUE.search(val.strip()):
                        continue
                    if name in ('shell_default', 'shell_literal',
                                'sql_identified_by', 'getenv_default',
                                'dict_get_default', 'env_name_literal'):
                        v = val.strip()
                        if OK_VALUE.search(v) or v.startswith('$'):
                            continue
                    if name == 'env_name_literal':
                        v = val.strip()
                        # Un valor con espacios es una descripcion, no una
                        # credencial: asi estan escritos los contratos JSON,
                        # donde el valor explica para que sirve la variable.
                        if ' ' in v or v.startswith('\\'):
                            continue
                    if name == 'assigned_secret':
                        v = val.strip()
                        # descartamos valores que son nombres de campo/descripciones
                        if ' ' in v or entropy(v) < 3.0 or len(v) < 12:
                            continue
                    prev = lines[i - 2] if i >= 2 else ''
                    rec = {'file': p, 'line': i, 'rule': name,
                           'value': val[:60], 'context': line.strip()[:160]}
                    if ALLOW.search(line) or ALLOW.search(prev):
                        rec['exencion'] = (ALLOW.search(line) or ALLOW.search(prev)).group(0)
                        allowed.append(rec)
                    else:
                        findings.append(rec)

print(f'archivos escaneados: {scanned}')
print(f'exenciones justificadas: {len(allowed)}')
for a in allowed:
    print('  EXENTO', a['file'], a['line'], '|', a['exencion'])
print(f'hallazgos: {len(findings)}')
for f in findings:
    print(json.dumps(f, ensure_ascii=False))
sys.exit(1 if findings else 0)
