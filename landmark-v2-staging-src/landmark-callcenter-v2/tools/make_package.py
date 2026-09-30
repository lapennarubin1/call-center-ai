#!/usr/bin/env python3
"""Construye landmark-callcenter-v2-complete.zip y lo VERIFICA.

No basta con comprimir: el zip es lo único que sale de aquí, así que se
comprueba sobre el zip YA ESCRITO, no sobre el directorio:

  · no lleva secretos, ni .session_key, ni __pycache__, ni .env
  · lleva los 7 workflows, los 19 documentos, el SQL y el MANIFEST
  · todo JSON que va dentro parsea al extraerlo
  · se puede extraer entero y el árbol extraído es el esperado

Uso:  python3 tools/make_package.py [destino.zip]
"""
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import zipfile

# Correr una herramienta no puede dejar .pyc dentro de lo que se
# entrega: tests/test_package_hygiene_v2.py lo comprueba.
sys.dont_write_bytecode = True
os.environ['PYTHONDONTWRITEBYTECODE'] = '1'

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.abspath(os.path.join(HERE, '..'))
PKG = os.path.basename(ROOT)

EXCLUDE_DIRS = {'__pycache__', '.pytest_cache', '.git', 'node_modules', '.venv'}
EXCLUDE_NAMES = {'.session_key', '.env', '.env.local', '.DS_Store',
                 'credentials.json', 'secrets.json', 'id_rsa'}
EXCLUDE_EXT = {'.pyc', '.pyo', '.zip', '.swp'}

REQUIRED_WORKFLOWS = [
    'TEMPLATE_FOLLOWUP_ENGINE_V2', 'TEMPLATE_WF2_CALL_DISPATCHER_V2',
    'TEMPLATE_WF9_POST_CALL_HANDLER_V2', 'TEMPLATE_WF3_ACCOUNT_CREATION_V2',
    'TEMPLATE_WF7_8_PAYMENT_CALLBACK_V2', 'TEMPLATE_WF10_RECORDINGS_V2',
    'TEMPLATE_WF14_RECONCILIATION_ANALYTICS_V2',
]
REQUIRED_OTHER = [
    'MANIFEST.md', 'sql/migration.sql', 'sql/rollback.sql',
    'docs/PENDING_VERIFICATION.md', 'docs/INSTALL_STAGING.md',
    'docs/CRM_ENGLISH_RULE.md', 'docs/STRINGEE_MINIMAL_PATCH_PROPOSAL.md',
    'contracts/SUITE_V2_BUILD_CONTRACT.json',
    'panel/app/v2_suite.py', 'panel/app/crm_notes.py',
    'tests/run_all.sh', 'tools/secret_scan.py',
    # nuevos en r2
    'sql/parts/004_billing_v2.sql', 'sql/parts/005_legacy_backup_v2.sql',
    'sql/parts/006_payments_v2.sql',
    'panel/app/billing.py', 'panel/app/legacy_mode.py', 'panel/app/payments.py',
    'panel/PANEL_BASE_CHECKSUMS.sha256', 'panel/PANEL_BASE_CONTRACT.json',
    'panel/.env.example', 'tools/js/lmpay.js',
    'tests/fixtures/okpay_reference.js',
    'tests/test_panel_base_regression_v2.py', 'tests/test_billing_v2.py',
    'tests/test_legacy_backup_v2.py', 'tests/test_payments_v2.py',
    'tests/test_analytics_filters_v2.py',
    'docs/BILLING_GUIDE.md', 'docs/PAYMENT_INTEGRATION_GUIDE.md',
    'docs/LEGACY_BACKUP_GUIDE.md', 'docs/LEGACY_V2_COMPATIBILITY_MATRIX.md',
    # nuevos en r2-final
    'tests/test_interlock_v2.py',
    'panel/app/templates/billing.html', 'panel/app/templates/legacy.html',
    # nuevos en r2-final2
    'tests/test_real_interlock_v2.py',          # el camino real del enclavamiento
    'panel/upgrade_callcenter_v2.sh',           # actualizar el panel en el VPS
    'PANEL_PRODUCTION_PATCH_MANIFEST.md',       # qué ficheros toca ese script
    'tools/check_consistency.py',               # los recuentos no se escriben a mano
    'tools/make_patch_manifest.py',             # el inventario tampoco
]
# Ficheros de la FUNDACIÓN que viajan dentro del panel y tienen que llegar:
# sus 5 suites forman 192 de las 604 comprobaciones del recuento.
REQUIRED_FUNDACION = [
    'panel/tests/_harness.py',
    'panel/tests/run_all.sh',
    'panel/tests/test_foundation_v2_2.py',
    'panel/tests/test_ops_analytics_v2_2.py',
    'panel/tests/test_http_v2_2.py',
    'panel/tests/test_migration_v2_2.py',
    'panel/tests/test_concurrency_v2_2.py',
]


def collect():
    out = []
    for dirpath, dirnames, filenames in os.walk(ROOT):
        dirnames[:] = sorted(d for d in dirnames if d not in EXCLUDE_DIRS)
        for fn in sorted(filenames):
            if fn in EXCLUDE_NAMES or os.path.splitext(fn)[1] in EXCLUDE_EXT:
                continue
            full = os.path.join(dirpath, fn)
            if os.path.islink(full):
                continue
            out.append((full, os.path.relpath(full, ROOT)))
    return out


def sha256(path):
    h = hashlib.sha256()
    with open(path, 'rb') as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b''):
            h.update(chunk)
    return h.hexdigest()


def build(dest):
    files = collect()
    with zipfile.ZipFile(dest, 'w', zipfile.ZIP_DEFLATED, compresslevel=9) as z:
        for full, rel in files:
            z.write(full, os.path.join(PKG, rel))
    return files


def verify(dest):
    """Verificación sobre el zip escrito, no sobre el directorio."""
    problemas = []
    with zipfile.ZipFile(dest) as z:
        bad = z.testzip()
        if bad:
            problemas.append(f'zip corrupto en {bad}')
        names = z.namelist()
        inner = [n[len(PKG) + 1:] for n in names if n.startswith(PKG + '/')]

        if len(inner) != len(names):
            problemas.append('hay entradas fuera del directorio raíz del paquete')

        for n in inner:
            base = os.path.basename(n)
            if base in EXCLUDE_NAMES or os.path.splitext(base)[1] in EXCLUDE_EXT:
                problemas.append(f'artefacto excluido que sí viajó: {n}')
            if '__pycache__' in n:
                problemas.append(f'__pycache__ dentro del zip: {n}')
            if n.startswith('/') or '..' in n.split('/'):
                problemas.append(f'ruta insegura: {n}')

        for w in REQUIRED_WORKFLOWS:
            if f'workflows/{w}.json' not in inner:
                problemas.append(f'falta workflow: {w}')
        for r in REQUIRED_OTHER + REQUIRED_FUNDACION:
            if r not in inner:
                problemas.append(f'falta fichero exigido: {r}')

        for n in inner:
            if n.endswith('.json'):
                try:
                    json.loads(z.read(os.path.join(PKG, n)).decode('utf-8'))
                except Exception as ex:
                    problemas.append(f'JSON ilegible dentro del zip: {n}: {ex}')

        for w in REQUIRED_WORKFLOWS:
            wf = json.loads(z.read(f'{PKG}/workflows/{w}.json').decode('utf-8'))
            if wf.get('active'):
                problemas.append(f'{w} viajaría ACTIVO')

    # extracción real + escáner de secretos sobre lo extraído
    with tempfile.TemporaryDirectory() as tmp:
        with zipfile.ZipFile(dest) as z:
            z.extractall(tmp)
        extraido = os.path.join(tmp, PKG)
        if not os.path.isdir(extraido):
            problemas.append('el zip no extrae el directorio esperado')
        else:
            r = subprocess.run([sys.executable,
                                os.path.join(extraido, 'tools', 'secret_scan.py')],
                               cwd=extraido, capture_output=True, text=True)
            if r.returncode != 0:
                problemas.append('el escáner encuentra secretos en lo EXTRAÍDO:\n'
                                 + r.stdout[-1200:])
            # Y los recuentos tienen que cuadrar en lo que se entrega, no
            # sólo en el directorio de trabajo: si el MANIFEST y los JSON
            # se contradicen, quien lo reciba lee una cifra inventada.
            r = subprocess.run([sys.executable,
                                os.path.join(extraido, 'tools',
                                             'check_consistency.py')],
                               cwd=extraido, capture_output=True, text=True)
            if r.returncode != 0:
                problemas.append('los recuentos NO cuadran en lo EXTRAÍDO:\n'
                                 + r.stdout[-1200:])
    return problemas


def main():
    dest = (sys.argv[1] if len(sys.argv) > 1
            else os.path.join(os.path.dirname(ROOT),
                              'landmark-callcenter-v2-complete.zip'))
    if os.path.exists(dest):
        os.remove(dest)

    files = build(dest)
    problemas = verify(dest)

    print(f'paquete    : {dest}')
    print(f'ficheros   : {len(files)}')
    print(f'tamaño     : {os.path.getsize(dest):,} bytes')
    print(f'SHA256     : {sha256(dest)}')
    if problemas:
        print('\nVERIFICACIÓN FALLIDA:')
        for p in problemas:
            print('  x ' + p)
        return 1
    print('verificación: OK (contenido, JSON, active:false, secretos, extracción)')
    return 0


if __name__ == '__main__':
    sys.exit(main())
