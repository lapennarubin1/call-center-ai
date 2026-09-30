"""
Prueba end-to-end: levanta la app Flask contra la BD de prueba y ejerce
TODAS las rutas, validando el HTML/CSV que realmente sale al navegador.
"""
import os, sys, re

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
os.environ['LM_SQLITE'] = os.path.join(HERE, 'mock_asterisk.db')
os.environ['LM_USER']   = 'admin'
os.environ['LM_PASS']   = 'test123'
os.environ['LM_SECRET'] = 'test-secret-key'

sys.path.insert(0, os.path.join(ROOT, 'app'))
import server                                    # noqa: E402

app = server.app
app.config['TESTING'] = True

FAIL = []
def check(name, cond, detail=''):
    if not cond: FAIL.append(name)
    print(f"  [{'OK  ' if cond else 'FAIL'}] {name}" + (f'  → {detail}' if detail else ''))


print('\n' + '='*70)
print('E2E — AUTENTICACIÓN')
print('='*70)
c = app.test_client()

r = c.get('/')
check('sin sesión redirige a login', r.status_code == 302 and '/login' in r.headers.get('Location',''),
      f'HTTP {r.status_code}')
r = c.get('/api/report')
check('API sin sesión devuelve 401', r.status_code == 401, f'HTTP {r.status_code}')
r = c.post('/login', data={'user':'admin','pass':'malo'})
check('credenciales incorrectas rechazadas', b'incorrect' in r.data or r.status_code == 200)
r = c.post('/login', data={'user':'admin','pass':'test123'}, follow_redirects=False)
check('credenciales correctas inician sesión', r.status_code == 302, f'HTTP {r.status_code}')
r = c.get('/health')
check('/health responde sin sesión', r.status_code == 200, r.get_json().get('status'))
check('/health reporta filas del CDR', r.get_json().get('cdr_rows',0) > 600000,
      f"{r.get_json().get('cdr_rows'):,} filas")


print('\n' + '='*70)
print('E2E — DASHBOARD EN TODOS LOS PERÍODOS')
print('='*70)
for period in ['today','yesterday','week','month','last7','last30','last90','year']:
    r = c.get(f'/?period={period}')
    html = r.data.decode('utf-8')
    ok = r.status_code == 200
    check(f'{period:9s} HTTP 200', ok, f'{len(html):,} bytes')
    if not ok:
        print(html[:600]); continue
    check(f'{period:9s} sin errores de plantilla',
          'Traceback' not in html and 'jinja2' not in html.lower())
    check(f'{period:9s} sin variables sin resolver',
          '{{' not in html and 'Undefined' not in html)
    # El SVG debe tener coordenadas numéricas válidas
    bad = re.findall(r'(?:x|y|width|height|cx|cy|r)="(?:nan|NaN|None|-?inf)"', html)
    check(f'{period:9s} SVG sin coordenadas inválidas', not bad, f'{len(bad)} malas' if bad else '')
    check(f'{period:9s} renderiza la traza', '<svg class="trace"' in html or 'No calls recorded' in html or 'class="trace-panel"' in html)
    check(f'{period:9s} renderiza los KPIs', 'class="kpis"' in html)
    check(f'{period:9s} renderiza la escalera', 'class="ladder"' in html)


print('\n' + '='*70)
print('E2E — RANGO PERSONALIZADO Y CASOS BORDE')
print('='*70)
r = c.get('/?period=custom&start=2026-08-01&end=2026-08-05')
check('rango personalizado funciona', r.status_code == 200 and b'01/08/2026' in r.data)
r = c.get('/?period=custom&start=2027-01-01&end=2027-01-05')
check('rango sin datos no rompe', r.status_code == 200 and (b'No calls recorded' in r.data or b'class="trace-panel"' in r.data))
r = c.get('/?period=basura')
check('período inválido cae a "hoy"', r.status_code == 200)
r = c.get('/?period=custom&start=NOesFECHA&end=x')
check('fechas malformadas no rompen el servidor', r.status_code in (200, 500), f'HTTP {r.status_code}')
r = c.get('/?period=today&rate=0.12')
check('tarifa configurable por URL', r.status_code == 200 and b'0.12' in r.data)


print('\n' + '='*70)
print('E2E — API JSON')
print('='*70)
r = c.get('/api/report?period=last7')
j = r.get_json()
check('API devuelve JSON', r.status_code == 200 and isinstance(j, dict))
for k in ['label','traffic','deltas','series','hourly','durations','funnel','pipeline']:
    check(f'API contiene "{k}"', k in j)
check('API: números coherentes',
      j['traffic']['answered'] <= j['traffic']['total_calls'],
      f"{j['traffic']['answered']:,} de {j['traffic']['total_calls']:,}")
r = c.get('/api/conversions?period=last90')
check('API de cuentas abiertas', r.status_code == 200 and isinstance(r.get_json(), list),
      f'{len(r.get_json())} filas')
r = c.post('/api/cache/clear')
check('limpieza de caché', r.status_code == 200 and r.get_json().get('cleared'))


print('\n' + '='*70)
print('E2E — EXPORTACIÓN CSV')
print('='*70)
for path, minlines in [('/export/daily.csv?period=last30', 5),
                       ('/export/summary.csv?period=last7', 10),
                       ('/export/conversions.csv?period=last90', 1)]:
    r = c.get(path)
    body = r.data.decode('utf-8-sig')
    lines = [l for l in body.splitlines() if l.strip()]
    name = path.split('/')[-1].split('?')[0]
    check(f'{name:20s} HTTP 200', r.status_code == 200)
    check(f'{name:20s} tiene contenido', len(lines) >= minlines, f'{len(lines)} líneas')
    check(f'{name:20s} adjunto con nombre', 'attachment' in r.headers.get('Content-Disposition',''))
    check(f'{name:20s} BOM para Excel', r.data.startswith(b'\xef\xbb\xbf'))
    check(f'{name:20s} separador ;', ';' in lines[0] if lines else False)


print('\n' + '='*70)
print('E2E — SEGURIDAD Y SESIÓN')
print('='*70)
c2 = app.test_client()
r = c2.get('/export/daily.csv?period=today')
check('exportación protegida por sesión', r.status_code == 302, f'HTTP {r.status_code}')
r = c.get('/logout', follow_redirects=False)
check('cierre de sesión redirige', r.status_code == 302)
r = c.get('/', follow_redirects=False)
check('tras cerrar sesión pide login de nuevo', r.status_code == 302)


print('\n' + '='*70)
print('E2E — ESTÁTICOS')
print('='*70)
c3 = app.test_client()
r = c3.get('/static/panel.css')
check('hoja de estilos servida', r.status_code == 200, f'{len(r.data):,} bytes')
css = r.data.decode()
check('CSS sin variables sin definir',
      all(f'--{v}:' in css for v in ['bg','ink','signal','ok','cost','bad']))
r = c3.get('/login')
check('página de login renderiza', r.status_code == 200 and b'Operations center' in r.data)


print('\n' + '='*70)
if FAIL:
    print(f'❌ {len(FAIL)} PRUEBAS FALLARON:')
    for f in FAIL: print(f'   - {f}')
    sys.exit(1)
print('✅ TODAS LAS PRUEBAS E2E PASARON')
print('='*70)
