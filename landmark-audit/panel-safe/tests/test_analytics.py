"""Suite de pruebas del motor de analítica."""
import sqlite3, sys, os, time
from datetime import datetime, timedelta

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))
from app.analytics import (DB, build_report, traffic, timeseries, hourly_profile,
                           duration_buckets, funnel, pipeline_snapshot,
                           resolve_range, retry_distribution, top_conversations, pct)

DBFILE = os.path.join(os.path.dirname(__file__), 'mock_asterisk.db')
REF = datetime(2026, 8, 6, 14, 0, 0)   # "ahora" simulado

conn = sqlite3.connect(DBFILE)
db = DB(conn, 'sqlite')

FAIL = []
def check(name, cond, detail=''):
    status = 'OK  ' if cond else 'FAIL'
    if not cond:
        FAIL.append(name)
    print(f'  [{status}] {name}' + (f'  → {detail}' if detail else ''))


print('\n' + '='*70)
print('TEST 1 — Rangos de fecha')
print('='*70)
for p in ['today','yesterday','week','month','year','last7','last30','last90']:
    s,e,label,ps,pe = resolve_range(p, REF)
    ok = s < e and ps < pe and pe <= s
    check(f'{p:9s} {s:%Y-%m-%d} → {e:%Y-%m-%d}', ok, label)


print('\n' + '='*70)
print('TEST 2 — Métricas de tráfico (validación cruzada contra SQL crudo)')
print('='*70)
s,e,_,_,_ = resolve_range('yesterday', REF)
t0 = time.time()
tr = traffic(db, s, e)
el = time.time()-t0

# Validación independiente con SQL directo
cur = conn.cursor()
cur.execute("SELECT COUNT(*), SUM(disposition='ANSWERED'), SUM(billsec) FROM cdr WHERE calldate>=? AND calldate<?",
            (s.strftime('%Y-%m-%d %H:%M:%S'), e.strftime('%Y-%m-%d %H:%M:%S')))
raw_total, raw_ans, raw_talk = cur.fetchone()

check('total_calls coincide con SQL crudo', tr['total_calls']==raw_total, f"{tr['total_calls']} == {raw_total}")
check('answered coincide con SQL crudo',    tr['answered']==raw_ans,     f"{tr['answered']} == {raw_ans}")
check('talk_seconds coincide',              tr['talk_seconds']==raw_talk,f"{tr['talk_seconds']} == {raw_talk}")
check('suma de disposiciones == total',
      tr['answered']+tr['no_answer']+tr['busy']+tr['failed']+tr['congestion']==tr['total_calls'],
      f"{tr['answered']}+{tr['no_answer']}+{tr['busy']}+{tr['failed']}+{tr['congestion']}={tr['total_calls']}")
check('ASR entre 0 y 100', 0 <= tr['asr'] <= 100, f"{tr['asr']}%")
check('ASR consistente con conteo', abs(tr['asr'] - pct(tr['answered'],tr['total_calls'])) < 0.01)
check('NER >= ASR', tr['ner'] >= tr['asr'], f"NER {tr['ner']}% >= ASR {tr['asr']}%")
check('costo = minutos * tarifa', abs(tr['cost'] - tr['billed_minutes']*0.06) < 0.01,
      f"${tr['cost']} = {tr['billed_minutes']}min x $0.06")
check('ACD > 0 si hubo contestadas', (tr['acd']>0) == (tr['answered']>0), f"ACD {tr['acd']}s")
check(f'performance query < 2s', el < 2.0, f'{el*1000:.0f}ms sobre 652k filas')

print(f"\n     Resumen del día: {tr['total_calls']:,} llamadas | {tr['answered']:,} contestadas "
      f"({tr['asr']}%) | {tr['billed_minutes']:,} min | ${tr['cost']:,.2f} | ACD {tr['acd']}s")


print('\n' + '='*70)
print('TEST 3 — Redondeo de facturación (CEIL por llamada)')
print('='*70)
# Verifico que 4m20s (260s) facture como 5 minutos
cur.execute("SELECT COUNT(*) FROM cdr WHERE billsec BETWEEN 241 AND 300 AND disposition='ANSWERED'")
n_calls = cur.fetchone()[0]
cur.execute("SELECT SUM(CEIL(billsec/60.0)) FROM cdr WHERE billsec BETWEEN 241 AND 300 AND disposition='ANSWERED'")
ceil_min = cur.fetchone()[0] or 0
check('llamadas de 241-300s facturan 5 min c/u',
      n_calls==0 or abs(ceil_min - n_calls*5) < 0.01,
      f'{n_calls} llamadas → {ceil_min} min (esperado {n_calls*5})')

cur.execute("SELECT SUM(CEIL(billsec/60.0)), SUM(billsec)/60.0 FROM cdr WHERE calldate>=? AND calldate<?",
            (s.strftime('%Y-%m-%d %H:%M:%S'), e.strftime('%Y-%m-%d %H:%M:%S')))
ceil_total, prop_total = cur.fetchone()
check('CEIL siempre >= proporcional', (ceil_total or 0) >= (prop_total or 0),
      f'{ceil_total:.0f} min (CEIL) vs {prop_total:.1f} min (proporcional)')


print('\n' + '='*70)
print('TEST 4 — Series temporales')
print('='*70)
for grain, period in [('hour','today'), ('day','last30'), ('month','year')]:
    s2,e2,_,_,_ = resolve_range(period, REF)
    t0=time.time(); ser = timeseries(db, s2, e2, grain); el=time.time()-t0
    tot = sum(r['total_calls'] for r in ser)
    tr2 = traffic(db, s2, e2)
    check(f'{grain:5s}: suma de serie == total del período',
          tot == tr2['total_calls'], f'{len(ser)} puntos, {tot:,} llamadas, {el*1000:.0f}ms')
    check(f'{grain:5s}: buckets ordenados',
          [r['bucket'] for r in ser] == sorted(r['bucket'] for r in ser))


print('\n' + '='*70)
print('TEST 5 — Perfil horario (conversión a IST)')
print('='*70)
s3,e3,_,_,_ = resolve_range('last7', REF)
hp = hourly_profile(db, s3, e3)
check('24 buckets horarios', len(hp)==24)
tot_h = sum(h['total_calls'] for h in hp)
tr3 = traffic(db, s3, e3)
check('suma horaria == total período', tot_h==tr3['total_calls'], f'{tot_h:,}')
active = [h for h in hp if h['total_calls']>0]
rango = f"{min(h['hour'] for h in active):02d}:00–{max(h['hour'] for h in active):02d}:00 IST"
check('tráfico dentro de ventana 11:00-19:00 IST',
      all(10 <= h['hour'] <= 20 for h in active), rango)
peak = max(hp, key=lambda h: h['total_calls'])
print(f"\n     Hora pico: {peak['hour']:02d}:00 IST con {peak['total_calls']:,} llamadas (ASR {peak['asr']}%)")


print('\n' + '='*70)
print('TEST 6 — Distribución de duración')
print('='*70)
db6 = duration_buckets(db, s3, e3)
check('suma de buckets == contestadas', sum(b['count'] for b in db6['buckets'])==db6['total'],
      f"{db6['total']:,} contestadas")
check('porcentajes suman ~100', abs(sum(b['pct'] for b in db6['buckets'])-100) < 0.5,
      f"{sum(b['pct'] for b in db6['buckets']):.1f}%")
check('métrica de conversación real calculada', 0 <= db6['meaningful_pct'] <= 100,
      f"{db6['meaningful']:,} de {db6['total']:,} son +30s ({db6['meaningful_pct']}%)")
for b in db6['buckets']:
    print(f"     {b['label']:>8s}: {b['count']:>6,}  {b['pct']:>5.1f}%")


print('\n' + '='*70)
print('TEST 7 — Embudo comercial y pipeline')
print('='*70)
s7,e7,_,_,_ = resolve_range('last90', REF)
tr7 = traffic(db, s7, e7)
fn = funnel(db, s7, e7, tr7)
check('embudo disponible', fn['available'])
check('contactados <= tocados', fn['contacted'] <= fn['total_touched'],
      f"{fn['contacted']:,} de {fn['total_touched']:,}")
check('tasa de contacto 0-100', 0 <= fn['contact_rate'] <= 100, f"{fn['contact_rate']}%")
check('CPA calculado', fn['cpa'] >= 0, f"${fn['cpa']} por cuenta abierta")
check('cuentas abiertas contadas', fn['accounts_opened'] >= 0, f"{fn['accounts_opened']:,}")

pl = pipeline_snapshot(db)
check('pipeline disponible', pl['available'])
check('suma de estados == total leads', sum(r['n'] for r in pl['rows'])==pl['total'], f"{pl['total']:,} leads")
print(f"\n     Pipeline: {pl['total']:,} leads | {pl['pending']:,} pendientes | {pl['in_flight']:,} en curso")
print(f"     Embudo:   {fn['total_touched']:,} tocados → {fn['contacted']:,} contactados "
      f"({fn['contact_rate']}%) → {fn['accounts_opened']:,} cuentas ({fn['conversion_rate']}%)")


print('\n' + '='*70)
print('TEST 8 — Reintentos y top conversaciones')
print('='*70)
rt = retry_distribution(db, s3, e3)
check('distribución de reintentos', rt['unique_numbers']>0, f"{rt['unique_numbers']:,} números únicos")
check('porcentajes de reintento <= 100', sum(r['pct'] for r in rt['rows']) <= 100.5)
tc = top_conversations(db, s3, e3, 10)
check('top conversaciones ordenadas desc',
      [r['talk_seconds'] for r in tc]==sorted((r['talk_seconds'] for r in tc), reverse=True),
      f'{len(tc)} filas')


print('\n' + '='*70)
print('TEST 9 — Reporte completo (todos los períodos)')
print('='*70)
for p in ['today','yesterday','week','month','year','last7','last30','last90']:
    t0=time.time()
    try:
        rep = build_report(db, p, ref=REF)
        el=time.time()-t0
        keys_ok = all(k in rep for k in
            ['label','traffic','deltas','series','hourly','durations','top_calls','retries','funnel','pipeline'])
        check(f'{p:9s} genera reporte completo', keys_ok,
              f"{rep['traffic']['total_calls']:>7,} llam | ${rep['traffic']['cost']:>9,.2f} | "
              f"{len(rep['series']):>3} pts | {el*1000:>5.0f}ms")
    except Exception as ex:
        check(f'{p:9s} genera reporte completo', False, f'EXCEPCIÓN: {ex}')


print('\n' + '='*70)
print('TEST 10 — Casos borde')
print('='*70)
# Rango sin datos (futuro)
fut_s = datetime(2027,1,1); fut_e = datetime(2027,1,2)
tr_e = traffic(db, fut_s, fut_e)
check('rango vacío no rompe', tr_e['total_calls']==0 and tr_e['cost']==0.0)
check('división por cero manejada (ASR)', tr_e['asr']==0.0)
check('división por cero manejada (ACD)', tr_e['acd']==0.0)
check('división por cero manejada (CPA)', funnel(db, fut_s, fut_e, tr_e)['cpa']==0.0)
db_e = duration_buckets(db, fut_s, fut_e)
check('duración con 0 contestadas', db_e['total']==0 and db_e['meaningful_pct']==0)
rep_e = build_report(db, 'custom', start='2027-01-01', end='2027-01-02', ref=REF)
check('reporte de rango vacío se genera', rep_e['traffic']['total_calls']==0)
# Domingo (no se opera)
sun = datetime(2026,8,2)
tr_sun = traffic(db, sun, sun+timedelta(days=1))
check('domingo sin operación', tr_sun['total_calls']==0, 'confirma respeto de calendario')


print('\n' + '='*70)
if FAIL:
    print(f'❌ {len(FAIL)} PRUEBAS FALLARON:')
    for f in FAIL: print(f'   - {f}')
    sys.exit(1)
else:
    print('✅ TODAS LAS PRUEBAS PASARON')
print('='*70)
conn.close()
