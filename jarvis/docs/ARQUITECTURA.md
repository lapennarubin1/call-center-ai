# Arquitectura

## Por qué está armado así

**Modular a propósito.** Cada archivo hace una cosa. Cambiar cómo se calcula el ASR toca
`skills/metrics.py`; agregar una integración toca `integrations.py`. Nunca hay que leer
todo el sistema para modificar una parte.

**Dependencias mínimas.** Flask, PyMySQL y gunicorn. Todo lo demás es la stdlib de Python.
En un servicio que corre 24/7, cada librería es una superficie de fallo más y una
actualización de seguridad más que seguir.

**Dos bases separadas.** La del panel (MySQL) tiene los datos operativos y Jarvis
mayormente la lee. La suya (SQLite) tiene memoria e historial, donde escribe seguido.
Mezclarlas sería meter escrituras constantes en la base de la que dependen las llamadas.

## El ciclo de una pregunta

```
1. Llega texto (escrito o transcripto desde audio)
2. Se detecta el idioma          → elige la voz, una sola para toda la respuesta
3. Se arma el prompt de sistema  → + memoria + lecciones + skills que fallan
4. El modelo decide qué skills usar
5. Se ejecutan y sus resultados vuelven al modelo
6. Repite 4-5 hasta tener la respuesta (con techo de vueltas)
7. Responde en JSON: {hablado, pantalla}
8. "hablado" → ElevenLabs → audio
   "pantalla" → markdown + gráficos → HUD
```

## Confirmación de acciones destructivas

Cuando el modelo quiere usar una skill `WRITE`, el cerebro **no la ejecuta**. Devuelve la
acción pendiente y una pregunta. El frontend muestra el diálogo. Si el operador confirma,
el turno siguiente manda esa acción en `confirmado` y recién ahí se ejecuta.

Esto existe porque el reconocimiento de voz falla. Ruido de fondo, una palabra a medias,
alguien hablando cerca — cualquiera de esas cosas podría apagar la operación de un país
sin que nadie se entere hasta la mañana siguiente.

## Aprendizaje

No hay reentrenamiento de modelos: eso sería caro, lento y frágil. Lo que hay es memoria
estructurada que se inyecta en el prompt.

- **`memories`** — hechos que el operador pidió recordar.
- **`lessons`** — correcciones de comportamiento, en imperativo. Se deduplican para que el
  prompt no se degrade con repeticiones.
- **`skill_runs`** — telemetría. Si una skill falla mucho, el prompt lo dice y Jarvis avisa
  del problema en vez de reintentar a ciegas.

El efecto práctico: la semana tres responde mejor que el día uno, porque acumuló contexto
sobre cómo trabaja este operador y este negocio.

## Manejo de errores

La regla es: **nunca un error crudo en la cara del operador.**

- Las skills devuelven `{'error': '...'}` en vez de lanzar. El modelo lo lee y lo explica.
- Las integraciones lanzan errores con texto legible, no stacktraces.
- Las rutas HTTP devuelven 200 con un mensaje hablable incluso cuando algo falló, porque
  el frontend lo va a leer en voz alta.
- El HUD calcula cada bloque por separado: si el PBX no responde, el resto se sigue viendo.

## Zonas horarias

El CDR guarda en UTC. Cuando alguien pregunta "¿a qué hora rinde más India?" quiere la hora
local de India, no UTC. Cada país tiene su corrimiento en `COUNTRIES` y las agrupaciones por
hora lo aplican antes de agrupar.

Sin esto, los "mejores horarios" saldrían corridos cinco horas y media y las decisiones que
se tomen con ese dato serían malas.

## Idioma

Se detecta una vez, al principio del turno, y define la voz de toda la respuesta. El
detector es simple a propósito (palabras frecuentes + alfabeto devanagari): el modelo
confirma el idioma al responder, así que un fallo del detector afecta la voz, no el
contenido.

El hinglish (hindi escrito en alfabeto latino) se trata como hindi. Es lo correcto: se
responde en una sola voz y no se mezclan idiomas dentro de una frase.
