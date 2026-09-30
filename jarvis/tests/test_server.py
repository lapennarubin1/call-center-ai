import sys, os, json
sys.path.insert(0,'/home/claude/jarvis')
os.environ.update(JARVIS_SQLITE='/tmp/jtest.db', JARVIS_DB_PATH='/tmp/jsrv.db',
                  JARVIS_USER='admin', JARVIS_PASS='secreta123',
                  ANTHROPIC_API_KEY='fake', ELEVENLABS_API_KEY='fake',
                  JARVIS_SECRET_KEY='testkey')
if os.path.exists('/tmp/jsrv.db'): os.remove('/tmp/jsrv.db')

from app import brain, voice
from app.server import app
app.testing = True

fails=[]
def chk(l, c, e=''):
    print(f"  [{'OK ' if c else 'FALLO'}] {l} {e}")
    if not c: fails.append(l)

def txt(o): return {'content':[{'type':'text','text':json.dumps(o, ensure_ascii=False)}]}
def tool(n,i): return {'content':[{'type':'tool_use','id':'t1','name':n,'input':i}]}
def mock(script):
    it=iter(script)
    return lambda payload, timeout=90: next(it)

print("── auth ──")
cli = app.test_client()
for path in ['/', '/api/state', '/api/skills']:
    r = cli.get(path)
    chk(f'{path} sin login bloqueado', r.status_code in (302,401), r.status_code)
r = cli.post('/api/ask', json={'mensaje':'hola'})
chk('/api/ask sin login -> 401', r.status_code==401)

r = cli.post('/login', data={'user':'admin','pass':'mala'})
chk('clave mala rechazada', b'incorrectas' in r.data)
r = cli.post('/login', data={'user':'admin','pass':'secreta123'})
chk('login ok', r.status_code==302)

print("── interfaz ──")
r = cli.get('/')
chk('index 200', r.status_code==200)
h = r.get_data(as_text=True)
for frag in ['reactor','JARVIS','jarvis.css','jarvis.js','confirmModal','telemetry']:
    chk(f'html contiene {frag}', frag in h)

print("── /api/skills ──")
r = cli.get('/api/skills')
d = r.get_json()
chk('19 skills', d['total']==19, d['total'])
chk('tiene write', any(s['categoria']=='write' for s in d['skills']))

print("── /api/state ──")
r = cli.get('/api/state')
d = r.get_json()
chk('state 200', r.status_code==200)
chk('paises', 'india' in d.get('paises',{}))
chk('india con datos reales', d['paises']['india'].get('llamadas_totales')==10,
    d['paises']['india'].get('llamadas_totales'))
chk('cuentas hoy', d.get('cuentas_hoy',{}).get('total')==4)
chk('call_center presente (con error, sin tabla)', 'call_center' in d)

print("── /api/ask ──")
brain._anthropic_call = mock([txt({'hablado':'Diez llamadas.','pantalla':'**detalle**'})])
r = cli.post('/api/ask', json={'mensaje':'llamadas de India'})
d = r.get_json()
chk('ask 200', r.status_code==200)
chk('hablado', d['hablado']=='Diez llamadas.')
chk('pantalla', d['pantalla']=='**detalle**')

print("── ask vacío ──")
r = cli.post('/api/ask', json={'mensaje':'  '})
chk('rechaza vacío', r.status_code==400)

print("── error del cerebro no rompe ──")
def boom(payload, timeout=90): raise brain.BrainError('la API key es inválida')
brain._anthropic_call = boom
r = cli.post('/api/ask', json={'mensaje':'hola'})
d = r.get_json()
chk('devuelve 200 igual', r.status_code==200)
chk('explica el problema', 'inválida' in d['hablado'], d['hablado'])
chk('marca error', 'error' in d)

print("── historial persiste ──")
brain._anthropic_call = mock([txt({'hablado':'uno','pantalla':''}),
                              txt({'hablado':'dos','pantalla':''})])
cli.post('/api/ask', json={'mensaje':'primero'})
cli.post('/api/ask', json={'mensaje':'segundo'})
r = cli.get('/api/history')
turnos = r.get_json()['turnos']
chk('historial guardado', len(turnos)>=4, len(turnos))
chk('roles alternados', turnos[0]['role']=='user')

print("── confirmación end-to-end vía HTTP ──")
import app.skills as sk
ejec = {'v':False}
orig = sk.get_skill('apagar_call_center')['fn']
sk.get_skill('apagar_call_center')['fn'] = lambda **kw: (ejec.__setitem__('v',True), {'switch':'India','accion':'apagado'})[1]
brain._anthropic_call = mock([tool('apagar_call_center',{'switch':'India'}),
                              txt({'hablado':'¿Confirmás?','pantalla':''})])
r = cli.post('/api/ask', json={'mensaje':'apagá India'})
d = r.get_json()
chk('pide confirmar por HTTP', d['confirmacion'] is not None)
chk('no ejecutó', ejec['v']==False)
brain._anthropic_call = mock([txt({'hablado':'Hecho.','pantalla':''})])
r = cli.post('/api/ask', json={'mensaje':'', 'confirmado': d['confirmacion']})
chk('ejecuta al confirmar', ejec['v']==True)
sk.get_skill('apagar_call_center')['fn'] = orig

print("── /api/speak con error de voz ──")
voice.synthesize = lambda t,i='es': (_ for _ in ()).throw(voice.VoiceError('key inválida'))
r = cli.post('/api/speak', json={'texto':'hola'})
chk('speak error -> json 200', r.status_code==200 and 'error' in r.get_json())
r = cli.post('/api/speak', json={'texto':''})
chk('speak vacío -> 400', r.status_code==400)

print("── /api/speak ok ──")
voice.synthesize = lambda t,i='es': b'ID3FAKEMP3'
r = cli.post('/api/speak', json={'texto':'hola','idioma':'es'})
chk('devuelve audio', r.status_code==200 and r.mimetype=='audio/mpeg')
chk('bytes', r.data==b'ID3FAKEMP3')

print("── /api/listen ──")
voice.transcribe = lambda b,f='a.webm': {'texto':'cuántas cuentas hay','idioma_detectado':'es'}
brain._anthropic_call = mock([txt({'hablado':'Cuatro cuentas.','pantalla':''})])
import io
r = cli.post('/api/listen', data={'audio':(io.BytesIO(b'FAKEAUDIO'),'a.webm')},
             content_type='multipart/form-data')
d = r.get_json()
chk('transcripción devuelta', d.get('transcripcion')=='cuántas cuentas hay')
chk('respuesta', d['hablado']=='Cuatro cuentas.')
r = cli.post('/api/listen', data={}, content_type='multipart/form-data')
chk('sin audio -> 400', r.status_code==400)

print("── reset ──")
r = cli.post('/api/reset')
chk('reset ok', r.get_json()['ok'])
r = cli.get('/api/history')
chk('historial nuevo vacío', len(r.get_json()['turnos'])==0)

print("── /health ──")
r = cli.get('/health')
d = r.get_json()
chk('health responde', r.status_code in (200,503))
chk('reporta skills', d['skills']==19)
chk('base panel ok', d['base_panel']=='ok', d['base_panel'])

print("── logout ──")
cli.get('/logout')
r = cli.get('/')
chk('tras logout redirige', r.status_code==302)

print("\n" + ("=== SERVER OK ===" if not fails else f"=== FALLOS: {fails} ==="))
sys.exit(1 if fails else 0)
