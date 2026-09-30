import sys, os, json, types
sys.path.insert(0,'/home/claude/jarvis')
os.environ.update(JARVIS_SQLITE='/tmp/jtest.db', JARVIS_DB_PATH='/tmp/jbrain.db',
                  JARVIS_PASS='x', ANTHROPIC_API_KEY='fake-key', ELEVENLABS_API_KEY='fake')
if os.path.exists('/tmp/jbrain.db'): os.remove('/tmp/jbrain.db')

from app.db import panel_db, jarvis_db, init_jarvis_schema
from app import brain, skills as sk
init_jarvis_schema()

def ctx():
    return types.SimpleNamespace(panel_db=panel_db(), jdb=jarvis_db(), session_id='s1', charts=[], language='es')

CALLS = []
def mock_api(script):
    """script: lista de respuestas a devolver en orden"""
    it = iter(script)
    def fake(payload, timeout=90):
        CALLS.append(payload)
        return next(it)
    return fake

def txt(o): return {'content':[{'type':'text','text':json.dumps(o, ensure_ascii=False)}]}
def tool(name, inp, tid='t1'): return {'content':[{'type':'tool_use','id':tid,'name':name,'input':inp}]}

fails=[]
def chk(label, cond, extra=''):
    print(f"  [{'OK ' if cond else 'FALLO'}] {label} {extra}")
    if not cond: fails.append(label)

# ══ 1. Respuesta directa sin herramientas ══
print("── 1. respuesta directa ──")
brain._anthropic_call = mock_api([txt({'hablado':'Hola, todo en orden.','pantalla':''})])
c = ctx(); r = brain.think(c, 'hola')
chk('hablado', r['hablado']=='Hola, todo en orden.')
chk('idioma es', r['idioma']=='es')
chk('sin skills', r['skills_usadas']==[])
chk('sin confirmacion', r['confirmacion'] is None)

# ══ 2. Loop de herramienta: pide datos y responde ══
print("── 2. usa una skill y responde ──")
CALLS.clear()
brain._anthropic_call = mock_api([
    tool('resumen_llamadas', {'pais':'india','periodo':'hoy'}),
    txt({'hablado':'India hizo diez llamadas hoy.','pantalla':'| x |'}),
])
c = ctx(); r = brain.think(c, 'cuántas llamadas hizo India hoy')
chk('skill usada', r['skills_usadas']==['resumen_llamadas'], r['skills_usadas'])
chk('hablado ok', 'diez llamadas' in r['hablado'])
# verificar que el resultado REAL de la skill viajó al modelo
segunda = CALLS[1]['messages']
tool_result = [m for m in segunda if m['role']=='user' and isinstance(m['content'],list)][-1]
payload = json.loads(tool_result['content'][0]['content'])
chk('datos reales al modelo', payload['llamadas_totales']==10, f"llamadas={payload['llamadas_totales']}")

# ══ 3. Gráfico ══
print("── 3. gráfico ──")
brain._anthropic_call = mock_api([
    tool('mostrar_grafico', {'titulo':'Llamadas','tipo':'barras',
         'datos':[{'h':10,'n':3},{'h':11,'n':5}],'eje_x':'h','series':['n']}),
    txt({'hablado':'Ahí lo tenés.','pantalla':''}),
])
c = ctx(); r = brain.think(c, 'graficá eso')
chk('un grafico', len(r['graficos'])==1)
chk('titulo', r['graficos'][0]['titulo']=='Llamadas')
chk('datos normalizados a float', r['graficos'][0]['datos'][0]['n']==3.0)

# ══ 4. Gráfico con clave inventada -> error claro, no crash ══
print("── 4. gráfico con clave inexistente ──")
brain._anthropic_call = mock_api([
    tool('mostrar_grafico', {'titulo':'X','tipo':'barras',
         'datos':[{'h':1}],'eje_x':'h','series':['no_existe']}),
    txt({'hablado':'No pude graficar eso.','pantalla':''}),
])
c = ctx(); r = brain.think(c, 'graficá')
chk('sin graficos', len(r['graficos'])==0)
seg = CALLS[-1]['messages'] if CALLS else []
chk('no crasheó', r['hablado']!='')

# ══ 5. Acción de escritura -> pide confirmación, NO ejecuta ══
print("── 5. write pide confirmación ──")
ejecutado = {'v': False}
orig = sk.get_skill('apagar_call_center')['fn']
def spy(**kw):
    ejecutado['v'] = True
    return {'switch':'India','accion':'apagado'}
sk.get_skill('apagar_call_center')['fn'] = spy

brain._anthropic_call = mock_api([
    tool('apagar_call_center', {'switch':'India'}),
    txt({'hablado':'¿Confirmás que apague India?','pantalla':''}),
])
c = ctx(); r = brain.think(c, 'apagá India')
chk('NO ejecutó todavía', ejecutado['v']==False)
chk('devolvió confirmacion', r['confirmacion'] is not None)
chk('skill correcta', r['confirmacion']['skill']=='apagar_call_center')
chk('params correctos', r['confirmacion']['params']=={'switch':'India'})
chk('pregunta armada', 'India' in r['confirmacion']['pregunta'])

# ══ 6. Confirmado -> ahora SÍ ejecuta ══
print("── 6. tras confirmar, ejecuta ──")
brain._anthropic_call = mock_api([txt({'hablado':'Listo, India apagado.','pantalla':''})])
c = ctx()
r = brain.think(c, '', None, {'skill':'apagar_call_center','params':{'switch':'India'}})
chk('ejecutó', ejecutado['v']==True)
chk('reporta', 'apagado' in r['hablado'].lower())
sk.get_skill('apagar_call_center')['fn'] = orig

# ══ 7. Auditoría de la acción ══
print("── 7. auditoría ──")
c = ctx()
audit = c.jdb.q("SELECT skill, params FROM audit_log ORDER BY id DESC LIMIT 1")
chk('quedó registrada', len(audit)==1 and audit[0]['skill']=='apagar_call_center')

# ══ 8. Skill que falla -> mensaje al modelo, no excepción ══
print("── 8. skill que falla ──")
brain._anthropic_call = mock_api([
    tool('resumen_llamadas', {'pais':'marte'}),
    txt({'hablado':'Ese país no existe.','pantalla':''}),
])
c = ctx(); r = brain.think(c, 'llamadas de marte')
chk('no crasheó', r['hablado']!='')
res = json.loads([m for m in CALLS[-1]['messages'] if isinstance(m.get('content'),list)
                  and m['content'] and m['content'][0].get('type')=='tool_result'][-1]['content'][0]['content'])
chk('error informado al modelo', 'error' in res, res.get('error','')[:40])

# ══ 9. Techo de vueltas ══
print("── 9. techo de vueltas ──")
from app.config import CFG
brain._anthropic_call = lambda payload, timeout=90: tool('resumen_llamadas', {'pais':'india'})
c = ctx(); r = brain.think(c, 'loop')
chk('cortó el loop', 'enredé' in r['hablado'] or 'límite' in r['pantalla'])
chk('no infinito', len(r['skills_usadas'])<=CFG.MAX_TOOL_ROUNDS)

# ══ 10. Memoria y lecciones en el prompt ══
print("── 10. memoria en el prompt ──")
c = ctx()
sk.run_skill('recordar', {'tema':'meta','contenido':'50 cuentas por semana'}, c)
sk.run_skill('aprender_correccion', {'leccion':'mostrar siempre el desglose por país'}, c)
p = brain.build_system_prompt(c, 'es')
chk('recuerdo inyectado', '50 cuentas por semana' in p)
chk('leccion inyectada', 'desglose por país' in p)
chk('idioma en prompt', 'español' in p)

# lección duplicada no se guarda dos veces
r2 = sk.run_skill('aprender_correccion', {'leccion':'  Mostrar Siempre El Desglose Por País  '}, c)
chk('no duplica lecciones', r2.get('guardado')==False, r2.get('motivo',''))

# ══ 11. Prompt en inglés ══
print("── 11. prompt por idioma ──")
p_en = brain.build_system_prompt(ctx(), 'en')
chk('ingles en prompt', 'English' in p_en)

print("\n" + ("=== BRAIN OK ===" if not fails else f"=== FALLOS: {fails} ==="))
sys.exit(1 if fails else 0)
