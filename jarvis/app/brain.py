"""
JARVIS — El cerebro
====================
Orquesta todo: recibe lo que dijo el operador, decide qué skills usar,
las ejecuta, y arma la respuesta final en el mismo idioma en que le
hablaron.

El ciclo es: pensar → usar herramienta → ver resultado → pensar otra
vez, hasta que tenga la respuesta. Con un techo de vueltas, porque si
una integración está caída el modelo podría reintentar para siempre.

Decisiones de diseño que importan:

  · La respuesta viene en DOS partes: `hablado` (lo que se lee en voz
    alta, corto y natural) y `pantalla` (el detalle, tablas, números).
    Leer 40 números en voz alta es inútil; mostrarlos en pantalla y
    resumirlos hablando es lo que hace un asistente de verdad.

  · Las acciones que MODIFICAN algo se frenan y piden confirmación
    antes de ejecutarse. El estado queda pendiente y se ejecuta recién
    cuando el operador confirma.

  · Las lecciones aprendidas se inyectan en el prompt de sistema. Eso
    es lo que hace que mejore con el uso.
"""
import json
import re
import time
import urllib.request
import urllib.error
from .config import CFG
from . import skills as skill_pkg


class BrainError(Exception):
    pass


# ══════════════════════════════════════════════════════════════════
#  Detección de idioma
# ══════════════════════════════════════════════════════════════════

_HINDI_DEVANAGARI = re.compile(r'[\u0900-\u097F]')
# Palabras muy frecuentes y poco ambiguas de cada idioma. Es
# deliberadamente simple: el modelo confirma el idioma después, esto
# solo elige la VOZ, y una voz equivocada se nota menos que una demora.
_ES_HINTS = {'que', 'cómo', 'cuánto', 'cuántas', 'dame', 'muéstrame', 'está',
             'hoy', 'ayer', 'semana', 'llamadas', 'cuentas', 'apagá', 'prendé',
             'quiero', 'por', 'para', 'del', 'las', 'los', 'una', 'saldo'}
_EN_HINTS = {'how', 'many', 'show', 'what', 'the', 'today', 'yesterday', 'week',
             'calls', 'accounts', 'turn', 'give', 'want', 'status', 'balance'}
_HI_HINTS = {'kitna', 'kitne', 'kaise', 'kya', 'aaj', 'kal', 'batao', 'dikhao',
             'hai', 'karo', 'nahi', 'accha', 'thik'}


def detect_language(text):
    """
    Devuelve 'es', 'en' o 'hi'. El hinglish (hindi en alfabeto latino)
    cae en 'hi' — que es lo que se quiere: se responde en una sola voz,
    nunca mezclando idiomas dentro de la misma respuesta.
    """
    if not text or not text.strip():
        return 'es'
    if _HINDI_DEVANAGARI.search(text):
        return 'hi'
    palabras = set(re.findall(r"[a-záéíóúñü]+", text.lower()))
    puntajes = {
        'es': len(palabras & _ES_HINTS),
        'en': len(palabras & _EN_HINTS),
        'hi': len(palabras & _HI_HINTS) * 2,   # las hindi son más distintivas
    }
    mejor = max(puntajes, key=puntajes.get)
    return mejor if puntajes[mejor] > 0 else 'es'


_NOMBRE_IDIOMA = {'es': 'español', 'en': 'English', 'hi': 'Hindi/Hinglish'}


# ══════════════════════════════════════════════════════════════════
#  Prompt de sistema
# ══════════════════════════════════════════════════════════════════

_BASE_PROMPT = """Sos {nombre}, el asistente operativo del call center de Landmark Markets.
Controlás y monitoreás la operación real: llamadas automáticas en varios países,
apertura de cuentas, saldo del proveedor SIP y el PBX Asterisk.

CÓMO RESPONDÉS
- Hablás como un operador experto que conoce el negocio: directo, concreto, sin vueltas.
- Nunca inventás números. Si no tenés el dato, usás una herramienta; si la herramienta
  falla, decís qué falló en una frase clara.
- Tu respuesta se lee EN VOZ ALTA. Por eso: nada de listas largas ni de leer tablas
  número por número. Contás lo importante en 1-3 frases y el detalle va a la pantalla.
- Cuando los datos tienen forma (evolución, comparación, distribución por hora),
  llamás a mostrar_grafico ADEMÁS de contestar. No preguntás si quiere el gráfico:
  lo mostrás.
- No repetís de memoria lo que ya está en pantalla; lo interpretás. "El pico está
  entre las 11 y la 1, ahí sale el 40% de las cuentas" vale más que leer 24 cifras.
- Si detectás algo que merece atención (saldo bajo, ASR desplomado, un trunk caído),
  lo decís aunque no te lo hayan preguntado.

IDIOMA
- El operador te habló en {idioma}. Respondé ÍNTEGRAMENTE en ese idioma.
- Nunca mezcles idiomas dentro de una misma respuesta: se lee con una sola voz y
  el cambio de idioma a mitad suena roto.

FORMATO DE SALIDA (obligatorio)
Devolvés SIEMPRE un JSON válido, sin markdown y sin texto alrededor:
{{"hablado": "...", "pantalla": "..."}}
- "hablado": lo que se dice en voz alta. Natural, 1-3 frases. Sin markdown, sin viñetas,
  sin símbolos que no se puedan pronunciar. Los números redondeados y en palabras
  naturales ("casi mil doscientas" mejor que "1187").
- "pantalla": el detalle en markdown — tablas, cifras exactas, desgloses. Puede estar
  vacío si no hay nada que mostrar.

DATOS DEL NEGOCIO
- Países operativos: India, México, Venezuela, Colombia.
- Un "switch" del call center agrupa varios workflows de n8n de un país. Prenderlo o
  apagarlo arranca o frena las llamadas automáticas de ese país.
- Las llamadas ya en curso terminan siempre; apagar solo impide que empiecen nuevas.
- Hora local: India IST (UTC+5:30), México CST (UTC-6), Venezuela VET (UTC-4),
  Colombia COT (UTC-5). El operador está en Dubái (UTC+4).
"""


def build_system_prompt(ctx, idioma):
    partes = [_BASE_PROMPT.format(nombre=CFG.ASSISTANT_NAME,
                                  idioma=_NOMBRE_IDIOMA.get(idioma, 'español'))]

    if CFG.OPERATOR_NAME:
        partes.append(f"\nEl operador se llama {CFG.OPERATOR_NAME}.")

    # Memoria permanente
    try:
        recuerdos = ctx.jdb.q("SELECT topic, content FROM memories ORDER BY updated_at DESC LIMIT 25")
        if recuerdos:
            partes.append("\nLO QUE TENÉS QUE RECORDAR (te lo pidió el operador):")
            for r in recuerdos:
                partes.append(f"- {r['topic']}: {r['content']}")
    except Exception:
        pass

    # Lecciones aprendidas — el mecanismo de mejora continua
    try:
        lecciones = ctx.jdb.q("SELECT lesson FROM lessons ORDER BY id DESC LIMIT 25")
        if lecciones:
            partes.append("\nCORRECCIONES QUE YA TE HIZO EL OPERADOR (respetálas siempre):")
            for l in lecciones:
                partes.append(f"- {l['lesson']}")
    except Exception:
        pass

    # Skills que vienen fallando: mejor avisar que reintentar a ciegas
    rotas = skill_pkg.failing_skills(ctx)
    if rotas:
        partes.append(
            f"\nATENCIÓN: estas capacidades vienen fallando: {', '.join(rotas)}. "
            f"Si el operador pide algo que las necesita, avisale del problema "
            f"en vez de insistir.")

    return '\n'.join(partes)


# ══════════════════════════════════════════════════════════════════
#  Cliente de la API
# ══════════════════════════════════════════════════════════════════

def _anthropic_call(payload, timeout=90):
    if not CFG.ANTHROPIC_API_KEY:
        raise BrainError('falta la API key de Anthropic (ANTHROPIC_API_KEY en el .env)')

    req = urllib.request.Request(
        'https://api.anthropic.com/v1/messages',
        data=json.dumps(payload).encode(), method='POST')
    req.add_header('content-type', 'application/json')
    req.add_header('x-api-key', CFG.ANTHROPIC_API_KEY)
    req.add_header('anthropic-version', '2023-06-01')

    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read())
    except urllib.error.HTTPError as ex:
        detalle = ex.read().decode('utf-8', 'replace')[:400]
        if ex.code == 401:
            raise BrainError('la API key de Anthropic es inválida')
        if ex.code == 429:
            raise BrainError('se alcanzó el límite de peticiones; probá en unos segundos')
        if ex.code == 529:
            raise BrainError('el servicio está sobrecargado; reintentá en un momento')
        raise BrainError(f'error {ex.code} del modelo: {detalle}')
    except urllib.error.URLError as ex:
        raise BrainError(f'no hay conexión con la API del modelo ({ex.reason})')
    except TimeoutError:
        raise BrainError('el modelo tardó demasiado en responder')


def _parse_reply(texto):
    """
    El modelo debe devolver {"hablado":..., "pantalla":...}. A veces
    envuelve el JSON en ```json o agrega una línea antes. En vez de
    fallar, se rescata lo que se pueda: una respuesta imperfecta es
    mucho mejor que un error en la cara del operador.
    """
    limpio = (texto or '').strip()
    limpio = re.sub(r'^```(?:json)?\s*|\s*```$', '', limpio).strip()
    try:
        datos = json.loads(limpio)
        if isinstance(datos, dict) and 'hablado' in datos:
            return {'hablado': str(datos.get('hablado') or '').strip(),
                    'pantalla': str(datos.get('pantalla') or '').strip()}
    except (json.JSONDecodeError, TypeError):
        pass
    inicio, fin = limpio.find('{'), limpio.rfind('}')
    if inicio != -1 and fin > inicio:
        try:
            datos = json.loads(limpio[inicio:fin + 1])
            if isinstance(datos, dict) and 'hablado' in datos:
                return {'hablado': str(datos.get('hablado') or '').strip(),
                        'pantalla': str(datos.get('pantalla') or '').strip()}
        except json.JSONDecodeError:
            pass
    # Último recurso: tratar todo el texto como hablado.
    return {'hablado': limpio, 'pantalla': ''}


# ══════════════════════════════════════════════════════════════════
#  Ciclo principal
# ══════════════════════════════════════════════════════════════════

def think(ctx, mensaje, historial=None, confirmado=None):
    """
    Procesa un turno completo.

    `confirmado` es la acción de escritura que el operador acaba de
    aprobar (viene del turno anterior). Si está presente, se ejecuta
    antes de seguir pensando.

    Devuelve un dict con: hablado, pantalla, idioma, graficos,
    skills_usadas y, si quedó algo esperando aprobación, `confirmacion`.
    """
    idioma = detect_language(mensaje)
    ctx.charts = []
    ctx.language = idioma

    mensajes = list(historial or [])
    mensajes.append({'role': 'user', 'content': mensaje})

    usadas = []
    pendiente = None

    # Acción aprobada en el turno anterior: se ejecuta ahora y su
    # resultado entra al contexto como si fuera una herramienta más.
    if confirmado:
        nombre = confirmado.get('skill')
        params = confirmado.get('params') or {}
        resultado = skill_pkg.run_skill(nombre, params, ctx)
        usadas.append(nombre)
        mensajes.append({
            'role': 'user',
            'content': (f'[Sistema] El operador confirmó la acción. Se ejecutó '
                        f'{nombre} con resultado: '
                        f'{json.dumps(resultado, ensure_ascii=False, default=str)[:1500]}. '
                        f'Contale qué pasó.'),
        })

    system = build_system_prompt(ctx, idioma)
    herramientas = skill_pkg.anthropic_tools()

    for _ronda in range(CFG.MAX_TOOL_ROUNDS):
        respuesta = _anthropic_call({
            'model': CFG.ANTHROPIC_MODEL,
            'max_tokens': CFG.MAX_TOKENS,
            'system': system,
            'tools': herramientas,
            'messages': mensajes,
        })

        bloques = respuesta.get('content', [])
        pedidos = [b for b in bloques if b.get('type') == 'tool_use']

        if not pedidos:
            texto = ''.join(b.get('text', '') for b in bloques if b.get('type') == 'text')
            salida = _parse_reply(texto)
            salida.update({'idioma': idioma, 'graficos': ctx.charts,
                           'skills_usadas': usadas, 'confirmacion': None})
            return salida

        mensajes.append({'role': 'assistant', 'content': bloques})
        resultados = []

        for pedido in pedidos:
            nombre = pedido.get('name')
            params = pedido.get('input') or {}
            spec = skill_pkg.get_skill(nombre)

            # Acción que modifica el sistema y todavía no fue aprobada:
            # se frena acá y se le pregunta al operador.
            if (spec and spec['category'] == skill_pkg.WRITE
                    and spec.get('confirm_prompt') and CFG.CONFIRM_WRITES):
                try:
                    pregunta = spec['confirm_prompt'].format(**params)
                except (KeyError, IndexError):
                    pregunta = f'¿Confirmás que ejecute {nombre}?'
                pendiente = {'skill': nombre, 'params': params, 'pregunta': pregunta}
                resultados.append({
                    'type': 'tool_result', 'tool_use_id': pedido.get('id'),
                    'content': json.dumps({
                        'pendiente_confirmacion': True,
                        'instruccion': ('NO ejecutada todavía. Pedile confirmación al '
                                        'operador con esta pregunta y nada más: ' + pregunta),
                    }, ensure_ascii=False),
                })
                continue

            resultado = skill_pkg.run_skill(nombre, params, ctx)
            usadas.append(nombre)
            resultados.append({
                'type': 'tool_result', 'tool_use_id': pedido.get('id'),
                'content': json.dumps(resultado, ensure_ascii=False, default=str)[:8000],
            })

        mensajes.append({'role': 'user', 'content': resultados})

        # Si quedó algo esperando aprobación, se da una vuelta más para
        # que el modelo formule la pregunta, y se corta ahí.
        if pendiente:
            final = _anthropic_call({
                'model': CFG.ANTHROPIC_MODEL,
                'max_tokens': CFG.MAX_TOKENS,
                'system': system,
                'messages': mensajes,
            })
            texto = ''.join(b.get('text', '') for b in final.get('content', [])
                            if b.get('type') == 'text')
            salida = _parse_reply(texto)
            if not salida['hablado']:
                salida['hablado'] = pendiente['pregunta']
            salida.update({'idioma': idioma, 'graficos': ctx.charts,
                           'skills_usadas': usadas, 'confirmacion': pendiente})
            return salida

    # Techo de vueltas alcanzado — se corta con un mensaje honesto en
    # vez de dejar al operador esperando.
    return {
        'hablado': ('Me enredé consultando los datos y no pude cerrar la respuesta. '
                    'Probá preguntándomelo de otra forma.'),
        'pantalla': f'Se alcanzó el límite de {CFG.MAX_TOOL_ROUNDS} consultas encadenadas.',
        'idioma': idioma, 'graficos': ctx.charts,
        'skills_usadas': usadas, 'confirmacion': None,
    }
