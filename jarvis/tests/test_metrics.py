import sys, os, types
sys.path.insert(0,'/home/claude/jarvis')
os.environ['JARVIS_SQLITE'] = '/tmp/jtest.db'
os.environ['JARVIS_DB_PATH'] = '/tmp/jarvis_mem.db'
from app.db import panel_db, jarvis_db, init_jarvis_schema
from app.skills import metrics
from app.skills.base import run_skill
init_jarvis_schema()
ctx = types.SimpleNamespace(panel_db=panel_db(), jdb=jarvis_db(), session_id='t1')
fails=[]
def check(label, got, want):
    ok = got == want
    print(f"  [{'OK ' if ok else 'FALLO'}] {label}: got={got} want={want}")
    if not ok: fails.append(label)

print("── resumen_llamadas india hoy ──")
r = run_skill('resumen_llamadas', {'pais':'india','periodo':'hoy'}, ctx)
check("llamadas", r['llamadas_totales'], 10); check("contestadas", r['contestadas'], 6)
check("ASR", r['asr_pct'], 60.0); check("minutos 3*1+2*2+1*4", r['minutos_facturados'], 11)
check("costo", r['costo_estimado_usd'], 0.66)

print("── resumen_llamadas mexico hoy ──")
r = run_skill('resumen_llamadas', {'pais':'mexico','periodo':'hoy'}, ctx)
check("llamadas", r['llamadas_totales'], 4); check("contestadas", r['contestadas'], 2)
check("ASR", r['asr_pct'], 50.0); check("minutos", r['minutos_facturados'], 2)

print("── cuentas_abiertas hoy ──")
r = run_skill('cuentas_abiertas', {'periodo':'hoy'}, ctx)
check("total", r['total'], 4)
byc = {d['pais']: d['cuentas'] for d in r['por_pais']}
check("India", byc.get('India'), 3); check("México", byc.get('México'), 1)
r = run_skill('cuentas_abiertas', {'periodo':'hoy','pais':'india'}, ctx)
check("filtrado india", r['total'], 3)

print("── embudo india hoy ──")
r = run_skill('embudo_conversion', {'pais':'india','periodo':'hoy'}, ctx)
check("llamadas", r['llamadas'], 10); check("contestadas", r['contestadas'], 6)
check("cuentas", r['cuentas_abiertas'], 3)
check("conv/contestadas 3/6", r['conversion_sobre_contestadas_pct'], 50.0)
check("conv/llamadas 3/10", r['conversion_sobre_llamadas_pct'], 30.0)
check("llamadas/cuenta 10/3", r['llamadas_por_cuenta'], 3.3)

print("── estado_leads india ──")
r = run_skill('estado_leads', {'pais':'india'}, ctx)
check("total", r['total'], 10)
check("NEW", {e['estado']:e['cantidad'] for e in r['por_estado']}['NEW'], 5)

print("── HUSOS: mejores_horarios india AYER (IST +5:30) ──")
r = run_skill('mejores_horarios', {'pais':'india','periodo':'ayer'}, ctx)
h = {x['hora']: x for x in r['por_hora']}
print("   horas:", sorted(h))
check("UTC5 -> IST 10", h.get(10,{}).get('llamadas'), 3)
check("UTC6 -> IST 11", h.get(11,{}).get('llamadas'), 2)
check("UTC7 -> IST 12", h.get(12,{}).get('llamadas'), 1)
check("UTC8 -> IST 13", h.get(13,{}).get('llamadas'), 4)
check("IST 13 contestadas 0", h.get(13,{}).get('contestadas'), 0)
check("tz label", r['zona_horaria'], 'IST')
check("mejor hora (ninguna >=10 llamadas -> cae a todas, max ASR)", r['mejor_hora_respuesta']['asr_pct'], 100.0)

print("── HUSOS: mejores_horarios mexico AYER (CST -6) ──")
r = run_skill('mejores_horarios', {'pais':'mexico','periodo':'ayer'}, ctx)
h = {x['hora']: x for x in r['por_hora']}
print("   horas:", sorted(h))
check("UTC20 -> CST 14", h.get(14,{}).get('llamadas'), 2)
check("UTC21 -> CST 15", h.get(15,{}).get('llamadas'), 2)

print("── tendencia_diaria india 7d ──")
r = run_skill('tendencia_diaria', {'pais':'india','dias':7}, ctx)
check("dias", len(r['serie']), 2)
check("hoy llamadas", r['serie'][-1]['llamadas'], 10)
check("hoy cuentas", r['serie'][-1]['cuentas'], 3)
check("ayer llamadas", r['serie'][0]['llamadas'], 10)
check("ayer cuentas", r['serie'][0]['cuentas'], 1)

print("── comparar_paises hoy ──")
r = run_skill('comparar_paises', {'periodo':'hoy'}, ctx)
check("paises con actividad", len(r['paises']), 2)
india = next(p for p in r['paises'] if p['pais']=='India')
check("costo/cuenta 0.66/3", india['costo_por_cuenta_usd'], 0.22)

print("── manejo de errores ──")
for label, args in [("pais invalido", {'pais':'marte'}), ("periodo invalido", {'pais':'india','periodo':'quincena'})]:
    r = run_skill('resumen_llamadas', args, ctx)
    ok = 'error' in r
    print(f"  [{'OK ' if ok else 'FALLO'}] {label} -> {r.get('error','SIN ERROR')}")
    if not ok: fails.append(label)
r = run_skill('no_existe', {}, ctx)
print(f"  [{'OK ' if 'error' in r else 'FALLO'}] skill inexistente")
r = run_skill('resumen_llamadas', {'pais':'india','argumento_inventado':1}, ctx)
print(f"  [{'OK ' if 'error' in r else 'FALLO'}] argumento inventado -> {r.get('error','')[:60]}")
if 'error' not in r: fails.append('arg inventado')
# alias
r = run_skill('resumen_llamadas', {'pais':'MX','periodo':'hoy'}, ctx)
check("alias MX -> mexico", r['pais'], 'México')

print("\n" + ("=== METRICS OK ===" if not fails else f"=== FALLOS: {fails} ==="))
sys.exit(1 if fails else 0)
