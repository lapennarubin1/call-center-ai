#!/usr/bin/env python3
"""Genera los siete templates n8n del suite V2 en workflows/.

    python3 tools/build_workflows.py

El layout, las sticky notes y las reglas del N8N_TEMPLATE_STANDARD se aplican y
se VERIFICAN acá (wfbuild.Workflow.validate). Si un workflow viola el estándar,
el build falla y no se escribe un JSON inválido.
"""
import importlib
import json
import os
import sys

# Correr una herramienta no puede dejar .pyc dentro de lo que se
# entrega: tests/test_package_hygiene_v2.py lo comprueba.
sys.dont_write_bytecode = True
os.environ['PYTHONDONTWRITEBYTECODE'] = '1'

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, HERE)
sys.path.insert(0, os.path.join(HERE, 'wf'))

import wfbuild

JS_DIR = os.path.join(HERE, 'js')
OUT = os.path.join(ROOT, 'workflows')

MODULES = [
    'followup_engine', 'wf2_dispatcher', 'wf9_post_call', 'wf3_account',
    'wf7_8_payment', 'wf10_recordings', 'wf14_reconciliation',
]


def load_js():
    return {f: open(os.path.join(JS_DIR, f), encoding='utf-8').read()
            for f in sorted(os.listdir(JS_DIR)) if f.endswith('.js')}


def main():
    os.makedirs(OUT, exist_ok=True)
    js = load_js()
    total_issues, report = 0, []
    for mod_name in MODULES:
        mod = importlib.import_module(mod_name)
        wf = mod.build(wfbuild, js)
        path = os.path.join(OUT, mod.NAME + '.json')
        issues = wf.write(path)
        real = [n for n in wf.nodes if n['type'] != wfbuild.STICKY]
        stickies = [n for n in wf.nodes if n['type'] == wfbuild.STICKY]
        report.append({'workflow': mod.NAME, 'file': os.path.basename(path),
                       'nodes': len(real), 'sticky_notes': len(stickies),
                       'connections': sum(len(o) for c in wf.conns.values()
                                          for o in c.get('main', [])),
                       'issues': issues})
        total_issues += len(issues)
        mark = 'OK  ' if not issues else 'FAIL'
        print(f'{mark} {mod.NAME:42s} {len(real):3d} nodos · '
              f'{len(stickies):2d} notas · {len(issues)} problemas')
        for i in issues:
            print(f'       - {i}')
    with open(os.path.join(OUT, 'BUILD_REPORT.json'), 'w', encoding='utf-8') as fh:
        json.dump(report, fh, ensure_ascii=False, indent=2)
    print(f'\ntotal problemas de estándar: {total_issues}')
    return 1 if total_issues else 0


if __name__ == '__main__':
    sys.exit(main())
