"""
JARVIS — Servidor
==================
Flask. Corre aparte del panel (puerto propio) para que una falla acá no
toque la operación del call center.

Rutas:
  GET  /            interfaz
  POST /login       autenticación
  POST /api/ask     texto → respuesta (dispara skills)
  POST /api/listen  audio → transcripción → respuesta
  POST /api/speak   texto → audio MP3
  GET  /api/state   estado en vivo para el HUD (se refresca solo)
  GET  /api/skills  catálogo de capacidades
  GET  /health      diagnóstico de arranque

Cada request abre y cierra su propia conexión a la base. Es más simple
que un pool y evita conexiones zombie cuando algo falla a mitad.
"""
import io
import os
import json
import time
import secrets
import functools
import types
from datetime import timedelta

from flask import (Flask, request, jsonify, render_template, session,
                   redirect, url_for, send_file)

from .config import CFG
from .db import panel_db, jarvis_db, init_jarvis_schema
from . import brain
from . import voice
from . import skills as skill_pkg

app = Flask(__name__)
app.secret_key = CFG.SECRET_KEY or secrets.token_hex(32)
app.permanent_session_lifetime = timedelta(hours=12)
app.config['MAX_CONTENT_LENGTH'] = 25 * 1024 * 1024   # audio de hasta 25 MB

init_jarvis_schema()


# ══════════════════════════════════════════════════════════════════
#  Contexto y autenticación
# ══════════════════════════════════════════════════════════════════

def make_context(session_id):
    """
    El contexto que reciben las skills: las dos bases, la sesión y la
    lista donde se acumulan los gráficos que se van a mostrar.
    """
    return types.SimpleNamespace(
        panel_db=panel_db(),
        jdb=jarvis_db(),
        session_id=session_id,
        charts=[],
        language='es',
    )


def close_context(ctx):
    for db in (getattr(ctx, 'panel_db', None), getattr(ctx, 'jdb', None)):
        if db:
            db.close()


def auth(fn):
    @functools.wraps(fn)
    def wrap(*a, **kw):
        if not session.get('ok'):
            if request.path.startswith('/api/'):
                return jsonify({'error': 'no autenticado'}), 401
            return redirect(url_for('login'))
        return fn(*a, **kw)
    return wrap


def session_id():
    if 'sid' not in session:
        session['sid'] = secrets.token_hex(8)
    return session['sid']


# ══════════════════════════════════════════════════════════════════
#  Historial
# ══════════════════════════════════════════════════════════════════

def load_history(ctx, limit=12):
    """
    Últimos turnos de la conversación, para que Jarvis entienda
    referencias como "y en México?" o "mostrámelo en gráfico".
    """
    filas = ctx.jdb.q("""SELECT role, content FROM conversations
                         WHERE session_id = § ORDER BY id DESC LIMIT §""",
                      (ctx.session_id, limit))
    return [{'role': f['role'], 'content': f['content']} for f in reversed(filas)]


def save_turn(ctx, role, content, language=None):
    try:
        ctx.jdb.execute(
            "INSERT INTO conversations (session_id, role, content, language) VALUES (§,§,§,§)",
            (ctx.session_id, role, content[:8000], language))
    except Exception:
        pass


# ══════════════════════════════════════════════════════════════════
#  Rutas
# ══════════════════════════════════════════════════════════════════

@app.route('/')
@auth
def index():
    return render_template('jarvis.html', nombre=CFG.ASSISTANT_NAME,
                           voz_activa=CFG.VOICE_ENABLED)


@app.route('/login', methods=['GET', 'POST'])
def login():
    error = None
    if request.method == 'POST':
        usuario = request.form.get('user', '')
        clave = request.form.get('pass', '')
        if (CFG.PASS and usuario == CFG.USER
                and secrets.compare_digest(clave, CFG.PASS)):
            session['ok'] = True
            session.permanent = True
            session_id()
            return redirect(url_for('index'))
        error = 'Credenciales incorrectas'
        time.sleep(1)      # freno simple contra fuerza bruta
    return render_template('login.html', nombre=CFG.ASSISTANT_NAME, error=error)


@app.route('/logout')
def logout():
    session.clear()
    return redirect(url_for('login'))


@app.route('/api/ask', methods=['POST'])
@auth
def api_ask():
    datos = request.get_json(silent=True) or {}
    mensaje = (datos.get('mensaje') or '').strip()
    confirmado = datos.get('confirmado')

    if not mensaje and not confirmado:
        return jsonify({'error': 'mensaje vacío'}), 400

    ctx = make_context(session_id())
    try:
        historial = load_history(ctx)
        if mensaje:
            save_turn(ctx, 'user', mensaje)
        respuesta = brain.think(ctx, mensaje or '(confirmación)', historial, confirmado)
        texto_guardado = respuesta['hablado']
        if respuesta.get('pantalla'):
            texto_guardado += '\n' + respuesta['pantalla']
        save_turn(ctx, 'assistant', texto_guardado, respuesta.get('idioma'))
        return jsonify(respuesta)
    except brain.BrainError as ex:
        return jsonify({
            'hablado': f'Tuve un problema: {ex}',
            'pantalla': '', 'idioma': 'es', 'graficos': [],
            'skills_usadas': [], 'confirmacion': None, 'error': str(ex),
        }), 200      # 200 a propósito: el frontend lo lee y lo dice en voz alta
    finally:
        close_context(ctx)


@app.route('/api/listen', methods=['POST'])
@auth
def api_listen():
    archivo = request.files.get('audio')
    if not archivo:
        return jsonify({'error': 'no llegó audio'}), 400
    try:
        transcripcion = voice.transcribe(archivo.read(), archivo.filename or 'audio.webm')
    except voice.VoiceError as ex:
        return jsonify({'error': str(ex)}), 200

    ctx = make_context(session_id())
    try:
        historial = load_history(ctx)
        save_turn(ctx, 'user', transcripcion['texto'])
        respuesta = brain.think(ctx, transcripcion['texto'], historial)
        respuesta['transcripcion'] = transcripcion['texto']
        texto_guardado = respuesta['hablado']
        if respuesta.get('pantalla'):
            texto_guardado += '\n' + respuesta['pantalla']
        save_turn(ctx, 'assistant', texto_guardado, respuesta.get('idioma'))
        return jsonify(respuesta)
    except brain.BrainError as ex:
        return jsonify({
            'hablado': f'Tuve un problema: {ex}', 'pantalla': '',
            'idioma': 'es', 'graficos': [], 'skills_usadas': [],
            'confirmacion': None, 'transcripcion': transcripcion['texto'],
            'error': str(ex),
        }), 200
    finally:
        close_context(ctx)


@app.route('/api/speak', methods=['POST'])
@auth
def api_speak():
    datos = request.get_json(silent=True) or {}
    texto = (datos.get('texto') or '').strip()
    idioma = datos.get('idioma') or 'es'
    if not texto:
        return jsonify({'error': 'texto vacío'}), 400
    try:
        audio = voice.synthesize(texto, idioma)
    except voice.VoiceError as ex:
        return jsonify({'error': str(ex)}), 200
    return send_file(io.BytesIO(audio), mimetype='audio/mpeg',
                     as_attachment=False, download_name='jarvis.mp3')


@app.route('/api/state')
@auth
def api_state():
    """
    Estado en vivo para el HUD — se refresca solo cada pocos segundos.
    Cada bloque se calcula por separado y con su propio try: si el PBX
    no responde, el resto del tablero tiene que seguir mostrándose.
    """
    ctx = make_context(session_id())
    estado = {'timestamp': int(time.time())}
    try:
        from .skills.metrics import resumen_llamadas, cuentas_abiertas
        for pais in ('india', 'mexico'):
            try:
                estado.setdefault('paises', {})[pais] = resumen_llamadas(pais, ctx=ctx, periodo='hoy')
            except Exception as ex:
                estado.setdefault('paises', {})[pais] = {'error': str(ex)}
        try:
            estado['cuentas_hoy'] = cuentas_abiertas(ctx=ctx, periodo='hoy')
        except Exception as ex:
            estado['cuentas_hoy'] = {'error': str(ex)}
        try:
            from .skills.control import estado_call_center
            estado['call_center'] = estado_call_center(ctx=ctx)
        except Exception as ex:
            estado['call_center'] = {'error': str(ex)}
        return jsonify(estado)
    finally:
        close_context(ctx)


@app.route('/api/skills')
@auth
def api_skills():
    catalogo = []
    for nombre, spec in skill_pkg.all_skills().items():
        catalogo.append({
            'nombre': nombre,
            'descripcion': spec['description'],
            'categoria': spec['category'],
            'ejemplos': spec['examples'],
        })
    return jsonify({'skills': catalogo, 'total': len(catalogo)})


@app.route('/api/history')
@auth
def api_history():
    ctx = make_context(session_id())
    try:
        return jsonify({'turnos': load_history(ctx, limit=40)})
    finally:
        close_context(ctx)


@app.route('/api/reset', methods=['POST'])
@auth
def api_reset():
    """Arranca una conversación nueva. La memoria permanente NO se toca."""
    session['sid'] = secrets.token_hex(8)
    return jsonify({'ok': True, 'sesion': session['sid']})


@app.route('/health')
def health():
    faltantes = CFG.missing_required()
    salud = {
        'ok': not faltantes,
        'asistente': CFG.ASSISTANT_NAME,
        'skills': len(skill_pkg.all_skills()),
        'voz': CFG.VOICE_ENABLED,
        'config_faltante': faltantes,
    }
    try:
        db = panel_db()
        db.one('SELECT 1 AS x')
        db.close()
        salud['base_panel'] = 'ok'
    except Exception as ex:
        salud['base_panel'] = f'error: {ex}'
        salud['ok'] = False
    return jsonify(salud), (200 if salud['ok'] else 503)


@app.errorhandler(413)
def too_large(_):
    return jsonify({'error': 'el audio es demasiado grande'}), 413


@app.errorhandler(500)
def server_error(ex):
    return jsonify({'error': f'error interno: {ex}'}), 500


def main():
    faltantes = CFG.missing_required()
    if faltantes:
        print('⚠️  Falta configurar:')
        for f in faltantes:
            print(f'    - {f}')
        print('   (el servidor arranca igual, pero esas funciones no van a andar)\n')
    print(f'→ {CFG.ASSISTANT_NAME} en http://0.0.0.0:{CFG.PORT}  '
          f'({len(skill_pkg.all_skills())} skills)')
    app.run(host='0.0.0.0', port=CFG.PORT, debug=False, threaded=True)


if __name__ == '__main__':
    main()
