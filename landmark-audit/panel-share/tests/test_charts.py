"""Pruebas de geometría de gráficos — que ningún SVG salga con coordenadas inválidas."""
import sqlite3, sys, os
from datetime import datetime
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))
from app.analytics import DB, build_report
from app import charts

db = DB(sqlite3.connect(os.path.join(os.path.dirname(__file__), 'mock_asterisk.db')), 'sqlite')
REF = datetime(2026, 8, 6, 14, 0, 0)

FAIL = []
def check(name, cond, detail=''):
    if not cond: FAIL.append(name)
    print(f"  [{'OK  ' if cond else 'FAIL'}] {name}" + (f'  → {detail}' if detail else ''))

def finite(*vals):
    return all(isinstance(v,(int,float)) and v==v and abs(v)<1e9 for v in vals)


print('\n' + '='*70)
print('GEOMETRÍA DE GRÁFICOS')
print('='*70)

for period in ['today','yesterday','week','month','last30','last90','year']:
    rep = build_report(db, period, ref=REF, use_cache=False)

    tr = charts.trace(rep['series'])
    if tr['empty']:
        check(f'{period:9s} traza (sin datos)', True, 'vacío manejado')
    else:
        ok_geo = all(finite(b['x'],b['y'],b['w'],b['h'],b['ay'],b['ah']) for b in tr['bars'])
        check(f'{period:9s} traza: coordenadas finitas', ok_geo, f"{len(tr['bars'])} barras")
        ok_bounds = all(b['h']>=0 and b['ah']>=0 and b['ah']<=b['h']+0.01 for b in tr['bars'])
        check(f'{period:9s} traza: contestadas <= total', ok_bounds)
        ok_x = all(0 <= b['x'] <= tr['w']+1 for b in tr['bars'])
        check(f'{period:9s} traza: dentro del lienzo', ok_x, f"ancho {tr['w']}")
        check(f'{period:9s} traza: path de costo válido',
              tr['cost_path'].startswith('M') and 'nan' not in tr['cost_path'].lower())

    hr = charts.hourly(rep['hourly'])
    if not hr['empty']:
        ok_h = all(finite(b['x'],b['y'],b['w'],b['h']) and b['ah']<=b['h']+0.01 for b in hr['bars'])
        check(f'{period:9s} perfil horario', ok_h, f"{len(hr['bars'])} horas activas")
        peaks = [b for b in hr['bars'] if b['peak']]
        check(f'{period:9s} hora pico marcada', len(peaks)>=1,
              f"{peaks[0]['hour']:02d}:00 IST" if peaks else '')

print('\n' + '='*70)
print('ESCALERA DEL EMBUDO')
print('='*70)
rep = build_report(db, 'last30', ref=REF, use_cache=False)
ld = charts.ladder(rep['traffic'], rep['durations'], rep['funnel'])
check('4 peldaños', len(ld)==4)
check('monótona decreciente', all(ld[i]['v'] >= ld[i+1]['v'] for i in range(len(ld)-1)),
      ' → '.join(f"{s['v']:,}" for s in ld))
check('anchos dentro de 0-100', all(0 < s['width'] <= 100 for s in ld))
check('% relativo al paso previo', ld[0]['pct_prev'] is None and all(s['pct_prev'] is not None for s in ld[1:]))
for s in ld:
    prev = f"{s['pct_prev']:>5.1f}% del anterior" if s['pct_prev'] is not None else '     —'
    print(f"     {s['label']:<22s} {s['v']:>8,}  {s['pct_total']:>6.2f}% del total  {prev}")

print('\n' + '='*70)
print('ANILLO Y BARRAS')
print('='*70)
t = rep['traffic']
dn = charts.donut([
    {'label':'Contestadas','value':t['answered'],'tone':'ok'},
    {'label':'Sin respuesta','value':t['no_answer'],'tone':'idle'},
    {'label':'Ocupado','value':t['busy'],'tone':'warn'},
    {'label':'Fallo técnico','value':t['tech_failures'],'tone':'bad'},
])
check('segmentos suman el total', sum(s['value'] for s in dn['segments'])==t['total_calls'])
check('porcentajes suman ~100', abs(sum(s['pct'] for s in dn['segments'])-100) < 0.5,
      f"{sum(s['pct'] for s in dn['segments']):.1f}%")
check('dash arrays válidos', all('nan' not in s['dash'].lower() for s in dn['segments']))

bh = charts.bars_h(rep['durations']['buckets'])
check('barras horizontales normalizadas', max(b['w'] for b in bh)==100.0)
check('anchos no negativos', all(b['w']>=0 for b in bh))

print('\n' + '='*70)
print('CASOS BORDE')
print('='*70)
check('traza vacía', charts.trace([])['empty'])
check('horario vacío', charts.hourly([{'hour':h,'total_calls':0,'answered':0,'asr':0,'cost':0,'billed_minutes':0} for h in range(24)])['empty'])
z = charts.ladder({'total_calls':0,'answered':0}, {'meaningful':0}, {'available':False})
check('escalera con ceros no divide por cero', all(s['v']==0 for s in z))
d0 = charts.donut([{'label':'x','value':0,'tone':'ok'}])
check('anillo con ceros', d0['total']==1)
one = charts.trace([{'bucket':'2026-08-06','total_calls':5,'answered':1,'asr':20.0,'cost':0.3,'billed_minutes':5,'no_answer':4,'failed':0,'talk_seconds':30}])
check('traza de un solo punto', len(one['bars'])==1 and one['bars'][0]['w']>0)

print('\n' + '='*70)
if FAIL:
    print(f'❌ {len(FAIL)} FALLARON: {FAIL}'); sys.exit(1)
print('✅ TODAS LAS PRUEBAS DE GRÁFICOS PASARON')
print('='*70)
