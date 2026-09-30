"""
JARVIS — Voz
=============
Entrada y salida de audio vía ElevenLabs.

  STT (lo que dice el operador → texto)  : modelo Scribe
  TTS (respuesta → audio)                : una voz por idioma

Sobre las voces: cada idioma tiene la suya y NUNCA se mezclan dentro de
una respuesta. El idioma se decide una vez, al principio, y esa voz lee
todo. Cambiar de voz a mitad de una frase suena roto y es exactamente
lo que hay que evitar en un asistente que se escucha, no se lee.

El texto se limpia antes de mandarlo a TTS: markdown, asteriscos y
símbolos que el sintetizador leería literalmente ("asterisco asterisco
India") arruinan la experiencia.
"""
import re
import json
import urllib.request
import urllib.error
from .config import CFG


class VoiceError(Exception):
    pass


_API = 'https://api.elevenlabs.io/v1'


# ══════════════════════════════════════════════════════════════════
#  Limpieza de texto para voz
# ══════════════════════════════════════════════════════════════════

_LIMPIEZA = [
    (re.compile(r'```.*?```', re.S), ' '),          # bloques de código
    (re.compile(r'`([^`]*)`'), r'\1'),              # código inline
    (re.compile(r'\*\*([^*]*)\*\*'), r'\1'),        # negrita
    (re.compile(r'\*([^*]*)\*'), r'\1'),            # cursiva
    (re.compile(r'^#{1,6}\s*', re.M), ''),          # títulos
    # Fila separadora de tabla markdown (|---|:--:|) — si no se saca
    # ANTES de quitar las barras, el sintetizador lee "guion guion guion".
    (re.compile(r'^\s*\|?[\s:|-]*-{2,}[\s:|-]*\|?\s*$', re.M), ' '),
    (re.compile(r'^\s*[-*+]\s+', re.M), ''),        # viñetas
    (re.compile(r'\|'), ' '),                       # tablas
    (re.compile(r'\[([^\]]*)\]\([^)]*\)'), r'\1'),  # links
    (re.compile(r'https?://\S+'), ' '),             # URLs sueltas
    (re.compile(r'[_~>]'), ' '),
    (re.compile(r'(?<![\d\w])[-–—]{2,}(?![\d\w])'), ' '),   # rayas sueltas
    (re.compile(r'\s+'), ' '),
]


def clean_for_speech(texto):
    limpio = texto or ''
    for patron, reemplazo in _LIMPIEZA:
        limpio = patron.sub(reemplazo, limpio)
    limpio = limpio.replace('%', ' por ciento').replace('$', ' dólares ')
    return re.sub(r'\s+', ' ', limpio).strip()


# ══════════════════════════════════════════════════════════════════
#  Texto → audio
# ══════════════════════════════════════════════════════════════════

def synthesize(texto, idioma='es'):
    """Devuelve bytes de MP3. Lanza VoiceError con un motivo legible."""
    if not CFG.ELEVENLABS_API_KEY:
        raise VoiceError('falta la API key de ElevenLabs (ELEVENLABS_API_KEY)')

    limpio = clean_for_speech(texto)
    if not limpio:
        raise VoiceError('no hay texto para sintetizar')
    # Techo defensivo: una respuesta larguísima es cara y además nadie
    # escucha tres minutos de audio seguidos.
    if len(limpio) > 2500:
        limpio = limpio[:2500].rsplit(' ', 1)[0] + '...'

    voz = CFG.voice_for_language(idioma)
    payload = {
        'text': limpio,
        'model_id': CFG.TTS_MODEL,
        'voice_settings': {'stability': 0.45, 'similarity_boost': 0.75,
                           'style': 0.15, 'use_speaker_boost': True},
    }
    req = urllib.request.Request(
        f'{_API}/text-to-speech/{voz}', data=json.dumps(payload).encode(), method='POST')
    req.add_header('xi-api-key', CFG.ELEVENLABS_API_KEY)
    req.add_header('content-type', 'application/json')
    req.add_header('accept', 'audio/mpeg')

    try:
        with urllib.request.urlopen(req, timeout=60) as resp:
            return resp.read()
    except urllib.error.HTTPError as ex:
        detalle = ex.read().decode('utf-8', 'replace')[:300]
        if ex.code == 401:
            raise VoiceError('la API key de ElevenLabs es inválida')
        if ex.code == 422:
            raise VoiceError(f'ElevenLabs rechazó la petición (¿voz {voz} inexistente?): {detalle}')
        if ex.code == 429:
            raise VoiceError('se alcanzó el límite de ElevenLabs')
        raise VoiceError(f'ElevenLabs devolvió {ex.code}: {detalle}')
    except urllib.error.URLError as ex:
        raise VoiceError(f'no hay conexión con ElevenLabs ({ex.reason})')
    except TimeoutError:
        raise VoiceError('ElevenLabs no respondió a tiempo')


# ══════════════════════════════════════════════════════════════════
#  Audio → texto
# ══════════════════════════════════════════════════════════════════

def transcribe(audio_bytes, filename='audio.webm'):
    """Devuelve {'texto': ..., 'idioma_detectado': ...}."""
    if not CFG.ELEVENLABS_API_KEY:
        raise VoiceError('falta la API key de ElevenLabs (ELEVENLABS_API_KEY)')
    if not audio_bytes:
        raise VoiceError('no llegó audio')

    cuerpo, content_type = _multipart({'model_id': CFG.STT_MODEL},
                                      {'file': (filename, audio_bytes)})
    req = urllib.request.Request(f'{_API}/speech-to-text', data=cuerpo, method='POST')
    req.add_header('xi-api-key', CFG.ELEVENLABS_API_KEY)
    req.add_header('content-type', content_type)

    try:
        with urllib.request.urlopen(req, timeout=90) as resp:
            datos = json.loads(resp.read())
    except urllib.error.HTTPError as ex:
        detalle = ex.read().decode('utf-8', 'replace')[:300]
        if ex.code == 401:
            raise VoiceError('la API key de ElevenLabs es inválida')
        raise VoiceError(f'la transcripción falló ({ex.code}): {detalle}')
    except urllib.error.URLError as ex:
        raise VoiceError(f'no hay conexión con ElevenLabs ({ex.reason})')
    except TimeoutError:
        raise VoiceError('la transcripción tardó demasiado')

    texto = (datos.get('text') or '').strip()
    if not texto:
        raise VoiceError('no se entendió nada del audio')
    return {'texto': texto, 'idioma_detectado': datos.get('language_code')}


def _multipart(campos, archivos):
    """Arma un multipart/form-data sin depender de `requests`."""
    frontera = '----JarvisBoundary7MA4YWxkTrZu0gW'
    partes = []
    for clave, valor in campos.items():
        partes.append(f'--{frontera}\r\n'
                      f'Content-Disposition: form-data; name="{clave}"\r\n\r\n'
                      f'{valor}\r\n'.encode())
    for clave, (nombre, contenido) in archivos.items():
        partes.append(f'--{frontera}\r\n'
                      f'Content-Disposition: form-data; name="{clave}"; filename="{nombre}"\r\n'
                      f'Content-Type: application/octet-stream\r\n\r\n'.encode())
        partes.append(contenido)
        partes.append(b'\r\n')
    partes.append(f'--{frontera}--\r\n'.encode())
    return b''.join(partes), f'multipart/form-data; boundary={frontera}'


def available_voices():
    """Lista las voces de la cuenta — para elegir cuáles poner en el .env."""
    if not CFG.ELEVENLABS_API_KEY:
        raise VoiceError('falta la API key de ElevenLabs')
    req = urllib.request.Request(f'{_API}/voices')
    req.add_header('xi-api-key', CFG.ELEVENLABS_API_KEY)
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            datos = json.loads(resp.read())
    except Exception as ex:
        raise VoiceError(f'no pude listar las voces: {ex}')
    return [{'id': v.get('voice_id'), 'nombre': v.get('name'),
             'idiomas': (v.get('labels') or {}).get('language', '')}
            for v in datos.get('voices', [])]
