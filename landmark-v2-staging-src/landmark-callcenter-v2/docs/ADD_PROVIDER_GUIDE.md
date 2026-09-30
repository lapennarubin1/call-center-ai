# ADD_PROVIDER_GUIDE.md
**Agregar un proveedor: dos casos muy distintos.**
Fase BUILD · 21/09/2026

---

## 0. La pregunta que decide todo

> **¿Se le habla igual que a uno que ya tenemos?**

| respuesta | qué hace falta |
|---|---|
| **sí** (otro proveedor SIP por ElevenLabs) | **solo configuración.** Cero código, cero n8n |
| **no** (otra API, otro protocolo) | un **adapter nuevo**: 3 nodos en WF2 + una entrada en el catálogo |

Eso es la separación entre `code` (proveedor **comercial**) y `adapter_key`
(implementación **técnica**).

---

## CASO A — Mismo adapter (lo habitual)

### Ejemplo: `PROVEEDOR2`, otro carrier SIP por ElevenLabs

### A1 · Crear el proveedor

`Panel → Countries & Routes → Providers → New`

```
code           proveedor2
display_name   PROVEEDOR2
adapter_key    ELEVENLABS_SIP     ← el MISMO que PROVEEDOR1
endpoint       (vacío: ELEVENLABS_SIP no lo necesita)
account_ref    <número de cuenta del contrato>
enabled        NO
```

### A2 · Crear sus rutas

```
route_key                   IN_PROVEEDOR2
iso                         IN
provider_id                 PROVEEDOR2
priority                    25                ← se reparte después de PROVEEDOR1
capacity_default            4
elevenlabs_agent_id         agent_...         ← puede ser el mismo agente
elevenlabs_phone_number_id  phnum_...         ← el número del nuevo carrier
followup_policy             STANDARD_CALL_RETRY
```

### A3 · Si usa otra cuenta de ElevenLabs

Crear una credential nueva en n8n y asociarla al nodo
`[ELEVENLABS_SIP] Dispatch`. **Es la única intervención en n8n, y es una
credential, no una edición del workflow.**

Si comparte la cuenta, no hay nada que hacer.

### A4 · Encender

```
la ruta → ON · el proveedor → ON · el país ya está ON
```

### A5 · Confirmar

```bash
curl -s -H "X-Service-Token: <token>" \
  http://172.18.0.1:8080/api/routes/active | grep -A5 IN_PROVEEDOR2
```

`adapter_key` debe decir `ELEVENLABS_SIP`. WF2 lo ramifica por ahí y usa **la
misma rama** que PROVEEDOR1.

**Tiempo estimado: 10 minutos. Código escrito: cero.**

---

## CASO B — Adapter nuevo

### Ejemplo: `TWILIO_VOICE`

Hace falta código porque nadie sabe todavía cómo hablarle.

### B1 · Agregar el adapter al catálogo

`panel/app/routes_config.py`:

```python
ADAPTERS = {
    'ELEVENLABS_SIP':  { ... },
    'STRINGEE_WORKER': { ... },
    'TWILIO_VOICE': {
        'label': 'Twilio Programmable Voice · POST /Calls.json',
        'route_requires': ['caller_id', 'elevenlabs_agent_id'],
        'provider_requires_endpoint': True,
        'result_timing': 'siempre post-call (el SID no dice si contestó)',
    },
}
```

`route_requires` es lo que el panel va a **exigir** antes de dejar activar una
ruta de ese adapter. No es documentación: es la validación.

### B2 · Los 3 nodos en WF2

Se agregan en `tools/wf/wf2_dispatcher.py` y se regenera con
`python3 tools/build_workflows.py`.

```
[TWILIO_VOICE] Build Request      ← dispatch_request → body de Twilio
[TWILIO_VOICE] Dispatch           ← HTTP, neverError, timeout, credential
[TWILIO_VOICE] Normalize Dispatch ← respuesta cruda → dispatch_result
```

Y una salida más en el Switch `[ROUTE] Resolve Adapter`.

### B3 · Reglas que el adapter DEBE cumplir

**Hace:**

1. recibe el `dispatch_request` normalizado
2. dispara la llamada contra su API
3. devuelve los identificadores que tenga (`conversation_id`, `provider_job_id`,
   `provider_call_id`, `sip_code`)
4. normaliza su respuesta a `dispatch_result`

**NO hace, nunca:**

- calcular reintentos ni fechas (`+2h`, `+3h`, días hábiles)
- decidir cerrar un lead
- crear follow-ups en LeadStudio
- hacer `PATCH` de estado en el CRM
- decidir que un código SIP significa "no contestó" — eso es **política**
- ramificar por país
- leer o escribir `wf_call_jobs`

Un nodo `[CRM]`, `[DB]` o `[ENGINE]` dentro de la franja del adapter **es un
error de diseño**, y hay un test que lo detecta.

### B4 · `dispatch_outcome`: la única salida que importa

| valor | cuándo |
|---|---|
| `ACCEPTED` | el proveedor aceptó; el resultado llega después |
| `FINAL` | el dispatch **ya es** el resultado y no habrá post-call |
| `TECHNICAL_ERROR` | no salió y se sabe: 401/403, config, conexión rechazada antes de enviar |
| `UNKNOWN` | timeout, conexión cortada tras enviar, respuesta ilegible |

> **Ante la duda entre `TECHNICAL_ERROR` y `UNKNOWN`: `UNKNOWN`.**
> Un `TECHNICAL_ERROR` mal puesto sobre una llamada que **sí** salió hace que el
> lead se llame dos veces. Un `UNKNOWN` de más solo genera una revisión manual.

### B5 · Si el resultado llega por un canal propio

Si el proveedor no manda el resultado por el post-call de ElevenLabs, hay que
agregar un normalizador que lo convierta al **CALL RESULT CONTRACT** y llame al
motor. Es lo que hace WF9 con el callback del worker Stringee.

**El motor no se toca.**

### B6 · Probar

```bash
python3 tools/build_workflows.py          # debe dar 0 problemas de estándar
python3 tests/run_all_report.py           # 604 PASS
```

El test `un adapter fuera del catálogo no puede quedar operativo` verifica que un
`adapter_key` sin entrada en `ADAPTERS` no se pueda activar.

### B7 · Recién ahí, configurar

```
Providers → New → code: twilio · adapter_key: TWILIO_VOICE · endpoint: https://api.twilio.com/...
Routes    → New → la ruta que corresponda
```

---

## 2. Qué NUNCA cambia al agregar un proveedor

- ❌ el **motor de follow-up**
- ❌ las **políticas**
- ❌ el **event store** y las métricas
- ❌ **WF9**, si el resultado llega por el post-call de ElevenLabs
- ❌ **WF3, WF7/8, WF10, WF14**

Las tools (cuentas, pagos, callback) son **del país**: no dependen del proveedor
de voz. India llamada por PROVEEDOR1 o por STRINGEE usa la misma configuración.

---

## 3. Quitar un proveedor

```
Providers → el proveedor → Disable
```

Paran **todas** sus rutas, en todos los países, en el ciclo siguiente.

Las rutas se archivan (`archived_at`), no se borran: su `route_key` sigue
resolviendo para post-calls y grabaciones que lleguen tarde.

---

## 4. Referencia rápida

| situación | qué hacer | ¿código? |
|---|---|---|
| otro carrier SIP por ElevenLabs | Providers + Routes | **no** |
| el mismo proveedor en un país nuevo | Routes | **no** |
| el mismo proveedor, otra cuenta de ElevenLabs | + una credential | **no** |
| otro worker que expone `POST /call` igual que Stringee | `STRINGEE_WORKER` + `endpoint` | **no** |
| un proveedor con otra API | adapter nuevo: catálogo + 3 nodos | **sí** |
| un proveedor que manda el resultado por su cuenta | + normalizador al CALL RESULT | **sí** |
