<div align="center">

# JARVIS

**El cerebro por voz de tu call center.**

Hablale. Te responde con voz, te muestra los datos en pantalla y ejecuta lo que le pidas.

`voz` · `análisis en vivo` · `control por lenguaje natural` · `multiidioma` · `aprende con el uso`

</div>

---

## Qué es

Un asistente operativo que entiende tu call center. No es un chatbot con acceso a una base
de datos: sabe qué es el ASR, entiende que "¿cómo viene India?" significa mirar llamadas,
conversión y saldo, y puede apagar la operación de un país si se lo pedís.

```
Vos  ─►  "¿cuántas cuentas se abrieron esta semana y a qué hora rinde más?"

JARVIS ─► [consulta cuentas]  [analiza horarios]  [dibuja el gráfico]

       🔊 "Cuarenta y siete cuentas. El pico está entre las once y la una,
           hora India: ahí sale casi el cuarenta por ciento."

       🖥  Tabla por país + gráfico de barras por hora
```

## Qué sabe hacer

**Analiza** — llamadas, contestadas, ASR, minutos, costo, cuentas abiertas, embudo de
conversión, mejores franjas horarias (en hora local de cada país), tendencias, comparativas
entre países, estado de la cola de leads.

**Controla** — prende y apaga los workflows de n8n por país, cambia los horarios automáticos,
consulta el estado en vivo del PBX Asterisk y el saldo del proveedor SIP.

**Muestra** — cuando los datos tienen forma, dibuja el gráfico solo. No pregunta si lo querés.

**Avisa** — si el saldo está por agotarse o un trunk se cayó, lo dice aunque no le hayas
preguntado.

**Aprende** — guarda lo que le pedís que recuerde y las correcciones que le hacés. Esas
lecciones se aplican en todas las conversaciones siguientes.

## Lo que lo diferencia

**Habla un idioma por vez.** Detecta si le hablaste en español, inglés o hinglish y responde
entero en ese idioma, con una sola voz. Cambiar de voz a mitad de una frase suena roto, y
eso no pasa acá.

**Separa lo hablado de lo mostrado.** La voz dice tres frases con lo importante; la pantalla
tiene la tabla completa. Nadie quiere escuchar veinticuatro cifras leídas en voz alta.

**Pide permiso antes de romper algo.** Las acciones que modifican el sistema real se frenan
y piden confirmación explícita. Un micrófono que escucha mal no puede apagarte un país.

**Nunca se queda mudo.** Si n8n no responde, si falta una clave, si el PBX está caído — lo
dice en una frase clara. No hay pantallas en blanco ni errores 500 en la cara.

**Cada acción queda auditada.** Si mañana amaneció todo apagado, hay un registro de qué se
ejecutó y cuándo.

## Instalación

```bash
git clone <tu-repo> jarvis && cd jarvis
sudo bash deploy/install.sh
nano /opt/jarvis/.env          # completar las claves
systemctl restart jarvis
```

Verificá que quedó bien:

```bash
cd /opt/jarvis && venv/bin/python -m app.tools check
```

Y entrá a `http://tu-servidor:8090`.

## Configuración mínima

| Variable | Para qué |
|---|---|
| `ANTHROPIC_API_KEY` | El cerebro |
| `ELEVENLABS_API_KEY` | La voz (poné `JARVIS_VOICE_ENABLED=0` si no la querés) |
| `JARVIS_PASS` | Tu contraseña de acceso |
| `LM_DB_PASS` | Base de datos con las métricas |
| `LM_N8N_API_KEY` | Solo si querés control del call center |

Todo lo demás tiene valores por defecto razonables. Ver [`.env.example`](.env.example).

## Uso

**Por voz:** mantené presionado el reactor (o la barra espaciadora) y hablá. Soltás y responde.

**Por texto:** escribí abajo. Útil cuando hay ruido o estás en una reunión.

Algunas cosas que le podés pedir:

```
¿cuántas llamadas hicimos hoy en India?
compará India contra México esta semana
¿a qué hora conviene llamar en México?
mostrame la tendencia de los últimos 15 días
¿cuánto saldo nos queda?
apagá México                          → pide confirmación
cambiá el horario de India a las 9    → pide confirmación
acordate que la meta son 50 cuentas por semana
¿cómo venís funcionando?
```

## Agregar una capacidad

Es una función con un decorador. Aparece sola — no hay que tocar el cerebro ni el servidor.

```python
# app/skills/mis_skills.py
from .base import skill, SkillError, READ

@skill(
    name='ventas_del_mes',
    description='Total de ventas del mes en curso',
    params={'pais': {'type': 'string', 'description': 'india o mexico'}},
    required=['pais'],
    category=READ,
    examples=['cuánto vendimos este mes'],
)
def ventas_del_mes(pais, ctx):
    fila = ctx.panel_db.one("SELECT SUM(monto) AS t FROM ventas WHERE pais = §", (pais,))
    return {'pais': pais, 'total': float(fila.get('t') or 0)}
```

Sumá el módulo a `_MODULOS` en `app/skills/__init__.py` y listo.

Si la skill modifica algo, poné `category=WRITE` y un `confirm_prompt`: se va a pedir
confirmación automáticamente.

## Arquitectura

```
Navegador ──voz/texto──► Flask ──► Cerebro (Claude) ──► Skills ──► MySQL
                           │            │                       └─► n8n
                           │            └── memoria + lecciones  └─► Asterisk
                           └──audio──► ElevenLabs
```

| Archivo | Qué hace |
|---|---|
| `app/config.py` | Toda la configuración, en un solo lugar |
| `app/db.py` | Dos bases: la del panel (métricas) y la propia (memoria) |
| `app/brain.py` | Orquestación, ciclo de herramientas, idioma, confirmaciones |
| `app/voice.py` | ElevenLabs: voz a texto y texto a voz |
| `app/integrations.py` | Clientes de n8n y Asterisk |
| `app/skills/` | Las capacidades, un módulo por dominio |
| `app/server.py` | Rutas HTTP |
| `app/static/` | El HUD |

Cada archivo es independiente: cambiar cómo se calcula el ASR toca `skills/metrics.py`
y nada más.

## Tests

```bash
bash tests/run_all.sh
```

Los tests arman una base sintética con números elegidos a mano y verifican cada cálculo
contra el valor esperado. Cubren: las métricas (incluida la conversión de husos horarios),
el ciclo completo del cerebro, las confirmaciones, el manejo de errores, y todas las rutas
HTTP. No necesitan claves de API ni conexión a internet.

## Seguridad

- Las credenciales viven en `.env`, nunca en el código. El repo se puede publicar tal cual.
- Las acciones destructivas piden confirmación (`JARVIS_CONFIRM_WRITES=1`).
- Los comandos de Asterisk están en lista blanca: solo lectura.
- El servicio corre con `ProtectSystem=full` y solo puede escribir en su carpeta de datos.
- Toda acción de escritura queda en `audit_log`.

## Licencia

MIT — ver [LICENSE](LICENSE).
