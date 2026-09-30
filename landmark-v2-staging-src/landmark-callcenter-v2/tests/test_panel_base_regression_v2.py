#!/usr/bin/env python3
"""NO-REGRESIÓN del panel base.

El panel real (panel-share) es la aplicación base. V2 se integra DENTRO.
Esta suite existe para demostrar —no afirmar— que integrarlo no cambió
nada de lo que ya funcionaba:

  · los ficheros que no debían tocarse siguen byte a byte idénticos
  · las 41 rutas Flask del panel real siguen existiendo
  · las 81 funciones de analytics.py y las 7 de charts.py siguen ahí
  · Extensions (§68) y Support (§69) intactos: rutas, plantilla y lógica
  · la navegación original no perdió ningún enlace
  · las vistas del panel real siguen respondiendo (§70)

Dos ficheros SÍ cambian, a propósito y de forma aditiva: `server.py`
(engancha V2) y `base.html` (añade enlaces). Para esos dos, en vez de
comparar huellas, se exige que todo lo anterior siga presente.
"""
import hashlib
import json
import os
import re
import sys

sys.dont_write_bytecode = True
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from _suite import Suite, ROOT, APP                      # noqa: E402

PANEL = os.path.join(ROOT, 'panel')
CHECKSUMS = os.path.join(PANEL, 'PANEL_BASE_CHECKSUMS.sha256')
CONTRACT = os.path.join(PANEL, 'PANEL_BASE_CONTRACT.json')

# Los dos únicos ficheros del panel base que V2 puede cambiar, y por qué.
ALLOWED_DIVERGENCE = {
    'app/server.py':
        'dos cambios, ambos verificados abajo. (1) Engancha v2_suite: '
        'import + register(); aditivo, las 41 rutas del panel real siguen '
        'registradas. (2) Ninguna contraseña tiene ya valor por defecto en '
        'el código: LM_DB_PASS, LM_PASS, LM_MASTER_PASS y LM_SUPPORT_PASS '
        'eran literales en CFG y viajaban en cada copia del paquete. Ahora '
        'pasan por _required_secret(), que detiene el arranque con un '
        'mensaje legible si falta alguna.',
    'app/templates/base.html':
        'añade enlaces de navegación de V2. Aditivo: se comprueba abajo '
        'que los 8 enlaces originales siguen presentes.',
    'deploy/install.sh':
        'dos cambios, ambos verificados abajo. (1) El original traía la '
        'contraseña de la BD en claro como valor por defecto '
        '(${LM_DB_PASS:-...}); prohibido por §61, ahora es obligatoria por '
        'entorno y el instalador se detiene si falta. (2) El .env se '
        'FUSIONA en vez de reescribirse: la versión anterior hacía '
        '`cat > .env` con nueve variables y, sobre una instalación ya '
        'configurada, borraba LM_MASTER_PASS, LM_SUPPORT_PASS, '
        'LM_ROUTES_API_TOKEN y la configuración de n8n, y regeneraba '
        'LM_SECRET deslogueando a todo el mundo.',
}

# Lo que el panel base aporta y no viaja igual en el árbol de V2.
#   tests/            → v1 trae su propia batería; V2 trae la suya
#   deploy/schema.sql → viaja saneado en sql/legacy/, no bajo panel/
#   README.md         → panel/README.md es el de la FUNDACIÓN; el del
#                       panel real viaja como panel/README_PANEL_REAL.md
#                       y se compara aparte, byte a byte
NOT_SHIPPED_PREFIXES = ('tests/', 'deploy/schema.sql')
SHIPPED_ELSEWHERE = {
    'README.md': 'README_PANEL_REAL.md',
    'deploy.sh': 'deploy.sh',
}


def sha256(path):
    h = hashlib.sha256()
    with open(path, 'rb') as fh:
        for c in iter(lambda: fh.read(1 << 20), b''):
            h.update(c)
    return h.hexdigest()


def load_checksums():
    """{ruta relativa al panel real: sha256}"""
    out = {}
    with open(CHECKSUMS, encoding='utf-8') as fh:
        for ln in fh:
            ln = ln.strip()
            if not ln:
                continue
            digest, path = ln.split(None, 1)
            out[path.lstrip('./')] = digest
    return out


def main():
    s = Suite('no-regresión del panel base')

    if not os.path.exists(CHECKSUMS) or not os.path.exists(CONTRACT):
        s.skip('no-regresión del panel base',
               'faltan PANEL_BASE_CHECKSUMS.sha256 / PANEL_BASE_CONTRACT.json')
        return s.finish()

    base = load_checksums()
    contract = json.load(open(CONTRACT, encoding='utf-8'))
    server_src = open(os.path.join(APP, 'server.py'), encoding='utf-8').read()

    # ══ ficheros intactos ═════════════════════════════════════════════
    s.section('los ficheros del panel base no se tocaron')

    def unchanged_files():
        cambiados, faltan = [], []
        for rel, digest in sorted(base.items()):
            if rel in ALLOWED_DIVERGENCE or rel.startswith(NOT_SHIPPED_PREFIXES):
                continue
            # panel-share/X  ->  panel/X   (deploy.sh y README quedan aparte)
            if rel in SHIPPED_ELSEWHERE:
                continue
            cand = os.path.join(PANEL, rel)
            if not os.path.exists(cand):
                faltan.append(rel)
                continue
            if sha256(cand) != digest:
                cambiados.append(rel)
        assert not faltan, f'ficheros del panel base que no viajan: {faltan}'
        assert not cambiados, (
            'ficheros del panel base MODIFICADOS (deberían ser byte a byte '
            f'idénticos): {cambiados}')
    s.check('cada fichero preservado coincide byte a byte con panel-share',
            unchanged_files)

    def divergence_is_declared():
        """Que los dos ficheros que cambian sean EXACTAMENTE los declarados."""
        reales = []
        for rel, digest in base.items():
            if rel.startswith(NOT_SHIPPED_PREFIXES) or rel in SHIPPED_ELSEWHERE:
                continue
            cand = os.path.join(PANEL, rel)
            if os.path.exists(cand) and sha256(cand) != digest:
                reales.append(rel)
        assert set(reales) == set(ALLOWED_DIVERGENCE), (
            f'divergencia no declarada. Cambian: {sorted(reales)}. '
            f'Declarados: {sorted(ALLOWED_DIVERGENCE)}')
    s.check('los ficheros que divergen son exactamente los 3 declarados',
            divergence_is_declared)

    def shipped_elsewhere_is_identical():
        malos = []
        for rel, destino in SHIPPED_ELSEWHERE.items():
            cand = os.path.join(PANEL, destino)
            if not os.path.exists(cand):
                malos.append(f'{rel} no viaja como {destino}')
            elif sha256(cand) != base[rel]:
                malos.append(f'{destino} no coincide con el {rel} original')
        assert not malos, '; '.join(malos)
    s.check('README y deploy.sh del panel real viajan byte a byte',
            shipped_elsewhere_is_identical)

    def install_sh_changes_are_the_two_declared():
        """Los dos cambios declarados en install.sh, y ningún otro.

        (1) ninguna contraseña por defecto; (2) el .env se fusiona.
        Todo lo demás —dependencias, venv, esquema, unidad de systemd,
        comprobación de salud— tiene que seguir igual.
        """
        import difflib
        ORIG = '/home/claude/src/panel-real/panel-share/deploy/install.sh'
        mio = open(os.path.join(PANEL, 'deploy', 'install.sh'), encoding='utf-8').read()

        # ── (1) ninguna contraseña por defecto ───────────────────────
        assert 'LM_DB_PASS:-}' in mio or 'LM_DB_PASS:-"}' in mio, \
            'install.sh volvió a traer una contraseña por defecto'
        assert not re.search(r'DB_PASS="\$\{LM_DB_PASS:-[^}]+\}"', mio), \
            'install.sh trae un valor por defecto en claro para DB_PASS'
        assert 'Falta LM_DB_PASS' in mio, \
            'install.sh no avisa cuando falta LM_DB_PASS'

        # ── (2) el .env se fusiona, no se reescribe ──────────────────
        assert not re.search(r'cat\s*>\s*"?\$\{?(ENV_FILE|APP_DIR)', mio), \
            'install.sh vuelve a reescribir el .env entero'
        for pieza, porque in (
                ('env_set_if_empty LM_PASS',   'resetearía la contraseña del panel'),
                ('env_set_if_empty LM_SECRET', 'deslogearía a todo el mundo'),
                ('env_set_if_empty LM_ROUTES_API_TOKEN',
                                               'rotaría el token de servicio'),
                ('.bak.',                      'no respalda el .env anterior')):
            assert pieza in mio, f'install.sh {porque} ({pieza} no está)'
        for preservada in ('LM_MASTER_PASS', 'LM_SUPPORT_PASS',
                           'LM_N8N_BASE_URL', 'LM_N8N_API_KEY'):
            assert preservada in mio, \
                f'install.sh ya no contempla {preservada}'

        # ── nada más cambió ──────────────────────────────────────────
        if not os.path.exists(ORIG):
            return
        real = open(ORIG, encoding='utf-8').read()

        def zona_declarada(txt):
            """Índices de línea donde los dos cambios pueden vivir:
            el bloque de variables de cabecera, la fase de configuración
            (5/6) y el bloque final que hablaba de la contraseña."""
            lineas = txt.splitlines()
            marcas = {}
            for i, ln in enumerate(lineas):
                for clave, patron in (('cab', r'^PANEL_PASS='),
                                      ('cfg', r'^c_head "5/6'),
                                      ('fin_cfg', r'^c_head "6/6'),
                                      ('tail', r'^\s*echo "  Usuario:')):
                    if clave not in marcas and re.match(patron, ln):
                        marcas[clave] = i
            ok = set(range(0, marcas.get('cab', 0) + 1))
            if 'cfg' in marcas:
                ok |= set(range(marcas['cfg'], marcas.get('fin_cfg', len(lineas))))
            if 'tail' in marcas:
                ok |= set(range(marcas['tail'], len(lineas)))
            return ok

        z_real, z_mio = zona_declarada(real), zona_declarada(mio)
        a, b = real.splitlines(), mio.splitlines()
        fuera = []
        for op, i1, i2, j1, j2 in difflib.SequenceMatcher(None, a, b).get_opcodes():
            if op == 'equal':
                continue
            fuera += [f'- {a[i]}' for i in range(i1, i2) if i not in z_real]
            fuera += [f'+ {b[j]}' for j in range(j1, j2) if j not in z_mio]
        assert not fuera, (
            'install.sh cambió fuera de la contraseña y del manejo del .env: '
            f'{fuera[:6]}')
    s.check('install.sh: sólo los dos cambios declarados (contraseña y .env)',
            install_sh_changes_are_the_two_declared)

    def server_py_has_no_password_defaults():
        """El otro cambio declarado en server.py: ninguna contraseña con
        valor por defecto. El original las traía en claro dentro de CFG."""
        for var in ('LM_DB_PASS', 'LM_PASS', 'LM_MASTER_PASS', 'LM_SUPPORT_PASS'):
            m = re.search(rf"getenv\(\s*['\"]{var}['\"]\s*,", server_src)
            assert not m, f'{var} volvió a tener un valor por defecto en server.py'
            assert f"_required_secret('{var}'" in server_src, \
                f'{var} ya no pasa por _required_secret()'
        assert 'raise SystemExit' in server_src, \
            'server.py no detiene el arranque cuando falta un secreto'
    s.check('server.py: ninguna contraseña con valor por defecto',
            server_py_has_no_password_defaults)

    # ══ rutas ═════════════════════════════════════════════════════════
    s.section('las rutas del panel real siguen existiendo')

    shipped_routes = set(re.findall(r"@app\.route\('([^']+)'", server_src))

    def all_routes_present():
        faltan = [r for r in contract['routes'] if r not in shipped_routes]
        assert not faltan, f'rutas del panel real que V2 perdió: {faltan}'
    s.check(f"las {len(contract['routes'])} rutas del panel real siguen registradas",
            all_routes_present)

    def v2_is_additive():
        nuevas = sorted(shipped_routes - set(contract['routes']))
        assert nuevas, 'V2 no añadió ninguna ruta: el enganche no está'
        intrusas = [r for r in nuevas
                    if not r.startswith(('/callcenter/', '/api/'))]
        assert not intrusas, (
            f'V2 añadió rutas fuera de su espacio (/callcenter/*, /api/*): {intrusas}')
    s.check('lo que V2 añade vive bajo /callcenter/* y /api/*', v2_is_additive)

    # ══ Extensions · §68 ══════════════════════════════════════════════
    s.section('Extensions intacto (§68)')

    EXT_ROUTES = ['/sip/extensions', '/sip/extensions/new',
                  '/sip/extensions/<int:extension_id>/toggle',
                  '/sip/extensions/<int:extension_id>/delete',
                  '/sip/extensions/reapply']
    EXT_FUNCS = ['ensure_sip_extensions_table', 'sip_extensions_list',
                 'sip_extension_create', 'sip_extension_toggle',
                 'sip_extension_delete', 'sip_extension_consumption',
                 'sip_extensions_apply']

    def extensions_routes():
        faltan = [r for r in EXT_ROUTES if r not in shipped_routes]
        assert not faltan, f'rutas de Extensions perdidas: {faltan}'
    s.check('las 5 rutas de Extensions siguen registradas', extensions_routes)

    def extensions_logic():
        an = open(os.path.join(APP, 'analytics.py'), encoding='utf-8').read()
        faltan = [f for f in EXT_FUNCS if f'def {f}(' not in an]
        assert not faltan, f'funciones de Extensions perdidas: {faltan}'
    s.check('la lógica de Extensions (7 funciones) está sin tocar',
            extensions_logic)

    def extensions_template():
        p = os.path.join(APP, 'templates', 'extensions.html')
        assert os.path.exists(p), 'falta extensions.html'
        assert sha256(p) == base['app/templates/extensions.html'], \
            'extensions.html fue modificado'
    s.check('extensions.html byte a byte idéntico', extensions_template)

    def v2_does_not_touch_extensions():
        """Ningún módulo de V2 escribe en las tablas de Extensions."""
        malos = []
        for mod in ('v2_suite.py', 'wf_settings.py', 'tool_requests.py',
                    'recording_ledger.py', 'analytics_v2.py'):
            p = os.path.join(APP, mod)
            if not os.path.exists(p):
                continue
            txt = open(p, encoding='utf-8').read()
            for tabla in ('sip_extensions',):
                if re.search(rf'(?i)\b(INSERT INTO|UPDATE|DELETE FROM|ALTER TABLE)\s+`?{tabla}\b', txt):
                    malos.append(f'{mod} escribe en {tabla}')
        assert not malos, '; '.join(malos)
    s.check('ningún módulo de V2 escribe en las tablas de Extensions',
            v2_does_not_touch_extensions)

    # ══ Support · §69 ═════════════════════════════════════════════════
    s.section('Support intacto (§69)')

    SUP_ROUTES = ['/support', '/support/account-open', '/support/payment-link',
                  '/support/settings']

    def support_routes():
        faltan = [r for r in SUP_ROUTES if r not in shipped_routes]
        assert not faltan, f'rutas de Support perdidas: {faltan}'
    s.check('las 4 rutas de Support siguen registradas', support_routes)

    def support_template():
        p = os.path.join(APP, 'templates', 'support.html')
        assert sha256(p) == base['app/templates/support.html'], \
            'support.html fue modificado'
    s.check('support.html byte a byte idéntico', support_template)

    def v2_does_not_touch_support():
        malos = []
        for mod in ('v2_suite.py', 'wf_settings.py', 'tool_requests.py',
                    'recording_ledger.py', 'analytics_v2.py'):
            p = os.path.join(APP, mod)
            if not os.path.exists(p):
                continue
            txt = open(p, encoding='utf-8').read()
            for tabla in ('support_actions', 'telegram_sessions'):
                if re.search(rf'(?i)\b(INSERT INTO|UPDATE|DELETE FROM|ALTER TABLE)\s+`?{tabla}\b', txt):
                    malos.append(f'{mod} escribe en {tabla}')
        assert not malos, '; '.join(malos)
    s.check('ningún módulo de V2 escribe en las tablas de Support',
            v2_does_not_touch_support)

    # ══ dashboard, SIP Balance, Call Center ═══════════════════════════
    s.section('dashboard, SIP Balance y Call Center intactos')

    def analytics_functions_present():
        an = open(os.path.join(APP, 'analytics.py'), encoding='utf-8').read()
        faltan = [f for f in contract['analytics_functions'] if f'def {f}(' not in an]
        assert not faltan, f'funciones de analytics.py perdidas: {faltan}'
    s.check(f"las {len(contract['analytics_functions'])} funciones de analytics.py están",
            analytics_functions_present)

    def charts_functions_present():
        ch = open(os.path.join(APP, 'charts.py'), encoding='utf-8').read()
        faltan = [f for f in contract['charts_functions'] if f'def {f}(' not in ch]
        assert not faltan, f'funciones de charts.py perdidas: {faltan}'
    s.check('las 7 funciones de charts.py están', charts_functions_present)

    def breakdown_by_hour_alive():
        an = open(os.path.join(APP, 'analytics.py'), encoding='utf-8').read()
        assert 'def hourly_profile(' in an, 'se perdió hourly_profile (Breakdown by Hour)'
        assert "'hourly'" in an, "build_report ya no emite 'hourly'"
    s.check('Breakdown by Hour sigue en el panel base (§32)', breakdown_by_hour_alive)

    def exports_alive():
        faltan = [r for r in ('/export/daily.csv', '/export/conversions.csv',
                              '/export/summary.csv') if r not in shipped_routes]
        assert not faltan, f'exports perdidos: {faltan}'
    s.check('los 3 exports CSV siguen registrados (§33)', exports_alive)

    def sip_balance_alive():
        an = open(os.path.join(APP, 'analytics.py'), encoding='utf-8').read()
        for f in ('ensure_sip_tables', 'sip_provider_balance', 'sip_deposit_add',
                  'sip_provider_pricing_upsert', 'sip_providers_list'):
            assert f'def {f}(' in an, f'SIP Balance perdió {f}'
        p = os.path.join(APP, 'templates', 'sip.html')
        assert sha256(p) == base['app/templates/sip.html'], 'sip.html fue modificado'
    s.check('SIP Balance intacto: lógica y plantilla (§40)', sip_balance_alive)

    def legacy_switches_alive():
        an = open(os.path.join(APP, 'analytics.py'), encoding='utf-8').read()
        for f in ('ensure_n8n_switches_table', 'n8n_switches_with_status',
                  'n8n_switch_set_state', 'schedule_upsert', 'run_due_schedules',
                  'n8n_switch_create', 'n8n_switch_delete'):
            assert f'def {f}(' in an, f'Call Center legacy perdió {f}'
        p = os.path.join(APP, 'templates', 'callcenter.html')
        assert sha256(p) == base['app/templates/callcenter.html'], \
            'callcenter.html fue modificado'
    s.check('los switches legacy y sus horarios siguen intactos (§46)',
            legacy_switches_alive)

    # ══ navegación ════════════════════════════════════════════════════
    s.section('navegación')

    def nav_links_preserved():
        html = open(os.path.join(APP, 'templates', 'base.html'), encoding='utf-8').read()
        faltan = [h for h in contract['base_nav_links'] if h not in html]
        assert not faltan, f'enlaces de navegación perdidos: {faltan}'
    s.check('base.html conserva los 8 enlaces originales', nav_links_preserved)

    return s.finish()


if __name__ == '__main__':
    sys.exit(main())
