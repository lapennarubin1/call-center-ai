#!/usr/bin/env python3
"""Genera PANEL_PRODUCTION_PATCH_MANIFEST.md contra el panel REAL.

Se genera, no se escribe a mano: un inventario de despliegue escrito a
mano envejece en la primera modificación y nadie se entera hasta que el
panel no arranca en producción.

La línea base es `panel/PANEL_BASE_CHECKSUMS.sha256`, las huellas del
`landmark-panel-safe.tar.gz` que corre hoy.
"""
import hashlib
import os
import sys

# Correr una herramienta no puede dejar .pyc dentro de lo que se
# entrega: tests/test_package_hygiene_v2.py lo comprueba.
sys.dont_write_bytecode = True
os.environ['PYTHONDONTWRITEBYTECODE'] = '1'

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.abspath(os.path.join(HERE, '..'))
PANEL = os.path.join(ROOT, 'panel')
CHECKSUMS = os.path.join(PANEL, 'PANEL_BASE_CHECKSUMS.sha256')

# Por qué cambia cada fichero que NO es nuevo. Un "modificado" sin motivo
# escrito es un fichero que nadie sabe si hay que copiar.
MOTIVOS = {
    'app/server.py':
        'Tres cambios, todos acotados: (1) import de v2_suite y llamada a '
        'v2.register() al final — el enganche de V2; (2) las cuatro '
        'contraseñas que estaban escritas en el código pasan a ser '
        'obligatorias por entorno; (3) se añade LM_DB_PORT. Las 41 rutas '
        'originales siguen todas, y hay un test que lo comprueba.',
    'app/templates/base.html':
        'Añade los enlaces de navegación de V2. Los 8 originales siguen.',
    'README.md':
        'Es el README de la FUNDACIÓN, no una modificación del que hay en '
        'producción. El original viaja intacto como README_PANEL_REAL.md. '
        'No hace falta copiarlo.',
    'deploy/install.sh':
        'La contraseña de la BD dejaba de venir en claro, y el .env pasa a '
        'FUSIONARSE en vez de sobrescribirse: antes borraba LM_MASTER_PASS, '
        'LM_SUPPORT_PASS, LM_N8N_* y LM_ROUTES_API_TOKEN en cada pasada.',
}

# Qué aporta cada módulo nuevo, para que no sea una lista de nombres.
QUE_HACE = {
    'app/routes_config.py': 'países, proveedores, rutas, validación y el cerrojo del modo',
    'app/call_jobs.py': 'claim atómico de llamadas y máquina de estados',
    'app/followup_engine.py': 'política de reintentos, provider-neutral',
    'app/ops_events.py': 'eventos de negocio idempotentes',
    'app/analytics_v2.py': 'analítica local con filtros por país/proveedor/ruta',
    'app/v2_suite.py': 'registra las vistas y la API de V2; instala el cerrojo',
    'app/billing.py': 'modelos de facturación y vínculo con SIP Balance',
    'app/legacy_mode.py': 'modo de operación, clasificación legacy y guard',
    'app/payments.py': 'adaptadores de pago: OkPay y router universal',
    'app/wf_settings.py': 'parámetros operativos del suite',
    'app/crm_notes.py': 'las frases en inglés que van al CRM',
    'app/tool_requests.py': 'claim idempotente de las tools de país',
    'app/recording_ledger.py': 'correlación y claim de grabaciones',
    'app/templates/routes.html': 'pantalla de países y rutas',
    'app/templates/country.html': 'ficha de un país, con validación READY',
    'app/templates/analytics.html': 'analítica V2, con la barra de filtros',
    'app/templates/issues.html': 'issues de reconciliación',
    'app/templates/settings.html': 'parámetros operativos',
    'app/templates/billing.html': 'facturación y vínculo con SIP Balance',
    'app/templates/legacy.html': 'modo de operación y Legacy Backup',
    '.env.example': 'plantilla de entorno, sin valores reales. Referencia, '
                    'no se copia sobre el .env',
    'PANEL_BASE_CONTRACT.json': 'las rutas y funciones del panel real, '
                                'congeladas. Lo usa la prueba de no-regresión',
    'README_PANEL_REAL.md': 'el README del panel real, conservado tal cual',
    'deploy.sh': 'script de despliegue del panel real, conservado tal cual',
    'deploy/requirements.txt': 'dependencias, sin cambios respecto de v1',
    'deploy/WF14_Sync_Panel.json': 'workflow de sincronización de v1, conservado',
    'deploy/WF14_Sync_Panel_v2.json': 'workflow de sincronización de v1, conservado',
    'deploy/install.sh': 'instalador',
    'upgrade_callcenter_v2.sh':
        'actualiza el panel EN EL VPS sin tocar .env, .session_key, venv, logs ni datos. Valida que el panel importa antes de reemplazar nada, y con DRY_RUN=1 sólo enseña qué haría',
}


def sha256(path):
    h = hashlib.sha256()
    with open(path, 'rb') as fh:
        for c in iter(lambda: fh.read(1 << 20), b''):
            h.update(c)
    return h.hexdigest()


def load_base():
    out = {}
    with open(CHECKSUMS, encoding='utf-8') as fh:
        for ln in fh:
            ln = ln.strip()
            if not ln:
                continue
            d, p = ln.split(None, 1)
            out[p.lstrip('./')] = d
    return out


def walk_panel():
    for dp, dn, fn in os.walk(PANEL):
        dn[:] = [d for d in dn if d not in ('__pycache__', 'tests', 'migrations')]
        for f in fn:
            full = os.path.join(dp, f)
            rel = os.path.relpath(full, PANEL)
            if f in ('.session_key',) or f.endswith(('.pyc', '.sha256')):
                continue
            yield rel, full


def main():
    base = load_base()
    nuevos, modificados, iguales = [], [], []
    for rel, full in sorted(walk_panel()):
        if rel in base:
            (iguales if sha256(full) == base[rel] else modificados).append(rel)
        else:
            nuevos.append(rel)

    total = len(nuevos) + len(modificados) + len(iguales)
    lineas = []
    w = lineas.append
    w('# PANEL_PRODUCTION_PATCH_MANIFEST')
    w('')
    w('**Qué hay que copiar al panel de producción, exactamente.**')
    w('')
    w('Generado por `tools/make_patch_manifest.py` comparando este paquete')
    w('contra `panel/PANEL_BASE_CHECKSUMS.sha256`, que son las huellas del')
    w('`landmark-panel-safe.tar.gz` — el panel que corre hoy en producción.')
    w('')
    w('> **No se escribe a mano.** Un inventario de despliegue escrito a mano')
    w('> envejece en la primera modificación, y nadie se entera hasta que el')
    w('> panel no arranca en producción.')
    w('')
    w('---')
    w('')
    w('## Resumen')
    w('')
    w('| | Ficheros |')
    w('|---|---:|')
    w(f'| 🆕 **NUEVOS** — no existen en producción | **{len(nuevos)}** |')
    w(f'| ✏️ **MODIFICADOS** — existen y cambian | **{len(modificados)}** |')
    w(f'| ✅ **SIN CAMBIOS** — byte a byte idénticos | **{len(iguales)}** |')
    w(f'| | **{total}** |')
    w('')
    w('> El panel de producción **no tiene** los módulos de la Fundación V2.')
    w('> Un plan que diga «sólo cambian unas líneas de `server.py`» es falso:')
    w(f'> hay **{len(nuevos)} ficheros nuevos** que copiar. Copiar `server.py`')
    w('> sin ellos deja el panel sin arrancar, porque importa módulos que no')
    w('> existen.')
    w('')
    w('---')
    w('')
    w(f'## 1. NUEVOS — {len(nuevos)} ficheros')
    w('')
    w('No existen en el panel de producción. **Hay que copiarlos todos.**')
    w('')
    w('| Fichero | Qué aporta |')
    w('|---|---|')
    for r in nuevos:
        w(f'| `{r}` | {QUE_HACE.get(r, "—")} |')
    w('')
    w('---')
    w('')
    w(f'## 2. MODIFICADOS — {len(modificados)} ficheros')
    w('')
    w('Existen en producción y **cambian**. Hacer copia antes de sustituir.')
    w('')
    for r in modificados:
        w(f'### `{r}`')
        w('')
        w(MOTIVOS.get(r, '_Sin motivo documentado — revisar antes de copiar._'))
        w('')
    w('---')
    w('')
    w(f'## 3. SIN CAMBIOS — {len(iguales)} ficheros')
    w('')
    w('Byte a byte idénticos a producción. **No hace falta copiarlos**, y')
    w('copiarlos tampoco rompe nada.')
    w('')
    for r in iguales:
        w(f'- `{r}`')
    w('')
    w('---')
    w('')
    w('## 4. Cómo aplicarlo')
    w('')
    w('No a mano. `panel/upgrade_callcenter_v2.sh` hace exactamente esto,')
    w('con respaldo, validación previa al reinicio y modo de prueba:')
    w('')
    w('```bash')
    w('sudo DRY_RUN=1 bash panel/upgrade_callcenter_v2.sh   # enseña qué haría')
    w('sudo bash panel/upgrade_callcenter_v2.sh             # lo hace')
    w('```')
    w('')
    w('## 5. Lo que este parche NO toca')
    w('')
    w('- el `.env` (se conserva y se respalda)')
    w('- el virtualenv')
    w('- logs y ficheros de ejecución')
    w('- los datos de Extensions y Support')
    w('- los grupos de workflows legacy y sus horarios')
    w('- la base de datos: el SQL va aparte y es una decisión separada')
    w('')

    dest = os.path.join(ROOT, 'PANEL_PRODUCTION_PATCH_MANIFEST.md')
    with open(dest, 'w', encoding='utf-8') as fh:
        fh.write('\n'.join(lineas) + '\n')
    print(f'{dest}')
    print(f'  nuevos {len(nuevos)} · modificados {len(modificados)} · iguales {len(iguales)}')
    return 0


if __name__ == '__main__':
    sys.exit(main())
