#!/usr/bin/env python3
"""Higiene del PAQUETE: lo que debe cumplirse antes de entregar el zip.

No prueba comportamiento (de eso van las otras seis suites). Prueba que lo
entregado sea instalable y que no lleve nada que no deba salir de aquí:

  · todo JSON parsea y los 7 workflows tienen la forma que n8n espera
  · todo .py compila
  · cero secretos en claro en TODO el paquete (tools/secret_scan.py)
  · cero credenciales embebidas en los workflows: sólo marcadores
  · los 7 workflows vienen DESACTIVADOS (active: false)
  · no se cuela ningún artefacto generado (.session_key, __pycache__, .pyc)
  · los 13 documentos obligatorios existen y no están vacíos
  · MANIFEST.md existe y nombra todo lo que hay
"""
import json
import os
import py_compile
import re
import subprocess
import sys
import tempfile

sys.dont_write_bytecode = True   # no ensuciar el paquete al importar _suite
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from _suite import Suite, ROOT, WORKFLOWS          # noqa: E402

DOCS = os.path.join(ROOT, 'docs')
TOOLS = os.path.join(ROOT, 'tools')

REQUIRED_DOCS = [
    'ARQUITECTURA_CORRECCIONES_V2_2.md', 'N8N_TEMPLATE_STANDARD_V2_2.md',
    'ANALYTICS_ARCHITECTURE_V2_2.md', 'STRINGEE_WORKER_AUDIT_V2_2.md',
    'TEST_PLAN_FOUNDATION_V2_2.md', 'PLAN_MIGRACION_V2_2.md',
    'STRINGEE_MINIMAL_PATCH_PROPOSAL.md', 'CRM_ENGLISH_RULE.md',
    'FOLLOWUP_POLICY_GUIDE.md', 'ARCHITECTURE_FINAL_V2.md',
    'INSTALL_STAGING.md', 'PRODUCTION_DEPLOYMENT_PLAN.md', 'ROLLBACK_PLAN.md',
    'CONFIGURATION_GUIDE.md', 'ADD_COUNTRY_GUIDE.md', 'ADD_PROVIDER_GUIDE.md',
    'ADD_ROUTE_GUIDE.md', 'ANALYTICS_GUIDE.md', 'PENDING_VERIFICATION.md',
    # nuevos en r2 (§73)
    'BILLING_GUIDE.md', 'PAYMENT_INTEGRATION_GUIDE.md',
    'LEGACY_BACKUP_GUIDE.md', 'LEGACY_V2_COMPATIBILITY_MATRIX.md',
]

EXPECTED_WORKFLOWS = [
    'TEMPLATE_FOLLOWUP_ENGINE_V2', 'TEMPLATE_WF2_CALL_DISPATCHER_V2',
    'TEMPLATE_WF9_POST_CALL_HANDLER_V2', 'TEMPLATE_WF3_ACCOUNT_CREATION_V2',
    'TEMPLATE_WF7_8_PAYMENT_CALLBACK_V2', 'TEMPLATE_WF10_RECORDINGS_V2',
    'TEMPLATE_WF14_RECONCILIATION_ANALYTICS_V2',
]

# Lo único que puede aparecer como credencial dentro de un workflow.
MARKER = re.compile(r'^__[A-Z0-9_]+_CREDENTIAL__$')

GENERATED = ('.session_key', '.pyc', '.pyo')


def walk(root):
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if d not in {'.git'}]
        for fn in filenames:
            yield os.path.join(dirpath, fn)


def main():
    s = Suite('higiene del paquete V2')

    # ══ JSON ══════════════════════════════════════════════════════════
    s.section('todo JSON parsea')

    jsons = [p for p in walk(ROOT) if p.endswith('.json')]

    def all_json_parses():
        malos = []
        for p in jsons:
            try:
                json.load(open(p, encoding='utf-8'))
            except Exception as ex:
                malos.append(f'{os.path.relpath(p, ROOT)}: {ex}')
        assert not malos, 'JSON inválido: ' + '; '.join(malos)
    s.check(f'los {len(jsons)} .json del paquete parsean', all_json_parses)

    wfs = {}
    for name in EXPECTED_WORKFLOWS:
        p = os.path.join(WORKFLOWS, name + '.json')
        if os.path.exists(p):
            wfs[name] = json.load(open(p, encoding='utf-8'))

    def seven_workflows():
        faltan = [n for n in EXPECTED_WORKFLOWS if n not in wfs]
        assert not faltan, f'faltan workflows: {faltan}'
    s.check('están los 7 workflows exigidos', seven_workflows)

    def n8n_shape():
        for name, wf in wfs.items():
            assert isinstance(wf.get('nodes'), list) and wf['nodes'], f'{name}: sin nodes'
            assert isinstance(wf.get('connections'), dict), f'{name}: sin connections'
            assert wf.get('name') == name, f'{name}: el campo name no coincide'
            for n in wf['nodes']:
                for k in ('id', 'name', 'type', 'position', 'parameters'):
                    assert k in n, f'{name}/{n.get("name")}: falta {k}'
                assert 'typeVersion' in n, f'{name}/{n["name"]}: falta typeVersion'
                assert isinstance(n['position'], list) and len(n['position']) == 2, \
                    f'{name}/{n["name"]}: position mal formada'
    s.check('cada workflow tiene la forma que n8n espera al importar', n8n_shape)

    def unique_node_ids():
        for name, wf in wfs.items():
            ids = [n['id'] for n in wf['nodes']]
            assert len(ids) == len(set(ids)), f'{name}: ids de nodo repetidos'
            nombres = [n['name'] for n in wf['nodes']]
            assert len(nombres) == len(set(nombres)), f'{name}: nombres de nodo repetidos'
    s.check('ids y nombres de nodo únicos dentro de cada workflow', unique_node_ids)

    def connections_point_somewhere():
        for name, wf in wfs.items():
            nombres = {n['name'] for n in wf['nodes']}
            for origen, salidas in wf['connections'].items():
                assert origen in nombres, f'{name}: conexión desde nodo inexistente {origen}'
                for rama in salidas.get('main', []):
                    for c in (rama or []):
                        assert c['node'] in nombres, \
                            f'{name}: {origen} conecta con nodo inexistente {c["node"]}'
    s.check('ninguna conexión apunta a un nodo que no existe',
            connections_point_somewhere)

    def imported_disabled():
        vivos = [n for n, wf in wfs.items() if wf.get('active')]
        assert not vivos, f'workflows que se importarían ACTIVOS: {vivos}'
    s.check('los 7 workflows vienen con active: false', imported_disabled)

    # ══ credenciales ══════════════════════════════════════════════════
    s.section('credenciales: sólo marcadores, nunca valores')

    def creds_are_markers():
        """n8n resuelve la credencial por `id`.  Ese id DEBE ser un marcador:
        es lo que el instalador sustituye.  `name` es sólo la etiqueta visible
        en la UI de n8n (p.ej. "Landmark MySQL") y no lleva ningún valor."""
        malos = []
        for name, wf in wfs.items():
            for n in wf['nodes']:
                for tipo, ref in (n.get('credentials') or {}).items():
                    cid = ref.get('id') if isinstance(ref, dict) else ref
                    if not cid or not MARKER.match(str(cid)):
                        malos.append(f'{name}/{n["name"]}/{tipo}: id = {cid!r}')
        assert not malos, ('credenciales cuyo id no es marcador: '
                           + '; '.join(malos))
    s.check('el id de toda credencial es un marcador __X_CREDENTIAL__',
            creds_are_markers)

    def cred_labels_are_labels():
        """La etiqueta visible no puede ser un valor: ni larga, ni con pinta
        de clave, ni con caracteres de secreto."""
        malos = []
        for name, wf in wfs.items():
            for n in wf['nodes']:
                for tipo, ref in (n.get('credentials') or {}).items():
                    nom = ref.get('name') if isinstance(ref, dict) else None
                    if nom is None:
                        continue
                    nom = str(nom)
                    if (len(nom) > 40 or not re.fullmatch(r'[A-Za-z0-9 ._\-]+', nom)
                            or re.search(r'(?i)\b(sk_|key|token|secret|pass)\b', nom)):
                        malos.append(f'{name}/{n["name"]}/{tipo}: name = {nom!r}')
        assert not malos, ('etiquetas de credencial con pinta de valor: '
                           + '; '.join(malos))
    s.check('la etiqueta visible de cada credencial es una etiqueta, no un valor',
            cred_labels_are_labels)

    def markers_documented():
        contrato = json.load(open(os.path.join(ROOT, 'contracts',
                                               'SUITE_V2_BUILD_CONTRACT.json'),
                                  encoding='utf-8'))
        declarados = set(contrato['credentials_required'])
        usados = set()
        for wf in wfs.values():
            for n in wf['nodes']:
                for ref in (n.get('credentials') or {}).values():
                    v = ref.get('id') if isinstance(ref, dict) else ref
                    if v and MARKER.match(str(v)):
                        usados.add(str(v))
        sin_documentar = usados - declarados
        assert not sin_documentar, \
            f'marcadores usados y no documentados en el contrato: {sorted(sin_documentar)}'
    s.check('todo marcador usado está documentado en el contrato de build',
            markers_documented)

    # ══ Python ════════════════════════════════════════════════════════
    s.section('todo Python compila')

    pys = [p for p in walk(ROOT) if p.endswith('.py')]

    def all_py_compiles():
        malos = []
        with tempfile.TemporaryDirectory() as tmp:
            for p in pys:
                out = os.path.join(tmp, os.path.basename(p) + 'c')
                try:
                    py_compile.compile(p, cfile=out, doraise=True)
                except py_compile.PyCompileError as ex:
                    malos.append(f'{os.path.relpath(p, ROOT)}: {ex.msg.strip()[:120]}')
        assert not malos, 'no compilan: ' + '; '.join(malos)
    s.check(f'los {len(pys)} .py del paquete compilan', all_py_compiles)

    def js_parses():
        js = [p for p in walk(os.path.join(TOOLS, 'js')) if p.endswith('.js')]
        assert js, 'no hay librerías JS en tools/js'
        for p in js:
            r = subprocess.run(['node', '--check', p], capture_output=True, text=True)
            assert r.returncode == 0, \
                f'{os.path.relpath(p, ROOT)}: {r.stderr.strip()[:160]}'
    s.check('las librerías JS del motor pasan node --check', js_parses)

    # ══ secretos ══════════════════════════════════════════════════════
    s.section('cero secretos en claro')

    def no_secrets():
        r = subprocess.run([sys.executable, os.path.join(TOOLS, 'secret_scan.py')],
                           cwd=ROOT, capture_output=True, text=True)
        assert r.returncode == 0, 'el escáner encontró secretos:\n' + r.stdout[-1500:]
    s.check('tools/secret_scan.py no encuentra nada en todo el paquete', no_secrets)

    def scanner_actually_works():
        """Control positivo: si el escáner no detecta un secreto sembrado,
        el check anterior no vale nada."""
        with tempfile.TemporaryDirectory() as tmp:
            with open(os.path.join(tmp, 'sembrado.json'), 'w') as fh:
                fh.write('{"api_key": "sk_' + 'a1b2c3d4e5f6a7b8c9d0e1f2' + '"}\n')
            r = subprocess.run([sys.executable, os.path.join(TOOLS, 'secret_scan.py')],
                               cwd=tmp, capture_output=True, text=True)
        assert r.returncode == 1, \
            'el escáner NO detecta un secreto sembrado: los demás checks son vacíos'
    s.check('control positivo: el escáner sí detecta un secreto sembrado',
            scanner_actually_works)

    def no_env_files():
        malos = [os.path.relpath(p, ROOT) for p in walk(ROOT)
                 if os.path.basename(p) in ('.env', '.env.local', 'credentials.json',
                                            'secrets.json', 'id_rsa')]
        assert not malos, f'ficheros de credenciales en el paquete: {malos}'
    s.check('no viaja ningún .env ni fichero de credenciales', no_env_files)

    # ══ artefactos generados ══════════════════════════════════════════
    s.section('no se cuelan artefactos generados')

    def no_generated():
        malos = [os.path.relpath(p, ROOT) for p in walk(ROOT)
                 if p.endswith(GENERATED) or '__pycache__' in p]
        assert not malos, f'artefactos generados en el paquete: {malos}'
    s.check('sin .session_key, .pyc ni __pycache__', no_generated)

    # ══ documentación ═════════════════════════════════════════════════
    s.section('documentación completa')

    def docs_present():
        faltan, vacios = [], []
        for d in REQUIRED_DOCS:
            p = os.path.join(DOCS, d)
            if not os.path.exists(p):
                faltan.append(d)
            elif os.path.getsize(p) < 500:
                vacios.append(d)
        assert not faltan, f'documentos que faltan: {faltan}'
        assert not vacios, f'documentos prácticamente vacíos: {vacios}'
    s.check(f'los {len(REQUIRED_DOCS)} documentos exigidos existen y tienen contenido',
            docs_present)

    def manifest_present():
        p = os.path.join(ROOT, 'MANIFEST.md')
        assert os.path.exists(p), 'falta MANIFEST.md en la raíz del paquete'
        txt = open(p, encoding='utf-8').read()
        for name in EXPECTED_WORKFLOWS:
            assert name in txt, f'MANIFEST.md no menciona {name}'
        for d in REQUIRED_DOCS:
            assert d in txt, f'MANIFEST.md no menciona docs/{d}'
        assert 'migration.sql' in txt and 'rollback.sql' in txt, \
            'MANIFEST.md no menciona los ficheros SQL'
    s.check('MANIFEST.md existe y nombra workflows, docs y SQL', manifest_present)

    def install_doc_is_usable():
        txt = open(os.path.join(DOCS, 'INSTALL_STAGING.md'), encoding='utf-8').read()
        for marcador in ('migration.sql', 'v2_suite', 'active', 'Credential'):
            assert marcador.lower() in txt.lower(), \
                f'INSTALL_STAGING.md no explica {marcador}'
    s.check('INSTALL_STAGING.md cubre migración, panel, credenciales y activación',
            install_doc_is_usable)

    # ══ SQL ═══════════════════════════════════════════════════════════
    s.section('SQL entregable')

    def sql_files_present():
        for rel in ('sql/migration.sql', 'sql/rollback.sql',
                    'sql/parts/001_multi_country_config_v2_2.sql',
                    'sql/parts/002_callcenter_suite_v2.sql',
                    'sql/parts/003_legacy_compat_tables.sql',
                    'sql/parts/004_billing_v2.sql',
                    'sql/parts/005_legacy_backup_v2.sql',
                    'sql/parts/006_payments_v2.sql'):
            p = os.path.join(ROOT, rel)
            assert os.path.exists(p) and os.path.getsize(p) > 1000, f'falta o vacío: {rel}'
    s.check('migration.sql, rollback.sql y las 6 partes están', sql_files_present)

    def migration_is_the_concatenation():
        partes = ''
        for rel in ('001_multi_country_config_v2_2.sql', '002_callcenter_suite_v2.sql',
                    '003_legacy_compat_tables.sql', '004_billing_v2.sql',
                    '005_legacy_backup_v2.sql', '006_payments_v2.sql'):
            partes += open(os.path.join(ROOT, 'sql', 'parts', rel),
                           encoding='utf-8').read()
        entero = open(os.path.join(ROOT, 'sql', 'migration.sql'), encoding='utf-8').read()

        def norm(t):
            return re.sub(r'\s+', ' ', t)
        for bloque in re.findall(r'CREATE TABLE IF NOT EXISTS `(\w+)`', partes):
            assert bloque in entero, \
                f'migration.sql no incluye la tabla {bloque} de las partes'
    s.check('migration.sql contiene todas las tablas de las partes',
            migration_is_the_concatenation)

    def rollback_is_not_destructive_by_default():
        txt = open(os.path.join(ROOT, 'sql', 'rollback.sql'), encoding='utf-8').read()
        activo = []
        for ln in txt.splitlines():
            t = ln.strip()
            if t.startswith('--') or not t:
                continue
            if re.match(r'(?i)^(DROP|TRUNCATE|DELETE)\b', t):
                activo.append(t[:80])
        assert not activo, \
            'rollback.sql trae sentencias destructivas SIN comentar: ' + '; '.join(activo)
    s.check('rollback.sql no ejecuta nada destructivo sin descomentar',
            rollback_is_not_destructive_by_default)

    return s.finish()


if __name__ == '__main__':
    sys.exit(main())
