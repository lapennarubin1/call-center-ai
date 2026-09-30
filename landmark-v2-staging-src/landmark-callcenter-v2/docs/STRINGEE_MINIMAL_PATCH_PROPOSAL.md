# STRINGEE_MINIMAL_PATCH_PROPOSAL.md
**El cambio mínimo al worker Stringee — y por qué hoy NO hace falta ninguno.**
Fase BUILD · 21/09/2026 · `NO APLICADO`

---

## 0. Conclusión

> ## `NO_CHANGE_REQUIRED` para operar
>
> El código real del worker (`server.js`, `bridge-call.js`) **sí se leyó en esta
> fase**. Eso cierra la pregunta que `STRINGEE_WORKER_AUDIT_V2_2.md` había dejado
> como `UNDETERMINED` por no tener el código disponible.
>
> **`bridge-call.js` reenvía `lead_id` a ElevenLabs** como *dynamic variable*.
> Con eso, WF9 correlaciona el post-call sin ambigüedad usando
> `wf_call_jobs.inflight_lead` (UNIQUE: un lead tiene como máximo una llamada en
> vuelo). El suite V2 funciona con el worker **tal como está hoy**.
>
> El parche de §3 es una **mejora opcional** que hace la correlación directa en
> vez de indirecta. No se aplica en esta fase.

---

## 1. Lo que el código dice (ya no es hipótesis)

### 1.1 `bridge-call.js` — las dynamic variables que llegan a ElevenLabs

```js
elevenWs.send(JSON.stringify({
  type: "conversation_initiation_client_data",
  user_id: BRIDGE_PHONE,
  dynamic_variables: {
      lead_id: BRIDGE_LEAD_ID,        // ← LA PREGUNTA CLAVE: sí, viaja
      phone: BRIDGE_PHONE,
      full_name: BRIDGE_FULL_NAME,
      country: BRIDGE_COUNTRY,
      language: BRIDGE_LANGUAGE,
      call_attempts: BRIDGE_CALL_ATTEMPTS,
      provider: "STRINGEE",
      call_status: "INITIATED"
    }
}));
```

Es una **lista fija que incluye `lead_id`**. Según la tabla de decisión §6 del
audit, ese caso es exactamente `NO_CHANGE_REQUIRED` para operar.

Dos consecuencias:

- La hipótesis §3.1 del audit —"si el worker no reenvía `lead_id`, WF9 v1 pierde
  todas las llamadas Stringee"— **queda descartada como causa**. El `lead_id`
  llega. Lo que descartaba esas llamadas en v1 era el filtro `_source_sheet` del
  nodo `🔍 Has lead + safe route?4`, no la falta de `lead_id`.
- WF2 V2 puede mandar `call_job_id` y `route_key` en el body de `/call` y **no
  llegarán** a ElevenLabs, porque la lista de variables es fija. Por eso WF9 V2
  correlaciona por `lead_id` como camino normal para Stringee.

### 1.2 `server.js` — el resto del contrato, confirmado

| Pregunta abierta del audit §7 | Respuesta del código |
|---|---|
| ¿`from_number` y `agent_id` salen del request o del `.env`? | **Del request**, con el `.env` como respaldo: `body.from_number \|\| process.env.DEFAULT_FROM_NUMBER \|\| "917971730907"`. ⇒ el `caller_id` y el `elevenlabs_agent_id` que configures en la ruta **sí tienen efecto** |
| ¿el worker conoce el `conversation_id`? | **Sí**: `job.elevenlabs_conversation_id`, expuesto en `GET /calls/:id` y en el callback |
| ¿`job_id` o `jobId`? | **`job_id`** (snake_case), junto a `ok: true` y `provider: "STRINGEE"` |
| ¿`/call` tiene autenticación? | **No.** Depende de que el puerto 8091 no sea alcanzable desde fuera |
| ¿`/recordings` o `/call-log` exponen `job_id`? | **No.** `/recordings` da `filename, phone, timestamp_ms, size_bytes` |

### 1.3 Dos capacidades que el audit no había visto y que V2 **sí usa**

**a) `callback_url`: el worker reenvía el resultado final.**

`/call` acepta `callback_url` en el body, y al terminar la llamada `sendCallback()`
hace POST del job completo (`publicJob`), con:

```
job_id · lead_id · phone · final_status · telephony_status · answered
sip_code · sip_reason · stringee_call_id · elevenlabs_conversation_id
created_at · started_at · ringing_at · answered_at · finished_at
```

Soporta además `Authorization: Bearer ${N8N_STATUS_CALLBACK_TOKEN}`.

Esto es **evidencia telefónica real** del resultado, del propio proveedor. WF9 V2
lo consume en `[TRIGGER] Stringee Worker Callback` y lo normaliza en
`[CONFIG] Parse Worker Callback`, usando `mapSipStatus()` del worker
(`408 -> NO_ANSWER`, `486 -> BUSY`, `600/603 -> REJECTED`, `200 -> ANSWERED`).

Es una mejora concreta sobre v1, que solo sabía que la llamada se había
disparado.

**b) `GET /call-audio/:conversation_id`.**

El worker sabe bajar el audio de la conversación desde ElevenLabs. No hace falta
para el diseño actual de WF10 (que usa `/recordings` para Stringee y la API de
ElevenLabs para SIP), pero queda anotado como alternativa.

### 1.4 Lo que el worker impone al despacho

`MAX_CONCURRENT_CALLS` (5 por defecto) hace que `/call` devuelva
**`429 worker_capacity_reached`** cuando está lleno.

En V2 eso es un **`TECHNICAL_ERROR`**, no un `NO_ANSWER`: la llamada no salió, el
job va a `RELEASED` con backoff y **no consume un intento de negocio**. Está
implementado en `[STRINGEE_WORKER] Normalize Dispatch`.

> Dato operativo: si la capacidad de la ruta `IN_STRINGEE` en el panel se
> configura por encima de `MAX_CONCURRENT_CALLS`, el exceso se traduce en 429 y
> reintentos técnicos. **La capacidad de la ruta debe ser <= la del worker.**

---

## 2. Por qué V2 funciona sin tocar el worker

```
WF2  →  POST /call { lead_id, phone, full_name, country, language,
                     call_attempts, from_number, agent_id, callback_url }
        ← { ok: true, job_id }                    ⇒ DISPATCHED (no ANSWERED)

        wf_call_jobs: call_job_id ← provider_job_id = job_id
                      inflight_lead = lead_id  (UNIQUE)

worker →  ElevenLabs  dynamic_variables { lead_id, … }

WF9  ←  post-call de ElevenLabs con lead_id
        [DB] Resolve Call Job → COALESCE(
              call_job_id,            (no llega desde Stringee)
              conversation_id,        (si el job ya lo tenía)
              provider_job_id,        (el job_id del worker)
              inflight_lead = lead_id ← ESTE resuelve el caso Stringee
        )
WF9  ←  callback del worker con job_id + elevenlabs_conversation_id
        (vía rápida, con el resultado telefónico real)
```

El `UNIQUE(inflight_lead)` es lo que hace que `lead_id` alcance: un lead no puede
tener dos llamadas en vuelo, así que no hay ambigüedad sobre a qué intento
pertenece el post-call.

**Riesgo residual documentado:** si el post-call llega *después* de que el
reconciliador movió el job a `NEEDS_RECONCILIATION`, la correlación **sigue
funcionando** — ese estado cuenta como "en vuelo" a efectos de `inflight_lead`.

---

## 3. El parche mínimo (OPCIONAL, no aplicado)

Propósito: que `call_job_id` y `route_key` lleguen a ElevenLabs y vuelvan en el
post-call, para correlacionar de forma **directa** en vez de a través del lead.

Son **tres cambios**, ninguno de los cuales altera el comportamiento actual: solo
agrega campos que, si no vienen, quedan vacíos.

### 3.1 `server.js` — pasar los campos nuevos al proceso hijo

```diff
     const env = {
       ...process.env,
       CALL_TO:        phone,
       CALL_FROM:      fromNumber,
       CALL_LEAD_ID:   leadId,
       CALL_FULL_NAME: String(body.full_name || ""),
       CALL_COUNTRY:   String(body.country || "India"),
       CALL_LANGUAGE:  String(body.language || "hi"),
       CALL_ATTEMPTS:  String(body.call_attempts || 1),
       CALL_AGENT_ID:  agentId,
+      // V2: identidad del despacho. Si no vienen, quedan vacíos y el
+      // comportamiento es exactamente el de hoy.
+      CALL_JOB_ID:    String(body.call_job_id || ""),
+      CALL_ROUTE_KEY: String(body.route_key || ""),
+      CALL_COUNTRY_ISO: String(body.country_iso || ""),
       CALL_INTERNAL_PORT: String(internalPort)
     };
```

Y en el objeto `job` (para que vuelvan en `publicJob` y en el callback):

```diff
     const job = {
       ok: true,
       provider: "STRINGEE",
       job_id: jobId,
       lead_id: leadId,
+      call_job_id: String(body.call_job_id || ""),
+      route_key: String(body.route_key || ""),
       phone,
```

### 3.2 `bridge-call.js` — leer las variables de entorno

```diff
 const CALL_ATTEMPTS =
   process.env.CALL_ATTEMPTS || "1";
+const CALL_JOB_ID =
+  process.env.CALL_JOB_ID || "";
+const CALL_ROUTE_KEY =
+  process.env.CALL_ROUTE_KEY || "";
+const CALL_COUNTRY_ISO =
+  process.env.CALL_COUNTRY_ISO || "";
```

Y, junto a las constantes que ya se inyectan en el contexto del bridge:

```diff
 const BRIDGE_CALL_ATTEMPTS = ${JSON.stringify(CALL_ATTEMPTS)};
+const BRIDGE_CALL_JOB_ID = ${JSON.stringify(CALL_JOB_ID)};
+const BRIDGE_ROUTE_KEY = ${JSON.stringify(CALL_ROUTE_KEY)};
+const BRIDGE_COUNTRY_ISO = ${JSON.stringify(CALL_COUNTRY_ISO)};
```

### 3.3 `bridge-call.js` — agregarlas a las dynamic variables

```diff
       dynamic_variables: {
           lead_id: BRIDGE_LEAD_ID,
           phone: BRIDGE_PHONE,
           full_name: BRIDGE_FULL_NAME,
           country: BRIDGE_COUNTRY,
           language: BRIDGE_LANGUAGE,
           call_attempts: BRIDGE_CALL_ATTEMPTS,
           provider: "STRINGEE",
-          call_status: "INITIATED"
+          call_status: "INITIATED",
+          call_job_id: BRIDGE_CALL_JOB_ID,
+          route_key: BRIDGE_ROUTE_KEY,
+          country_iso: BRIDGE_COUNTRY_ISO,
+          adapter_key: "STRINGEE_WORKER"
         }
```

**Total: ~14 líneas, todas aditivas.**

### 3.4 Qué cambia, si se aplica

| | hoy | con el parche |
|---|---|---|
| correlación en WF9 | por `lead_id` → llamada en vuelo | por `call_job_id` → directa |
| si el reconciliador ya cerró el job | funciona (el estado sigue "en vuelo") | funciona igual |
| dos llamadas al mismo lead a la vez | imposible por diseño (UNIQUE) | imposible igual |
| WF2 / WF9 V2 | **no cambian** | **no cambian**: ya leen `call_job_id` si viene |

Los templates V2 ya están escritos para aprovecharlo el día que exista: WF2 manda
esos campos en el body y WF9 los lee primero. Por eso el parche no requiere tocar
n8n.

### 3.5 Cómo verificarlo, si algún día se aplica

```bash
# 1. una llamada de prueba desde n8n con call_job_id
curl -s -X POST http://172.18.0.1:8091/call \
  -H 'Content-Type: application/json' \
  -d '{"lead_id":"<uuid real>","phone":"91XXXXXXXXXX","full_name":"Test",
       "country":"India","language":"hi","call_attempts":1,
       "from_number":"917971730907","agent_id":"<agent>",
       "call_job_id":"IN_STRINGEE-TEST-000001","route_key":"IN_STRINGEE"}'

# 2. el job debe devolver los campos nuevos
curl -s http://172.18.0.1:8091/calls/<job_id> | jq '{call_job_id, route_key, lead_id}'

# 3. en ElevenLabs, la conversación debe traerlos en dynamic_variables
#    (Conversations -> la llamada -> Client data)

# 4. en la base, el post-call debe correlacionar por CALL_JOB y no por INFLIGHT_LEAD
mysql -e "SELECT call_job_id, provider, conversation_id FROM wf_call_jobs
          WHERE call_job_id='IN_STRINGEE-TEST-000001'" asterisk
```

---

## 4. Lo que **no** se propone tocar

| | por qué no |
|---|---|
| el bridge de audio, el WS con ElevenLabs, la señalización Stringee | funcionan; no hay nada que V2 necesite ahí |
| `MAX_CONCURRENT_CALLS` | es la capacidad real comprada. Se ajusta la capacidad de la RUTA en el panel, no el worker |
| `/recordings`, `/call-log` | V2 los usa tal cual |
| poner autenticación en `/call` | mejora de seguridad legítima, pero **no** es parte de este build: cambiarla sin coordinar rompe WF2 v1, que sigue activo |
| `mapSipStatus()` | su mapeo coincide con el enum de V2; WF9 lo consume tal cual |

---

## 5. Estado

- **No se aplicó ningún cambio al worker en esta fase.** El archivo subido se usó
  únicamente como lectura.
- El suite V2 está construido y probado **contra el contrato actual**.
- Si se aplica el parche de §3, **no hay que regenerar ningún workflow**.
