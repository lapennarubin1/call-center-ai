# ARCHITECTURE_FINAL_V2.md
**Landmark Markets · Call Center V2 — arquitectura del suite construido.**
Fase BUILD · 21/09/2026 · basado en la FUNDACIÓN V2.2 aprobada

---

## 0. En una página

```
                    ┌──────────────────────────────────────────┐
                    │        LANDMARK PANEL  (MySQL local)     │
                    │  Countries · Providers · Routes ·        │
                    │  Policies · Tool configs · Analytics      │
                    └───────────────┬──────────────────────────┘
                     GET /api/routes/active   (los 3 interruptores
                     GET /api/routes/by-key    ya aplicados, READY
                     GET /api/countries/{iso}/tools  y horario incluidos)
                     GET /api/settings
                                    │
   ┌────────────────────────────────┼─────────────────────────────────┐
   │                                │                                 │
┌──▼───────────────┐   ┌────────────▼──────────┐   ┌──────────────────▼──┐
│ WF2 DISPATCHER   │   │ WF3 ACCOUNT           │   │ WF10 RECORDINGS     │
│ claim → dispatch │   │ WF7/8 PAYMENT+CALLBACK│   │ correlación por     │
│ por adapter_key  │   │ por country_tool_config│  │ call_job_id         │
└──┬────────────┬──┘   └───────────┬───────────┘   └──────────┬──────────┘
   │ FINAL      │ ACCEPTED         │                          │
   │            ▼                  │                          │
   │      (post-call)              │                          │
   │   ┌────────────────┐          │                          │
   │   │ WF9 POST-CALL  │          │                          │
   │   │ normaliza      │          │                          │
   │   └────────┬───────┘          │                          │
   │            │                  │                          │
   └────────────▼──────────────────┼──────────────────────────┘
        ┌───────────────────┐      │
        │ FOLLOW-UP ENGINE  │      │      todos escriben
        │ (único, neutral)  │      │      wf_call_jobs + wf_events
        └─────────┬─────────┘      │      ANTES de tocar el CRM
                  │                │
                  ▼                ▼
            LeadStudio        MySQL local  ◄── WF14 reconcilia
            (operación)       (analytics)       local ↔ CRM
```

**Las tres frases que gobiernan todo el diseño:**

1. **Agregar un país es configuración. Agregar un proveedor es escribir su
   adapter. Ninguno de los dos toca el motor de follow-up.**
2. **Los proveedores solo llaman.** Un adapter dispara la llamada, devuelve
   identificadores y normaliza la respuesta. Nada más.
3. **Se escribe local primero, el CRM después.** El panel ve el resultado en
   segundos aunque LeadStudio esté caído.

---

## 1. Qué se construyó

| componente | archivo | nodos |
|---|---|---|
| motor de follow-up | `TEMPLATE_FOLLOWUP_ENGINE_V2.json` | 60 |
| despachador | `TEMPLATE_WF2_CALL_DISPATCHER_V2.json` | 58 |
| post-call | `TEMPLATE_WF9_POST_CALL_HANDLER_V2.json` | 27 |
| creación de cuenta | `TEMPLATE_WF3_ACCOUNT_CREATION_V2.json` | 38 |
| pagos + callback | `TEMPLATE_WF7_8_PAYMENT_CALLBACK_V2.json` | 52 |
| grabaciones | `TEMPLATE_WF10_RECORDINGS_V2.json` | 34 |
| reconciliación | `TEMPLATE_WF14_RECONCILIATION_ANALYTICS_V2.json` | 27 |
| panel V2 | `panel/` | +5 módulos, +3 pantallas, +4 endpoints |
| migración | `sql/migration.sql` | 210 sentencias |

---

## 2. Los tres interruptores

```
countries.enabled        PAÍS       India OFF → ninguna ruta de India llama
voice_providers.enabled  PROVEEDOR  PROVEEDOR1 OFF → ninguna ruta suya, en ningún país
call_routes.enabled      RUTA       IN_PROVEEDOR1 OFF → solo esa ruta
```

**Condición efectiva de llamada:**

```
calling_now = country.enabled
            ∧ provider.enabled
            ∧ route.enabled
            ∧ route.archived_at IS NULL
            ∧ ROUTE READY  (configuración completa)
            ∧ COUNTRY READY
            ∧ el horario lo permite
```

Los interruptores **no se evalúan en n8n**: `/api/routes/active` ya devuelve solo
lo que debe llamar. Un template que lea `countries`, `voice_providers` o
`call_routes` de la base viola el estándar — y hay un test que lo comprueba.

`READY` (configuración completa) y `ON/OFF` (decisión operativa) son **ejes
distintos**: se puede dejar todo configurado y encender el país al final.

Además, la API **revalida en tiempo de ejecución**: una ruta encendida que quedó
inválida por una edición posterior (o por un `UPDATE` manual) devuelve
`calling_now = false`. Fail-closed.

---

## 3. Proveedor comercial ≠ adapter técnico

```
PROVEEDOR1  (voice_providers.code)          ← contrato, cuenta, factura, interruptor
    adapter_key = ELEVENLABS_SIP            ← cómo se le habla
    │
    ├── IN_PROVEEDOR1     capacidad 6
    ├── NP_PROVEEDOR1     capacidad 6
    ├── MX_PROVEEDOR1     capacidad 4
    ├── CO_PROVEEDOR1 · VE_PROVEEDOR1 · …   (configuración, no código)

STRINGEE
    adapter_key = STRINGEE_WORKER
    └── IN_STRINGEE       capacidad 1

PROVEEDOR2 (futuro)
    adapter_key = ELEVENLABS_SIP            ← REUTILIZA el adapter: cero código
```

Esto refleja la realidad del Asterisk sanitizado: `proveedor-mx` y
`proveedor-nepal` son *endpoints* distintos que comparten `proveedor1-aor` y
`proveedor1-auth` — **el mismo trunk y las mismas credenciales**. El ruteo por
país es comportamiento de endpoint/dialplan, no un proveedor por país.

**WF2 ramifica por `adapter_key`**, nunca por proveedor ni por país.
`adapter_key` es `VARCHAR`, no `ENUM`: el catálogo de adapters con código vive en
`routes_config.ADAPTERS`. Un adapter fuera del catálogo se puede guardar
**apagado** pero no activar.

### Contrato del adapter — 4 pasos, siempre

```
[<KIND>] Build Request → [<KIND>] Dispatch → (respuesta cruda) → [<KIND>] Normalize Dispatch
```

Un adapter **nunca** contiene `[CRM]`, `[DB]` ni `[ENGINE]`, no calcula fechas,
no cuenta intentos, no compara códigos SIP con una lista y no ramifica por país.

### `dispatch_outcome`

| valor | significa | qué hace el dispatcher |
|---|---|---|
| `ACCEPTED` | aceptada; el resultado llega después | job → `DISPATCHED`, evento, **fin de la rama** |
| `FINAL` | el dispatch **es** el resultado (SIP 603) | registra el resultado y **llama al motor** |
| `TECHNICAL_ERROR` | no salió y se sabe (401/403, config, worker lleno) | job → `RELEASED` + backoff. **No consume intento. No llega al motor** |
| `UNKNOWN` | timeout, respuesta ilegible: **pudo salir** | job → `UNKNOWN` → `NEEDS_RECONCILIATION`. **Jamás re-despacho** |

> Ante la duda entre `TECHNICAL_ERROR` y `UNKNOWN`: **`UNKNOWN`**. Es preferible
> reconciliar a mano que llamar dos veces al mismo cliente.

---

## 4. El claim atómico

```sql
-- 1. el índice UNIQUE decide, de forma atómica
INSERT IGNORE INTO wf_call_jobs (call_job_id, lead_id, attempt, …) VALUES (…);

-- 2. verificación por RELECTURA del token propio
SELECT call_job_id FROM wf_call_jobs WHERE lead_id = ? AND attempt = ?;
--   es el mío → gané · otro → SKIP
```

**Prohibido decidir por `affectedRows`**: el driver `mysql2` de n8n cambia su
significado según el flag `FOUND_ROWS`. La relectura es correcta con cualquier
driver.

Dos protecciones, no una:

| índice | garantiza |
|---|---|
| `UNIQUE (lead_id, attempt)` | el mismo intento del mismo lead se despacha una vez |
| `UNIQUE (inflight_lead)` | un lead **nunca** tiene dos llamadas en vuelo, en ningún intento, desde ninguna ruta |

`inflight_lead` es una columna generada `STORED` que vale `lead_id` mientras el
job está en `CLAIMED / DISPATCHING / DISPATCHED / UNKNOWN / NEEDS_RECONCILIATION`.

Un `call_job_id` aleatorio **por sí solo no alcanza**: la unicidad la da el
índice, no el identificador.

### Máquina de estados

```
CLAIMED ─► DISPATCHING ─► DISPATCHED ─► COMPLETED        (post-call)
   │            │   └────────────────► COMPLETED        (resultado inmediato)
   │            ├──► UNKNOWN ─► NEEDS_RECONCILIATION    (pudo salir: nunca re-dispatch)
   └────────────┴──► RELEASED ─► (backoff) ─► CLAIMED   (técnico: no salió nada)
FAILED  terminal (dato inválido detectado antes de enviar)
```

### Intento de NEGOCIO vs reintento TÉCNICO

| | `attempt` (negocio) | `tech_retry_count` (técnico) |
|---|---|---|
| lo consume | una llamada que **pudo salir** | nada |
| lo ve | la política de follow-up | solo WF2 y el panel |
| causa | `NO_ANSWER`, `ANSWERED`, SIP 603… | 401/403, credencial, config, worker caído, PATCH `ATTEMPTING` fallido |
| próximo intento | `scheduleNextAt` de la política | backoff 1, 2, 4 … 60 min |
| evento | `CALL_RESULT` | `CALL_TECH_FAILED` |

Un error técnico **nunca** se registra como `NO_ANSWER` y **nunca** llega al
motor. Esa confusión, en v1, hacía que una caída del proveedor consumiera los 9
intentos de un lead en una tarde.

---

## 5. Capacidad y horario

La capacidad es **configuración por ruta**, con franjas en hora **local del
país**:

```
09:00–14:00 → 5
14:00–15:00 → 8
15:00–20:00 → 5
fuera de toda franja → no llama (OUTSIDE_SCHEDULE)
```

- sin franjas: llama 24/7 con `capacity_default` (el comportamiento de v1)
- con franjas: la ventana operativa **es** el conjunto de franjas
- una franja con capacidad `0` apaga ese tramo (turno de noche cerrado)
- una franja `20:00 → 02:00` se evalúa como un tramo continuo hasta las 2 del día
  **siguiente**
- el modelo de solapamiento y el de resolución horaria son **el mismo**, así que
  no pueden contradecirse

**DST:** las franjas siguen el reloj **local**. Verificado con `Europe/Madrid` a
través del cambio de marzo de 2026.

> **Dato verificado en esta fase:** ninguno de los países del sistema (IN, NP,
> MX, CO, VE) observa horario de verano hoy — México lo abolió en 2022. El
> manejo de DST es defensivo, para mercados futuros, y está probado con un huso
> que sí cambia.

**Capacidad de negocio ≠ batching HTTP de n8n.** Los `BATCH_SIZE=6`,
`BATCH_SIZE=1` y `batchSize=5` de v1 no existen en V2: hay un test que falla si
reaparecen.

### Demanda por país

```
India:  IN_PROVEEDOR1 = 5  +  IN_STRINGEE = 3   →  demanda 8
```

WF2 pide **una sola cola por país** con `limit = demanda` y reparte los leads
entre las rutas por `priority`. v1 pedía una cola por rama, así que dos ramas del
mismo país leían el mismo pool.

> Supuesto documentado (**PV-13**): esto asume que `GET /api/leads/queue` es una
> **lectura** y no una reserva. La evidencia es que v1 la llamaba en paralelo
> desde dos ramas y el riesgo conocido era justamente que ambas tomaran el mismo
> lead. Si resultara que reserva, el reparto sigue siendo correcto —el claim es
> por lead— pero habría que revisar el `limit`.

---

## 6. Analytics: el panel no depende del CRM

```
wf_call_jobs   HECHO de cada llamada: resultado y duración, UNA sola vez
               → intentos, respuesta, minutos
wf_events      LOG append-only e idempotente de todo lo demás
               → cuentas, pagos, tools, follow-ups, grabaciones
```

`event_key` es **UNIQUE y determinista**:

```
CALL_RESULT:{call_job_id}
ACCOUNT_CREATED:{provider}:{lead_id}
PAYMENT_LINK_CREATED:{provider}:{order_ref}
```

El mismo hecho escrito dos veces —webhook y polling, reintento de un nodo,
re-ejecución de n8n— choca con el UNIQUE y **no se cuenta dos veces**. La
idempotencia es por construcción, no por disciplina de quien escribe.

**Un solo `CALL_RESULT` por llamada**, no un tipo por resultado: si hubiera
`CALL_ANSWERED` y `CALL_NO_ANSWER` separados, un webhook que dice una cosa y un
polling que dice otra quedarían los dos guardados. Con una clave por llamada, el
segundo choca y —si contradice al primero— abre un issue `RESULT_CONFLICT`.

El panel lee **MySQL local**. Nunca llama a LeadStudio para un KPI, un gráfico o
un refresh. Hay un test que bloquea el socket y exige que Analytics siga
funcionando.

### Definiciones (son contrato)

| métrica | definición |
|---|---|
| `attempted` | `state ∈ DISPATCHING, DISPATCHED, UNKNOWN, COMPLETED, NEEDS_RECONCILIATION`. Excluye `RELEASED` (técnico) y `CLAIMED` |
| `answered` | `result ∈ ANSWERED, CALLBACK` (pedir un callback implica haber hablado) |
| `answer_rate` | `answered / with_result` — **no** sobre `attempted`: las pendientes no deben bajar la tasa |
| `talk_seconds` | `SUM(duration_seconds)` de las contestadas |
| `failed_technical` | eventos `CALL_TECH_FAILED` — no consumen intento |
| `conversion_rate` | `accounts_opened / answered` del mismo rango — **PENDING_PRODUCT_DECISION** |

La atribución de cuentas usa `LAST_CONNECTED_CALL_V1` (última llamada contestada
del lead, ventana 30 días). Es una **convención de reporting declarada**, no un
hecho: una cuenta sin llamada previa queda `UNATTRIBUTED`, no se inventa.

---

## 7. Idempotencia, por dominio

| operación | clave | protección |
|---|---|---|
| despacho | `(lead_id, attempt)` | `wf_call_jobs` UNIQUE |
| una llamada por lead | `lead_id` en vuelo | `UNIQUE(inflight_lead)` |
| follow-up con conversación | `conversation_id` | `wf_conversation_ledger` PK |
| follow-up sin conversación | `call_job_id` | `wf_call_jobs.result IS NULL` |
| tool de país | `{TOOL}:{lead_id}:{request_ref}` | `wf_tool_requests` UNIQUE |
| grabación | `recording_ref` | `wf_recording_ledger` UNIQUE |
| evento | `event_key` determinista | `wf_events` UNIQUE |
| issue | `{tipo}:{entidad}:{id}` | `wf_reconciliation_issues` UNIQUE |

**Nunca `$getWorkflowStaticData`**: no sobrevive un restart, no se comparte entre
workers y no protege ejecuciones simultáneas.

### La regla de oro ante lo ambiguo

> Si una operación **con efecto** (crear un follow-up, una cuenta, un link de
> pago) se envió y la respuesta no llegó, **no se reintenta**. Se marca
> `NEEDS_RECONCILIATION` y lo resuelve una persona.

Reintentar a ciegas duplica el seguimiento de un lead, le crea dos cuentas o le
manda dos links de pago. Un issue abierto es más barato que cualquiera de esas
tres cosas.

---

## 8. Correlación del post-call

Orden de preferencia, del más fuerte al más débil:

1. `call_job_id` de `dynamic_variables` — identidad directa (lo pone WF2)
2. `conversation_id` ya guardado en el job
3. `provider_job_id` — el `job_id` del worker Stringee
4. **la única llamada en vuelo del lead** — `UNIQUE(inflight_lead)` elimina la ambigüedad

El caso 4 es lo que hace que el worker Stringee **funcione sin ningún cambio**:
reenvía `lead_id` a ElevenLabs (verificado en el código, ver
`STRINGEE_MINIMAL_PATCH_PROPOSAL.md`), y eso alcanza.

**Prohibido inferir el proveedor por el formato del teléfono.** v1 usaba
`phone.startsWith('+')`; hay un test que falla si reaparece.

Un post-call sin `route_key` (llamada lanzada por v1) usa la **ruta de
compatibilidad configurada** en `wf_settings.legacy_compat_route_key`. Vacía =
no se procesa, y se registra con motivo. **Nunca se adivina la ruta.**

---

## 9. Errores

| código | reintento | acción |
|---|---|---|
| `CONFIG_ERROR` | no | **aborta el ciclo; nadie llama** |
| `AUTH_ERROR` | no | aborta la rama, alerta |
| `VALIDATION_ERROR` | no | descarta el item **con motivo** |
| `PROVIDER_ERROR` | según `dispatch_outcome` | §3 |
| `CRM_ERROR` | **solo si es seguro** | antes del efecto: sí. Tras enviar un POST sin respuesta: `NEEDS_RECONCILIATION` |
| `RETRYABLE_ERROR` | con backoff | timeouts/429 **de lecturas**, nunca de escrituras con efecto |
| `PERMANENT_ERROR` | no | marca y sigue |

`neverError: true` **solo** si el nodo siguiente evalúa el resultado
explícitamente. En v1, un `neverError` seguido de un nodo que asumía éxito hacía
que un 404 y un 200 se procesaran igual.

---

## 10. Secretos

**Ningún secreto en un nodo, ni en la base, ni en un JSON exportable.**

| secreto | dónde vive |
|---|---|
| API key de ElevenLabs | credential n8n `ElevenLabs API` (HTTP Header Auth) |
| usuario/clave de LeadStudio | credential n8n `LeadStudio Login` (Custom Auth) |
| token de servicio del panel | credential n8n `Landmark Panel API` |
| HMAC del webhook de ElevenLabs | variable de entorno de n8n |
| gateway de SMS, proveedores de pago | credentials n8n |

La base guarda **referencias**: `country_tool_configs.credential_ref = 'OKPAY_IN'`.
El panel rechaza `config_json` con claves tipo `api_key`, `secret`, `password`,
`token`, y `wf_settings` rechaza claves con esa pinta.

Los JSON entregados llevan marcadores `__ELEVENLABS_CREDENTIAL__` en vez de IDs
reales: al importar hay que asociarlas. Un test falla si aparece un secreto.

---

## 11. Qué cambia respecto de v1, en concreto

| v1 | V2 |
|---|---|
| dos motores de follow-up (WF2 y WF9) con reglas distintas | **uno**, provider-neutral |
| `posInCycle`, `cycle`, `% 3` | reglas explícitas `(result, attempt) → action` |
| `if (country === 'india')`, mapas `{india: …}` | configuración del país |
| `phone.startsWith('+')` para saber el proveedor | correlación explícita |
| `$getWorkflowStaticData` para deduplicar | ledgers en la base |
| `BATCH_SIZE = 6` / `= 1` como capacidad | `capacity_now` de la ruta, con franjas |
| un 401 se registraba como `NO_ANSWER` | `RELEASED`: no consume intento |
| `FAILED → NO_ANSWER` global | clasificación por evidencia; sin evidencia, `UNKNOWN` |
| reintento del 502 con un nodo `Wait` | `NEEDS_RECONCILIATION`: nunca a ciegas |
| WF2 creaba un followup `CONNECTED duration=0` al despachar | espera el post-call |
| grabación por `RIGHT(phone,10)` y "el más reciente" | `call_job_id → conversation_id → followup_id` |
| chats de Telegram y agentes en los nodos | `route_telegram_targets` y config de ruta |
| notas del CRM en español con emojis | catálogo en inglés, verificado por test |
| el panel llamaba a LeadStudio para cada KPI | MySQL local; WF14 solo reconcilia |
| nodos huérfanos y desactivados que "apagaban" cosas | cero, verificado por test |
| clave de firma MD5 y contraseñas en los JSON | credentials de n8n |

---

## 12. Lo que este build **no** hace

- no despliega nada: todo es material de staging para revisar
- no toca los workflows v1, la base de producción, Asterisk ni el worker Stringee
- no gestiona prompts, voces ni knowledge base del agente — eso se configura en
  ElevenLabs y la ruta solo guarda los IDs que el adapter necesita
- no inventa markets de CashStudio ni credenciales de Monetix: lo que no está
  confirmado queda apagado y documentado

---

## 13. Dónde seguir

| pregunta | documento |
|---|---|
| ¿cómo lo instalo en staging? | `INSTALL_STAGING.md` |
| ¿cómo lo llevo a producción? | `PRODUCTION_DEPLOYMENT_PLAN.md` |
| ¿cómo vuelvo atrás? | `ROLLBACK_PLAN.md` |
| ¿cómo configuro todo? | `CONFIGURATION_GUIDE.md` |
| ¿cómo agrego un país / proveedor / ruta? | `ADD_COUNTRY_GUIDE.md`, `ADD_PROVIDER_GUIDE.md`, `ADD_ROUTE_GUIDE.md` |
| ¿cómo cambio los reintentos? | `FOLLOWUP_POLICY_GUIDE.md` |
| ¿de dónde sale cada número del panel? | `ANALYTICS_GUIDE.md` |
| ¿por qué todo en inglés en el CRM? | `CRM_ENGLISH_RULE.md` |
| ¿hay que tocar el worker Stringee? | `STRINGEE_MINIMAL_PATCH_PROPOSAL.md` |
| ¿qué queda sin confirmar? | `PENDING_VERIFICATION.md` |
