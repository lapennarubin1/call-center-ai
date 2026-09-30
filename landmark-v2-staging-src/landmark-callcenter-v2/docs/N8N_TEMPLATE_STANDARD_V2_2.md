# N8N_TEMPLATE_STANDARD_V2_2.md
**Estándar obligatorio para los templates V2. Reemplaza a `N8N_TEMPLATE_STANDARD_V2_1.md`.**
Versión 2.2 · 21/09/2026

Aplica a: `TEMPLATE_WF2_CALL_DISPATCHER_V2`, `TEMPLATE_FOLLOWUP_ENGINE_V2` (nuevo),
`TEMPLATE_WF3_ACCOUNT_CREATION_V2`, `TEMPLATE_WF7_8_PAYMENT_CALLBACK_V2`,
`TEMPLATE_WF9_POST_CALL_FOLLOWUP_V2`, `TEMPLATE_WF10_RECORDINGS_V2`, `TEMPLATE_WF14_CRM_SYNC_V2`.

Contratos que este estándar referencia:
`TEMPLATE_DATA_CONTRACT_V2_2.json` · `FOLLOWUP_ENGINE_CONTRACT_V2_2.json` ·
`PROVIDER_ADAPTER_CONTRACT_V2_2.json` · `ANALYTICS_EVENT_CONTRACT_V2_2.json`

---

## 0. Cambios respecto de V2.1

| # | Cambio | Sección |
|---|---|---|
| 1 | **Tres interruptores** (país · proveedor · ruta). WF2 no los evalúa: los aplica `/api/routes/active` | §1 |
| 2 | **`adapter_key`** separado del proveedor. WF2 ramifica por `adapter_key`, nunca por proveedor ni país | §3 |
| 3 | `REJECTED` → **`TECHNICAL_ERROR`**: no consume intento de negocio, `RELEASED` con backoff | §3.3, §6.5 |
| 4 | **Una llamada en vuelo por lead** (UNIQUE en la base). Intento N+1 con N en vuelo → SKIP | §6.2 |
| 5 | **`attempt` = `next_attempt()`**, no `crm_attempts + 1` | §6.3 |
| 6 | **Todo template escribe eventos locales** en el momento, antes del CRM | §11 (nueva) |
| 7 | WF9 correlaciona por `call_job_id` o, si falta, por la **única llamada en vuelo del lead** | §8 |
| 8 | Tools del **país**: WF3/WF7 no ejecutan si el país está apagado | §7.3 |
| 9 | WF14 pasa a **reconciliar** (no es fuente de analytics) | §11.3 |
| 10 | Timestamps UTC en todo lo que escribe V2 (`UTC_TIMESTAMP()`) | §11.2 |

## 0.1 Cambios de V2.1 respecto de V2 (se conservan)

| # | Cambio | Motivo |
|---|---|---|
| 1 | **Proveedores solo llaman.** Sección nueva §3 | principio arquitectónico definitivo |
| 2 | **Follow-up Engine único** como sub-workflow. §4 | WF2 y WF9 decidían follow-up por separado y distinto |
| 3 | **Claim de despacho** `wf_call_jobs` UNIQUE(lead_id, attempt). §6.2 | varias rutas activas por país → race condition real |
| 4 | Claims verificados **por relectura de token**, nunca por `affectedRows`. §6.1 | el driver `mysql2` de n8n cambia su significado según `FOUND_ROWS` |
| 5 | Sin reintento ciego de estados intermedios → `NEEDS_RECONCILIATION`. §6.4 | reintentar puede duplicar followups o llamadas |
| 6 | Resolución de ruta por clave **incluidas archivadas**. §8 | post-calls tardíos de rutas archivadas |
| 7 | Red interna o HTTPS para la API del panel. §7.2 | el token no viaja por HTTP público |
| 8 | Se elimina "una ruta activa por país" | un país puede tener varias rutas simultáneas |

---

## 1. Principio rector

> Agregar un **país** es configuración. Agregar un **proveedor** es escribir su adapter.
> Ninguno de los dos toca el motor de follow-up.

Los tres interruptores (`countries.enabled`, `voice_providers.enabled`, `call_routes.enabled`)
**no se evalúan en n8n**. `/api/routes/active` ya devuelve solo las rutas con
país ON ∧ proveedor ON ∧ ruta ON ∧ READY ∧ horario. WF2 llama lo que la API devuelve y nada más.
Un template que lee las tablas de configuración directamente viola el estándar.

Tres prohibiciones sin excepción:

1. **Ningún literal de negocio en un nodo**: país, prefijo, agente, chat, capacidad, horario,
   endpoint, política. Todo sale de `/api/routes/*` o `/api/countries/*`.
2. **Ninguna inferencia**: prohibido `phone.startsWith('+')`, prohibido deducir país o
   proveedor por prefijo, prohibido `if (country === 'india')`, prohibido `if (isStringee)`.
3. **Ninguna política de follow-up fuera del motor**: ni en un adapter, ni en WF2, ni en WF9.

---

## 2. Layout visual

### 2.1 Secciones (izquierda → derecha)

```
00 TRIGGERS · 01 LOAD CONFIG · 02 INPUT/FETCH · 03 NORMALIZE · 04 ELIGIBILITY · 05 ROUTING
06 PROVIDER ADAPTER · 07 RESULT NORMALIZATION · 08 PERSISTENCE · 09 ERROR HANDLING · 10 AUDIT/METRICS
```

Las que un template no use se omiten sin renumerar. Cada sección avanza **+460 px en X**;
ramas paralelas **+220 px en Y**; ningún nodo a la izquierda de otro del que depende;
sin cruces de conexiones.

### 2.2 Sticky notes (una por sección, ancho 420)

| Sección | Color | Contenido obligatorio |
|---|---|---|
| 00 | 4 azul | qué dispara, frecuencia **real**, ventana de datos + cabecera de versión |
| 01 | 3 verde | endpoint, fail-closed, qué pasa si el panel no responde |
| 04 | 6 amarillo | criterios de exclusión, uno por línea |
| 05 | 4 azul | cómo se reparte capacidad entre rutas y por qué no se infiere nada |
| 06 | 5 naranja | contrato del adapter, qué devuelve, qué NO hace |
| 08 | 3 verde | claves de idempotencia y el UNIQUE que las protege |
| 09 | 2 rojo | tabla de códigos de error y acción de cada uno |

Cabecera de la nota 00:

```
TEMPLATE_WF2_CALL_DISPATCHER_V2 · v2.0.0
Contratos: TEMPLATE_DATA_CONTRACT v2.2 · PROVIDER_ADAPTER v2.2 · FOLLOWUP_ENGINE v2.2 · ANALYTICS_EVENT v2.2
Config: GET /api/routes/active  (red interna)
Rollback: WF2 UNIFICADO (v1) — §12
```

### 2.3 Prohibiciones visuales

- **Nodos huérfanos.** En n8n un nodo sin entrada no da error: no corre. Causa raíz de dos
  bugs de v1 (`🆔 Lead ID Válido?` en WF3, reactivación Stringee en WF2).
- **Nodos desactivados.** En n8n un nodo `disabled` **deja pasar los datos** al siguiente.
  Eso convirtió el `PATCH Status Final (Stringee)` de v1 en un no-fix. Lo que se apaga, se
  apaga desde el panel.
- **Salidas de IF sin conectar.** Toda salida va a algún lado, aunque sea a un `[LOG]`.

---

## 3. Provider adapters — "proveedores solo llaman"

Especificación completa en `PROVIDER_ADAPTER_CONTRACT_V2_2.json`. Resumen normativo:

### 3.1 Estructura fija de 4 pasos

```
[<KIND>] Build Request  →  [<KIND>] Dispatch  →  (respuesta cruda)  →  [<KIND>] Normalize Dispatch
   INPUT NORMALIZATION        DISPATCH             PROVIDER RESPONSE      RESULT NORMALIZATION
```

El prefijo es el **`adapter_key`**, no el proveedor: `[ELEVENLABS_SIP] Dispatch` sirve a
PROVEEDOR1 y a un futuro PROVEEDOR2 SIP sin duplicar nodos. `[STRINGEE_WORKER] Dispatch Worker`.
El Switch `[ROUTE] Resolve Adapter` ramifica por `route.adapter_key`; su salida *fallback*
(adapter desconocido) va a `[ERROR] CONFIG_ERROR` — no debería ocurrir porque el panel no
deja activar adapters fuera del catálogo, pero la rama existe.

### 3.2 Lo que un adapter NO contiene

Ninguno de estos nodos puede aparecer dentro de la franja 06 de un adapter:
`[CRM] …`, `[DB] …`, `[ENGINE] …`, ni un Code que calcule fechas, cuente intentos, compare
códigos SIP con una lista o ramifique por país.

### 3.3 Salida: `dispatch_outcome`

| Valor | Significa | Qué hace el dispatcher después |
|---|---|---|
| `ACCEPTED` | el proveedor aceptó; el resultado llega después | job → `DISPATCHED`, `PATCH ATTEMPTING`, **termina la rama** |
| `FINAL` | el dispatch ya es el resultado (p. ej. SIP 603), no habrá post-call | `record_call_result()` → **llama al motor** con clave `call_job_id` |
| `TECHNICAL_ERROR` | no salió y se sabe: 401/403, credencial, config, conexión rechazada antes de enviar | job → `RELEASED` (backoff 1…60 min), evento `CALL_TECH_FAILED`. **No consume intento. No llega al motor.** AUTH/CONFIG cortan el proveedor en ese ciclo |
| `UNKNOWN` | timeout / respuesta ilegible: **pudo salir** | job → `UNKNOWN` → `NEEDS_RECONCILIATION`. **Jamás re-despachar** |

Ante la duda entre `TECHNICAL_ERROR` y `UNKNOWN`, **`UNKNOWN`**: es preferible reconciliar
a mano que llamar dos veces. Tabla completa en `PROVIDER_ADAPTER_CONTRACT_V2_2.json ·
technical_classification`.

El SIP crudo viaja como `sip_code`. Que 603 signifique "no contestó" lo decide la política
de la ruta (`no_answer_sip_codes`), dentro del motor.

### 3.4 Stringee no es un caso especial

Stringee es un adapter cuyo dispatch siempre termina en `ACCEPTED` (o `REJECTED`/`UNKNOWN`).
No tiene política propia. Su resultado llega por WF9 y pasa por el mismo motor que el SIP.

---

## 4. Follow-up Engine

Sub-workflow `TEMPLATE_FOLLOWUP_ENGINE_V2`, invocado con **Execute Workflow** desde WF2
(solo `FINAL`) y desde WF9 (todo post-call). Contrato completo en
`FOLLOWUP_ENGINE_CONTRACT_V2_2.json`; implementación de referencia ejecutable en
`app/followup_engine.py`.

Reglas:

1. Recibe **solo** el CALL RESULT CONTRACT. No recibe respuestas crudas de proveedores.
2. Resuelve la política desde la ruta (`/api/routes/by-key/{route_key}`), nunca desde un
   literal.
3. **Primero escribe localmente** (`record_call_result`: hecho + `CALL_RESULT`), después
   llama al CRM. El panel ve el resultado aunque LeadStudio esté caído.
4. Reglas explícitas `(result, attempt) → action`, con `"*"` como comodín y
   `unmatched_action` como red de seguridad. **Sin módulo, sin `% 3`.**
5. Calcula `scheduleNextAt` en el **huso del país** (días hábiles incluidos).
6. Es el **único** que hace `POST /followups`.
7. Es idempotente por `conversation_id` (o `call_job_id` si no hay conversación).
8. Perder el claim **no es un error**: devuelve `{skipped: true, reason: ALREADY_CLAIMED}`.

---

## 5. Nomenclatura

`[ÁMBITO] Acción`. Ámbitos: `[TRIGGER] [CONFIG] [LEADS] [ROUTE] [SIP] [STRINGEE] [<KIND>]
[RESULT] [ENGINE] [CRM] [DB] [TELEGRAM] [ERROR] [LOG]`.

Sin emojis. Sin sufijos numéricos (`…4`, `…1`: delatan copy-paste y rompen referencias).
Nombre estable entre versiones: **renombrar un nodo rompe todas las expresiones
`$('nombre')`** que lo usan, así que es un cambio MAJOR.

---

## 6. Idempotencia y claims

### 6.1 Cómo se decide un ganador

```sql
-- 1. intento de claim: el índice UNIQUE decide de forma atómica
INSERT IGNORE INTO wf_call_jobs (call_job_id, lead_id, attempt, …, state)
VALUES (:mi_call_job_id, :lead_id, :attempt, …, 'CLAIMED');

-- 2. verificación por relectura
SELECT call_job_id FROM wf_call_jobs WHERE lead_id = :lead_id AND attempt = :attempt;
-- es :mi_call_job_id → gané · otro → SKIP
```

**Prohibido decidir por `affectedRows` o `ROW_COUNT()`.** El nodo MySQL de n8n usa el
driver `mysql2`; con el flag `FOUND_ROWS` el conteo cambia de significado según la
sentencia. La relectura del token propio es correcta con cualquier driver.
Validado con 40 hilos concurrentes en MariaDB (`test_concurrency_v2_1.py`).

El ledger de conversaciones usa el mismo patrón con `claim_token`.

### 6.2 Una llamada en vuelo por lead

`wf_call_jobs.inflight_lead` (columna generada, **UNIQUE**) vale `lead_id` mientras el job
está en CLAIMED / DISPATCHING / DISPATCHED / UNKNOWN / NEEDS_RECONCILIATION. La base rechaza
un segundo job en vuelo para el mismo lead **en cualquier intento y desde cualquier ruta**.

- Intento N+1 mientras el N está en vuelo → el claim pierde → **SKIP**. No es error.
- Un lead en `NEEDS_RECONCILIATION` **no se vuelve a llamar** hasta que alguien lo resuelve.
- WF9 usa esta garantía para correlacionar (§8).

### 6.2.1 Claves por template

| Template | Clave | Protección |
|---|---|---|
| WF2 despacho | `(lead_id, attempt)` | `wf_call_jobs` UNIQUE |
| Motor (con conversación) | `conversation_id` | `wf_conversation_ledger` PK |
| Motor (sin conversación, SIP FINAL) | `call_job_id` | `wf_call_jobs` UNIQUE + estado |
| WF10 grabaciones | `filename` / `conversation_id` | `wf10_sent_recordings` (a revisar en WF10 V2) |
| WF3 cuentas | `lead_id` | el 409 de CashStudio (se respeta) |
| WF7 pagos | `X-Idempotency-Key` | se conserva de v1 |

**Nunca `$getWorkflowStaticData` para deduplicar.** No sobrevive un restart, no se comparte
entre workers, no protege ejecuciones simultáneas.

### 6.3 Flujo de despacho (WF2)

```
1  obtener lead elegible de la cola de la ruta
2  attempt = next_attempt(lead_id, lead.attempts)
     · si hay un intento RELEASED (técnico) → ese mismo número
     · si no → max(CRM, MAX local) + 1   (protege si LeadStudio no incrementa: PV-1)
3  claim (lead_id, attempt)            → perdí: SKIP, siguiente lead
4  PATCH LeadStudio → ATTEMPTING       → falla: mark_released(CRM_ERROR) — técnico, no consume
5  job → DISPATCHING                   ← SIEMPRE antes del HTTP al proveedor
6  adapter: dispatch + normalize
7  según dispatch_outcome (§3.3)
```

El paso 5 va **antes** del HTTP. Si n8n cae entre el 6 y el 7, el job queda en
`DISPATCHING` y el reconciliador lo manda a `NEEDS_RECONCILIATION`. No se vuelve a llamar.

### 6.4 Reintentos: nunca a ciegas

| Estado viejo | Qué pasó | Qué hace el reconciliador |
|---|---|---|
| `CLAIMED` | nunca llegó a `DISPATCHING`: no salió llamada | `RELEASED` → el mismo intento se re-reclama tras el backoff |
| `DISPATCHING` / `UNKNOWN` | la llamada pudo salir | `NEEDS_RECONCILIATION` |
| `DISPATCHED` sin post-call | la llamada salió, el resultado no llegó | `NEEDS_RECONCILIATION` |
| ledger `CLAIMED` sin followup_id | el POST /followups pudo ejecutarse | `NEEDS_RECONCILIATION` |

`NEEDS_RECONCILIATION` se resuelve a mano o con un lookup verificable en LeadStudio
(**PENDING_VERIFICATION**: hoy no hay idempotency key ni lookup por `conversation_id`).

### 6.5 Técnico vs negocio

| | Intento de NEGOCIO (`attempt`) | Reintento TÉCNICO (`tech_retry_count`) |
|---|---|---|
| lo consume | una llamada que pudo salir | nada |
| lo ve | la política de follow-up | solo WF2 y el panel |
| causa | NO_ANSWER, ANSWERED, SIP 603… | 401/403, credencial, config, worker caído, PATCH ATTEMPTING fallido |
| próximo intento | `scheduleNextAt` de la política | backoff 1, 2, 4 … 60 min |
| evento | `CALL_RESULT` | `CALL_TECH_FAILED` |

Un error técnico **nunca** se registra como NO_ANSWER y **nunca** llega al motor. Un
`UNKNOWN` **nunca** es técnico: pudo salir.

### 6.6 Varias rutas del mismo país

India con `IN_PROVEEDOR1` (5) e `IN_STRINGEE` (3) es válido y esperado. Cada ruta despacha
hasta su `capacity_now`. El claim de §6.1 garantiza que ninguna llame al mismo lead.

El diseño **no impide** la optimización futura "una sola consulta por país, repartida
entre rutas": `/api/routes/active` ya devuelve todas las rutas del país con su capacidad, y
el claim es por lead, no por ruta. Esa optimización **no se construye todavía**.

---

## 7. Credenciales y transporte

### 7.1 Credenciales

**Ningún secreto en un nodo, ni en la base, ni en un JSON exportable.**

| Secreto | Dónde vive |
|---|---|
| ElevenLabs API key | credential n8n HTTP Header Auth (`xi-api-key`) |
| LeadStudio user/pass | credential n8n, en un sub-workflow `GET_LEADSTUDIO_TOKEN` (un login por ciclo) |
| Service token del panel | credential n8n HTTP Header Auth (`X-Service-Token`) |
| HMAC webhook ElevenLabs | credential / variable de entorno n8n |
| SMS, OkPay, APIs de país | credentials n8n |

La base guarda **referencias**: `country_tool_configs.credential_ref = 'PAKISTAN_ACCOUNT_API'`.
El panel rechaza `config_json` con claves tipo `api_key`, `secret`, `password`, `token`, y
un `credential_ref` que no tenga forma de nombre lógico.

### 7.2 La API del panel no se consume por HTTP público

- **Permitido:** red interna Docker/VPS o HTTPS.
- **Rechazado por el panel:** HTTP plano desde IP pública → `403 insecure transport`.
- **No usar:** `http://<IP_PUBLICA>:8080/api/routes/active`.

Candidato para n8n: `http://172.18.0.1:8080`, porque WF2/WF10/WF14 v1 ya alcanzan al
worker Stringee en `172.18.0.1:8091` (gateway del bridge Docker del VPS).
**PENDING_VERIFICATION PV-12**: probarlo desde dentro del contenedor de n8n y confirmar
que el panel escucha en esa interfaz.

### 7.3 Tools del país

Las tools (`CREATE_ACCOUNT`, `CREATE_PAYMENT_LINK`, `CALLBACK`) son del **país**:
`IN_PROVEEDOR1` e `IN_STRINGEE` usan la misma config de India. WF3/WF7 en modo
`CONFIG_ROUTER` leen `GET /api/countries/{iso}/tools` y **no ejecutan** si
`country_enabled` es false (responden a ElevenLabs con un error de negocio legible, no con
un 500).

---

## 8. Propagación de la ruta

```
WF2   [CONFIG] Load Active Routes      → route_key entra al item
WF2   [ROUTE]  Claim Job               → call_job_id, attempt
WF2   [SIP]/[STRINGEE] Build Request   → dynamic_variables: route_key, route_id, provider,
                                         provider_kind, country_iso, attempt, call_job_id
      ElevenLabs post-call             → devuelve dynamic_variables
WF9   [CONFIG] Parse Post-Call         → lee call_job_id; si no viene, lead_id
WF9   [DB]     Resolve Job             → call_job_id directo, o la ÚNICA llamada en vuelo
                                         del lead (inflight_lead) → route_key, attempt, provider
WF9   [CONFIG] Resolve Route           → /api/routes/by-key/{route_key}  (INCLUYE archivadas)
Motor [ENGINE] …                       → policy de ESA ruta
WF10  [DB] Lookup                      → por conversation_id, acotado a route_key
WF14  [CONFIG] Load All Routes         → ?all=1 en vez de DIALCODES
```

Sin `route_key` después de ROUTING → `VALIDATION_ERROR / LEGACY_NO_ROUTE`. Un post-call de
una llamada lanzada por un workflow v1 cae ahí y se trata con una **ruta de compatibilidad
explícita** configurada, nunca adivinando.

---

## 9. Errores

| Código | Reintento | Acción |
|---|---|---|
| `CONFIG_ERROR` | no | **aborta el ciclo; nadie llama** |
| `AUTH_ERROR` | no | aborta la rama, alerta |
| `VALIDATION_ERROR` | no | descarta el item con motivo |
| `PROVIDER_ERROR` | según `dispatch_outcome` | §3.3 |
| `CRM_ERROR` | **solo si es seguro** | antes del efecto: sí. Tras enviar un POST sin respuesta: `NEEDS_RECONCILIATION` |
| `RETRYABLE_ERROR` | con backoff | timeouts/429 **de lecturas**; nunca de escrituras con efecto |
| `PERMANENT_ERROR` | no | marca y sigue |

`neverError: true` solo si el nodo siguiente es un check explícito del resultado. Un
`neverError` seguido de un nodo que asume éxito es un bug: en v1 hizo que un 404 y un 200
se procesaran igual.

---

## 10. Logging

Una línea por decisión relevante:

```
[WF2][exec=…][IN_PROVEEDOR1] action=dispatch job=… lead=… attempt=3 provider=proveedor1
    outcome=ACCEPTED conv=conv_… result=- code=- phone=…3210
```

Campos siempre presentes (null si no aplican): `workflow, execution_id, call_job_id,
route_key, country, provider, lead_id, phone_last4, attempt, conversation_id, followup_id,
result, error_code`. Prohibido loguear teléfono completo, tokens o `accessToken`.

---

## 11. Eventos locales (analytics)

### 11.1 Qué escribe cada template

| Template | Eventos | Momento |
|---|---|---|
| WF2 | `CALL_CLAIMED`, `CALL_DISPATCHED`, `CALL_TECH_FAILED`, `CALL_UNKNOWN`, `CALL_RESULT` (solo FINAL) | en cada transición |
| WF9 | `record_call_result()` → hecho + `CALL_RESULT` | **al recibir el post-call, antes del CRM** |
| Motor | `FOLLOWUP_CREATED`, `CALLBACK_SCHEDULED`, `LEAD_CLOSED` | tras el POST /followups |
| WF3 | `ACCOUNT_REQUESTED`, `ACCOUNT_CREATED` / `_ALREADY_EXISTS` / `_FAILED` | con la respuesta del proveedor de cuentas |
| WF7/8 | `PAYMENT_LINK_*`, `PAYMENT_CONFIRMED` | con la respuesta del proveedor de pagos |
| WF10 | `RECORDING_ATTACHED` / `_SKIPPED_SHORT` / `_MISSING` | al procesar la grabación |

### 11.2 Cómo

- Nodo `[DB] Record Event` con el SQL de `ANALYTICS_EVENT_CONTRACT_V2_2.json · sql_templates`.
- `event_key` **determinista** según la tabla del contrato. Nunca un UUID aleatorio: eso
  anula la idempotencia.
- `INSERT IGNORE`: el UNIQUE decide. Un evento duplicado no es un error.
- `occurred_at` = cuándo pasó, en UTC. Nunca `NOW()` del servidor: `UTC_TIMESTAMP()` o el
  timestamp del proveedor convertido a UTC.
- Un fallo al escribir el evento **no bloquea** la operación (se loguea); un fallo al
  escribir el HECHO de la llamada (`record_call_result`) sí detiene la rama.

### 11.3 WF14 reconcilia

WF14 conserva la sincronización LeadStudio → `crm_leads` y call-log → `stringee_calls`, y
compara contra lo local. Diferencias → `wf_reconciliation_issues`. **Nunca** borra ni
corrige datos locales.

---

## 12. Checklist de aceptación (26 puntos)

- [ ] Cero literales de negocio (país, prefijo, agente, chat, capacidad, horario, endpoint, política)
- [ ] Cero inferencias de proveedor/país por teléfono o prefijo
- [ ] Cero política de follow-up fuera del motor
- [ ] Adapters con exactamente 4 pasos y sin `[CRM]`/`[DB]`/`[ENGINE]`
- [ ] Todo followup creado por el motor, y solo por él
- [ ] Claims por relectura de token, nunca por `affectedRows`
- [ ] `DISPATCHING` escrito antes del HTTP al proveedor
- [ ] Ningún reintento automático de `DISPATCHING`/`UNKNOWN`/`DISPATCHED`/ledger `CLAIMED`
- [ ] Sin `$getWorkflowStaticData` para deduplicar
- [ ] Cero nodos huérfanos · cero nodos desactivados · toda salida de IF conectada
- [ ] Cero secretos en el JSON exportado
- [ ] Un login a LeadStudio por ciclo
- [ ] `neverError` siempre seguido de un check explícito
- [ ] `CONFIG_ERROR` aborta sin llamar
- [ ] Rutas resueltas por clave **incluidas archivadas** en WF9/WF10/motor
- [ ] API del panel por red interna o HTTPS
- [ ] Sin `.first()` en nodos multi-item (`.item` / `itemMatching`)
- [ ] SQL parametrizado
- [ ] Nombres `[ÁMBITO] Acción`, sin emojis ni sufijos
- [ ] Sticky note por sección con cabecera de versión
- [ ] Ramificación por `adapter_key`, nunca por proveedor ni por país
- [ ] Ninguna lectura directa de `countries`/`voice_providers`/`call_routes`: solo la API
- [ ] Errores técnicos → `RELEASED`, nunca NO_ANSWER, nunca al motor
- [ ] `attempt` desde `next_attempt()`, nunca `crm_attempts + 1` a secas
- [ ] Evento local con `event_key` determinista en cada hecho de la tabla §11.1
- [ ] Todo timestamp escrito en UTC

---

## 13. Versionado y rollback

- `TEMPLATE_<WF>_V2_v2.MINOR.PATCH.json`. MAJOR = rompe contrato → todos los templates.
- El v1 **no se toca ni se renombra** mientras exista.
- Rollback en caliente, en este orden:
  1. **Panel → apagar el país o las rutas del V2** (efecto en el ciclo siguiente, sin tocar n8n)
  2. n8n → desactivar el V2
  3. `wf2_provider_config` → reactivar el proveedor en el v1
  4. revisar `wf_call_jobs` y `wf_conversation_ledger` en `NEEDS_RECONCILIATION`
- El v1 se desactiva cuando todas sus rutas pasaron al V2. No se borra. Mínimo 30 días.
