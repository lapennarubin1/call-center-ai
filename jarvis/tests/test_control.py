import sys, os, types, sqlite3
sys.path.insert(0,'/home/claude/jarvis')
os.environ.update(JARVIS_SQLITE='/tmp/jctl.db', JARVIS_DB_PATH='/tmp/jctlm.db',
                  ANTHROPIC_API_KEY='f', ELEVENLABS_API_KEY='f')
for p in ('/tmp/jctl.db','/tmp/jctlm.db'):
    if os.path.exists(p): os.remove(p)

# base con switches + sip
c = sqlite3.connect('/tmp/jctl.db')
c.execute("CREATE TABLE cdr (calldate DATETIME, disposition TEXT, billsec INTEGER, dst TEXT, channel TEXT)")
c.execute("CREATE TABLE n8n_switches (id INTEGER PRIMARY KEY, label TEXT, workflow_ids TEXT)")
c.execute("INSERT INTO n8n_switches VALUES (1,'INDIA','wf1,wf2')")
c.execute("INSERT INTO n8n_switches VALUES (2,'MEXICO','wf3')")
c.execute("INSERT INTO n8n_switches VALUES (3,'PANEL','wf9')")
c.execute("""CREATE TABLE n8n_switch_schedules (id INTEGER PRIMARY KEY AUTOINCREMENT, switch_id INTEGER UNIQUE,
             timezone TEXT, on_time TEXT, off_time TEXT, days TEXT, enabled INTEGER, last_error TEXT)""")
c.execute("INSERT INTO n8n_switch_schedules (switch_id,timezone,on_time,off_time,days,enabled) VALUES (1,'Asia/Dubai','10:30','17:30','mon,tue,wed,thu,fri',1)")
c.execute("CREATE TABLE sip_providers (id INTEGER PRIMARY KEY, name TEXT, active INTEGER, billing_start_date TEXT)")
c.execute("INSERT INTO sip_providers VALUES (1,'650098',1,NULL)")
c.execute("CREATE TABLE sip_provider_pricing (id INTEGER PRIMARY KEY, provider_id INT, country TEXT, trunk_name TEXT, price_per_minute REAL)")
c.execute("INSERT INTO sip_provider_pricing VALUES (1,1,'india','proveedor1',0.06)")
c.execute("CREATE TABLE sip_deposits (id INTEGER PRIMARY KEY, provider_id INT, amount_usd REAL, reference TEXT, deposit_date TEXT)")
c.execute("INSERT INTO sip_deposits VALUES (1,1,100.0,'dep1','2026-01-01')")
# 10 llamadas contestadas de 60s = 10 min * 0.06 = $0.60
for i in range(10):
    c.execute("INSERT INTO cdr VALUES ('2026-08-01 10:00:00','ANSWERED',60,'+919812345678','x')")
c.commit(); c.close()

from app.db import panel_db, jarvis_db, init_jarvis_schema
from app import integrations
from app.skills import control, sip_and_memory
from app.skills.base import run_skill
init_jarvis_schema()
def ctx(): return types.SimpleNamespace(panel_db=panel_db(), jdb=jarvis_db(), session_id='s', charts=[])

fails=[]
def chk(l,c,e=''):
    print(f"  [{'OK ' if c else 'FALLO'}] {l} {e}")
    if not c: fails.append(l)

# mock n8n
WF = {'wf1':{'id':'wf1','name':'WF1 India','active':True},
      'wf2':{'id':'wf2','name':'WF2 India','active':True},
      'wf3':{'id':'wf3','name':'WF3 MX','active':False},
      'wf9':{'id':'wf9','name':'WF9 Shared','active':True}}
control.n8n_list_workflows = lambda: list(WF.values())
def setwf(wid, active):
    if wid not in WF: raise integrations.IntegrationError('no existe')
    WF[wid]['active']=active
control.n8n_set_workflow = setwf

print("── estado_call_center ──")
r = run_skill('estado_call_center', {}, ctx())
sws = {s['switch']: s for s in r['switches']}
chk('3 switches', len(r['switches'])==3)
chk('INDIA todo prendido', sws['INDIA']['estado']=='todo prendido', sws['INDIA']['estado'])
chk('MEXICO todo apagado', sws['MEXICO']['estado']=='todo apagado')
chk('horario de INDIA', sws['INDIA'].get('horario',{}).get('prende')=='10:30')
chk('dias', sws['INDIA']['horario']['dias']==['mon','tue','wed','thu','fri'])

print("── apagar / prender ──")
r = run_skill('apagar_call_center', {'switch':'India'}, ctx())
chk('apagó 2 wf', r['workflows_afectados']==2)
chk('wf1 off', WF['wf1']['active']==False)
chk('wf2 off', WF['wf2']['active']==False)
r = run_skill('prender_call_center', {'switch':'india'}, ctx())
chk('prendió (case-insensitive)', WF['wf1']['active']==True)

print("── búsqueda tolerante de switch ──")
r = run_skill('apagar_call_center', {'switch':'mexico'}, ctx())
chk('match parcial minúsculas', r.get('switch')=='MEXICO', r)
r = run_skill('apagar_call_center', {'switch':'noexiste'}, ctx())
chk('no encontrado -> error claro', 'error' in r and 'no encontré' in r['error'])
chk('lista disponibles', 'INDIA' in r['error'])

print("── error parcial de n8n ──")
c2 = sqlite3.connect('/tmp/jctl.db')
c2.execute("INSERT INTO n8n_switches VALUES (4,'ROTO','wf1,wf_inexistente')"); c2.commit(); c2.close()
r = run_skill('apagar_call_center', {'switch':'ROTO'}, ctx())
chk('aplicó el que sí existe', r['workflows_afectados']==1, r)
chk('reporta el que falló', 'errores' in r and len(r['errores'])==1)
chk('advertencia', 'advertencia' in r)

print("── cambiar_horario ──")
r = run_skill('cambiar_horario', {'switch':'MEXICO','hora_prende':'09:00','hora_apaga':'19:00',
                                  'zona_horaria':'America/Mexico_City','dias':['mon','fri']}, ctx())
chk('guardó', r.get('prende')=='09:00', r)
chk('dias ordenados', r.get('dias')==['mon','fri'])
r2 = run_skill('estado_call_center', {}, ctx())
mx = {s['switch']:s for s in r2['switches']}['MEXICO']
chk('persistió', mx['horario']['apaga']=='19:00')
chk('tz', mx['horario']['zona_horaria']=='America/Mexico_City')

print("── validaciones de horario ──")
for bad, label in [({'hora_prende':'25:00'},'hora fuera de rango'),
                   ({'hora_prende':'9:00'},'sin cero adelante'),
                   ({'zona_horaria':'Mars/City'},'tz inventada'),
                   ({'dias':['funday']},'dia invalido')]:
    args = {'switch':'MEXICO','hora_prende':'09:00','hora_apaga':'19:00',
            'zona_horaria':'America/Mexico_City'}
    args.update(bad)
    r = run_skill('cambiar_horario', args, ctx())
    chk(label+' rechazado', 'error' in r, r.get('error','')[:50])

print("── saldo_sip ──")
r = run_skill('saldo_sip', {}, ctx())
p = r['proveedores'][0]
chk('depositado', p['depositado_usd']==100.0)
chk('consumido 10min*0.06', p['consumido_usd']==0.6, p['consumido_usd'])
chk('restante', p['saldo_restante_usd']==99.4)
chk('desglose', p['por_pais'][0]['minutos']==10)

print("── alerta de saldo bajo ──")
c3 = sqlite3.connect('/tmp/jctl.db')
c3.execute("UPDATE sip_deposits SET amount_usd = 0.65"); c3.commit(); c3.close()
r = run_skill('saldo_sip', {}, ctx())
chk('alerta saldo bajo', 'alerta' in r['proveedores'][0], r['proveedores'][0].get('alerta'))

print("── memoria ──")
c = ctx()
run_skill('recordar', {'tema':'meta','contenido':'50 cuentas'}, c)
r = run_skill('consultar_memoria', {}, c)
chk('recuerda', any(x['tema']=='meta' for x in r['recuerdos']))
run_skill('recordar', {'tema':'meta','contenido':'60 cuentas'}, c)
r = run_skill('consultar_memoria', {}, c)
metas = [x for x in r['recuerdos'] if x['tema']=='meta']
chk('actualiza sin duplicar', len(metas)==1 and '60' in metas[0]['contenido'])
r = run_skill('olvidar', {'tema':'meta'}, c)
chk('olvida', r.get('borrado'))
r = run_skill('olvidar', {'tema':'noexiste'}, c)
chk('olvidar inexistente -> error', 'error' in r)

print("── diagnóstico ──")
r = run_skill('diagnostico_jarvis', {}, ctx())
chk('reporta uso', len(r['uso_ultimos_7_dias'])>0)
chk('reporta errores', isinstance(r['errores_ultimas_24h'], list))
chk('cuenta acciones', isinstance(r['ultimas_acciones'], list))

print("── parseo asterisk ──")
raw = """ Endpoint:  <Endpoint/CID.....>  <State.....>  <Channels.>
 Endpoint:  ext100                       Unavailable   0 of inf
     InAuth:  ext100-auth/mexico
 Endpoint:  proveedor1                   Not in use    0 of inf
"""
eps = integrations.parse_pjsip_endpoints(raw)
chk('2 endpoints', len(eps)==2, [e['nombre'] for e in eps])
chk('ext100 unavailable', eps[0]['nombre']=='ext100' and 'Unavail' in eps[0]['estado'])
chk('ignora encabezado', all(not e['nombre'].startswith('<') for e in eps))

print("── asterisk comando no permitido ──")
try:
    integrations.asterisk_cli('core restart now')
    chk('bloquea comando peligroso', False)
except integrations.IntegrationError as ex:
    chk('bloquea comando peligroso', 'no permitido' in str(ex))

print("\n" + ("=== CONTROL OK ===" if not fails else f"=== FALLOS: {fails} ==="))
sys.exit(1 if fails else 0)
