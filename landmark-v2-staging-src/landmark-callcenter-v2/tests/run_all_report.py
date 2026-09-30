#!/usr/bin/env python3
"""Corre las suites una vez y emite el recuento consolidado PASS/FAIL/SKIP."""
import json
import os
import re
import subprocess
import sys

os.environ['PYTHONDONTWRITEBYTECODE'] = '1'
HERE = os.path.dirname(os.path.abspath(__file__))
SUITES = ['test_engine_parity_v2.py', 'test_package_hygiene_v2.py', 'test_crm_english_v2.py',
          'test_panel_base_regression_v2.py', 'test_billing_v2.py',
          'test_legacy_backup_v2.py', 'test_payments_v2.py',
          'test_workflow_standard_v2.py', 'test_workflow_sql_v2.py',
          'test_suite_behaviour_v2.py', 'test_migration_suite_v2.py',
          'test_analytics_filters_v2.py', 'test_interlock_v2.py',
          'test_real_interlock_v2.py',
          'test_panel_v2.py']
# Las 5 suites de la fundación V2.2 viajan en el paquete y también se
# cuentan: si V2 rompe algo de la fundación, tiene que salir en el
# recuento, no quedarse en una carpeta que nadie corre.
FUNDACION_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                             '..', 'panel', 'tests')
FUNDACION = ['test_foundation_v2_2.py', 'test_ops_analytics_v2_2.py',
             'test_http_v2_2.py', 'test_migration_v2_2.py',
             'test_concurrency_v2_2.py']
LINE = re.compile(r'(?P<name>.+?): (?P<p>\d+) PASS · (?P<f>\d+) FAIL · (?P<s>\d+) SKIP')

def main():
    total = {'pass': 0, 'fail': 0, 'skip': 0}
    rows = []
    todas = ([(s_, HERE, 'v2') for s_ in SUITES]
             + [(s_, os.path.abspath(FUNDACION_DIR), 'fundacion')
                for s_ in FUNDACION
                if os.path.exists(os.path.join(FUNDACION_DIR, s_))])
    for suite, cwd, origen in todas:
        r = subprocess.run([sys.executable, suite], cwd=cwd,
                           capture_output=True, text=True, timeout=1800)
        m = None
        for ln in r.stdout.splitlines():
            m = LINE.search(ln) or m
        if not m:
            rows.append({'suite': suite, 'origen': origen, 'pass': 0, 'fail': 1,
                         'skip': 0, 'note': 'la suite no terminó',
                         'salida': (r.stdout + r.stderr)[-400:]})
            total['fail'] += 1
            continue
        p, f, sk = int(m.group('p')), int(m.group('f')), int(m.group('s'))
        rows.append({'suite': suite, 'origen': origen,
                     'name': m.group('name').strip(),
                     'pass': p, 'fail': f, 'skip': sk})
        total['pass'] += p; total['fail'] += f; total['skip'] += sk
    print(f"{'SUITE':42s} {'PASS':>5s} {'FAIL':>5s} {'SKIP':>5s}")
    print('-' * 62)
    for r in rows:
        print(f"{r['suite']:42s} {r['pass']:>5d} {r['fail']:>5d} {r['skip']:>5d}")
    print('-' * 62)
    print(f"{'TOTAL':42s} {total['pass']:>5d} {total['fail']:>5d} {total['skip']:>5d}")
    por_origen = {}
    for r in rows:
        d = por_origen.setdefault(r.get('origen', 'v2'),
                                  {'suites': 0, 'pass': 0, 'fail': 0, 'skip': 0})
        d['suites'] += 1
        for k in ('pass', 'fail', 'skip'):
            d[k] += r[k]
    print()
    for origen, d in por_origen.items():
        print(f"  {origen:10s} {d['suites']:2d} suites · {d['pass']:3d} PASS · "
              f"{d['fail']} FAIL · {d['skip']} SKIP")
    with open(os.path.join(HERE, 'TEST_RESULTS.json'), 'w') as fh:
        json.dump({'suites': rows, 'total': total, 'por_origen': por_origen},
                  fh, indent=2, ensure_ascii=False)
    return 1 if total['fail'] else 0

if __name__ == '__main__':
    sys.exit(main())
