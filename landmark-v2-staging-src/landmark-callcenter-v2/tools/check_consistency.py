#!/usr/bin/env python3
"""Comprueba que los números del paquete coinciden con la realidad.

Existe porque un recuento escrito a mano envejece en cuanto alguien toca
un workflow o añade un test, y nadie se entera. Este comprobador compara:

  · BUILD_REPORT.json   contra los JSON reales
  · MANIFEST.md         contra los JSON reales
  · MANIFEST.md         contra tests/TEST_RESULTS.json
  · los 7 workflows     active == false
  · el escáner de secretos
  · el código del panel  sin contraseñas por defecto
  · la migración         modo inicial LEGACY_BACKUP

Con --fix reescribe los recuentos de MANIFEST y BUILD_REPORT en vez de
sólo quejarse.

Salida: 0 si todo cuadra, 1 si no.
"""
import argparse
import json
import os
import re
import subprocess
import sys

# Correr una herramienta no puede dejar .pyc dentro de lo que se
# entrega: tests/test_package_hygiene_v2.py lo comprueba.
sys.dont_write_bytecode = True
os.environ['PYTHONDONTWRITEBYTECODE'] = '1'

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.abspath(os.path.join(HERE, '..'))
WF = os.path.join(ROOT, 'workflows')
STICKY = 'n8n-nodes-base.stickyNote'


def real_workflows():
    """Lo que dicen los JSON, que es la única verdad."""
    out = {}
    for f in sorted(os.listdir(WF)):
        if not f.endswith('.json') or f == 'BUILD_REPORT.json':
            continue
        wf = json.load(open(os.path.join(WF, f), encoding='utf-8'))
        nodos = [n for n in wf['nodes'] if n['type'] != STICKY]
        conexiones = sum(len(r or []) for c in wf.get('connections', {}).values()
                         for r in c.get('main', []))
        out[wf['name']] = {'file': f, 'nodes': len(nodos),
                           'sticky_notes': len(wf['nodes']) - len(nodos),
                           'connections': conexiones,
                           'active': bool(wf.get('active'))}
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--fix', action='store_true',
                    help='reescribe los recuentos en vez de sólo avisar')
    args = ap.parse_args()

    fallos, avisos, arreglados = [], [], []
    reales = real_workflows()
    tn = sum(w['nodes'] for w in reales.values())
    ts = sum(w['sticky_notes'] for w in reales.values())

    # ── 1 · BUILD_REPORT contra los JSON ─────────────────────────────
    rp = os.path.join(WF, 'BUILD_REPORT.json')
    if not os.path.exists(rp):
        fallos.append('falta workflows/BUILD_REPORT.json')
    else:
        rep = json.load(open(rp, encoding='utf-8'))
        filas = {r['workflow']: r for r in rep} if isinstance(rep, list) else rep
        malos = []
        for nombre, real in reales.items():
            r = filas.get(nombre)
            if not r:
                malos.append(f'{nombre}: no está en BUILD_REPORT')
                continue
            for campo in ('nodes', 'sticky_notes'):
                if r.get(campo) != real[campo]:
                    malos.append(f'{nombre}.{campo}: report={r.get(campo)} '
                                 f'json={real[campo]}')
        sobran = set(filas) - set(reales)
        if sobran:
            malos.append(f'BUILD_REPORT tiene workflows que ya no existen: {sorted(sobran)}')
        if malos and args.fix:
            nuevo = [{'workflow': n, 'file': w['file'], 'nodes': w['nodes'],
                      'sticky_notes': w['sticky_notes'],
                      'connections': w['connections'], 'issues': []}
                     for n, w in sorted(reales.items())]
            json.dump(nuevo, open(rp, 'w', encoding='utf-8'), indent=2,
                      ensure_ascii=False)
            arreglados.append(f'BUILD_REPORT.json regenerado ({len(malos)} desfases)')
        elif malos:
            fallos.extend(malos)

    # ── 2 · MANIFEST contra los JSON y contra TEST_RESULTS ───────────
    mp = os.path.join(ROOT, 'MANIFEST.md')
    tr = os.path.join(ROOT, 'tests', 'TEST_RESULTS.json')
    if not os.path.exists(mp):
        fallos.append('falta MANIFEST.md')
    else:
        man = open(mp, encoding='utf-8').read()
        original = man

        # workflows
        for patron, valor, que in (
                (r'7 · (\d+) nodos · (\d+) notas', (tn, ts), 'cabecera'),
                (r'\| \*\*TOTAL\*\* \| \*\*(\d+)\*\* \| \*\*(\d+)\*\*', (tn, ts), 'tabla')):
            m = re.search(patron, man)
            if not m:
                avisos.append(f'MANIFEST: no encuentro el total de workflows ({que})')
            elif tuple(int(g) for g in m.groups()) != valor:
                if args.fix:
                    man = man.replace(m.group(0), m.group(0)
                                      .replace(m.group(1), str(valor[0]))
                                      .replace(m.group(2), str(valor[1])))
                    arreglados.append(f'MANIFEST {que}: {m.group(1)}/{m.group(2)} → {tn}/{ts}')
                else:
                    fallos.append(f'MANIFEST {que} dice {m.group(1)} nodos/'
                                  f'{m.group(2)} notas; los JSON dicen {tn}/{ts}')

        # tests
        if not os.path.exists(tr):
            avisos.append('falta tests/TEST_RESULTS.json: no puedo verificar '
                          'los recuentos de tests del MANIFEST')
        else:
            res = json.load(open(tr, encoding='utf-8'))
            total = res['total']['pass']
            suites = len(res['suites'])
            # Números que dicen ser el total del PAQUETE. Se excluyen las
            # líneas que acotan el recuento a una parte —la fundación, o
            # las suites de V2 por separado—, que son legítimamente
            # distintas del total y se verifican fila por fila más abajo.
            ACOTADO = re.compile(r'(?i)fundaci[oó]n|foundation|suites del|de los cuales')
            lineas = man.splitlines()
            for m in re.finditer(r'(\d{2,4})\s*(?:PASS|comprobaciones)', man):
                n = int(m.group(1))
                linea = man[:m.start()].count('\n')
                contexto = lineas[linea] if linea < len(lineas) else ''
                if ACOTADO.search(contexto):
                    continue                  # no pretende ser el total
                if n != total and n > 50:      # >50 para no tocar recuentos por suite
                    if args.fix:
                        man = man.replace(m.group(0),
                                          m.group(0).replace(m.group(1), str(total)))
                        arreglados.append(f'MANIFEST tests: {n} → {total}')
                    else:
                        fallos.append(f'MANIFEST dice {n} tests; '
                                      f'TEST_RESULTS.json dice {total}')
            m = re.search(r'(\d+) suites', man)
            if m and int(m.group(1)) != suites:
                if args.fix:
                    man = man.replace(m.group(0), f'{suites} suites')
                    arreglados.append(f'MANIFEST suites: {m.group(1)} → {suites}')
                else:
                    fallos.append(f'MANIFEST dice {m.group(1)} suites; son {suites}')
            if res['total']['fail']:
                fallos.append(f"TEST_RESULTS.json tiene {res['total']['fail']} FAIL")
            if res['total']['skip']:
                fallos.append(f"TEST_RESULTS.json tiene {res['total']['skip']} SKIP: "
                              'un salto silencioso esconde una suite que no corrió')

            # ── la tabla de suites, fila por fila ────────────────────
            # No basta con que cuadre el total: la tabla del MANIFEST se
            # quedó atrás dos veces (faltaban suites enteras y tres
            # recuentos estaban viejos) mientras el total cuadraba.
            esperado = {r['suite']: r['pass'] for r in res['suites']}
            visto = {}
            for m in re.finditer(r'^\|\s*`(test_[a-z0-9_]+\.py)`\s*\|\s*(\d+)\s*\|',
                                 man, re.M):
                visto[m.group(1)] = (int(m.group(2)), m.group(0))
            for suite, n in esperado.items():
                if suite not in visto:
                    fallos.append(f'MANIFEST: la suite {suite} no está en la tabla')
                elif visto[suite][0] != n:
                    if args.fix:
                        fila = visto[suite][1]
                        man = man.replace(
                            fila, re.sub(r'\|\s*\d+\s*\|$', f'| {n} |', fila))
                        arreglados.append(f'MANIFEST {suite}: '
                                          f'{visto[suite][0]} → {n}')
                    else:
                        fallos.append(f'MANIFEST dice {visto[suite][0]} checks para '
                                      f'{suite}; TEST_RESULTS.json dice {n}')
            for suite in set(visto) - set(esperado):
                fallos.append(f'MANIFEST lista {suite}, que ya no existe')

        if args.fix and man != original:
            open(mp, 'w', encoding='utf-8').write(man)

    # ── 2b · los documentos que citan el total de tests ──────────────
    # Tres guías arrastraron "156 PASS" desde antes de r2. El MANIFEST se
    # revisaba; los docs no los miraba nadie.
    if os.path.exists(tr):
        total = json.load(open(tr, encoding='utf-8'))['total']['pass']
        ACOTADO = re.compile(r'(?i)fundaci[oó]n|foundation|suites del|de los cuales')
        CITA = re.compile(r'(\d{2,4})\s*(?:PASS|tests\b|comprobaciones)')
        for dp, dn, fn in os.walk(os.path.join(ROOT, 'docs')):
            for f in sorted(fn):
                # Los documentos de la FUNDACIÓN (…_V2_2.md) cuentan su
                # propia batería, no la de este paquete: su "92 PASS sin
                # MariaDB" es correcto y no se toca.
                if not f.endswith('.md') or f.endswith('_V2_2.md'):
                    continue
                ruta = os.path.join(dp, f)
                txt = open(ruta, encoding='utf-8').read()
                lineas = txt.splitlines()
                cambiado = False
                for m in CITA.finditer(txt):
                    n = int(m.group(1))
                    ln = lineas[txt[:m.start()].count('\n')]
                    if ACOTADO.search(ln) or n == total or n <= 50:
                        continue
                    if args.fix:
                        txt = txt.replace(m.group(0),
                                          m.group(0).replace(m.group(1), str(total)))
                        cambiado = True
                        arreglados.append(f'docs/{f}: {n} → {total} tests')
                    else:
                        fallos.append(f'docs/{f} dice {n} tests; son {total}')
                if cambiado:
                    open(ruta, 'w', encoding='utf-8').write(txt)

    # ── 3 · los 7 workflows desactivados ─────────────────────────────
    vivos = [n for n, w in reales.items() if w['active']]
    if vivos:
        fallos.append(f'workflows que se importarían ACTIVOS: {vivos}')
    if len(reales) != 7:
        fallos.append(f'hay {len(reales)} workflows, se esperaban 7')

    # ── 4 · escáner de secretos ──────────────────────────────────────
    r = subprocess.run([sys.executable, os.path.join(HERE, 'secret_scan.py')],
                       cwd=ROOT, capture_output=True, text=True)
    if r.returncode != 0:
        fallos.append('el escáner de secretos encuentra hallazgos:\n'
                      + r.stdout[-800:])

    # ── 5 · sin contraseñas por defecto en el panel ──────────────────
    mala = re.compile(
        r'''(?ix)(?:os\.)?(?:environ\.get|getenv)\s*\(\s*
            ["'][A-Z0-9_]*(?:PASS|SECRET|TOKEN|API_?KEY)[A-Z0-9_]*["']
            \s*,\s*["'][^"'\n]{4,}["']''')
    for dp, dn, fn in os.walk(os.path.join(ROOT, 'panel', 'app')):
        dn[:] = [d for d in dn if d != '__pycache__']
        for f in fn:
            if not f.endswith('.py'):
                continue
            ruta = os.path.join(dp, f)
            txt = open(ruta, encoding='utf-8', errors='replace').read()
            for m in mala.finditer(txt):
                fallos.append(f'contraseña por defecto en '
                              f'{os.path.relpath(ruta, ROOT)}: {m.group(0)[:70]}')

    # ── 6 · el modo inicial de la migración ──────────────────────────
    mig = os.path.join(ROOT, 'sql', 'parts', '005_legacy_backup_v2.sql')
    if os.path.exists(mig):
        txt = open(mig, encoding='utf-8').read()
        m = re.search(r"SELECT\s+'lm_operating_mode',\s*'(\w+)'", txt)
        if not m:
            fallos.append('no encuentro el modo inicial en la migración 005')
        elif m.group(1) != 'LEGACY_BACKUP':
            fallos.append(f'el modo inicial de la migración es {m.group(1)}, '
                          'debería ser LEGACY_BACKUP para forzar un cutover '
                          'deliberado')

    # ── salida ───────────────────────────────────────────────────────
    print('CONSISTENCIA DEL PAQUETE')
    print('─' * 62)
    print(f'workflows      : {len(reales)} · {tn} nodos · {ts} notas · '
          f'{sum(w["connections"] for w in reales.values())} conexiones')
    print(f'todos inactivos: {"sí" if not vivos else "NO"}')
    if os.path.exists(tr):
        t = json.load(open(tr, encoding='utf-8'))['total']
        print(f'tests          : {t["pass"]} PASS · {t["fail"]} FAIL · {t["skip"]} SKIP')
    print(f'secretos       : {"0 hallazgos" if r.returncode == 0 else "HAY HALLAZGOS"}')
    print('─' * 62)
    for a in arreglados:
        print(f'  ARREGLADO  {a}')
    for a in avisos:
        print(f'  aviso      {a}')
    for f in fallos:
        print(f'  FALLO      {f}')
    print('─' * 62)
    print('TODO CUADRA' if not fallos else f'{len(fallos)} INCONSISTENCIAS')
    return 1 if fallos else 0


if __name__ == '__main__':
    sys.exit(main())
